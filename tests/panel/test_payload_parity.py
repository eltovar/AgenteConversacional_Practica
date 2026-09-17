"""Paridad de payload: los servicios responden igual que el legacy.

Estrategia sin staging ni BD:
- 401: con API key invalida, cada endpoint GET nuevo responde 401 igual que
  el legacy (mismo guard, mismo mensaje).
- Forma: `GET /stages` (sin BD: lista estatica) devuelve el payload legacy
  exacto [{id, name}] + count, pasando por el dominio propio.

Si un servicio cambia la forma, este test cae antes que el frontend.
"""

import os

os.environ.setdefault("HUBSPOT_API_KEY", "test_key")
os.environ.setdefault("ADMIN_API_KEY", "test_admin_key")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_stages_payload_identico_al_legacy(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "test_admin_key")
    monkeypatch.delenv("PANEL_API_KEY", raising=False)
    from middleware.outbound_panel import PIPELINE_STAGES_LIST
    from services.panel.reference_service import get_pipeline_stages

    out = await get_pipeline_stages(x_api_key="test_admin_key")
    assert out["stages"] == PIPELINE_STAGES_LIST
    assert out["count"] == len(PIPELINE_STAGES_LIST)


@pytest.mark.asyncio
async def test_guard_401_identico_en_lecturas():
    from fastapi import Request
    from services.panel.reference_service import get_pipeline_stages
    from services.panel.diagnostics_service import websocket_stats
    from services.panel.advisors_service import list_advisors
    from services.panel.notes_service import get_contact_notes
    from services.panel.metrics_service import get_social_media_metrics

    scope = {"type": "http", "method": "GET", "path": "/",
             "headers": [], "query_string": b""}
    req = Request(scope)
    casos = [
        (get_pipeline_stages, {}),
        (websocket_stats, {}),
        (list_advisors, {}),
        (get_contact_notes, {"contact_id": "1"}),
        (get_social_media_metrics, {}),
    ]
    for fn, kw in casos:
        try:
            if "request" in fn.__code__.co_varnames:
                kw = dict(kw, request=req)
            await fn(x_api_key="invalida", **kw)
            raise AssertionError(f"{fn.__name__} no rechazo 401")
        except HTTPException as e:
            assert e.status_code == 401, (fn.__name__, e.status_code)
            assert e.detail == "API Key inválida", (fn.__name__, e.detail)
