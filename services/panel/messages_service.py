"""Manual message service for the advisor panel.

Moved verbatim from `middleware.outbound_panel` — no behavior changes.
Rate limiting decorators travel with the endpoints.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, Body, File, Form, Header, Query, Request, UploadFile, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from logging_config import logger
from middleware import outbound_panel as _legacy
from middleware.conversation_state import ConversationStatus
from middleware.outbound_panel import (
    _backfill_im_sid_after_send,
    _ensure_bsuid_hubspot_contact,
    _get_contact_manager,
    _get_redis_client,
    _get_state_manager,
    _log_advisor_message_to_hubspot,
    _resolve_outbound_address,
    _resolve_panel_target,
    check_24h_window,
    get_bogota_now,
    get_mongo_manager,
    limiter,
    twilio_client,
    ws_manager,
)
from utils.reply_quote_formatter import inject_quote
from utils.safe_logging import safe_error, safe_id, safe_phone


class SendMessageRequest(BaseModel):
    """Modelo para envío de mensajes via JSON (para testing E2E)."""
    phone: str = Field(..., description="Número de destino (+573001234567)")
    message: Optional[str] = Field(None, description="Contenido del mensaje de texto")
    body: Optional[str] = Field(None, description="Alias: contenido del mensaje (compatibilidad)")
    contact_id: Optional[str] = Field(None, description="ID del contacto en HubSpot")
    canal: Optional[str] = Field("whatsapp", description="Canal de origen para segregación")
    force_send: bool = Field(False, description="Forzar envío aunque ventana esté cerrada")
    reply_to_id: Optional[str] = Field(None, description="ID del mensaje citado")
    reply_to_preview: Optional[dict] = Field(None, description="Preview del mensaje citado")

    class Config:
        json_schema_extra = {
            "example": {
                "phone": "+573001112235",
                "message": "Hola Carlos, un gusto. Soy el asesor asignado.",
                "canal": "whatsapp"
            }
        }
@limiter.limit("20/minute")
async def send_message(
    request: Request,
    background_tasks: BackgroundTasks,
    to: Optional[str] = Form(None, description="Número de destino (+573001234567)"),
    phone: Optional[str] = Form(None, description="Alias legacy: 'phone' (compatibilidad)"),
    body: Optional[str] = Form(None, description="Contenido del mensaje"),
    contact_id: Optional[str] = Form(None, description="ID del contacto en HubSpot"),
    canal: Optional[str] = Form(None, description="Canal de origen para segregación"),
    force_send: bool = Form(False, description="Forzar envío aunque ventana esté cerrada"),
    media_file: Optional[UploadFile] = File(None, description="Archivo multimedia (imagen/audio)"),
    reply_to_id: Optional[str] = Form(None, description="ID del mensaje citado"),
    reply_to_preview: Optional[str] = Form(None, description="Preview JSON del mensaje citado"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Envía un mensaje de WhatsApp desde el panel de asesores.
    Soporta envío de texto, multimedia (imagen/audio), o ambos.
    """
    # Validar API Key
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida o no configurada")

    # Validar que haya contenido (texto o archivo)
    if not body and not media_file:
        raise HTTPException(status_code=400, detail="Debe enviar un mensaje de texto o un archivo multimedia")

    # Si solo hay texto, validar que no esté vacío
    if body and not body.strip() and not media_file:
        raise HTTPException(status_code=400, detail="El mensaje no puede estar vacío")

    # Normalizar número (aceptar legacy 'phone' form param)
    target_number = to or phone
    if not target_number:
        raise HTTPException(status_code=400, detail="Campo 'to' (o 'phone') es requerido")

    # =========================================================================
    # ASIGNACIÓN DE CANAL POR DEFECTO    
    # =========================================================================
    if not canal or canal.strip() == "" or canal.lower() == "null":
        canal_final = "whatsapp"
    else:
        canal_final = canal.lower().strip()

    target = _resolve_panel_target(target_number)
    if target.error:
        raise HTTPException(status_code=400, detail=target.error)

    phone_normalized = target.key
    if not target.is_bsuid and target_number != phone_normalized:
        logger.info(
            "[Panel][ManualSend] Destino normalizado antes de enviar: %s -> %s",
            safe_phone(target_number),
            safe_phone(phone_normalized),
        )
    outbound_to, outbound_error = await _resolve_outbound_address(target, canal_final)
    if outbound_error:
        raise HTTPException(status_code=400, detail=outbound_error)

    # Parsear reply_to_preview (Form data no soporta objetos anidados)
    parsed_reply_preview = None
    if reply_to_id and reply_to_preview:
        try:
            parsed_reply_preview = json.loads(reply_to_preview)
        except (json.JSONDecodeError, TypeError):
            parsed_reply_preview = None

    # Verificar ventana de 24 horas
    window_status = await check_24h_window(phone_normalized)

    if not window_status.is_open and not force_send:
        return JSONResponse(
            status_code=200,
            content={
                "status": "warning",
                "window_closed": True,
                "message": window_status.message,
                "requires_template": True,
                "hint": "Use force_send=true para enviar de todas formas (requiere Template)"
            }
        )

    # Verificar disponibilidad de Twilio
    if not twilio_client.is_available:
        raise HTTPException(
            status_code=503,
            detail="Twilio no está configurado correctamente"
        )

    # Obtener/crear contacto si no se proporcionó
    if not contact_id and not target.is_bsuid:
        try:
            contact_manager = _get_contact_manager()
            contact_info = await contact_manager.identify_or_create_contact(
                phone_raw=phone_normalized,  # Usar número normalizado, no 'to' que puede ser None
                source_channel="panel_asesor"
            )
            contact_id = contact_info.contact_id
        except Exception as e:
            logger.warning(f"[Panel] No se pudo obtener contacto: {safe_error(e)}")
            # Continuar sin contact_id
    elif not contact_id and target.is_bsuid:
        try:
            contact_id = await asyncio.wait_for(
                _ensure_bsuid_hubspot_contact(target, canal_final),
                timeout=3.0,
            )
        except Exception as e:
            logger.warning(f"[Panel][BSUID] No se pudo asegurar contacto HubSpot: {safe_error(e)}")

    # Pausar Sofía y cambiar a IN_CONVERSATION (asesora está chateando activamente)
    # SEGREGACIÓN POR CANAL: Usar el canal proporcionado para operaciones de estado
    try:
        state_manager = _get_state_manager()

        canal_info = f":{canal_final}"

        # Verificar estado actual (con canal)
        current_status = await state_manager.get_status(phone_normalized, canal_final)

        if current_status in [ConversationStatus.HUMAN_ACTIVE, ConversationStatus.PENDING_HANDOFF]:
            # Ya está en espera, cambiar a IN_CONVERSATION (asesora está atendiendo)
            await state_manager.set_status(
                phone_normalized,
                ConversationStatus.IN_CONVERSATION,
                ttl=state_manager.HUMAN_PANEL_STATE_TTL,
                canal=canal_final
            )
            logger.info(f"[Panel] Estado cambiado a IN_CONVERSATION para {safe_phone(phone_normalized)}:{canal_final}")
        elif current_status == ConversationStatus.IN_CONVERSATION:
            # Ya está en conversación, solo refrescar TTL
            await state_manager.set_status(
                phone_normalized,
                ConversationStatus.IN_CONVERSATION,
                ttl=state_manager.HUMAN_PANEL_STATE_TTL,
                canal=canal_final
            )
            logger.info(f"[Panel] TTL refrescado para IN_CONVERSATION: {safe_phone(phone_normalized)}:{canal_final}")
        else:
            # Era BOT_ACTIVE o CLOSED, activar humano y cambiar a IN_CONVERSATION
            _existing_owner = None
            try:
                _sm_meta = await state_manager.get_meta(phone_normalized, canal_final)
                if _sm_meta:
                    _existing_owner = _sm_meta.assigned_owner_id
            except Exception:
                pass
            await state_manager.activate_human(
                phone_normalized, canal_origen=canal_final, owner_id=_existing_owner
            )
            await state_manager.set_status(
                phone_normalized,
                ConversationStatus.IN_CONVERSATION,
                ttl=state_manager.HUMAN_PANEL_STATE_TTL,
                canal=canal_final
            )
            logger.info(f"[Panel] Sofía pausada y estado IN_CONVERSATION para {safe_phone(phone_normalized)}:{canal_final}")
    except Exception as e:
        logger.warning(f"[Panel] Error manejando estado: {safe_error(e)}")

    # =========================================================================
    # Procesar archivo multimedia si se envió (Bunny.net Storage)
    # =========================================================================
    permanent_media_url = None
    media_type = None
    # Inicializados aquí porque el envío los referencia aunque no haya adjunto.
    file_bytes = None
    content_type = None
    media_subido: Dict[str, Any] = {}

    if media_file and media_file.filename:
        try:
            # Leer contenido del archivo
            file_bytes = await media_file.read()
            content_type = media_file.content_type or "application/octet-stream"

            logger.info(f"[Panel] 📁 Archivo recibido: {media_file.filename}, tipo={content_type}, tamaño={len(file_bytes)} bytes")

            # Validar tamaño máximo para documentos y videos
            if content_type in DOCUMENT_MIME_TYPES and len(file_bytes) > MAX_DOCUMENT_SIZE_BYTES:
                raise HTTPException(status_code=413, detail="El archivo excede el límite de 10MB")
            if content_type.startswith("video/") and len(file_bytes) > MAX_VIDEO_SIZE_BYTES:
                raise HTTPException(status_code=413, detail="El video excede el límite de 16MB")

            # Subir a Bunny.net Storage (CDN).
            # `media_subido` recoge los bytes REALMENTE subidos: el audio se convierte
            # a MP3 aquí dentro, y son esos bytes —no los originales— los que hay que
            # mandar al MCS de Twilio. Con los originales, un WebM del grabador del
            # panel viajaría sin convertir y WhatsApp lo rechazaría con 63021.
            # (ya inicializado arriba)
            permanent_media_url = await media_processor.upload_outgoing_media(
                file_bytes=file_bytes,
                content_type=content_type,
                phone=phone_normalized,
                salida=media_subido,
            )

            logger.info(f"[Panel] Bunny.net URL obtenida: {safe_url(permanent_media_url)}")

            # Determinar tipo de media (incluir webm como audio)
            if content_type in DOCUMENT_MIME_TYPES:
                media_type = "document"
            elif content_type.startswith("video/"):
                media_type = "video"
            elif content_type.startswith("image/"):
                media_type = "image"
            elif content_type.startswith("audio/") or "webm" in content_type.lower():
                media_type = "audio"
            else:
                media_type = "file"

            logger.info(f"[Panel] Multimedia subido a Bunny.net: {media_type} -> {safe_url(permanent_media_url)}")

        except Exception as e:
            logger.error(f"[Panel] Error procesando multimedia: {safe_error(e)}")
            raise HTTPException(
                status_code=500,
                detail=f"Error procesando archivo multimedia: {str(e)}"
            )

    # Preparar body para envío
    message_body = body.strip() if body else ""

    # ── Citación inline (compensa que Twilio Conversations API no soporta ReplyTo) ──
    # Para audio: WhatsApp NO acepta caption; enviamos primero un texto-cita y
    body_for_twilio = message_body
    audio_intro_sid = None
    if parsed_reply_preview:
        if media_type == "audio":
            intro_text = reply_audio_intro(parsed_reply_preview)
            if intro_text:
                intro_result = await twilio_client.send_whatsapp_message(
                    to=outbound_to,
                    body=intro_text,
                )
                if intro_result.get("status") == "success":
                    audio_intro_sid = intro_result.get("message_sid")
                    logger.info(f"[Panel][QuoteInject] Audio intro enviado: {audio_intro_sid}")
                else:
                    logger.warning(
                        f"[Panel][QuoteInject] Falló intro de audio: {intro_result.get('message')}"
                    )
        else:
            is_caption = bool(permanent_media_url)
            body_for_twilio = inject_quote(message_body, parsed_reply_preview, is_caption=is_caption)
            logger.info(f"[Panel][QuoteInject] Quote inyectada (is_caption={is_caption})")

    # Enviar mensaje con multimedia si corresponde.
    # Los bytes van además del media_url: con TWILIO_FORCE_CONVERSATIONS activo se
    # suben al Media Content Service de Twilio, porque Conversations no acepta URLs
    # externas. Bunny sigue siendo la fuente de verdad del historial y del panel.
    result = await twilio_client.send_whatsapp_message(
        to=outbound_to,
        body=body_for_twilio or "📎",  # Twilio requiere body, usar emoji si solo hay media
        media_url=permanent_media_url,
        media_bytes=media_subido.get("bytes") if permanent_media_url else None,
        media_content_type=media_subido.get("content_type") if permanent_media_url else None,
        media_filename=(media_file.filename if media_file else None),
    )

    if result["status"] == "success":
        message_sid = result.get("message_sid")
        sent_via = result.get("via")
        conv_sid_from_send = result.get("conversation_sid")
        im_sid_from_send = result.get("conversations_message_sid")
        chat_svc_sid_from_send = result.get("chat_service_sid")

        # =====================================================================
        # PASO 1: Guardar en MongoDB INMEDIATAMENTE (~5ms)
        # MongoDB es la fuente de verdad para el panel en tiempo real
        # =====================================================================
        mongo_message_id = None
        try:
            mongo_manager = get_mongo_manager()

            # Construir diccionario `media` compatible con MongoDBManager.save_message
            media_dict: Optional[Dict[str, Any]] = None
            if permanent_media_url:
                doc_fmt, doc_icon = DOCUMENT_MIME_TYPES.get(content_type, (None, None)) if media_type == "document" else (None, None)
                media_dict = {
                    "permanent_url": permanent_media_url,
                    "type": media_type,
                    "size_bytes": len(file_bytes) if 'file_bytes' in locals() and file_bytes is not None else None,
                    "format": (content_type.split('/')[-1] if content_type else None),
                    "duration_seconds": None,
                    "processed_at": datetime.utcnow(),
                    "uploaded_by": "advisor",
                    "doc_format": doc_fmt,
                    "doc_icon": doc_icon,
                    "original_filename": media_file.filename if media_file else None,
                }

            mongo_message_id = await mongo_manager.save_message(
                phone=phone_normalized,
                content=message_body or (f"[{media_type.upper()}]" if media_type else message_body),
                sender="advisor",
                channel=canal_final,
                hubspot_contact_id=contact_id,
                message_sid=message_sid,
                metadata={"source": "Manual via Panel", "send_via": sent_via or "legacy"},
                media=media_dict,
                reply_to_id=reply_to_id,
                reply_to_preview=parsed_reply_preview,
                conversation_sid=conv_sid_from_send,
                conversations_message_sid=im_sid_from_send,
                chat_service_sid=chat_svc_sid_from_send,
            )
            if mongo_message_id:
                logger.info(f"[Panel] Mensaje guardado en MongoDB: {mongo_message_id}, media_type={media_type}")
        except Exception as e:
            logger.error(f"[Panel] Error guardando en MongoDB: {safe_error(e)}")
            # No bloquear el flujo si MongoDB falla

        # Backfill solo si NO enviamos vía Conversations (sin IM SID en mano)
        if mongo_message_id and not im_sid_from_send:
            background_tasks.add_task(
                _backfill_im_sid_after_send,
                phone_normalized,
                message_body or "📎",
                str(mongo_message_id),
                None,
            )

        # =====================================================================
        # PASO 2: Registrar en HubSpot Timeline (BACKGROUND - no bloqueante)
        # HubSpot es archivo histórico, no afecta la experiencia del panel
        # =====================================================================
        if contact_id:
            # Construir contenido para HubSpot incluyendo link multimedia si existe
            hubspot_content = message_body
            if permanent_media_url:
                media_label = {"image": "📷 Imagen", "audio": "🎵 Audio", "video": "🎬 Video", "file": "📎 Archivo", "document": "📄 Documento"}.get(media_type, "📎 Archivo")
                hubspot_content = f"{message_body}\n\n{media_label}: {permanent_media_url}" if message_body else f"{media_label}: {permanent_media_url}"

            background_tasks.add_task(
                _log_advisor_message_to_hubspot,
                contact_id,
                hubspot_content,
                phone_normalized,
                "Manual via Panel",
                mongo_message_id  # Para marcar como sincronizado después
            )

        # Actualizar timestamp del asesor para TTL diferenciado
        background_tasks.add_task(
            _update_advisor_timestamp,
            phone_normalized,
            canal_final
        )

        # Mover contacto al top de la lista (actualizar score ZSET + last_activity en meta)
        _rc = None  # P1-B fix: inicializar antes del try para evitar UnboundLocalError si Redis cae
        try:
            _rc = await _get_redis_client()
            _now_ts = datetime.now(timezone.utc).timestamp()
            _now_iso = get_bogota_now().isoformat()

            # FIX: Actualizar last_activity síncronamente ANTES del WS para que loadContacts()
            # reciba el contacto en la posición correcta. El sort de GET /contacts usa last_activity
            # del meta; sin esto, el reorder en memoria del frontend se pierde al llamar loadContacts().
            _meta_key_la = f"conv_meta:{phone_normalized}:{canal_final}"
            _meta_raw_la = await _rc.get(_meta_key_la)
            if _meta_raw_la:
                _meta_obj_la = json.loads(_meta_raw_la)
                _meta_obj_la["last_activity"] = _now_iso
                _meta_ttl_la = await _rc.ttl(_meta_key_la)
                _ex_la = _meta_ttl_la if _meta_ttl_la and _meta_ttl_la > 0 else 7 * 86400
                await _rc.set(_meta_key_la, json.dumps(_meta_obj_la), ex=_ex_la)
                logger.debug(f"[Panel] last_activity actualizado síncronamente para {safe_phone(phone_normalized)}:{canal_final}")

            await _rc.zadd("active_conversations_sorted", {f"{phone_normalized}:{canal_final}": _now_ts})
            logger.info(f"[Panel] ZSET actualizado para {safe_phone(phone_normalized)}:{canal_final}")
        except Exception as _ze:
            logger.warning(f"[Panel] No se pudo actualizar ZSET en send_message: {_ze}")

        # Notificar a todos los asesores via WS para que refresquen la lista
        try:
            if _rc:  # P1-B fix: solo publicar si _rc fue asignado exitosamente
                await ws_manager.publish_broadcast(_rc, {
                    "type": "contact_updated",
                    "phone": phone_normalized,
                    "action": "new_message",
                    "canal": canal_final
                })
        except Exception as _we:
            logger.warning(f"[Panel] Error broadcast WS en send_message: {_we}")

        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message_sid": message_sid,
                "mongo_id": mongo_message_id,
                "to": phone_normalized,
                "contact_id": contact_id,
                "canal": canal_final,
                "window_status": {
                    "is_open": window_status.is_open,
                    "time_remaining": window_status.time_remaining_seconds
                },
                "sofia_paused": True,
                "message_source": "Manual via Panel",
                "media_url": permanent_media_url,
                "media_type": media_type,
                "reply_to_id": reply_to_id,
                "reply_to_preview": parsed_reply_preview
            }
        )
    else:
        twilio_code = result.get("code")
        status_code = 400 if twilio_code in (21211, 21614, 63003) else 500
        if twilio_code in (21211, 21614, 63003):
            detail = (
                "Twilio rechazó el destino normalizado. "
                "Verifica que el número tenga WhatsApp activo y código de país correcto."
            )
        else:
            detail = f"Error enviando mensaje: {result.get('message')}"
        raise HTTPException(
            status_code=status_code,
            detail=detail
        )
EDIT_WINDOW_SECONDS = 15 * 60
DELETE_WINDOW_SECONDS = 60 * 60
def _msg_age_seconds(msg_doc: Dict[str, Any]) -> float:
    ts = msg_doc.get("timestamp_utc") or msg_doc.get("timestamp")
    if not ts:
        return 0.0
    try:
        if ts.tzinfo is None:
            return (datetime.utcnow() - ts).total_seconds()
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return 0.0
@limiter.limit("20/minute")
async def edit_advisor_message(
    request: Request,
    mongo_id: str,
    advisor_id: str = Query(..., description="ID del asesor que solicita editar"),
    new_content: str = Form(..., description="Nuevo contenido del mensaje"),
    notify_client: bool = Form(False, description="Si True, envía mensaje '✏️ Corrección' al cliente"),
):
    """Edita un mensaje del asesor.
    """
    new_content = (new_content or "").strip()
    if not new_content:
        raise HTTPException(status_code=400, detail="Contenido vacío")

    mongo_manager = get_mongo_manager()
    msg = await mongo_manager.get_message_by_id(mongo_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Mensaje no encontrado")

    if msg.get("sender") != "advisor":
        raise HTTPException(status_code=403, detail="Solo se pueden editar mensajes propios del asesor")

    if msg.get("deleted"):
        raise HTTPException(status_code=410, detail="Mensaje ya eliminado")

    age = _msg_age_seconds(msg)
    conv_sid = msg.get("conversation_sid")
    im_sid   = msg.get("conversations_message_sid")
    chat_svc_sid = msg.get("chat_service_sid")

    # Best-effort: sincronizar Twilio Conversations Store (solo dentro de 15min)
    twilio_store_synced = False
    if conv_sid and im_sid and age <= EDIT_WINDOW_SECONDS:
        tw = await twilio_client.update_conversation_message(conv_sid, im_sid, new_content, chat_service_sid=chat_svc_sid)
        twilio_store_synced = tw.get("status") == "success"
        if not twilio_store_synced:
            logger.warning(
                f"[Panel][Edit] Twilio store update fallo conv={conv_sid} im={im_sid}: "
                f"{tw.get('message')} (continúa con edición local)"
            )

    # Persistir edición local (siempre, fuente de verdad del panel)
    updated = await mongo_manager.update_message_content(mongo_id, new_content)
    if not updated:
        raise HTTPException(status_code=500, detail="No se pudo persistir la edición")

    # Modo D: notificar al cliente con mensaje nuevo "✏️ Corrección: ..."
    correction_sid = None
    correction_mongo_id = None
    if notify_client:
        phone = msg.get("phone")
        if not phone:
            logger.warning(f"[Panel][Edit-Notify] msg sin phone, no se puede notificar")
        else:
            correction_body = f"✏️ Corrección: {new_content}"
            target = _resolve_panel_target(phone)
            outbound_to, outbound_error = await _resolve_outbound_address(
                target, msg.get("channel", "whatsapp")
            )
            if outbound_error:
                logger.warning(f"[Panel][Edit-Notify] sin routing válido para {phone}: {outbound_error}")
                outbound_to = None
            if not outbound_to:
                snd = {"status": "error", "message": outbound_error or "destino inválido"}
            else:
                snd = await twilio_client.send_whatsapp_message(
                    to=outbound_to,
                    body=correction_body,
                    conversation_sid=conv_sid,
                    chat_service_sid=chat_svc_sid,
                )
            if snd.get("status") == "success":
                correction_sid = snd.get("message_sid")
                try:
                    correction_mongo_id = await mongo_manager.save_message(
                        phone=phone,
                        content=correction_body,
                        sender="advisor",
                        channel=msg.get("channel", "whatsapp"),
                        hubspot_contact_id=msg.get("hubspot_contact_id"),
                        message_sid=correction_sid,
                        metadata={
                            "source": "Edit Notify",
                            "ref_message_id": str(mongo_id),
                            "send_via": snd.get("via") or "legacy",
                        },
                        conversation_sid=snd.get("conversation_sid"),
                        conversations_message_sid=snd.get("conversations_message_sid"),
                    )
                except Exception as _se:
                    logger.error(f"[Panel][Edit-Notify] save_message fallo: {_se}")
            else:
                logger.warning(
                    f"[Panel][Edit-Notify] envío fallo a {phone}: {snd.get('message')}"
                )

    # Broadcast a otras pestañas / asesores
    try:
        rc = await _get_redis_client()
        await ws_manager.publish_broadcast(rc, {
            "type": "message_edited",
            "message_id": str(updated["_id"]),
            "phone": updated.get("phone", ""),
            "new_content": new_content,
            "notified": bool(notify_client and correction_sid),
            "correction_message_id": correction_mongo_id,
        })
    except Exception as _we:
        logger.warning(f"[Panel] WS broadcast edit fallo: {_we}")

    return JSONResponse(content={
        "status": "success",
        "message_id": str(updated["_id"]),
        "new_content": new_content,
        "twilio_store_synced": twilio_store_synced,
        "notified_client": bool(notify_client and correction_sid),
        "correction_message_id": correction_mongo_id,
    })
@limiter.limit("20/minute")
async def delete_advisor_message(
    request: Request,
    mongo_id: str,
    advisor_id: str = Query(..., description="ID del asesor que solicita eliminar"),
    notify_client: bool = Query(False, description="Si True, envía '🚫 Mensaje anterior anulado' al cliente"),
):
    """Elimina un mensaje del asesor.
    """
    mongo_manager = get_mongo_manager()
    msg = await mongo_manager.get_message_by_id(mongo_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Mensaje no encontrado")

    if msg.get("sender") != "advisor":
        raise HTTPException(status_code=403, detail="Solo se pueden eliminar mensajes propios del asesor")

    if msg.get("deleted"):
        return JSONResponse(content={"status": "success", "already_deleted": True})

    if _msg_age_seconds(msg) > DELETE_WINDOW_SECONDS:
        raise HTTPException(status_code=422, detail="Ventana de eliminación vencida (>60 min)")

    conv_sid = msg.get("conversation_sid")
    im_sid   = msg.get("conversations_message_sid")
    chat_svc_sid = msg.get("chat_service_sid")

    # Best-effort: borrar del Twilio Conversations Store (no afecta WhatsApp del cliente)
    twilio_store_deleted = False
    if conv_sid and im_sid:
        tw = await twilio_client.delete_conversation_message(conv_sid, im_sid, chat_service_sid=chat_svc_sid)
        twilio_store_deleted = (tw.get("status") == "success")
        if not twilio_store_deleted:
            logger.warning(
                f"[Panel][Delete] Twilio store delete fallo conv={conv_sid} im={im_sid}: "
                f"{tw.get('message')} (continúa con soft-delete local)"
            )

    # Soft-delete local (siempre)
    deleted = await mongo_manager.soft_delete_message(mongo_id)
    if not deleted:
        raise HTTPException(status_code=500, detail="No se pudo soft-delete en MongoDB")

    # Modo D: notificar al cliente con mensaje nuevo "🚫 Mensaje anulado"
    cancel_sid = None
    cancel_mongo_id = None
    if notify_client:
        phone = msg.get("phone")
        if not phone:
            logger.warning(f"[Panel][Delete-Notify] msg sin phone, no se puede notificar")
        else:
            cancel_body = "🚫 Mensaje anterior anulado"
            target = _resolve_panel_target(phone)
            outbound_to, outbound_error = await _resolve_outbound_address(
                target, msg.get("channel", "whatsapp")
            )
            if outbound_error:
                logger.warning(f"[Panel][Delete-Notify] sin routing válido para {phone}: {outbound_error}")
                outbound_to = None
            if not outbound_to:
                snd = {"status": "error", "message": outbound_error or "destino inválido"}
            else:
                snd = await twilio_client.send_whatsapp_message(
                    to=outbound_to,
                    body=cancel_body,
                    conversation_sid=conv_sid,
                    chat_service_sid=chat_svc_sid,
                )
            if snd.get("status") == "success":
                cancel_sid = snd.get("message_sid")
                try:
                    cancel_mongo_id = await mongo_manager.save_message(
                        phone=phone,
                        content=cancel_body,
                        sender="advisor",
                        channel=msg.get("channel", "whatsapp"),
                        hubspot_contact_id=msg.get("hubspot_contact_id"),
                        message_sid=cancel_sid,
                        metadata={
                            "source": "Delete Notify",
                            "ref_message_id": str(mongo_id),
                            "send_via": snd.get("via") or "legacy",
                        },
                        conversation_sid=snd.get("conversation_sid"),
                        conversations_message_sid=snd.get("conversations_message_sid"),
                    )
                except Exception as _se:
                    logger.error(f"[Panel][Delete-Notify] save_message fallo: {_se}")
            else:
                logger.warning(
                    f"[Panel][Delete-Notify] envío fallo a {phone}: {snd.get('message')}"
                )

    try:
        rc = await _get_redis_client()
        await ws_manager.publish_broadcast(rc, {
            "type": "message_deleted",
            "message_id": str(deleted["_id"]),
            "phone": deleted.get("phone", ""),
            "notified": bool(notify_client and cancel_sid),
            "cancel_message_id": cancel_mongo_id,
        })
    except Exception as _we:
        logger.warning(f"[Panel] WS broadcast delete fallo: {_we}")

    return JSONResponse(content={
        "status": "success",
        "message_id": str(deleted["_id"]),
        "twilio_store_deleted": twilio_store_deleted,
        "notified_client": bool(notify_client and cancel_sid),
        "cancel_message_id": cancel_mongo_id,
    })
@limiter.limit("20/minute")
async def send_message_json(
    request: Request,
    background_tasks: BackgroundTasks,
    msg_request: SendMessageRequest = Body(...),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Envía un mensaje de WhatsApp desde el panel (formato JSON).
    """
    # Validar API Key
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida o no configurada")

    # Obtener mensaje (soportar 'message' o 'body')
    body = msg_request.message or msg_request.body
    
    # Validar que haya contenido
    if not body or not body.strip():
        raise HTTPException(status_code=400, detail="El mensaje no puede estar vacío")

    # Normalizar canal
    canal_final = msg_request.canal.lower().strip() if msg_request.canal else "whatsapp"
    if canal_final == "null" or not canal_final:
        canal_final = "whatsapp"

    target = _resolve_panel_target(msg_request.phone)
    if target.error:
        raise HTTPException(status_code=400, detail=target.error)

    phone_normalized = target.key
    outbound_to, outbound_error = await _resolve_outbound_address(target, canal_final)
    if outbound_error:
        raise HTTPException(status_code=400, detail=outbound_error)

    # Verificar ventana de 24 horas
    window_status = await check_24h_window(phone_normalized)

    if not window_status.is_open and not msg_request.force_send:
        return JSONResponse(
            status_code=200,
            content={
                "status": "warning",
                "window_closed": True,
                "message": window_status.message,
                "requires_template": True,
                "hint": "Use force_send=true para enviar de todas formas (requiere Template)"
            }
        )

    # Verificar disponibilidad de Twilio
    if not twilio_client.is_available:
        raise HTTPException(
            status_code=503,
            detail="Twilio no está configurado correctamente"
        )

    # Obtener/crear contacto si no se proporcionó
    contact_id = msg_request.contact_id
    if not contact_id and not target.is_bsuid:
        try:
            contact_manager = _get_contact_manager()
            contact_info = await contact_manager.identify_or_create_contact(
                phone_raw=phone_normalized,
                source_channel="panel_asesor"
            )
            contact_id = contact_info.contact_id
        except Exception as e:
            logger.warning(f"[Panel-JSON] No se pudo obtener contacto: {safe_error(e)}")
    elif not contact_id and target.is_bsuid:
        try:
            contact_id = await asyncio.wait_for(
                _ensure_bsuid_hubspot_contact(target, canal_final),
                timeout=3.0,
            )
        except Exception as e:
            logger.warning(f"[Panel-JSON][BSUID] No se pudo asegurar contacto HubSpot: {safe_error(e)}")

    # Pausar Sofía y cambiar estado
    try:
        state_manager = _get_state_manager()

        current_status = await state_manager.get_status(phone_normalized, canal_final)

        if current_status in [ConversationStatus.HUMAN_ACTIVE, ConversationStatus.PENDING_HANDOFF, ConversationStatus.IN_CONVERSATION]:
            await state_manager.set_status(
                phone_normalized,
                ConversationStatus.IN_CONVERSATION,
                ttl=state_manager.HUMAN_PANEL_STATE_TTL,
                canal=canal_final
            )
            logger.info(f"[Panel-JSON] Estado: IN_CONVERSATION para {safe_phone(phone_normalized)}:{canal_final}")
        else:
            # Crear nuevo estado para la conversación
            await state_manager.set_status(
                phone_normalized,
                ConversationStatus.IN_CONVERSATION,
                ttl=state_manager.HUMAN_PANEL_STATE_TTL,
                canal=canal_final
            )
            logger.info(f"[Panel-JSON] Nuevo estado IN_CONVERSATION para {safe_phone(phone_normalized)}:{canal_final}")

    except Exception as e:
        logger.error(f"[Panel-JSON] Error actualizando estado: {safe_error(e)}")

    # ── Citación inline: prepend quote SOLO para Twilio. Mongo guarda body original. ──
    body_to_send = body
    if msg_request.reply_to_id and msg_request.reply_to_preview:
        body_to_send = inject_quote(body, msg_request.reply_to_preview, is_caption=False)
        logger.info(f"[Panel-JSON][QuoteInject] Quote inyectada en body (solo Twilio)")

    # Enviar mensaje vía Twilio
    result = await twilio_client.send_whatsapp_message(
        to=outbound_to,
        body=body_to_send
    )

    if result["status"] == "success":
        message_sid = result.get("message_sid")
        sent_via = result.get("via")
        conv_sid_from_send = result.get("conversation_sid")
        im_sid_from_send = result.get("conversations_message_sid")
        chat_svc_sid_from_send = result.get("chat_service_sid")
        logger.info(f"[Panel-JSON] ✅ Mensaje enviado: {safe_id(message_sid, 'message')} a {safe_phone(phone_normalized)} (via={sent_via or 'legacy'})")

        # Guardar en MongoDB
        mongo_message_id = None
        try:
            mongo_manager = get_mongo_manager()
            mongo_message_id = await mongo_manager.save_message(
                phone=phone_normalized,
                content=body,
                sender="advisor",
                channel=canal_final,
                hubspot_contact_id=contact_id,
                message_sid=message_sid,
                metadata={"source": "Panel JSON API", "send_via": sent_via or "legacy"},
                reply_to_id=msg_request.reply_to_id,
                reply_to_preview=msg_request.reply_to_preview,
                conversation_sid=conv_sid_from_send,
                conversations_message_sid=im_sid_from_send,
                chat_service_sid=chat_svc_sid_from_send,
            )
        except Exception as e:
            logger.error(f"[Panel-JSON] Error guardando en MongoDB: {safe_error(e)}")

        if mongo_message_id and not im_sid_from_send:
            background_tasks.add_task(
                _backfill_im_sid_after_send,
                phone_normalized,
                body,
                str(mongo_message_id),
                None,
            )

        # Registrar en HubSpot Timeline
        if contact_id:
            background_tasks.add_task(
                _log_advisor_message_to_hubspot,
                contact_id,
                body,
                phone_normalized,
                "Panel JSON API",
                mongo_message_id
            )

        # Mover contacto al top de la lista (actualizar score ZSET + last_activity en meta)
        _rc = None  # P1-B fix: inicializar antes del try para evitar UnboundLocalError si Redis cae
        try:
            _rc = await _get_redis_client()
            _now_ts = datetime.now(timezone.utc).timestamp()
            _now_iso = get_bogota_now().isoformat()

            # FIX: Actualizar last_activity síncronamente ANTES del WS para que loadContacts()
            # reciba el contacto en la posición correcta. El sort de GET /contacts usa last_activity
            # del meta; sin esto, el reorder en memoria del frontend se pierde al llamar loadContacts().
            _meta_key_la = f"conv_meta:{phone_normalized}:{canal_final}"
            _meta_raw_la = await _rc.get(_meta_key_la)
            if _meta_raw_la:
                _meta_obj_la = json.loads(_meta_raw_la)
                _meta_obj_la["last_activity"] = _now_iso
                _meta_ttl_la = await _rc.ttl(_meta_key_la)
                _ex_la = _meta_ttl_la if _meta_ttl_la and _meta_ttl_la > 0 else 7 * 86400
                await _rc.set(_meta_key_la, json.dumps(_meta_obj_la), ex=_ex_la)
                logger.debug(f"[Panel-JSON] last_activity actualizado síncronamente para {safe_phone(phone_normalized)}:{canal_final}")

            await _rc.zadd("active_conversations_sorted", {f"{phone_normalized}:{canal_final}": _now_ts})
            logger.info(f"[Panel-JSON] ZSET actualizado para {safe_phone(phone_normalized)}:{canal_final}")
        except Exception as _ze:
            logger.warning(f"[Panel-JSON] No se pudo actualizar ZSET: {_ze}")

        # Notificar a todos los asesores via WS
        try:
            if _rc:  # P1-B fix: solo publicar si _rc fue asignado exitosamente
                await ws_manager.publish_broadcast(_rc, {
                    "type": "contact_updated",
                    "phone": phone_normalized,
                    "action": "new_message",
                    "canal": canal_final
                })
        except Exception as _we:
            logger.warning(f"[Panel-JSON] Error broadcast WS: {_we}")

        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message_sid": message_sid,
                "mongo_id": mongo_message_id,
                "to": phone_normalized,
                "contact_id": contact_id,
                "canal": canal_final,
                "window_status": {
                    "is_open": window_status.is_open,
                    "time_remaining": window_status.time_remaining_seconds
                },
                "sofia_paused": True,
                "message_source": "Panel JSON API"
            }
        )
    else:
        raise HTTPException(
            status_code=500,
            detail=f"Error enviando mensaje: {result.get('message')}"
        )
