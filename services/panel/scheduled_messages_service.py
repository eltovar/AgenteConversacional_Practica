"""Scheduled message service for the advisor panel.

Moved verbatim from `middleware.outbound_panel` — no behavior changes.
"""

from typing import Optional

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

from logging_config import logger
from middleware import outbound_panel as _legacy
from middleware.outbound_panel import (
    _get_hubspot_contact_info,
    get_mongo_manager,
)
from utils.safe_logging import safe_id


class ScheduledMessageCreateRequest(BaseModel):
    template_sid: str = Field(..., description="Content SID de la plantilla Twilio (ej. HX...)")
    template_name: str = Field(..., description="Nombre legible de la plantilla")
    template_variables: dict = Field(..., description="Variables de la plantilla, ej. {'1': 'Carlos'}")
    scheduled_dt: str = Field(..., description="Fecha y hora de envío en ISO 8601 (naive = hora Colombia)")
    advisor_id: Optional[str] = Field(None, description="HubSpot owner ID de la asesora")
    canal: str = Field("whatsapp", description="Canal del contacto")
    notes: str = Field("", description="Nota interna opcional")


async def create_scheduled_message(
    contact_id: str,
    body: ScheduledMessageCreateRequest,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """
    Programa un mensaje de plantilla WhatsApp para enviarse en una fecha/hora específica.
    El scheduler (check_scheduled_messages, cada 1 min) lo detecta y lo envía vía Twilio.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    from datetime import datetime as dt
    from zoneinfo import ZoneInfo
    BOGOTA_TZ = ZoneInfo("America/Bogota")

    # Parsear scheduled_dt: tratamos naive como hora Colombia
    try:
        sched_dt = dt.fromisoformat(body.scheduled_dt)
        if sched_dt.tzinfo is None:
            sched_dt = sched_dt.replace(tzinfo=BOGOTA_TZ)
        sched_dt_bogota = sched_dt.astimezone(BOGOTA_TZ)
    except ValueError:
        raise HTTPException(status_code=422, detail="Formato de fecha inválido. Use ISO 8601.")

    # Validar que sea en el futuro (mínimo 1 minuto)
    now_bogota = dt.now(BOGOTA_TZ)
    if sched_dt_bogota <= now_bogota:
        raise HTTPException(status_code=422, detail="La fecha de envío debe ser en el futuro.")

    # Resolver phone y firstname del contacto
    phone = ""
    contact_firstname = ""
    try:
        hs_info = await _get_hubspot_contact_info(contact_id)
        if hs_info:
            phone = hs_info.get("phone", "")
            contact_firstname = (hs_info.get("firstname") or "").strip()
    except Exception:
        pass

    if not phone:
        raise HTTPException(status_code=422, detail="No se encontró teléfono para el contacto.")

    # Persistir en MongoDB (se guarda con tz-aware → Motor lo convierte a UTC en BSON)
    mongo_mgr = get_mongo_manager()
    message_id = await mongo_mgr.create_scheduled_message(
        contact_id=contact_id,
        phone=phone,
        canal=body.canal,
        template_sid=body.template_sid,
        template_name=body.template_name,
        template_variables=body.template_variables,
        scheduled_dt=sched_dt_bogota,
        advisor_id=body.advisor_id or "",
        notes=body.notes,
    )

    if not message_id:
        raise HTTPException(status_code=500, detail="Error guardando el mensaje en base de datos.")

    logger.info(
        f"[Panel] Mensaje programado creado: contact={contact_id}, "
        f"template={body.template_name}, dt={sched_dt_bogota.isoformat()}, id={message_id}"
    )
    return {
        "message_id": message_id,
        "template_name": body.template_name,
        "scheduled_dt": sched_dt_bogota.isoformat(),
    }


async def get_scheduled_messages(
    contact_id: str,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Lista todos los mensajes programados de un contacto."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    messages = await mongo_mgr.get_scheduled_messages(contact_id)
    return {"messages": messages, "total": len(messages)}


async def cancel_scheduled_message(
    message_id: str,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Cancela un mensaje programado pendiente."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    ok = await mongo_mgr.cancel_scheduled_message(message_id)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail="Mensaje no encontrado o ya no está en estado pending."
        )
    logger.info(f"[Panel] Mensaje programado cancelado: {safe_id(message_id, 'scheduled_message')}")
    return {"ok": True}
