"""Realtime WebSocket service for the advisor panel.

Moved verbatim from `middleware.outbound_panel` — no behavior changes.
"""

import asyncio
import json

from fastapi import WebSocket, WebSocketDisconnect

from logging_config import logger
from middleware.outbound_panel import (
    _get_advisor_name,
    _get_redis_client,
    get_bogota_now,
    ws_manager,
)
from utils.safe_logging import safe_error, safe_id, safe_phone


async def websocket_endpoint(websocket: WebSocket, advisor_id: str):
    """
    Endpoint WebSocket para notificaciones en tiempo real.

    Conexión:
        ws://host/whatsapp/panel/ws/{advisor_id}
    """
    await ws_manager.connect(websocket, advisor_id)

    # Enviar transfer_requests pendientes donde este asesor es el propietario
    try:
        _rc_ws = await _get_redis_client()
        pending_keys = await _rc_ws.keys("transfer_req:*")
        for pk in pending_keys:
            raw_req = await _rc_ws.get(pk)
            if raw_req:
                req = json.loads(raw_req)
                if req.get("owner_id") == advisor_id:
                    requester_name = _get_advisor_name(req.get("requester_id", ""))
                    await websocket.send_json({
                        "type": "transfer_request",
                        "contact_id": req.get("contact_id"),
                        "phone": req.get("phone"),
                        "contact_name": req.get("contact_name", req.get("phone")),
                        "requester_id": req.get("requester_id"),
                        "requester_name": requester_name,
                        "message": f"{requester_name} quiere atender este contacto (solicitud pendiente)"
                    })
    except Exception:
        pass

    # Keepalive: si no llega ningún mensaje del cliente en 20s el servidor envía
    # un ping para que Railway no cierre la conexión TCP por inactividad.
    WS_KEEPALIVE_INTERVAL = 20

    try:
        while True:
            try:
                data = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=WS_KEEPALIVE_INTERVAL
                )
                message = json.loads(data) if data else {}

                # Responder a pings
                if message.get("type") == "ping":
                    await websocket.send_json({
                        "type": "pong",
                        "timestamp": get_bogota_now().isoformat()
                    })

                # Comando para registrar teléfono activo
                elif message.get("type") == "watching":
                    phone = message.get("phone")
                    if phone:
                        ws_manager.register_phone_owner(phone, advisor_id)
                        logger.debug(f"[WebSocket] Asesor {safe_id(advisor_id, 'advisor')} observando {safe_phone(phone)}")

            except asyncio.TimeoutError:
                # Sin mensajes del cliente → ping proactivo para mantener viva la conexión
                await websocket.send_json({
                    "type": "ping",
                    "timestamp": get_bogota_now().isoformat()
                })

            except json.JSONDecodeError:
                pass

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, advisor_id)
        logger.info(f"[WebSocket] Asesor {safe_id(advisor_id, 'advisor')} desconectado")

    except Exception as e:
        logger.error(f"[WebSocket] Error en conexión de {safe_id(advisor_id, 'advisor')}: {safe_error(e)}")
        ws_manager.disconnect(websocket, advisor_id)
