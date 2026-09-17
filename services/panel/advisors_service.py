"""Advisor service for the advisor panel.

Moved verbatim from `middleware.outbound_panel` — no behavior changes.
"""

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

from logging_config import logger
from middleware import outbound_panel as _legacy
from middleware.outbound_panel import (
    get_mongo_manager,
)
from utils.safe_logging import safe_id


async def list_advisors(x_api_key: str = Header(None, alias="X-API-Key")):
    """Lista todos los asesores con sus nombres editables."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    advisors = await mongo_mgr.get_advisors()
    return {"advisors": advisors}


class AdvisorUpdateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


async def update_advisor(
    advisor_id: str,
    body: AdvisorUpdateRequest,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Actualiza el nombre de un asesor."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    ok = await mongo_mgr.update_advisor(advisor_id, body.name)
    if not ok:
        raise HTTPException(status_code=404, detail="Asesor no encontrado")
    logger.info(f"[Panel] Advisor {safe_id(advisor_id, 'advisor')} renombrado a: {safe_id(body.name, 'name')}")
    return {"ok": True, "id": advisor_id, "name": body.name}
