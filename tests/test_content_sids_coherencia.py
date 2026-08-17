"""
Coherencia de los Content SID de Twilio entre codigo, panel y cuenta real.

Why: en agosto de 2026 las plantillas se recrearon dos veces y todos los SID
cambiaron. El sistema parecio seguir funcionando porque Twilio hace un aliasing
interno, no documentado, de los SID viejos hacia los nuevos; el unico sintoma
eran mensajes `undelivered` sueltos. Estos tests convierten ese fallo silencioso
en uno ruidoso, y cubren las dos rutas por igual: la automatica (schedulers de
cita) y la manual (plantillas que las asesoras mandan desde el panel).

Los tests estaticos no tocan la red. Los `live` consultan la Content API y se
saltan solos si no hay credenciales reales (conftest inyecta dummies).
"""

import base64
import json
import os
import re
import urllib.request
from pathlib import Path

import pytest

from middleware.templates import content_sids as sids
from middleware.templates.templates import DEFAULT_TEMPLATES

RAIZ = Path(__file__).resolve().parents[1]
FORMATO_SID = re.compile(r"^HX[0-9a-f]{32}$")
SID_EN_TEXTO = re.compile(r"HX[0-9a-f]{32}")

# Ficheros que NO deben contener un SID literal: el unico sitio donde pueden
# vivir es content_sids.py, como default de la variable de entorno.
BACKEND_SIN_SIDS = [
    "app.py",
    "middleware/templates/templates.py",
    "middleware/outbound_panel.py",
]


def _normaliza(texto: str) -> str:
    """Colapsa placeholders y espacios para comparar textos entre sistemas.

    Twilio usa {{1}}; el registro del panel usa {nombre}. Comparar en crudo
    daria falsos negativos por la sola diferencia de notacion.
    """
    texto = re.sub(r"\{\{\d+\}\}", "\x00", texto or "")
    texto = re.sub(r"\{[a-z_]+\}", "\x00", texto)
    return re.sub(r"\s+", " ", texto).strip().lower()


# ---------------------------------------------------------------------------
# Estaticos — corren siempre
# ---------------------------------------------------------------------------


def test_todos_los_sids_del_registro_tienen_formato_valido():
    malos = {n: s for n, s in sids.all_sids().items() if not FORMATO_SID.match(s or "")}
    assert not malos, f"SID con formato invalido: {malos}"


def test_no_hay_sids_duplicados_en_el_registro():
    por_sid: dict = {}
    for nombre, sid in sids.all_sids().items():
        por_sid.setdefault(sid, []).append(nombre)
    duplicados = {s: n for s, n in por_sid.items() if len(n) > 1}
    assert not duplicados, (
        f"Un mismo SID sirve a varias plantillas logicas: {duplicados}. "
        "Suele indicar un copy-paste al renombrar variables."
    )


@pytest.mark.parametrize("archivo", BACKEND_SIN_SIDS)
def test_el_backend_no_hardcodea_content_sids(archivo):
    """Regresion del bug original: SID hardcodeado en el call site."""
    encontrados = sorted(set(SID_EN_TEXTO.findall((RAIZ / archivo).read_text(encoding="utf-8"))))
    assert not encontrados, (
        f"{archivo} hardcodea {encontrados}. Los SID deben resolverse desde "
        "content_sids.py para que un cambio de cuenta sea una variable de entorno."
    )


def test_los_sids_del_frontend_existen_en_el_registro():
    """index.js lleva fallbacks embebidos; no pueden quedar desincronizados."""
    texto = (RAIZ / "middleware/PanelAsesores/index.js").read_text(encoding="utf-8")
    huerfanos = sorted(set(SID_EN_TEXTO.findall(texto)) - set(sids.all_sids().values()))
    assert not huerfanos, (
        f"index.js referencia SID que no estan en el registro: {huerfanos}. "
        "El panel los usa hasta que el backend le responde, asi que un SID "
        "muerto aqui se envia de verdad."
    )


@pytest.mark.parametrize("nombre", ["BULK_TEMPLATES", "SCHEDULABLE_TEMPLATES"])
def test_los_catalogos_manuales_del_panel_salen_del_registro(nombre):
    """Envio masivo y programado: las dos rutas manuales con SID explicito."""
    catalogo = getattr(sids, nombre)
    assert catalogo, f"{nombre} quedo vacio: el panel se queda sin plantillas"
    del_registro = set(sids.all_sids().values())
    for entrada in catalogo:
        assert entrada["sid"] in del_registro, (
            f"{nombre}: '{entrada['name']}' usa {entrada['sid']}, ajeno al registro"
        )
        assert entrada.get("vars"), f"{nombre}: '{entrada['name']}' sin variables declaradas"


def test_bulk_allowed_templates_se_deriva_sin_perder_entradas():
    """Es la allowlist del envio masivo: si queda vacia, se rechaza todo."""
    assert len(sids.BULK_ALLOWED_TEMPLATES) == len([t for t in sids.BULK_TEMPLATES if t["sid"]])
    for sid in sids.BULK_ALLOWED_TEMPLATES:
        assert FORMATO_SID.match(sid), f"allowlist con SID mal formado: {sid}"


@pytest.mark.parametrize("tid", sorted(DEFAULT_TEMPLATES))
def test_las_plantillas_con_sid_lo_toman_del_registro(tid):
    sid = DEFAULT_TEMPLATES[tid].get("content_sid")
    if not sid:
        pytest.skip("plantilla de texto plano, sin Content SID")
    assert sid in set(sids.all_sids().values()), (
        f"'{tid}' usa {sid}, que no sale de content_sids.py"
    )


@pytest.mark.parametrize("tid", sorted(DEFAULT_TEMPLATES))
def test_la_aridad_declarada_es_coherente(tid):
    """El numero de variables debe cuadrar con el body y con el mapa a Twilio."""
    plantilla = DEFAULT_TEMPLATES[tid]
    mapa = plantilla["content_variables_map"]
    if isinstance(mapa, dict):
        pytest.skip("numeracion explicita: se valida contra Twilio en los tests live")
    assert len(mapa) == len(plantilla["variables"]), (
        f"'{tid}': {len(plantilla['variables'])} variables declaradas pero "
        f"{len(mapa)} en content_variables_map"
    )
    for variable in plantilla["variables"]:
        assert "{" + variable + "}" in plantilla["body"], (
            f"'{tid}' declara '{variable}' pero el body no la usa: el panel "
            "mandaria un placeholder crudo o un hueco vacio (error 21656)"
        )


# ---------------------------------------------------------------------------
# Live — contra la cuenta real de Twilio
# ---------------------------------------------------------------------------

_ACCOUNT = os.getenv("TWILIO_ACCOUNT_SID", "")
_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
_HAY_CREDENCIALES = (
    _ACCOUNT.startswith("AC") and "dummy" not in _ACCOUNT.lower() and "dummy" not in _TOKEN.lower()
)

live = pytest.mark.skipif(not _HAY_CREDENCIALES, reason="requiere credenciales reales de Twilio")

# Plantillas ligadas a una cita: transaccionales, deberian ser UTILITY.
# Meta limita la entrega de las MARKETING (error 63049).
SIDS_DE_CITA = ("recordatorio_cita", "followup_1", "followup_2")


@pytest.fixture(scope="module")
def catalogo_twilio():
    auth = base64.b64encode(f"{_ACCOUNT}:{_TOKEN}".encode()).decode()
    peticion = urllib.request.Request(
        "https://content.twilio.com/v1/ContentAndApprovals?PageSize=200",
        headers={"Authorization": f"Basic {auth}"},
    )
    with urllib.request.urlopen(peticion, timeout=60) as respuesta:
        datos = json.load(respuesta)
    return {c["sid"]: c for c in datos["contents"]}


@live
def test_live_ningun_sid_del_registro_esta_muerto(catalogo_twilio):
    muertos = {n: s for n, s in sids.all_sids().items() if s not in catalogo_twilio}
    assert not muertos, (
        f"SID que devuelven 404 en la Content API: {muertos}. "
        "Hoy pueden entregar por el aliasing de Twilio, pero es un accidente."
    )


@live
def test_live_todas_las_plantillas_estan_aprobadas(catalogo_twilio):
    sin_aprobar = {}
    for nombre, sid in sids.all_sids().items():
        entrada = catalogo_twilio.get(sid)
        if not entrada:
            continue  # lo cubre el test anterior
        estado = (entrada.get("approval_requests") or {}).get("status")
        if estado != "approved":
            sin_aprobar[nombre] = estado
    assert not sin_aprobar, f"Plantillas no aprobadas por Meta: {sin_aprobar}"


@live
@pytest.mark.parametrize("tid", sorted(DEFAULT_TEMPLATES))
def test_live_el_preview_del_panel_coincide_con_twilio(tid, catalogo_twilio):
    """Si el preview miente, la asesora manda algo distinto a lo que ve."""
    plantilla = DEFAULT_TEMPLATES[tid]
    sid = plantilla.get("content_sid")
    if not sid:
        pytest.skip("plantilla de texto plano")
    entrada = catalogo_twilio.get(sid)
    if not entrada:
        pytest.skip("SID muerto: lo reporta test_live_ningun_sid_del_registro_esta_muerto")

    cuerpo_twilio = entrada["types"]["twilio/text"]["body"]
    assert _normaliza(cuerpo_twilio) == _normaliza(plantilla["body"]), (
        f"'{tid}': el body del panel no coincide con el de Twilio"
    )

    variables_twilio = entrada.get("variables") or {}
    assert len(variables_twilio) == len(plantilla["content_variables_map"]), (
        f"'{tid}': Twilio espera {len(variables_twilio)} variables y el panel "
        f"manda {len(plantilla['content_variables_map'])} (error 21656 al enviar)"
    )


@live
@pytest.mark.xfail(
    reason="las plantillas de cita siguen categorizadas MARKETING: causa del error 63049",
    strict=False,
)
def test_live_las_plantillas_de_cita_son_utility(catalogo_twilio):
    registro = sids.all_sids()
    marketing = {}
    for nombre in SIDS_DE_CITA:
        entrada = catalogo_twilio.get(registro[nombre])
        if not entrada:
            continue
        categoria = (entrada.get("approval_requests") or {}).get("category")
        if categoria != "UTILITY":
            marketing[nombre] = categoria
    assert not marketing, (
        f"Plantillas transaccionales categorizadas como marketing: {marketing}. "
        "Meta descarta entregas a destinatarios de baja interaccion."
    )
