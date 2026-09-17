"""Notification service for the advisor panel.

Moved verbatim from `middleware.outbound_panel` — no behavior changes.
"""

from typing import Optional

from fastapi import Body, Header, Query, Request, HTTPException

from logging_config import logger
from middleware import outbound_panel as _legacy
from middleware.outbound_panel import (
    _get_state_manager,
    limiter,
)
from utils.safe_logging import safe_error, safe_id


@limiter.limit("60/minute")
async def get_advisor_notifications(
    request: Request,
    advisor: str = Query(..., description="ID del asesor"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Retorna las notificaciones activas del asesor (inactividad 24h + recordatorios diarios).
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        state_manager = _get_state_manager()
        notifications = await state_manager.get_advisor_notifications(advisor)
        return {
            "notifications": notifications,
            "unread_count": len(notifications),
        }
    except Exception as e:
        logger.error(f"[Panel] Error obteniendo notificaciones advisor={safe_id(advisor, 'advisor')}: {safe_error(e)}")
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")


async def mark_notification_read(
    notif_id: str,
    advisor: str = Body(..., embed=True),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Marca una notificación como leída (la elimina del ZSET)."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        state_manager = _get_state_manager()
        removed = await state_manager.remove_advisor_notification(advisor, notif_id)
        return {"status": "success", "removed": removed}
    except Exception as e:
        logger.error(f"[Panel] Error marcando notificación leída: {safe_error(e)}")
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")


async def mark_all_notifications_read(
    advisor: str = Body(..., embed=True),
    notif_type: Optional[str] = Body(None, embed=True),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Marca todas las notificaciones del asesor como leídas.
    Si notif_type se especifica (ej: 'inactivity_24h'), solo borra ese tipo.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        state_manager = _get_state_manager()
        notifications = await state_manager.get_advisor_notifications(advisor)
        removed = 0
        for notif in notifications:
            if notif_type is None or notif.get("type") == notif_type:
                ok = await state_manager.remove_advisor_notification(advisor, notif["id"])
                if ok:
                    removed += 1
        return {"status": "success", "removed": removed}
    except Exception as e:
        logger.error(f"[Panel] Error marcando todas las notificaciones: {safe_error(e)}")
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")
