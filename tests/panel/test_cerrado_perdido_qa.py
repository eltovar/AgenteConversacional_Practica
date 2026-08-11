"""
QA Fase 6 — "Cerrado perdido" no dispara el auto-cierre en produccion.

Recorre la cadena completa panel -> backend -> Redis/Mongo y senala EN QUE PASO
se corta. La Fase 6 anterior fallo porque probo _auto_close_by_stage() en
aislamiento: nunca valido el contrato del panel, que es donde esta el fallo.

CADENA:
  1. El panel arma el body del PATCH        <- incluye `phone`?
  2. PATCH /contacts/{id}/stage
  3. HubSpot actualiza lifecyclestage
  4. Backend evalua `stage_id in STAGES_AUTO_CLOSE and phone`
  5. _auto_close_by_stage -> _close_conversation_internal
  6. Redis: zrem del ZSET + in_panel=False
  7. MongoDB: archived=True
  8. El panel limpia la UI con _performCloseCleanup(body.phone)

Ejecutar:
    python -m pytest tests/panel/test_cerrado_perdido_qa.py -v
"""
import json
import os
import re
import sys

import pytest
from unittest.mock import AsyncMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PANEL_JS = os.path.join(ROOT, "middleware", "PanelAsesores", "index.js")

CONTACT_ID = "12345"
PHONE = "+573001234567"
CANAL = "whatsapp"
STAGE_CERRADO_PERDIDO = "evangelist"
STAGE_NO_RESPONDE = "other"


def _read_panel_js() -> str:
    with open(PANEL_JS, "r", encoding="utf-8") as f:
        return f.read()


def _mock_hubspot_ok():
    class _R:
        status_code = 200
        text = "{}"
    return _R()


def _patch_backend(close_spy):
    """Aisla el endpoint: HubSpot y el cierre real quedan mockeados."""
    return [
        patch.dict(os.environ, {"ADMIN_API_KEY": "test_admin_key"}),
        patch("middleware.outbound_panel._hubspot_patch",
              new=AsyncMock(return_value=_mock_hubspot_ok())),
        patch("middleware.outbound_panel._invalidate_contact_stage_cache",
              new=AsyncMock()),
        patch("middleware.outbound_panel._close_conversation_internal",
              new=close_spy),
        patch("middleware.outbound_panel.ws_manager.notify_status_change",
              new=AsyncMock(return_value=1)),
    ]


def _call_stage_endpoint(body: dict, close_spy: AsyncMock):
    from fastapi.testclient import TestClient
    from app import app

    patches = _patch_backend(close_spy)
    for p in patches:
        p.start()
    try:
        client = TestClient(app)
        resp = client.patch(
            f"/whatsapp/panel/contacts/{CONTACT_ID}/stage",
            json=body,
            headers={"X-API-Key": "test_admin_key"},
        )
        return resp
    finally:
        for p in reversed(patches):
            p.stop()


# ═══════════════════════════════════════════════════════════════
# PASO 1 — Contrato del panel: el body debe llevar `phone`
# ═══════════════════════════════════════════════════════════════

def test_paso1_panel_envia_phone_para_cerrado_perdido():
    """
    PASO 1 — El panel solo adjunta `phone` para 'other' y LUISA_TRANSFER_STAGES.
    'evangelist' no esta en ninguno, asi que el backend lo recibe como None y
    la rama de auto-cierre nunca corre. Este es el fallo de produccion.
    """
    js = _read_panel_js()

    m = re.search(r"const body = \{ stage_id: stageId \};\s*\n(.*?)\n\s*\}", js, re.DOTALL)
    assert m, "no se encontro la construccion del body en updateDealStage()"
    guard = m.group(0)

    envia_phone = "body.phone" in guard
    assert envia_phone, "el panel nunca adjunta phone"

    cubre_cerrado_perdido = (
        "AUTO_CLOSE_STAGES" in guard
        or "evangelist" in guard
    )
    assert cubre_cerrado_perdido, (
        "PASO 1 ROTO: el guard del body no cubre 'evangelist'. "
        "El PATCH sale sin `phone` y el backend no puede cerrar."
    )


def test_paso1b_panel_declara_cerrado_perdido_como_auto_close():
    """PASO 1b — El panel debe declarar el stage de cierre, no solo los de Luisa."""
    js = _read_panel_js()

    m = re.search(r"const LUISA_TRANSFER_STAGES = \{(.*?)\};", js, re.DOTALL)
    assert m, "LUISA_TRANSFER_STAGES no encontrado"
    assert "evangelist" not in m.group(1), (
        "'evangelist' esta en LUISA_TRANSFER_STAGES: seria transferido a Luisa "
        "en vez de cerrado"
    )

    assert re.search(r"AUTO_CLOSE_STAGES\s*=\s*\{", js), (
        "PASO 1b ROTO: el panel no declara AUTO_CLOSE_STAGES con 'Cerrado perdido'"
    )


# ═══════════════════════════════════════════════════════════════
# PASO 2-5 — El endpoint con el payload REAL del panel
# ═══════════════════════════════════════════════════════════════

def test_paso2_repro_produccion_sin_phone_no_cierra():
    """
    PASO 2 — REPRODUCCION EXACTA del fallo: el panel manda solo stage_id.
    El endpoint responde 200 y actualiza HubSpot, pero closed=False y el
    contacto se queda donde estaba. Documenta el comportamiento observado.
    """
    close_spy = AsyncMock(return_value={"phone": PHONE, "canal": CANAL,
                                        "new_status": "BOT_ACTIVE"})

    resp = _call_stage_endpoint({"stage_id": STAGE_CERRADO_PERDIDO}, close_spy)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "success", "la etapa si se actualiza en HubSpot"
    assert data["closed"] is False, "sin phone el backend no puede cerrar"
    close_spy.assert_not_awaited()


def test_paso3_con_phone_el_backend_si_cierra():
    """
    PASO 3 — Con `phone` en el body el backend cierra correctamente.
    Aisla la culpa: el backend esta bien, lo que falla es el contrato del panel.
    """
    close_spy = AsyncMock(return_value={"phone": PHONE, "canal": CANAL,
                                        "new_status": "BOT_ACTIVE"})

    resp = _call_stage_endpoint(
        {"stage_id": STAGE_CERRADO_PERDIDO, "phone": PHONE, "canal": CANAL},
        close_spy,
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["closed"] is True, "con phone deberia cerrar"
    close_spy.assert_awaited_once()


def test_paso4_no_responde_sigue_funcionando():
    """
    PASO 4 — Contra-prueba: 'No responde' si manda phone desde el panel y
    sigue cerrando. Confirma que el fallo es especifico de 'Cerrado perdido'.
    """
    close_spy = AsyncMock(return_value={"phone": PHONE, "canal": CANAL,
                                        "new_status": "BOT_ACTIVE"})

    resp = _call_stage_endpoint(
        {"stage_id": STAGE_NO_RESPONDE, "phone": PHONE, "canal": CANAL},
        close_spy,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["closed"] is True


def test_paso5_cerrado_perdido_no_transfiere_a_luisa():
    """PASO 5 — 'Cerrado perdido' cierra sin cambiar de dueno."""
    from middleware import outbound_panel as op

    assert STAGE_CERRADO_PERDIDO in op.STAGES_AUTO_CLOSE
    assert STAGE_CERRADO_PERDIDO not in op.STAGES_TRANSFER_TO_LUISA

    close_spy = AsyncMock(return_value={"phone": PHONE, "canal": CANAL,
                                        "new_status": "BOT_ACTIVE"})
    resp = _call_stage_endpoint(
        {"stage_id": STAGE_CERRADO_PERDIDO, "phone": PHONE, "canal": CANAL},
        close_spy,
    )

    data = resp.json()
    assert data.get("transferred") is None, "no debe transferir owner"
    assert data.get("to_owner") is None


# ═══════════════════════════════════════════════════════════════
# PASO 8 — Limpieza de UI
# ═══════════════════════════════════════════════════════════════

def test_paso8_ui_limpia_tras_cierre():
    """
    PASO 8 — La limpieza de UI depende de body.phone. Si el body no lo lleva,
    aunque el backend cerrara el contacto seguiria visible hasta recargar.
    """
    js = _read_panel_js()

    m = re.search(r"if \(data\.closed[^)]*\)\s*\{\s*\n\s*_performCloseCleanup\(([^)]+)\)", js)
    assert m, "no se encontro la llamada a _performCloseCleanup tras data.closed"

    arg = m.group(1).strip()
    assert "phone" in arg, f"_performCloseCleanup recibe '{arg}', que no es un telefono"


# ═══════════════════════════════════════════════════════════════
# Metricas de calidad
# ═══════════════════════════════════════════════════════════════

def test_metricas_cobertura_stages_de_cierre():
    """
    Todo stage que el backend cierra debe estar declarado en el panel; si no,
    el panel no manda `phone` y el cierre no se ejecuta. Este invariante es el
    que se rompio con 'Cerrado perdido'.
    """
    from middleware import outbound_panel as op

    js = _read_panel_js()
    m = re.search(r"const body = \{ stage_id: stageId \};\s*\n(.*?)\n\s*\}", js, re.DOTALL)
    assert m, "no se encontro la construccion del body"
    guard = m.group(0)

    faltantes = []
    for stage_id in list(op.STAGES_AUTO_CLOSE) + list(op.STAGES_TRANSFER_TO_LUISA):
        declarado = (
            f"'{stage_id}'" in guard
            or f'"{stage_id}"' in guard
            or "LUISA_TRANSFER_STAGES" in guard and stage_id in op.STAGES_TRANSFER_TO_LUISA
            or "AUTO_CLOSE_STAGES" in guard and stage_id in op.STAGES_AUTO_CLOSE
        )
        if not declarado:
            faltantes.append(f"{stage_id} ({op.PIPELINE_STAGES.get(stage_id, '?')})")

    total = len(op.STAGES_AUTO_CLOSE) + len(op.STAGES_TRANSFER_TO_LUISA)
    cubiertos = total - len(faltantes)
    print(f"\n  [METRIC] Stages con cierre cubiertos por el panel: {cubiertos}/{total}")

    assert not faltantes, (
        f"stages que el backend cierra pero el panel no envia con phone: {faltantes}"
    )
