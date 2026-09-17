"""Transfer service for the advisor panel.

Moved verbatim from `middleware.outbound_panel` — no behavior changes.
"""

import asyncio
import json
from typing import Optional

from fastapi import Form, Header, Query, HTTPException

from logging_config import logger
from middleware import outbound_panel as _legacy
from middleware.outbound_panel import (
    _ensure_bsuid_hubspot_contact,
    _get_advisor_name,
    _get_redis_client,
    _get_state_manager,
    _resolve_panel_target,
    ws_manager,
)
from utils.safe_logging import safe_error, safe_id, safe_phone


async def transfer_contact(
    phone: str,
    to_owner_id: str = Form(..., description="ID del asesor destino"),
    mode: str = Form("exclusive", description="Modo: exclusive o collaborative"),
    reason: str = Form("", description="Motivo de la transferencia"),
    contact_id: Optional[str] = Form(None, description="ID del contacto en HubSpot"),
    canal: str = Form("whatsapp", description="Canal de la conversación"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Transfiere un contacto a otro asesor.
    """
    logger.info(f"[Panel] POST /contacts/{safe_phone(phone)}/transfer -> {safe_id(to_owner_id, 'owner')} (modo: {mode})")

    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    target = _resolve_panel_target(phone)
    if target.error:
        raise HTTPException(status_code=400, detail=target.error)
    phone_normalized = target.key

    if not contact_id and target.is_bsuid:
        try:
            contact_id = await asyncio.wait_for(
                _ensure_bsuid_hubspot_contact(target, canal or "whatsapp"),
                timeout=3.0,
            )
        except Exception as e:
            logger.warning(f"[Panel][Transfer][BSUID] No se pudo asegurar contacto HubSpot: {safe_error(e)}")

    # Validar modo
    if mode not in ["exclusive", "collaborative"]:
        raise HTTPException(status_code=400, detail="Modo debe ser 'exclusive' o 'collaborative'")

    # === 1. Transferir en Redis + MongoDB + HubSpot (centralizado) ===
    state_manager = _get_state_manager()

    result = await state_manager.transfer_ownership(
        phone=phone_normalized,
        canal=canal,
        to_owner_id=to_owner_id,
        contact_id=contact_id,
        mode=mode,
        reason=reason,
    )

    if result.get("status") != "success":
        raise HTTPException(status_code=400, detail=result.get("message", "Error en transferencia"))

    from_owner = result.get("from_owner")
    hubspot_updated = "queued" if contact_id and mode == "exclusive" else "skipped"
    from_owner_name = _get_advisor_name(from_owner) if from_owner else "Sin asesor previo"
    to_owner_name = _get_advisor_name(to_owner_id)
    transfer_scope = "exclusiva" if mode == "exclusive" else "colaborativa"

    # === 3. Notificar vía WebSocket ===
    try:
        # Obtener nombre del contacto
        meta = await state_manager.get_meta(phone_normalized, canal)
        contact_name = meta.display_name if meta else phone_normalized

        await ws_manager.notify_contact_transferred(
            phone=phone_normalized,
            from_advisor=from_owner or "unknown",
            to_advisor=to_owner_id,
            contact_name=contact_name,
            mode=mode,
            redis_client=state_manager.redis
        )
    except Exception as e:
        logger.warning(f"[Panel] Error notificando WebSocket: {safe_error(e)}")

    # Transferencia silenciosa: no agregar al inbox de no-leídos del receptor.
    # El owner ya quedó transferido arriba; el contacto aparecerá por propiedad,
    # pero no como mensaje nuevo urgente ni anclado al tope.
    try:
        if from_owner:
            await state_manager.remove_from_advisor_inbox(from_owner, phone_normalized, canal or "whatsapp")
    except Exception as _tr_inbox_err:
        logger.warning(f"[Panel][Inbox] Error en inbox transfer (non-fatal): {_tr_inbox_err}")

    return {
        "status": "success",
        "message": (
            f"Transferencia {transfer_scope} confirmada: el chat pasó de "
            f"{from_owner_name} a {to_owner_name}."
        ),
        "phone": phone_normalized,
        "from_owner": from_owner,
        "from_owner_name": from_owner_name,
        "to_owner": to_owner_id,
        "to_owner_name": to_owner_name,
        "mode": mode,
        "transfer_scope": transfer_scope,
        "hubspot_updated": hubspot_updated,
        "transfer_history": result.get("transfer_history", [])
    }
async def request_transfer(
    contact_id: str,
    phone: str = Query(..., description="Teléfono normalizado del contacto"),
    requesting_advisor_id: str = Query(..., description="ID del asesor que solicita"),
    owner_advisor_id: str = Query(..., description="ID del asesor propietario actual"),
    canal: str = Query("whatsapp_directo", description="Canal del contacto"),
    contact_name: str = Query("", description="Nombre del contacto para mostrar"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Transfiere un contacto al asesor solicitante de forma inmediata.
    El contacto aparece en el panel del solicitante al instante.
    El ex-propietario recibe una notificación informativa (no de aprobación).
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    requester_name = _get_advisor_name(requesting_advisor_id)
    display = contact_name or phone

    # 1. Activar contacto en el panel del solicitante inmediatamente
    state_manager = _get_state_manager()
    try:
        await state_manager.activate_human(
            phone_normalized=phone,
            canal_origen=canal,
            owner_id=requesting_advisor_id,
            contact_id=contact_id,
            display_name=display,
            reason=f"Transferencia directa desde modal de creación"
        )
    except Exception as e:
        logger.warning(f"[Panel] activate_human en transfer-request falló: {safe_error(e)}")

    # 2. Reasignar owner en HubSpot via centralizado
    _sm_hs = _get_state_manager()
    asyncio.create_task(_sm_hs._sync_hubspot_owner(contact_id, requesting_advisor_id, phone))

    # 3-5. Un cliente redis para phone_cache + notificaciones + broadcast
    _rc_transfer = await _get_redis_client()
    try:
        await _rc_transfer.set(f"phone_cache:{contact_id}", phone, ex=86400, nx=True)
    except Exception:
        pass

    # 4. Notificar al ex-propietario (solo informativo) — cross-worker safe
    await ws_manager.notify_contact_transferred(
        phone=phone,
        from_advisor=owner_advisor_id,
        to_advisor=requesting_advisor_id,
        contact_name=display,
        mode="exclusive",
        redis_client=_rc_transfer
    )

    # 5. Broadcast para que todos los paneles refresquen
    await ws_manager.publish_broadcast(_rc_transfer, {
        "type": "contact_updated",
        "phone": phone,
        "action": "transfer_completed"
    })

    # Transferencia silenciosa: el receptor no recibe badge/no-leído automático.
    try:
        _sm_tr = _get_state_manager()
        await _sm_tr.remove_from_advisor_inbox(owner_advisor_id, phone, canal or "whatsapp")
    except Exception as _tr_req_inbox_err:
        logger.warning(f"[Panel][Inbox] Error inbox transfer-request (non-fatal): {_tr_req_inbox_err}")

    logger.info(f"[Panel] Transfer directo: {safe_id(owner_advisor_id, 'advisor')} -> {safe_id(requesting_advisor_id, 'advisor')} para {safe_id(contact_id, 'contact')} ({canal})")
    return {"status": "transferred", "phone": phone, "canal": canal}


async def accept_transfer(
    contact_id: str,
    by_advisor_id: str = Query(..., description="ID del asesor que acepta (propietario actual)"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    El asesor propietario acepta la solicitud de transferencia.
    Reasigna en HubSpot + Redis y notifica al solicitante.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    _rc = await _get_redis_client()
    raw = await _rc.get(f"transfer_req:{contact_id}")
    if not raw:
        raise HTTPException(status_code=400, detail="Solicitud expirada o no encontrada")

    req_data = json.loads(raw)
    requester_id = req_data["requester_id"]
    phone = req_data["phone"]
    contact_name = req_data.get("contact_name", phone)

    # 1+2. Transferir ownership centralizado (Redis + MongoDB + HubSpot)
    _sm_accept = _get_state_manager()
    try:
        _meta_keys = await _rc.keys(f"conv_meta:{phone}:*")
        for mk in _meta_keys:
            if isinstance(mk, bytes):
                mk = mk.decode("utf-8", errors="ignore")
            _canal = mk.split(":")[-1] if mk.count(":") >= 2 else "whatsapp"
            await _sm_accept.transfer_ownership(
                phone=phone,
                canal=_canal,
                to_owner_id=requester_id,
                from_owner_id=by_advisor_id,
                contact_id=contact_id,
                reason="transfer_accepted",
            )
    except Exception as e:
        logger.warning(f"[Panel] Error en transfer_ownership en transfer-accept: {safe_error(e)}")

    # 3. Limpiar solicitud pendiente
    await _rc.delete(f"transfer_req:{contact_id}")

    # 4. Notificar al solicitante
    await ws_manager.publish_to_advisor(_rc, requester_id, {
        "type": "transfer_accepted",
        "contact_id": contact_id,
        "phone": phone,
        "contact_name": contact_name,
        "message": "Transferencia aceptada — el contacto es tuyo"
    })

    # 5. Broadcast para refrescar el panel de todos
    await ws_manager.publish_broadcast(_rc, {
        "type": "contact_updated",
        "phone": phone,
        "action": "transfer_completed"
    })

    # Transferencia silenciosa: el solicitante no recibe badge/no-leído automático.
    try:
        _sm_ta = _get_state_manager()
        await _sm_ta.remove_from_advisor_inbox(by_advisor_id, phone, None)
    except Exception as _ta_inbox_err:
        logger.warning(f"[Panel][Inbox] Error inbox transfer-accept (non-fatal): {_ta_inbox_err}")

    logger.info(f"[Panel] Transfer accept: {safe_id(by_advisor_id, 'advisor')} -> {safe_id(requester_id, 'advisor')} para {safe_id(contact_id, 'contact')}")
    return {"status": "accepted"}


async def reject_transfer(
    contact_id: str,
    by_advisor_id: str = Query(..., description="ID del asesor que rechaza"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    El asesor propietario rechaza la solicitud de transferencia.
    Notifica al solicitante y limpia Redis.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    _rc = await _get_redis_client()
    raw = await _rc.get(f"transfer_req:{contact_id}")
    req_data = json.loads(raw) if raw else {}
    requester_id = req_data.get("requester_id", "")
    phone = req_data.get("phone", "")
    contact_name = req_data.get("contact_name", phone)

    await _rc.delete(f"transfer_req:{contact_id}")

    if requester_id:
        await ws_manager.publish_to_advisor(_rc, requester_id, {
            "type": "transfer_rejected",
            "contact_id": contact_id,
            "phone": phone,
            "contact_name": contact_name,
            "message": "El asesor rechazó la transferencia"
        })

    logger.info(f"[Panel] Transfer reject: {safe_id(by_advisor_id, 'advisor')} para {safe_id(contact_id, 'contact')}")
    return {"status": "rejected"}
