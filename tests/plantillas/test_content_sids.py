"""
Las plantillas de WhatsApp viven en TRES espejos que nadie obliga a coincidir:

    1. el ContentSid en `middleware/templates/content_sids.py` — lo que Twilio manda
       de verdad al cliente;
    2. el `body` en `middleware/templates/templates.py` — lo que la asesora lee en el
       panel y lo que se envía como texto plano dentro de la ventana de 24h;
    3. los previews duplicados en `app.py`, que son lo que se guarda en Mongo y acaba
       en el historial del panel.

Cuando divergen no se rompe nada visible: el cliente recibe un texto y la asesora lee
otro, y el fallo solo aparece leyendo una conversación real. Estos tests son el único
sitio donde esa divergencia se convierte en rojo.

Son de análisis estático: no hay red, no hay Redis, no hay Twilio.
"""
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from middleware.templates import content_sids as sids  # noqa: E402
from middleware.templates.templates import DEFAULT_TEMPLATES  # noqa: E402

APP_PY = os.path.join(ROOT, "app.py")
PANEL_JS = os.path.join(ROOT, "middleware", "PanelAsesores", "index.js")

# SIDs vigentes en la cuenta Twilio (WABA 1579913223763554), verificados contra
# GET content.twilio.com/v1/ContentAndApprovals el 10-sep-2026: los 10 aprobados.
SIDS_ESPERADOS = {
    "SALUDO_INMUEBLE": "HX7dccecd8a5ee36a74a6f1099b088f6a6",       # saludo_inicial
    "REACTIVACION_LINK": "HXb733162e38a6786faf5db72133660a0d",     # reactivacion_link
    "AUN_EN_BUSQUEDA": "HXfd6fcb949b5747ca39d7b19af4f988fe",       # aun_en_busqueda
    "MENSAJE_PERSONALIZADO": "HXbca931e16b226053193dc3e003e06372",  # mensaje_personalizado
    "FOLLOWUP_1": "HXb586a84cf325689db3efd19ec2f2e93f",            # seguimiento_de_cita
    "FOLLOWUP_2": "HXf09d950f78a7997939a0424203d18598",            # experiencia_citav2
    "RECORDATORIO_CITA": "HXdd7160cc287929ec3ae4d18160e7c51b",     # recordatorio
    "CAMPANA_PROPIETARIOS": "HXfe0ca6d5004d215819a05d504577e4d7",  # campana_propietarios
}

# Nombre de la env var que puede sobreescribir cada constante. Si está definida en el
# entorno donde corre la suite, el default del código no es lo que se está midiendo y
# el assert de igualdad no tendría sentido.
ENV_VAR = {
    "SALUDO_INMUEBLE": "TWILIO_TPL_SALUDO_INMUEBLE",
    "REACTIVACION_LINK": "TWILIO_TPL_REACTIVACION_LINK",
    "AUN_EN_BUSQUEDA": "TWILIO_TPL_AUN_EN_BUSQUEDA",
    "MENSAJE_PERSONALIZADO": "TWILIO_TPL_MENSAJE_PERSONALIZADO",
    "FOLLOWUP_1": "TWILIO_TPL_CITA_SEGUIMIENTO",
    "FOLLOWUP_2": "TWILIO_TPL_CITA_EXPERIENCIA",
    "RECORDATORIO_CITA": "TWILIO_REMINDER_TEMPLATE_SID",
    "CAMPANA_PROPIETARIOS": "TWILIO_TPL_CAMPANA_PROPIETARIOS",
}


def _leer(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ── Los ContentSid ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("constante", sorted(SIDS_ESPERADOS))
def test_sid_vigente(constante):
    """El default del código es el SID aprobado en Meta hoy.

    Why: un SID de la cuenta anterior no falla al importar ni al desplegar —
    falla con HTTP 400 en el primer envío real, en producción y en silencio.
    """
    if os.getenv(ENV_VAR[constante]):
        pytest.skip(f"{ENV_VAR[constante]} sobreescribe el default en este entorno")
    assert getattr(sids, constante) == SIDS_ESPERADOS[constante]


def test_todos_los_sids_tienen_forma_de_content_sid():
    for nombre, sid in sids.all_sids().items():
        assert re.fullmatch(r"HX[0-9a-f]{32}", sid), f"{nombre} no es un ContentSid: {sid!r}"


def test_no_quedan_sids_hardcodeados_fuera_del_catalogo():
    """`content_sids.py` es la fuente única de verdad; ningún .py debe llevar HX a fuego.

    Why: el SID de `scripts/campanas/envio_propietarios_seguimiento.py` sobrevivió a
    la migración de cuenta Twilio precisamente porque estaba escrito a mano allí.
    """
    catalogo = os.path.join(ROOT, "middleware", "templates", "content_sids.py")
    culpables = []
    for carpeta in ("middleware", "scripts", "integrations", "utils", "agents"):
        for base, _, ficheros in os.walk(os.path.join(ROOT, carpeta)):
            if "__pycache__" in base:
                continue
            for fichero in ficheros:
                if not fichero.endswith(".py"):
                    continue
                ruta = os.path.join(base, fichero)
                if os.path.abspath(ruta) == os.path.abspath(catalogo):
                    continue
                for n, linea in enumerate(_leer(ruta).splitlines(), 1):
                    if linea.lstrip().startswith("#"):
                        continue  # los comentarios citan SIDs viejos a propósito
                    if re.search(r"HX[0-9a-f]{32}", linea):
                        culpables.append(f"{ruta}:{n}")
    assert not culpables, "ContentSid hardcodeado fuera de content_sids.py: " + ", ".join(culpables)


# ── Los espejos de texto ─────────────────────────────────────────────────────

def test_encuesta_experiencia_espejo_en_templates():
    """El body del panel dice lo mismo que `experiencia_citav2` en Meta."""
    body = DEFAULT_TEMPLATES["experiencia_cita"]["body"]
    assert "Esta encuesta toma un minuto y es anónima" in body
    assert "¡Gracias por confiar en nosotros!" in body
    # El texto de la v1 hablaba de «seguir mejorando la calidad de nuestro servicio».
    assert "seguir mejorando la calidad" not in body


def test_encuesta_experiencia_espejo_en_app():
    """`survey_preview` (app.py) es lo que se guarda en Mongo y ve la asesora.

    Why: Twilio envía por ContentSid, así que este texto no afecta al cliente —
    solo al historial. Si diverge, la asesora lee un mensaje que nunca se mandó.
    """
    app = _leer(APP_PY)
    assert "importante conocer tu " in app and "experiencia: https://forms.gle/" in app
    assert "experiencia y seguir mejorando la calidad" not in app


def test_seguimiento_post_cita_espejo():
    """`seguimiento_de_cita` está duplicado entre templates.py y app.py:1557."""
    body = DEFAULT_TEMPLATES["seguimiento_cita"]["body"]
    assert "¿Qué te pareció el inmueble?" in body
    assert "¿Qué te pareció el inmueble?" in _leer(APP_PY)


def test_saludo_inicial_espejo():
    tpl = DEFAULT_TEMPLATES["saludo_reactivador_inmueble"]
    assert "el inmueble esta disponible" in tpl["body"]
    # La plantilla en Meta no tiene variables: declarar alguna rompería el envío.
    assert tpl["variables"] == []
    assert tpl["content_variables_map"] == []


def test_variables_declaradas_coinciden_con_el_mapa():
    """`variables` y `content_variables_map` son la misma lista en el mismo orden.

    Why: el orden es lo que decide qué llega como {{1}} y qué como {{2}}. Invertirlo
    manda el nombre del cliente donde va el texto libre, y nadie lo ve hasta que llega.
    """
    for tid, tpl in DEFAULT_TEMPLATES.items():
        assert tpl["variables"] == tpl["content_variables_map"], tid


# ── Ocultar no es borrar ─────────────────────────────────────────────────────

AUTOMATICAS = ["cita_confirmacion", "seguimiento_cita", "experiencia_cita"]


def test_el_picker_filtra_por_la_etiqueta_del_backend():
    """El JS no puede volver a tener su propia lista de ids.

    Why: `ADVISOR_ID` sale de un query param de la URL (index.js:126-133). Un
    reparto decidido en el navegador es cosmético; quien decide es el backend.
    """
    js = _leer(PANEL_JS)
    assert ".filter(t => t.picker_visible !== false)" in js
    assert "PICKER_HIDDEN_TEMPLATE_IDS" not in js


def test_solo_automaticas_son_las_del_scheduler():
    from middleware.templates.templates import SOLO_AUTOMATICAS

    assert set(SOLO_AUTOMATICAS) == {"seguimiento_cita", "experiencia_cita"}
    # Se envía sola al agendar, pero se decidió dejarla visible para reenviarla.
    assert "cita_confirmacion" not in SOLO_AUTOMATICAS


@pytest.mark.parametrize("tid", AUTOMATICAS)
def test_las_automaticas_siguen_existiendo(tid):
    """Ocultarlas del panel no puede eliminarlas del catálogo.

    Why: `_init_default_templates` (outbound_panel.py:1847) BORRA de Redis toda
    plantilla que desaparezca de DEFAULT_TEMPLATES. Quitar `cita_confirmacion` de
    aquí dejaría la confirmación automática de citas en `plantilla_no_encontrada`.
    """
    assert tid in DEFAULT_TEMPLATES
