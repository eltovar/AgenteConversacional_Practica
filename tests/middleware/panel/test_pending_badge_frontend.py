"""
tests/panel/test_pending_badge_frontend.py
==========================================

El aviso del panel significa NO LEIDO y se apaga al abrir el chat.

Historia, porque importa para no repetirla. El 12-ago-2026 se cambio a "sin
responder": el aviso solo se apagaba al contestar. Se revirtio el mismo dia. Dos
motivos, los dos medidos en produccion:

  El cliente suele responder a los pocos segundos de que la asesora conteste
  —13s, 7s en los casos revisados—, asi que el aviso volvia a encenderse sin
  parar y dejaba de leerse.

  La reconciliacion se salta el contacto abierto (`_rp !== currentPhone`), asi
  que tras contestar el aviso seguia encendido hasta cambiar de chat. Desde la
  silla de la asesora, "contesté y no se fue".

**Revertirlo no pierde visibilidad.** Que un contacto con el cliente esperando
aparezca en la lista NO depende del badge: lo garantiza `pending_reply` en el
corte del backend (outbound_panel._split_always_visible), que sigue en pie. Las
dos cosas son independientes y estos tests lo fijan.

Los tests no buscan cadenas: **extraen la condicion real del fuente y la
ejecutan en node** contra objetos de prueba.

Requiere node en el PATH (se salta si no lo hay).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INDEX_JS = os.path.join(_ROOT, "middleware", "PanelAsesores", "index.js")

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node no esta disponible"
)


def _src() -> str:
    with open(INDEX_JS, encoding="utf-8") as fh:
        return fh.read()


def _run_node(script: str) -> str:
    r = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise AssertionError(f"node fallo:\n{r.stderr}")
    return r.stdout.strip()


def _extraer(desde: str, hasta: str = ") {") -> str:
    """Recorta el fragmento real de index.js entre un ancla y el cierre del if."""
    src = _src()
    i = src.find(desde)
    assert i != -1, f"No se encontro el ancla en index.js: {desde!r}"
    j = src.find(hasta, i)
    assert j != -1, f"No se encontro el cierre {hasta!r} tras el ancla"
    return src[i:j + len(hasta)]


# ── Guard 1: inicializacion del aviso desde el servidor ───────────────────────

_ANCLA_INIT = "                if (\n                    contact.has_unread &&"


def _evalua_init(contact: dict, current_phone=None, vistos=()) -> bool:
    fragmento = _extraer(_ANCLA_INIT)
    script = f"""
        const contact = {json.dumps(contact)};
        const phone = contact.phone || '';
        const currentPhone = {json.dumps(current_phone)};
        const _seenPhones = new Set({json.dumps(list(vistos))});
        {fragmento}
            console.log('SI');
        }} else {{
            console.log('NO');
        }}
    """
    return _run_node(script) == "SI"


def test_el_aviso_aparece_por_mensaje_sin_leer():
    assert _evalua_init({"phone": "+57300", "has_unread": True}) is True


def test_la_deuda_sola_no_enciende_el_aviso():
    """
    `pending_reply` mantiene al contacto EN LA LISTA, pero no pinta aviso. Si
    volviera a pintarlo, vuelve el ruido del 12-ago.

    Mutacion: anadir `|| contact.pending_reply` a la condicion deja esto en rojo.
    """
    assert _evalua_init({"phone": "+57300", "has_unread": False,
                         "pending_reply": True}) is False


def test_abrir_el_chat_silencia_el_aviso_en_la_sesion():
    """El guard _seenPhones vuelve a aplicar sin excepciones."""
    assert _evalua_init(
        {"phone": "+57300", "has_unread": True, "pending_reply": True},
        vistos=["+57300"],
    ) is False


def test_el_contacto_abierto_no_lleva_aviso():
    assert _evalua_init(
        {"phone": "+57300", "has_unread": True}, current_phone="+57300",
    ) is False


def test_sin_ninguna_senal_no_hay_aviso():
    assert _evalua_init({"phone": "+57300", "has_unread": False}) is False


# ── Guard 2: el servidor limpia el aviso ──────────────────────────────────────

_ANCLA_RECON = "            if (!contact.has_unread && _rp && _rp !== currentPhone &&"


def _evalua_limpieza(contact: dict, unread_local=1, current_phone=None,
                     recien_cerrados=()) -> bool:
    fragmento = _extraer(_ANCLA_RECON)
    script = f"""
        const contact = {json.dumps(contact)};
        const _rp = contact.phone || '';
        const currentPhone = {json.dumps(current_phone)};
        const unreadCounts = {{}};
        unreadCounts[_rp] = {unread_local};
        const recentlyClosedPhones = new Set({json.dumps(list(recien_cerrados))});
        {fragmento}
            console.log('LIMPIA');
        }} else {{
            console.log('NO');
        }}
    """
    return _run_node(script) == "LIMPIA"


def test_el_servidor_limpia_cuando_no_hay_sin_leer():
    assert _evalua_limpieza({"phone": "+57300", "has_unread": False}) is True


def test_la_deuda_ya_no_impide_limpiar():
    """
    Mientras el aviso significo "sin responder", `pending_reply` bloqueaba la
    limpieza y por eso quedaba encendido. Ya no.

    Mutacion: reintroducir `&& !contact.pending_reply` deja esto en rojo.
    """
    assert _evalua_limpieza({"phone": "+57300", "has_unread": False,
                            "pending_reply": True}) is True


def test_no_se_limpia_el_contacto_abierto():
    assert _evalua_limpieza(
        {"phone": "+57300", "has_unread": False}, current_phone="+57300",
    ) is False


def test_el_contacto_recien_cerrado_no_se_toca():
    assert _evalua_limpieza(
        {"phone": "+57300", "has_unread": False}, recien_cerrados=["+57300"],
    ) is False


# ── Guard 3: el clic apaga el aviso ───────────────────────────────────────────


_ANCLA_CLIC = "    if (phone && unreadCounts[phone]) {"


def _evalua_clic(phone: str, unread_local: int) -> bool:
    """Ejecuta la condicion real del clic. La version anterior de este test
    miraba el TEXTO del bloque y pasaba en falso con `if (false)` delante."""
    fragmento = _extraer(_ANCLA_CLIC, "{")
    script = f"""
        const phone = {json.dumps(phone)};
        const unreadCounts = {{}};
        unreadCounts[phone] = {unread_local};
        {fragmento}
            console.log('BORRA');
        }} else {{
            console.log('NO');
        }}
    """
    return _run_node(script) == "BORRA"


def test_el_clic_borra_el_aviso_al_abrir():
    """
    Conservarlo cuando habia deuda es lo que producia "contesté y no se fue",
    porque la reconciliacion se salta el contacto abierto.

    Mutacion: desactivar la condicion deja esto en rojo.
    """
    assert _evalua_clic("+57300", 1) is True
    assert _evalua_clic("+57300", 3) is True


def test_el_clic_no_hace_nada_si_no_habia_aviso():
    assert _evalua_clic("+57300", 0) is False


def test_el_clic_no_consulta_la_deuda():
    """El bloque del clic no puede volver a mirar `pending_reply`."""
    src = _src()
    i = src.find("// Al abrir el chat de un contacto, marcar sus mensajes como leídos")
    assert i != -1, "Desaparecio el bloque del clic"
    bloque = src[i:i + 900]

    assert "delete unreadCounts[phone];" in bloque
    assert "pending_reply" not in bloque, (
        "El clic vuelve a mirar la deuda: el aviso se quedara encendido tras "
        "contestar, porque la reconciliacion se salta el contacto abierto"
    )


def test_el_frontend_no_decide_nada_con_la_deuda():
    """
    Barrido: `pending_reply` no puede aparecer en ninguna condicion de index.js.
    Es una senal del backend para no cortar el contacto de la lista, no para
    pintar. Si vuelve a colarse en un `if`, esto lo caza.
    """
    import re

    src = _src()
    sospechosas = []
    for n, linea in enumerate(src.splitlines(), 1):
        if "pending_reply" not in linea:
            continue
        sin_comentario = re.sub(r"//.*$", "", linea).strip()
        if sin_comentario:
            sospechosas.append((n, sin_comentario))

    assert not sospechosas, (
        "index.js vuelve a decidir con pending_reply:\n"
        + "\n".join(f"  linea {n}: {t}" for n, t in sospechosas)
    )


# ── El invariante que hace segura la reversion ────────────────────────────────


def test_la_visibilidad_no_depende_del_badge():
    """
    Lo que permite revertir el badge sin perder contactos: el backend mete en la
    lista a todo el que espera respuesta, tenga aviso o no.

    Si alguien quitara `pending_reply` del corte pensando que ya no se usa
    —porque el frontend dejo de mirarlo—, volverian a desaparecer contactos.
    Este test ata las dos piezas.
    """
    import sys

    sys.path.insert(0, _ROOT)
    os.environ.setdefault("HUBSPOT_API_KEY", "test-dummy-key")
    os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")
    os.environ.setdefault("ADMIN_API_KEY", "test-admin")

    from middleware.outbound_panel import _split_always_visible

    contactos = [
        {"phone": f"+5730000{i:04d}", "last_activity": "2026-08-12T10:00:00-05:00"}
        for i in range(200)
    ]
    sepultado = contactos[150]

    activos, stats = _split_always_visible(
        contactos, unread_phones=set(), pending_phones={sepultado["phone"]},
        page=1, limit=30,
    )

    assert sepultado in activos, (
        "El contacto con el cliente esperando ya no entra en la lista: revertir "
        "el badge SI perdio visibilidad"
    )
    assert stats["pending"] == 1


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
