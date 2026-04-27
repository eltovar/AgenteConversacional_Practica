# middleware/webhook_handler.py
"""
Este es el punto de entrada principal del middleware.
Recibe mensajes de Twilio, los procesa con Sofía y responde.

Flujo:
1. Recibe mensaje de Twilio (POST /whatsapp/webhook)
2. Normaliza número telefónico
3. Consulta estado en Redis (BOT_ACTIVE / HUMAN_ACTIVE)
4. Si BOT_ACTIVE → Procesa con Sofía
5. Si HUMAN_ACTIVE → Espejea a HubSpot sin responder
6. Registra en HubSpot y responde via Twilio
"""

import os
import json
from typing import Any, Optional
from datetime import datetime

from fastapi import APIRouter, Form, Request, BackgroundTasks
from fastapi.responses import Response
from twilio.twiml.messaging_response import MessagingResponse

from logging_config import logger
from .phone_normalizer import PhoneNormalizer, normalize_colombian_phone
from .conversation_state import ConversationStateManager, ConversationStatus
from .contact_manager import ContactManager
from .sofia_brain import SofiaBrain
from .websocket_manager import ws_manager

# Importaciones para integración con HubSpot Timeline
from integrations.hubspot import get_timeline_logger, hubspot_client

# MongoDB para almacenamiento en tiempo real
from database.mongodb_client import get_mongo_manager

# Procesador de multimedia (Bunny.net CDN + Whisper)
from utils.media_processor import media_processor

# Importación para actualizar ventana de 24h
from .outbound_panel import update_last_client_message

# Detector de códigos de inmuebles
from utils.property_code_detector import detect_property_code

# Detector de links de portales y redes sociales
from utils.link_detector import LinkDetector, PortalOrigen

# Módulo de horarios laborales
from utils.business_hours import (
    is_business_hours,
    get_out_of_hours_message,
    should_add_out_of_hours_message
)

# Cliente Twilio para respuestas diferidas (evita timeout de 15 segundos)
from utils.twilio_client import twilio_client

# Agregador de mensajes para esperar múltiples mensajes antes de responder
from utils.message_aggregator import message_aggregator

# Gestor de citas (para confirmar automáticamente cuando el cliente responde)
from .appointment_manager import get_appointment_manager


# ─────────────────────────────────────────────────────────────────────────────
# CONVERSATIONS API — Feature flag
# Cuando TWILIO_CONVERSATIONS_ENABLED=true el webhook parsea JSON (Conversations).
# Cuando es false (default) sigue usando form-encoded (Programmable Messaging).
# Activar SOLO después de mover el número al Messaging Service en Twilio Console.
# ─────────────────────────────────────────────────────────────────────────────
CONVERSATIONS_ENABLED = os.getenv("TWILIO_CONVERSATIONS_ENABLED", "false").lower() == "true"


# ═══════════════════════════════════════════════════════════════════════════════
# RESPUESTA DIFERIDA PARA EVITAR TIMEOUT DE TWILIO (15 segundos)
# ══════════════════════════════════════════════════════════════════════════════
# Twilio cierra la conexión del webhook después de 15 segundos y envía un retry.
# El retry causa errores 422 porque el body ya fue consumido en la primera request.
# SOLUCIÓN: Responder inmediatamente con 200 OK (TwiML vacío) y procesar
# el mensaje en background, enviando la respuesta de Sofia via REST API.
# ═══════════════════════════════════════════════════════════════════════════════


async def _get_historical_channel(
    phone_normalized: str, 
    detected_channel: str,
    hubspot_client=None
) -> str:
    """
    Obtiene el canal histórico del contacto (Redis → HubSpot → detectado actual).
    
    ORDEN DE PRIORIDAD:
    1. Si el contacto ya existe en Redis: usar su canal_origen persistido
    2. Si existe en HubSpot: usar su canal_origen de la propiedad
    3. Si es contacto NUEVO: usar el canal detectado del mensaje actual
    
    Esto asegura que un lead de metrocuadrado siempre vaya al asesor de portales,
    incluso si sus mensajes posteriores no tienen links.
    """
    state_manager = get_state_manager()
    
    # ── 1. Verificar Redis (todos los canales posibles) ──
    for canal in CANALES_A_VERIFICAR:
        meta = await state_manager.get_meta(phone_normalized, canal)
        if meta and meta.canal_origen and meta.canal_origen != "whatsapp":
            logger.info(
                f"[ChannelHistory] Canal histórico de Redis: {meta.canal_origen} "
                f"(teléfono: {phone_normalized})"
            )
            return meta.canal_origen
        elif meta and meta.canal_origen:
            # Tiene meta con canal whatsapp, pero seguir buscando por si hay otro
            continue
    
    # ── 2. Verificar HubSpot (si hay cliente disponible) ──
    if hubspot_client:
        try:
            contact_result = await hubspot_client.search_contact_by_phone_with_properties(phone_normalized)
            if contact_result:
                props = contact_result.get("properties", {})
                hs_canal = props.get("canal_origen", "").lower().strip()
                if hs_canal and hs_canal not in ("whatsapp", "whatsapp_directo", ""):
                    logger.info(
                        f"[ChannelHistory] Canal histórico de HubSpot: {hs_canal} "
                        f"(teléfono: {phone_normalized})"
                    )
                    return hs_canal
        except Exception as e:
            logger.debug(f"[ChannelHistory] Error consultando HubSpot: {e}")
    
    # ── 3. Contacto nuevo: usar canal detectado del mensaje actual ──
    if detected_channel and detected_channel != "whatsapp":
        logger.info(
            f"[ChannelHistory] Contacto nuevo con canal detectado: {detected_channel} "
            f"(teléfono: {phone_normalized})"
        )
    return detected_channel


async def _fetch_reply_context_deferred(
    im_sid: str,
    conversation_sid: str,
    chat_service_sid: Optional[str],
    mongo_message_sid: str,
    phone: str,
    advisor_id: Optional[str],
    delay: float = 3.0,
) -> None:
    """Fetch diferido de reply context desde Twilio Conversations REST API.

    Twilio resuelve el WAMid → IM SID de forma asíncrona post-entrega.
    Esperamos `delay` segundos y consultamos el mensaje directamente;
    si Attributes tiene originalRepliedMessageSid, actualizamos Mongo
    y emitimos message_updated por WebSocket.
    """
    import asyncio
    await asyncio.sleep(delay)
    try:
        result = await twilio_client.get_conversation_message(
            conversation_sid=conversation_sid,
            message_sid=im_sid,
            chat_service_sid=chat_service_sid,
        )
        if result.get("status") != "success":
            logger.debug(f"[ReplyFetch] Fetch fallo: {result.get('message')}")
            return

        msg_data  = result["message"]
        attrs_raw = msg_data.get("attributes") or "{}"
        try:
            attrs = attrs_raw if isinstance(attrs_raw, dict) else json.loads(attrs_raw)
        except Exception:
            attrs = {}

        # Intentar extraer IM SID del mensaje citado desde Attributes
        replied_sid = (
            attrs.get("originalRepliedMessageSid")
            or attrs.get("original_replied_message_sid")
            or (attrs.get("replyTo") or {}).get("messageSid")
            or (attrs.get("replyTo") or {}).get("sid")
            or (attrs.get("reply_to") or {}).get("message_sid")
        )

        if not replied_sid:
            logger.debug(f"[ReplyFetch] Attributes sin reply context para {im_sid}")
            return

        mongo_manager = get_mongo_manager()
        replied_msg = await mongo_manager.get_message_by_sid(replied_sid)
        if not replied_msg:
            logger.warning(f"[ReplyFetch] SID citado {replied_sid} no en Mongo")
            return

        reply_to_id      = replied_msg["id"]
        reply_to_preview = {
            "sender":      replied_msg["sender"],
            "sender_name": replied_msg["sender_name"],
            "content":     replied_msg["content"],
            "media_type":  replied_msg["media_type"],
            "timestamp":   replied_msg["timestamp"],
        }

        updated = await mongo_manager.update_message_reply_context(
            message_sid=mongo_message_sid,
            reply_to_id=reply_to_id,
            reply_to_preview=reply_to_preview,
        )
        if not updated:
            logger.warning(f"[ReplyFetch] Mongo no actualizó {mongo_message_sid}")
            return

        logger.info(
            f"[ReplyFetch] ✅ Reply context resuelto: {im_sid} cita {replied_sid} → mongo:{reply_to_id}"
        )

        # Notificar al panel via WebSocket para que refresque el mensaje
        try:
            import redis.asyncio as aioredis
            redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
            rc = aioredis.from_url(redis_url, decode_responses=True)
            payload = {
                "type":       "message_updated",
                "phone":      phone,
                "message_id": mongo_message_sid,
                "reply_to_id":      reply_to_id,
                "reply_to_preview": reply_to_preview,
            }
            if advisor_id:
                await ws_manager.publish_to_advisor(rc, advisor_id, payload)
            else:
                await ws_manager.publish_broadcast(rc, payload)
            await rc.aclose()
        except Exception as ws_err:
            logger.warning(f"[ReplyFetch] WS notify fallo: {ws_err}")

    except Exception as e:
        logger.warning(f"[ReplyFetch] Error inesperado para {im_sid}: {e}")


async def _process_message_deferred(
    phone_normalized: str,
    phone_raw: str,
    body: str,
    profile_name: Optional[str],
    message_sid: Optional[str],
    num_media: int,
    media_url: Optional[str],
    media_content_type: Optional[str],
    early_channel: str,
    original_replied_message_sid: Optional[str] = None,
    incoming_channel: Optional[str] = None,
    conversation_sid: Optional[str] = None,
    chat_service_sid: Optional[str] = None,
):
    """
    Procesa un mensaje de WhatsApp de forma diferida (background task).

    Esta función contiene toda la lógica pesada:
    - Procesamiento de multimedia
    - Identificación/creación de contacto en HubSpot
    - Verificación de estado de conversación
    - Procesamiento con Sofia
    - Envío de respuesta via Twilio REST API
    """
    try:
        logger.info(f"[DeferredProcess] Iniciando procesamiento diferido para {phone_normalized}")

        # ════════════════════════════════════════════════════════════
        # IDEMPOTENCIA: Prevenir doble procesamiento por Twilio retry
        # P1-A FIX: Se separa en dos fases:
        #   FASE 1 (aquí): solo CHECK — si la key ya existe, saltar
        #   FASE 2 (post-save): ESCRITURA — solo después de guardar en MongoDB
        # Razón: escribir aquí + morir antes del save = retry descartado = pérdida permanente
        # ════════════════════════════════════════════════════════════
        _idem_key = f"msg_processed:{message_sid}" if message_sid else None
        _redis_idem = get_state_manager().redis
        if _idem_key:
            _already_processed = await _redis_idem.get(_idem_key)
            if _already_processed:
                logger.info(f"[DeferredProcess] Mensaje {message_sid} ya procesado — skip (Twilio retry)")
                return

        # ════════════════════════════════════════════════════════════
        # PASO 1: Procesamiento de Multimedia (si existe)
        # ════════════════════════════════════════════════════════════
        media_result = None
        processed_body = body
        
        if num_media > 0 and media_url:
            logger.info(f"[DeferredProcess] Procesando {num_media} archivo(s) multimedia")
            try:
                media_result = await media_processor.process_incoming_media(
                    media_url=media_url,
                    content_type=media_content_type or "application/octet-stream",
                    phone=phone_normalized
                )
                
                if media_result.get("transcription"):
                    processed_body = media_result.get("body_for_ai", body)
                elif media_result.get("analysis"):
                    if body:
                        processed_body = f"{body}\n\n{media_result.get('body_for_ai', '')}"
                    else:
                        processed_body = media_result.get("body_for_ai", "[Imagen recibida]")
                elif media_result.get("media_type") == "document":
                    processed_body = media_result.get("body_for_ai", "[El cliente envió un documento]")

            except Exception as e:
                logger.error(f"[DeferredProcess] Error procesando multimedia: {e}")
                processed_body = body or "[El cliente envió un archivo]"
        
        # ════════════════════════════════════════════════════════════
        # PASO 2: Obtener canal histórico y crear/identificar contacto
        # ════════════════════════════════════════════════════════════
        contact_manager = get_contact_manager()
        contact_info = None
        
        # ✅ FIX: Obtener canal histórico ANTES de crear el contacto
        # Si el contacto ya existe, usa su canal persistido (portales, redes sociales)
        # Si es nuevo y el mensaje tiene link, usa ese canal
        # Esto asegura asignación correcta al asesor correspondiente
        historical_channel = await _get_historical_channel(
            phone_normalized=phone_normalized,
            detected_channel=early_channel,
            hubspot_client=contact_manager.hubspot if contact_manager else None
        )
        
        logger.info(
            f"[DeferredProcess] Canal: detectado={early_channel}, "
            f"histórico={historical_channel}"
        )
        
        try:
            contact_info = await contact_manager.identify_or_create_contact(
                phone_raw=phone_raw,
                source_channel=historical_channel,  # ← Usar canal histórico
                create_deal=False
            )
            contact_id = contact_info.contact_id if contact_info else None
            _contact_is_new = contact_info.is_new if contact_info else False
            logger.info(f"[DeferredProcess] Contacto: {contact_id} (nuevo={_contact_is_new})")
        except Exception as e:
            logger.error(f"[DeferredProcess] Error con HubSpot: {e}")
            contact_id = None
            _contact_is_new = False
        
        # ════════════════════════════════════════════════════════════
        # PASO 3: Verificar si Sofía debe responder
        # ════════════════════════════════════════════════════════════
        should_respond, reason, special_message, redis_channel = await should_bot_respond(
            phone_normalized=phone_normalized,
            contact_id=contact_id
        )
        
        # Determinar final_channel: si el mensaje llegó DIRECTAMENTE por WhatsApp
        # (prefijo 'whatsapp:' en el campo From de Twilio), forzar 'whatsapp' para
        # que el panel pueda leer los mensajes (MongoDB filtra por channel=whatsapp).
        # Sin este guard, contactos con historial de otro canal (instagram, finca_raiz, etc.)
        # tendrían sus mensajes de WhatsApp almacenados con channel='instagram', haciendo
        # que el panel nunca los muestre al cargar el historial con canal='whatsapp'.
        _real_incoming = incoming_channel or early_channel
        if _real_incoming == "whatsapp":
            final_channel = "whatsapp"
        else:
            final_channel = historical_channel if historical_channel != "whatsapp" else detect_channel_dynamic(processed_body, redis_channel)
        
        # Guardar mensaje en MongoDB SIEMPRE (para el panel)
        mongo_manager = get_mongo_manager()
        media_dict = None
        if media_result and media_result.get("permanent_url"):
            media_dict = {
                "permanent_url": media_result.get("permanent_url"),
                "type": media_result.get("media_type"),
                "transcription": media_result.get("transcription"),
                "analysis": media_result.get("analysis"),
                "doc_format": media_result.get("doc_format") or None,
                "doc_icon": media_result.get("doc_icon") or None,
                "original_filename": media_result.get("original_filename") or None,
            }

        # Resolver reply context si el cliente citó un mensaje en WhatsApp
        logger.info(f"[DeferredProcess][Reply] original_replied_message_sid={original_replied_message_sid}")
        reply_to_id = None
        reply_to_preview = None
        if original_replied_message_sid:
            try:
                replied_msg = await mongo_manager.get_message_by_sid(original_replied_message_sid)
                if replied_msg:
                    reply_to_id = replied_msg["id"]
                    reply_to_preview = {
                        "sender": replied_msg["sender"],
                        "sender_name": replied_msg["sender_name"],
                        "content": replied_msg["content"],
                        "media_type": replied_msg["media_type"],
                        "timestamp": replied_msg["timestamp"]
                    }
                    logger.info(f"[DeferredProcess] Reply context resuelto: {original_replied_message_sid} → mongo:{reply_to_id}")
                else:
                    logger.warning(f"[DeferredProcess] Reply SID no encontrado en MongoDB: {original_replied_message_sid} (mensaje expirado o pre-sistema)")
            except Exception as e:
                logger.warning(f"[DeferredProcess] Error resolviendo reply context: {e}")

        client_mongo_id = await mongo_manager.save_message(
            phone=phone_normalized,
            content=processed_body,
            sender="client",
            channel=final_channel,
            hubspot_contact_id=contact_id,
            message_sid=message_sid,
            media=media_dict,
            reply_to_id=reply_to_id,
            reply_to_preview=reply_to_preview,
            conversation_sid=conversation_sid,
            chat_service_sid=chat_service_sid,
        )
        logger.info(f"[DeferredProcess] Mensaje guardado en MongoDB: {client_mongo_id}")

        # Fetch diferido de reply context: si el mensaje tiene conv_sid pero
        # aún no resolvimos reply_to_id, consultamos Twilio API en background
        # tras 3 segundos (tiempo para que Twilio resuelva WAMid → IM SID async).
        if conversation_sid and message_sid and not reply_to_id:
            import asyncio
            asyncio.create_task(_fetch_reply_context_deferred(
                im_sid=message_sid,
                conversation_sid=conversation_sid,
                chat_service_sid=chat_service_sid,
                mongo_message_sid=message_sid,
                phone=phone_normalized,
                advisor_id=None,  # broadcast; el panel filtra por phone
                delay=3.0,
            ))

        # P1-A FIX FASE 2: Escribir key de idempotencia DESPUÉS del save exitoso.
        # Si el worker muere aquí, el retry de Twilio procesará el mensaje de nuevo
        # (tolerable: save_message es idempotente por message_sid en MongoDB).
        if _idem_key and client_mongo_id:
            try:
                await _redis_idem.set(_idem_key, "1", ex=300)  # TTL 5 min
            except Exception:
                pass  # No crítico — en el peor caso hay doble procesamiento tolerable

        # Nota automática en HubSpot cuando el cliente envía un documento
        if (
            media_result
            and media_result.get("media_type") == "document"
            and media_result.get("permanent_url")
            and contact_id
        ):
            try:
                doc_name = media_result.get("original_filename", "documento")
                doc_url = media_result.get("permanent_url")
                note_body = f"📄 Documento recibido: {doc_name} - {doc_url}"
                await hubspot_client.create_note(contact_id=contact_id, body=note_body)
                logger.info(f"[HubSpot] Nota de documento creada para contacto {contact_id}")
            except Exception as e:
                logger.warning(f"[HubSpot] Error creando nota de documento: {e}")

        # Fix CR-1: actualizar ZSET antes de notificar via WS.
        # Garantiza que el contacto sea visible en GET /contacts cuando la asesora recibe el ping.
        # Se llama get_state_manager() inline (no se asigna a state_manager) para evitar
        # UnboundLocalError: state_manager se asigna localmente en línea ~430, por lo que
        # Python lo marca como variable local en todo el scope — no puede referenciarse antes.
        # canal=final_channel: evita que contactos no-whatsapp generen ZSET member duplicado
        # (phone:whatsapp vs phone:instagram) cuando update_activity usa el default "whatsapp".
        await get_state_manager().update_activity(phone_normalized, canal=final_channel or "whatsapp")

        # Inbox de no-leídos: registrar actividad para el asesor asignado.
        # Se ejecuta ANTES del WS notify para que GET /contacts ya devuelva has_unread=True
        # cuando el panel llama tras recibir el ping.
        _inbox_meta = None  # Fix-A: inicializar fuera del try para usar en notify_new_message
        try:
            _inbox_meta = await get_state_manager().get_meta(
                phone_normalized, final_channel or "whatsapp"
            )
            # FIX: Para contactos nuevos el temp_meta no tiene assigned_owner_id aún.
            # Usamos hubspot_owner_id de contact_info como fallback para garantizar inbox write
            # y WS enrutado antes de que ensure_meta_with_channel corra (línea ~589).
            _hs_owner_early = (
                contact_info.properties.get("hubspot_owner_id")
                if contact_info and contact_info.properties else None
            )
            _assigned_owner_def = (
                (_inbox_meta.assigned_owner_id if _inbox_meta else None) or _hs_owner_early
            )
            if _assigned_owner_def:
                await get_state_manager().add_to_advisor_inbox(
                    advisor_id=_assigned_owner_def,
                    phone=phone_normalized,
                    canal=final_channel or "whatsapp",
                )
        except Exception as _inbox_err:
            logger.warning(f"[Inbox][Webhook] Error escribiendo inbox (non-fatal): {_inbox_err}")
            _assigned_owner_def = None

        # Notificar al panel vía WebSocket (cross-worker via Redis Pub/Sub)
        # Fix-A: pasar assigned_owner_id para enrutamiento targeted (evita broadcast global)
        # _assigned_owner_def ya fue calculado en el bloque anterior (con fallback hubspot_owner)
        await ws_manager.notify_new_message(
            phone=phone_normalized,
            canal=final_channel or "whatsapp",
            message_preview=processed_body[:100] if processed_body else "",
            sender="client",
            contact_name="",
            redis_client=get_state_manager().redis,
            assigned_owner_id=_assigned_owner_def,
            state_manager=get_state_manager(),
        )

        # ════════════════════════════════════════════════════════════
        # AGREGACIÓN DE MENSAJES: Esperar que el cliente termine de escribir
        # ════════════════════════════════════════════════════════════
        # Agregar mensaje al buffer del agregador
        # Si hay otro mensaje siendo procesado, este se agrega al buffer y termina
        # El proceso principal espera ~30s y procesa todos los mensajes juntos
        aggregation_result = await message_aggregator.add_message_to_buffer(
            session_id=phone_normalized,
            message=processed_body
        )
        
        if not aggregation_result.get("should_process", True):
            # Ya hay un proceso esperando - este mensaje se agregó al buffer
            # Sincronizar con HubSpot y salir (el proceso principal responderá)
            logger.info(
                f"[DeferredProcess] Mensaje agregado al buffer (total: {aggregation_result.get('buffer_count', 1)}). "
                f"Otro proceso responderá."
            )
            
            # Sincronizar con HubSpot incluso si no procesamos con Sofia
            if contact_info:
                await _sync_message_to_hubspot(
                    contact_info.contact_id,
                    processed_body,
                    "incoming",
                    phone_normalized,
                    final_channel,
                    media_result,
                    message_sid
                )
            return
        
        # Este proceso es el principal - esperar a que lleguen más mensajes
        if aggregation_result.get("is_aggregating", False):
            logger.info(
                f"[DeferredProcess] Proceso principal. Esperando mensajes adicionales..."
            )
            # Esperar y obtener mensajes combinados
            combined_message = await message_aggregator.wait_and_get_combined_message(phone_normalized)
            
            if combined_message:
                logger.info(
                    f"[DeferredProcess] Mensajes combinados: '{combined_message[:100]}...'"
                )
                processed_body = combined_message
        
        if not should_respond:
            logger.info(f"[DeferredProcess] Bot silenciado ({reason})")
            
            # Sincronizar con HubSpot si hay contacto
            if contact_info:
                await _sync_message_to_hubspot(
                    contact_info.contact_id,
                    processed_body,
                    "incoming",
                    phone_normalized,
                    final_channel,
                    media_result,
                    message_sid
                )
            
            # Enviar mensaje especial si existe (ej: PENDING_HANDOFF)
            if special_message:
                result = await twilio_client.send_whatsapp_message(
                    to=phone_normalized,
                    body=special_message
                )
                logger.info(f"[DeferredProcess] Mensaje especial enviado: {result}")
            
            return
        
        # ════════════════════════════════════════════════════════════
        # PASO 4: Procesar con Sofia y enviar respuesta
        # ════════════════════════════════════════════════════════════
        logger.info(f"[DeferredProcess] Procesando con Sofia...")
        
        sofia = get_sofia_brain()
        
        # Detectar código de inmueble o link de red social
        property_code_result = detect_property_code(processed_body)
        link_detector = get_link_detector()
        link_result = link_detector.analizar_mensaje(processed_body)
        
        # Construir lead_context con propiedades del contacto de HubSpot
        lead_context = {}
        
        # Incluir propiedades del contacto si existen
        if contact_info:
            if contact_info.firstname:
                lead_context["firstname"] = contact_info.firstname
            if contact_info.lastname:
                lead_context["lastname"] = contact_info.lastname
            if contact_info.email:
                lead_context["email"] = contact_info.email
            
            # Incluir propiedades chatbot de HubSpot
            if contact_info.properties:
                props = contact_info.properties
                if props.get("chatbot_property_type"):
                    lead_context["chatbot_property_type"] = props["chatbot_property_type"]
                if props.get("chatbot_operation_type"):
                    lead_context["chatbot_operation_type"] = props["chatbot_operation_type"]
                if props.get("chatbot_location"):
                    lead_context["chatbot_location"] = props["chatbot_location"]
                if props.get("chatbot_budget"):
                    lead_context["chatbot_budget"] = props["chatbot_budget"]
                if props.get("chatbot_preference"):
                    lead_context["chatbot_preference"] = props["chatbot_preference"]
                if props.get("chatbot_rooms"):
                    lead_context["chatbot_rooms"] = props["chatbot_rooms"]
                if props.get("chatbot_score"):
                    lead_context["chatbot_score"] = props["chatbot_score"]
                if props.get("canal_origen"):
                    lead_context["canal_origen"] = props["canal_origen"]
                logger.info(f"[DeferredProcess] Propiedades HubSpot cargadas: {list(lead_context.keys())}")
        
        # Agregar contexto de código de propiedad si existe
        if property_code_result.has_code:
            lead_context.update({
                "property_code": property_code_result.code,
                "high_intent": True,
                "code_context": property_code_result.context
            })
        elif link_result.tiene_link and link_result.portal in [
            PortalOrigen.INSTAGRAM, PortalOrigen.FACEBOOK,
            PortalOrigen.TIKTOK, PortalOrigen.YOUTUBE, PortalOrigen.LINKEDIN
        ]:
            lead_context.update({
                "social_media_link": True,
                "social_media_portal": link_result.portal.value,
                "social_media_url": link_result.url_original,
                "es_inmueble": link_result.es_inmueble,
                "high_intent": True
            })
        elif link_result.tiene_link and link_result.portal in [
            PortalOrigen.FINCA_RAIZ, PortalOrigen.METRO_CUADRADO,
            PortalOrigen.MERCADO_LIBRE, PortalOrigen.CIENCUADRAS,
            PortalOrigen.PAGINA_WEB
        ]:
            portal_display = get_link_detector().obtener_nombre_portal(link_result.portal)
            lead_context.update({
                "portal_link": True,
                "portal_id": link_result.portal.value,   # "finca_raiz", "ciencuadras", etc.
                "portal_name": portal_display,
                "portal_url": link_result.url_original,
                "high_intent": True
            })
            logger.info(
                f"[DeferredProcess] Link de portal inmobiliario: {portal_display} "
                f"({link_result.url_original[:80]})"
            )

        # Procesar mensaje con Sofia
        result = await sofia.process_message_with_analysis(
            session_id=phone_normalized,
            user_message=processed_body,
            lead_context=lead_context
        )
        
        response_text = result.respuesta
        analysis = result.analisis
        logger.info(f"[DeferredProcess] Sofia respondió: {response_text[:100]}...")
        
        # ════════════════════════════════════════════════════════════
        # PASO 5: Manejar handoff según análisis
        # ════════════════════════════════════════════════════════════
        state_manager = get_state_manager()
        
        # Capturar intención de handoff sin ejecutarlo todavía.
        # El handoff se ejecutará DESPUÉS de enviar el mensaje al cliente,
        # para que el cliente reciba la notificación antes de que el bot se bloquee.
        pending_handoff = False
        handoff_reason = ""

        if analysis.handoff_priority == "immediate":
            pending_handoff = True
            handoff_reason = f"Cliente urgente - Emoción: {analysis.emocion}"
        elif analysis.handoff_priority == "high":
            # Solo activar handoff si ya tenemos el nombre del cliente.
            # Si no, el bot sigue activo para capturarlo en el siguiente mensaje.
            has_name = bool(analysis.nombre_detectado or lead_context.get("firstname"))
            if has_name:
                reason_parts = []
                if analysis.intencion_visita:
                    reason_parts.append("Intención de visita")
                pending_handoff = True
                handoff_reason = ", ".join(reason_parts) if reason_parts else "Cliente potencial"
            else:
                logger.info(
                    f"[DeferredProcess] Handoff diferido para {phone_normalized}: "
                    f"esperando nombre antes de activar PENDING_HANDOFF"
                )
        
        # update_activity() fue movido antes del WS notify (Fix CR-1 — ver líneas anteriores)

        # ✅ FIX: Persistir canal_origen y owner_id en Redis para asignación correcta de asesores
        # El canal histórico se preserva para futuros mensajes del mismo contacto
        # El owner_id es CRÍTICO para que el contacto aparezca en el panel del asesor correcto
        hubspot_owner_id = None
        if contact_info and contact_info.properties:
            hubspot_owner_id = contact_info.properties.get("hubspot_owner_id")

        # Si se detectó código de inmueble, forzar handoff high (espejo del flujo principal).
        # Protege contra el caso en que Sofia falle JSON parsing y retorne análisis vacío.
        if property_code_result.has_code and analysis and analysis.handoff_priority not in ("immediate", "high"):
            analysis.handoff_priority = "high"
            analysis.intencion_visita = True

      # Solo agregar al panel (ZSET) si el contacto tiene señal comercial.
        # Contactos que regresan (is_new=False): su meta ya existe, el bloque
        # "else" de ensure_meta_with_channel no corre → add_to_zset no tiene efecto.
        _is_new_contact = contact_info.is_new if contact_info else True
        _add_to_panel = (
            not _is_new_contact          # Contacto que regresa: sin cambio de comportamiento
            or not analysis              # Sin análisis (error): safe default = mostrar en panel
            or analysis.handoff_priority not in ("none", "low")  # Señal comercial confirmada
            or not is_business_hours()   # Fuera de horario → siempre visible para seguimiento
        )

        await state_manager.ensure_meta_with_channel(
            phone=phone_normalized,
            canal=final_channel,
            canal_origen=historical_channel,
            contact_id=contact_id,
            display_name=contact_info.firstname if contact_info else None,
            owner_id=hubspot_owner_id,
            add_to_zset=_add_to_panel
        )
        
        # ════════════════════════════════════════════════════════════
        # PASO 6: Verificar horario y estado final
        # ════════════════════════════════════════════════════════════
        out_of_hours_msg = None
        if should_add_out_of_hours_message(analysis.handoff_priority):
            out_of_hours_msg = get_out_of_hours_message()
        
        # Re-verificar estado (anti race-condition)
        final_status = await state_manager.get_status(phone_normalized)
        if final_status in [
            ConversationStatus.HUMAN_ACTIVE,
            ConversationStatus.IN_CONVERSATION,
            ConversationStatus.PENDING_HANDOFF
        ]:
            logger.warning(f"[DeferredProcess] Race condition: estado={final_status.value}")
            
            if contact_info:
                await _sync_conversation_with_analysis_to_hubspot(
                    contact_info.contact_id,
                    processed_body,
                    f"[BOT BLOQUEADO] {response_text}",
                    phone_normalized,
                    analysis,
                    final_channel,
                    media_result,
                    client_mongo_id,
                    lead_context=lead_context,
                )
            
            if out_of_hours_msg:
                await twilio_client.send_whatsapp_message(
                    to=phone_normalized,
                    body=out_of_hours_msg
                )
            return
        
        # Concatenar mensaje de fuera de horario si aplica
        if out_of_hours_msg:
            response_text = f"{response_text}\n\n{out_of_hours_msg}"
        
        # ════════════════════════════════════════════════════════════
        # PASO 7: Enviar respuesta via Twilio REST API
        # ════════════════════════════════════════════════════════════
        send_result = await twilio_client.send_whatsapp_message(
            to=phone_normalized,
            body=response_text
        )
        
        if send_result.get("status") == "error":
            logger.error(f"[DeferredProcess] Error enviando respuesta: {send_result}")
        else:
            logger.info(f"[DeferredProcess] ✅ Respuesta enviada: {send_result.get('message_sid', 'OK')}")
            # Ejecutar handoff DESPUÉS de confirmar envío, para que el cliente
            # reciba el mensaje antes de que el bot quede bloqueado.
            if pending_handoff:
                await state_manager.request_handoff(
                    phone_normalized,
                    reason=handoff_reason,
                    contact_id=contact_id,
                    canal=final_channel
                )
                logger.info(f"[DeferredProcess] ✅ Handoff solicitado: {handoff_reason}")

        # Guardar respuesta de Sofia en MongoDB
        await mongo_manager.save_message(
            phone=phone_normalized,
            content=response_text,
            sender="bot",
            channel=final_channel,
            hubspot_contact_id=contact_id
        )
        
        # Sincronizar con HubSpot
        if contact_info:
            await _sync_conversation_with_analysis_to_hubspot(
                contact_info.contact_id,
                processed_body,
                response_text,
                phone_normalized,
                analysis,
                final_channel,
                media_result,
                client_mongo_id,
                lead_context=lead_context,
            )
        
        logger.info(f"[DeferredProcess] ✅ Procesamiento completado para {phone_normalized}")
        
    except Exception as e:
        logger.error(f"[DeferredProcess] Error fatal: {e}", exc_info=True)
        # Intentar enviar mensaje de error al usuario
        try:
            await twilio_client.send_whatsapp_message(
                to=phone_normalized,
                body="Disculpa, tuve un inconveniente técnico. Por favor intenta de nuevo."
            )
        except:
            pass


# Instancia global del detector de links
_link_detector: Optional[LinkDetector] = None


def get_link_detector() -> LinkDetector:
    """Obtiene el detector de links (lazy init)."""
    global _link_detector
    if _link_detector is None:
        _link_detector = LinkDetector()
    return _link_detector


# Router de FastAPI para el middleware
router = APIRouter(prefix="/whatsapp", tags=["WhatsApp Middleware"])


class MiddlewareConfig:
    """Configuración del middleware."""

    def __init__(self):
        # Priorizar REDIS_PUBLIC_URL para desarrollo local
        self.redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
        self.sync_to_hubspot = os.getenv("SYNC_TO_HUBSPOT", "true").lower() == "true"


# Instancias globales (lazy initialization)
_config: Optional[MiddlewareConfig] = None
_state_manager: Optional[ConversationStateManager] = None
_contact_manager: Optional[ContactManager] = None
_sofia_brain: Optional[SofiaBrain] = None


def get_config() -> MiddlewareConfig:
    """Obtiene la configuración del middleware."""
    global _config
    if _config is None:
        _config = MiddlewareConfig()
    return _config


def get_state_manager() -> ConversationStateManager:
    """Obtiene el gestor de estado (lazy init)."""
    global _state_manager
    if _state_manager is None:
        config = get_config()
        _state_manager = ConversationStateManager(config.redis_url)
    return _state_manager


def get_contact_manager() -> ContactManager:
    """Obtiene el gestor de contactos (lazy init)."""
    global _contact_manager
    if _contact_manager is None:
        _contact_manager = ContactManager(hubspot_client=hubspot_client)
    return _contact_manager


def get_sofia_brain() -> SofiaBrain:
    """Obtiene el cerebro de Sofía (lazy init)."""
    global _sofia_brain
    if _sofia_brain is None:
        config = get_config()
        _sofia_brain = SofiaBrain(
            redis_url=config.redis_url,
        )
    return _sofia_brain


# ═══════════════════════════════════════════════════════════════════════════════
# DETECCIÓN DINÁMICA DE CANAL (UNIFICADA CON LinkDetector)
# ═══════════════════════════════════════════════════════════════════════════════

def detect_channel_dynamic(body: str, current_redis_channel: Optional[str]) -> str:
    """
    Detecta el canal basado en links usando LinkDetector (robusto).

    UNIFICADO: Usa el mismo sistema de detección que el análisis de Sofía,
    eliminando redundancias y garantizando consistencia.
    """
    if not body:
        if current_redis_channel and current_redis_channel not in ["desconocido", "default", None]:
            return current_redis_channel
        return "whatsapp"

    # 1. Usar LinkDetector para detección robusta
    link_detector = get_link_detector()
    result = link_detector.analizar_mensaje(body)

    if result.tiene_link and result.portal != PortalOrigen.DESCONOCIDO:
        # Mapear PortalOrigen a string de canal compatible con MongoDB/Redis
        channel = result.portal.value  # "instagram", "facebook", "finca_raiz", etc.
        logger.debug(
            f"[Webhook] Canal detectado por LinkDetector: {channel} "
            f"(URL: {result.url_original[:50] if result.url_original else 'N/A'}...)"
        )
        return channel

    # 2. Si no hay link detectado, usar canal de Redis si existe
    if current_redis_channel and current_redis_channel not in ["desconocido", "default", None]:
        logger.debug(f"[Webhook] Canal preservado del Redis: {current_redis_channel}")
        return current_redis_channel

    # 3. Default absoluto
    logger.debug(f"[Webhook] Canal default: whatsapp (sin links ni contexto previo)")
    return "whatsapp"


# ═══════════════════════════════════════════════════════════════════════════════
# LÓGICA HÍBRIDA: should_bot_respond
# ═══════════════════════════════════════════════════════════════════════════════

# Lista de canales a verificar para HUMAN_ACTIVE
# IMPORTANTE: Debe coincidir con PortalOrigen en link_detector.py
CANALES_A_VERIFICAR = [
    # WhatsApp directo
    "whatsapp", "whatsapp_directo",
    # Redes Sociales
    "instagram", "facebook", "linkedin", "youtube", "tiktok",
    # Portales Inmobiliarios
    "finca_raiz", "metrocuadrado", "mercado_libre", "ciencuadras",
    # Otros
    "pagina_web", "default", "desconocido"
]

async def should_bot_respond(
    phone_normalized: str,
    contact_id: Optional[str] = None
) -> tuple[bool, str, Optional[str], str]:
    """
    Determina si Sofía debe responder al mensaje.

    Esta función centraliza la lógica de verificación híbrida que evita
    colisión entre respuestas del bot y el asesor.

    Verificaciones:
    1. Estado en Redis EN CUALQUIER CANAL (BOT_ACTIVE / HUMAN_ACTIVE / PENDING_HANDOFF)
    2. Propiedad `sofia_activa` en HubSpot (si hay contact_id)

    Retorna:
        (bool, str, Optional[str], str): (should_respond, reason, special_message, detected_redis_channel)
        El 4to elemento es el canal en el que se encontró al usuario en Redis, útil para la
        detección dinámica de canal basada en contenido.

    FIX: Ahora verifica HUMAN_ACTIVE en TODOS los canales posibles,
    no solo en 'whatsapp' por defecto. Esto evita que Sofía responda
    cuando un asesor está atendiendo desde otro canal (ej: instagram).
    """
    state_manager = get_state_manager()
    detected_redis_channel = "whatsapp"  # Default

    # LOG DE DEBUG: Mostrar que se está verificando
    logger.info(f"🔍 [should_bot_respond] Verificando estado para: {phone_normalized}")

    # ═══════════════════════════════════════════════════════════════════════
    # 1. Verificar estado en Redis EN CUALQUIER CANAL
    # ═══════════════════════════════════════════════════════════════════════
    estados_encontrados = []  # Para debug

    for canal in CANALES_A_VERIFICAR:
        status = await state_manager.get_status(phone_normalized, canal)

        # LOG DE DEBUG: Mostrar cada estado encontrado (solo los no-None)
        if status:
            estados_encontrados.append(f"{canal}:{status.value}")

        if status == ConversationStatus.HUMAN_ACTIVE:
            logger.info(
                f"🤫 [should_bot_respond] Bot silenciado: HUMANO_INTERVINIENDO "
                f"(teléfono: {phone_normalized}, canal detectado: {canal})"
            )
            return False, "HUMANO_INTERVINIENDO", None, canal

        if status == ConversationStatus.IN_CONVERSATION:
            logger.info(
                f"🤫 [should_bot_respond] Bot silenciado: ASESOR_EN_CONVERSACION "
                f"(teléfono: {phone_normalized}, canal detectado: {canal})"
            )
            return False, "ASESOR_EN_CONVERSACION", None, canal

        if status == ConversationStatus.PENDING_HANDOFF:
            logger.info(
                f"⏳ [should_bot_respond] Bot en espera: PENDIENTE_HANDOFF "
                f"(teléfono: {phone_normalized}, canal detectado: {canal})"
            )
            special_message = (
                "En un momento uno de nuestros asesores te atenderá. "
                "Gracias por tu paciencia. 🙏"
            )
            return False, "PENDIENTE_HANDOFF", special_message, canal
        
        # Guardar el primer canal activo (aunque no sea HUMAN_ACTIVE)
        if status and detected_redis_channel == "whatsapp":
            detected_redis_channel = canal

    # LOG DE DEBUG: Mostrar resumen de estados
    if estados_encontrados:
        logger.info(f"🔍 [should_bot_respond] Estados encontrados: {', '.join(estados_encontrados)}")
    else:
        logger.info(f"🔍 [should_bot_respond] Sin estados en Redis para {phone_normalized} (todos los canales)")

    # ═══════════════════════════════════════════════════════════════════════
    # 2. Verificar propiedad 'sofia_activa' en HubSpot
    # ═══════════════════════════════════════════════════════════════════════
    if contact_id:
        timeline_logger = get_timeline_logger()
        sofia_activa = await timeline_logger.is_sofia_active(contact_id)

        if not sofia_activa:
            logger.info(
                f"🤫 [should_bot_respond] Bot silenciado: DESACTIVADO_EN_CRM "
                f"(contact_id: {contact_id})"
            )
            return False, "DESACTIVADO_EN_CRM", None, detected_redis_channel

    # ═══════════════════════════════════════════════════════════════════════
    # Todo OK - Sofía puede responder
    # ═══════════════════════════════════════════════════════════════════════
    logger.info(
        f"✅ [should_bot_respond] Bot ACTIVO: OK (teléfono: {phone_normalized})"
    )
    return True, "OK", None, detected_redis_channel


@router.post("/webhook")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Endpoint principal del webhook de Twilio.

    PATRÓN DE RESPUESTA DIFERIDA:
    - Responder inmediatamente con 200 OK (TwiML vacío) — siempre < 1 segundo
    - Procesar el mensaje en background (sin límite de tiempo)
    - Enviar respuesta de Sofia via Twilio REST API

    DUAL-PARSER:
    - Si Content-Type es application/json → Twilio Conversations API (nuevo)
    - Si Content-Type es application/x-www-form-urlencoded → Programmable Messaging (legacy)
    El flag CONVERSATIONS_ENABLED activa el path JSON pero el parser detecta automáticamente
    el formato real, actuando como safety net durante la transición.
    """
    # ════════════════════════════════════════════════════════════
    # PASO 0: Parsear request — dual-parser JSON / form-encoded
    # ════════════════════════════════════════════════════════════
    content_type = request.headers.get("content-type", "").lower()
    is_json_request = "application/json" in content_type

    try:
        _raw_peek = await request.body()
        _peek_str = _raw_peek[:1500].decode("utf-8", errors="replace")
        _twilio_sig = request.headers.get("x-twilio-signature", "")[:16]
        logger.info(
            f"[Webhook][RAW] ct='{content_type}' sig='{_twilio_sig}...' "
            f"len={len(_raw_peek)} body[:1500]={_peek_str!r}"
        )
        async def _replay_body():
            return {"type": "http.request", "body": _raw_peek, "more_body": False}
        request._receive = _replay_body
    except Exception as _diag_e:
        logger.warning(f"[Webhook][RAW] No se pudo leer body para diagnóstico: {_diag_e}")

    From: str = ""
    Body: str = ""
    ProfileName: Optional[str] = None
    MessageSid: Optional[str] = None
    NumMedia: int = 0
    MediaUrl0: Optional[str] = None
    MediaContentType0: Optional[str] = None
    OriginalRepliedMessageSid: Optional[str] = None
    OriginalRepliedMessageSender: Optional[str] = None
    conversation_sid: Optional[str] = None

    def _extract_reply_sid_from_attrs(attrs_raw: Any) -> Optional[str]:
        """Extrae el SID del mensaje citado desde el campo Attributes (JSON).

        Twilio Conversations pone el contexto de reply en Attributes SOLO cuando
        puede resolver el WAMid al IM SID del mensaje original (ambos en la misma
        Conversation). Probamos todas las variantes de key conocidas.
        """
        if not attrs_raw:
            return None
        try:
            attrs = attrs_raw if isinstance(attrs_raw, dict) else json.loads(attrs_raw)
        except Exception:
            return None
        if not isinstance(attrs, dict) or not attrs:
            return None
        sid = (
            attrs.get("originalRepliedMessageSid")           # camelCase Twilio Conversations
            or attrs.get("original_replied_message_sid")     # snake_case alternativo
            or attrs.get("OriginalRepliedMessageSid")        # PascalCase legacy
            or (attrs.get("replyTo") or {}).get("messageSid")   # camelCase nested (Conversations API docs)
            or (attrs.get("replyTo") or {}).get("sid")
            or (attrs.get("reply_to") or {}).get("message_sid")
            or (attrs.get("reply_to") or {}).get("id")
            or (attrs.get("context") or {}).get("id")
            or (attrs.get("context") or {}).get("message_sid")
            or (attrs.get("referral") or {}).get("id")
        )
        return sid or None

    def _extract_reply_sid_from_channel_metadata(channel_metadata_raw: Any) -> Optional[str]:
        """Extrae el SID del mensaje citado desde ChannelMetadata (fallback).

        ChannelMetadata contiene metadata WhatsApp nativa. Para replies incluye
        el WAMid del mensaje original en data.context. Si el mensaje original
        fue enviado via Conversations API, Twilio puede haber guardado la
        correspondencia IM SID → WAMid, pero no lo expone directamente aquí.
        Logeamos para análisis; en producción el SID útil viene de Attributes.
        """
        if not channel_metadata_raw:
            return None
        try:
            meta = channel_metadata_raw if isinstance(channel_metadata_raw, dict) \
                   else json.loads(channel_metadata_raw)
        except Exception:
            return None
        data_ctx = (meta.get("data") or {}).get("context") or {}
        # WAMid del mensaje citado (PascalCase en WhatsApp native metadata)
        wamid = (
            data_ctx.get("MessageId")          # PascalCase — campo real de Twilio/WhatsApp
            or data_ctx.get("quoted_message_id")
            or data_ctx.get("message_id")
            or data_ctx.get("id")
        )
        if wamid:
            logger.info(
                f"[Webhook][Reply] WAMid en ChannelMetadata: {wamid[:60]} "
                f"(se intentará lookup en Mongo por wamid)"
            )
            return wamid  # devolver WAMid para intento de lookup en Mongo
        return None

    if is_json_request:
        # ── Path Conversations API (JSON) ──────────────────────────────────
        try:
            data = await request.json()
        except Exception as e:
            logger.error(f"[Webhook] Error parseando JSON de Conversations API: {e}")
            return Response(content="", media_type="text/xml")

        From             = data.get("Author") or data.get("From", "")
        Body             = data.get("Body", "")
        ProfileName      = data.get("ProfileName") or data.get("AuthorAttributes", {}).get("name")
        MessageSid       = data.get("MessageSid") or data.get("Sid")
        conversation_sid = data.get("ConversationSid")
        chat_service_sid_inbound = data.get("ChatServiceSid")
        NumMedia         = int(data.get("NumMedia", 0))
        MediaUrl0        = data.get("MediaUrl0")
        MediaContentType0 = data.get("MediaContentType0")
        OriginalRepliedMessageSid    = data.get("OriginalRepliedMessageSid")
        OriginalRepliedMessageSender = data.get("OriginalRepliedMessageSender")
        if not OriginalRepliedMessageSid:
            OriginalRepliedMessageSid = _extract_reply_sid_from_attrs(data.get("Attributes"))

        logger.info(
            f"[Webhook][JSON] Conversations API | From={From} | "
            f"ConversationSid={conversation_sid} | MessageSid={MessageSid}"
        )
    else:
        # ── Path form-encoded: Legacy Programmable Messaging O Conversations ──
        try:
            form_data = await request.form()
        except Exception as e:
            logger.error(f"[Webhook] Error parseando form data: {e}")
            return Response(content="", media_type="text/xml")

        event_type = form_data.get("EventType", "")

        if event_type:
            # ── Edición de mensaje (cliente editó en WhatsApp) ─────────────────
            if event_type == "onMessageUpdated":
                message_sid = form_data.get("MessageSid") or form_data.get("Sid")
                new_body    = form_data.get("Body", "")
                logger.info(
                    f"[Webhook][Conversations] onMessageUpdated | "
                    f"MessageSid={message_sid} | NewBody={new_body[:80]!r} | "
                    f"AllFields={list(form_data.keys())}"
                )
                if message_sid and new_body:
                    try:
                        mongo_manager = get_mongo_manager()
                        updated = await mongo_manager.update_message_content(message_sid, new_body)
                        if updated:
                            await ws_manager.publish_broadcast(
                                get_state_manager().redis,
                                {
                                    "type": "message_edited",
                                    "message_id": str(updated["_id"]),
                                    "phone": updated.get("phone", ""),
                                    "new_content": new_body,
                                }
                            )
                    except Exception as _e:
                        logger.error(f"[Webhook][onMessageUpdated] Error: {_e}")
                return Response(content="", media_type="text/xml")

            # ── Eliminación de mensaje (cliente borró en WhatsApp) ────────────
            if event_type == "onMessageRemoved":
                message_sid = form_data.get("MessageSid") or form_data.get("Sid")
                logger.info(
                    f"[Webhook][Conversations] onMessageRemoved | "
                    f"MessageSid={message_sid} | AllFields={list(form_data.keys())}"
                )
                if message_sid:
                    try:
                        mongo_manager = get_mongo_manager()
                        deleted = await mongo_manager.soft_delete_message(message_sid)
                        if deleted:
                            await ws_manager.publish_broadcast(
                                get_state_manager().redis,
                                {
                                    "type": "message_deleted",
                                    "message_id": str(deleted["_id"]),
                                    "phone": deleted.get("phone", ""),
                                }
                            )
                    except Exception as _e:
                        logger.error(f"[Webhook][onMessageRemoved] Error: {_e}")
                return Response(content="", media_type="text/xml")

            # ── Conversations API — form-encoded (onMessageAdd pre-webhook) ──
            # Ignorar eventos que no son mensajes entrantes
            if event_type not in ("onMessageAdd", "onMessageAdded"):
                logger.debug(f"[Webhook][Conversations] Evento ignorado: {event_type}")
                return Response(content="", media_type="text/xml")

            # Mensajes outbound que envió el panel/bot via API:
            # NO seguir el pipeline (no respuesta, no doble save), pero SÍ
            # backfillar el conversations_message_sid (IMxxx) en el doc del asesor
            # para que las citaciones del cliente y los edit/delete funcionen.
            source = form_data.get("Source", "")
            if source == "API":
                _conv_sid = form_data.get("ConversationSid")
                _im_sid   = form_data.get("MessageSid") or form_data.get("Sid")
                _body     = form_data.get("Body", "") or ""
                if _conv_sid and _im_sid:
                    try:
                        _mm = get_mongo_manager()
                        await _mm.backfill_conversations_sid(_conv_sid, _body, _im_sid)
                        # Intentar extraer WAMid del ChannelMetadata para mensajes outbound
                        # (Twilio a veces incluye el WAMid asignado por WhatsApp aquí)
                        _cm_raw = form_data.get("ChannelMetadata")
                        if _cm_raw:
                            try:
                                _cm = json.loads(_cm_raw) if isinstance(_cm_raw, str) else _cm_raw
                                _ctx = (_cm.get("data") or {}).get("context") or {}
                                _wamid = _ctx.get("MessageId") or _ctx.get("message_id") or _ctx.get("id")
                                if _wamid:
                                    await _mm.store_wamid_for_im_sid(_im_sid, _wamid)
                                    logger.info(f"[Webhook][Backfill] WAMid almacenado: IM={_im_sid[:20]} wamid={_wamid[:40]}")
                            except Exception:
                                pass
                    except Exception as _be:
                        logger.warning(f"[Webhook][Backfill] Error: {_be}")
                else:
                    logger.debug("[Webhook][Conversations] Source=API sin ConversationSid/IM — skip backfill")
                return Response(content="", media_type="text/xml")

            From             = form_data.get("Author", "")
            Body             = form_data.get("Body", "")
            ProfileName      = None  # Conversations no provee ProfileName directamente
            MessageSid       = form_data.get("MessageSid") or form_data.get("Sid")
            conversation_sid = form_data.get("ConversationSid")
            chat_service_sid_inbound = form_data.get("ChatServiceSid")
            NumMedia         = int(form_data.get("NumMedia", 0))
            MediaUrl0        = form_data.get("MediaUrl0")
            MediaContentType0 = form_data.get("MediaContentType0")

            # Conversations API entrega media como JSON array en campo "Media",
            # NO como NumMedia/MediaUrl0/MediaContentType0 (legacy Programmable Messaging).
            # Estructura: [{"Sid":"MExxx","Filename":"...","ContentType":"image/jpeg","Size":123}]
            # Resolución: construir URL del Media Content Service (MCS) que requiere
            # un GET autenticado para obtener la URL temporal de descarga real.
            if NumMedia == 0:
                media_raw = form_data.get("Media")
                chat_service_sid = form_data.get("ChatServiceSid")
                if media_raw and chat_service_sid:
                    try:
                        media_items = json.loads(media_raw) if isinstance(media_raw, str) else media_raw
                        if isinstance(media_items, list) and media_items:
                            first = media_items[0] or {}
                            media_sid = first.get("Sid")
                            if media_sid:
                                NumMedia = len(media_items)
                                MediaUrl0 = (
                                    f"https://mcs.us1.twilio.com/v1/Services/"
                                    f"{chat_service_sid}/Media/{media_sid}"
                                )
                                MediaContentType0 = first.get("ContentType") or MediaContentType0
                                logger.info(
                                    f"[Webhook][Conversations/Media] {NumMedia} archivo(s) | "
                                    f"MediaSid={media_sid} | ContentType={MediaContentType0} | "
                                    f"Filename={first.get('Filename')}"
                                )
                    except Exception as _me:
                        logger.error(f"[Webhook][Conversations/Media] Error parseando Media JSON: {_me} | raw={str(media_raw)[:200]}")
            OriginalRepliedMessageSid    = form_data.get("OriginalRepliedMessageSid")
            OriginalRepliedMessageSender = form_data.get("OriginalRepliedMessageSender")

            # Reply context: primero Attributes (Twilio resuelve IM SID allí cuando puede),
            # luego ChannelMetadata como fallback de diagnóstico.
            attrs_raw = form_data.get("Attributes")
            channel_metadata_raw = form_data.get("ChannelMetadata")
            if not OriginalRepliedMessageSid and attrs_raw:
                OriginalRepliedMessageSid = _extract_reply_sid_from_attrs(attrs_raw)
                if OriginalRepliedMessageSid:
                    logger.info(
                        f"[Webhook][Conversations/Form][Reply] SID extraído de Attributes: "
                        f"{OriginalRepliedMessageSid}"
                    )
                elif attrs_raw and attrs_raw != "{}":
                    logger.info(
                        f"[Webhook][Conversations/Form][Reply] Attributes sin SID resoluble: "
                        f"{str(attrs_raw)[:300]}"
                    )
            # Diagnóstico ChannelMetadata (loguear cuando hay context de reply)
            if channel_metadata_raw:
                _extract_reply_sid_from_channel_metadata(channel_metadata_raw)
                try:
                    _cm = json.loads(channel_metadata_raw) if isinstance(channel_metadata_raw, str) else channel_metadata_raw
                    _ctx = (_cm.get("data") or {}).get("context") or {}
                    if len(_ctx) > 2:  # más que solo ProfileName y WaId → hay reply context
                        logger.info(
                            f"[Webhook][Conversations/Form][Reply] ChannelMetadata.context: {_ctx}"
                        )
                except Exception:
                    pass

            logger.info(
                f"[Webhook][Conversations/Form] EventType={event_type} | "
                f"Author={From} | ConversationSid={conversation_sid} | "
                f"MessageSid={MessageSid} | Source={source} | "
                f"HasAttributes={bool(attrs_raw)} | "
                f"AllFields={list(form_data.keys())}"
            )

        else:
            # ── Legacy Programmable Messaging (form-encoded) ──────────────────
            From              = form_data.get("From", "")
            Body              = form_data.get("Body", "")
            ProfileName       = form_data.get("ProfileName")
            MessageSid        = form_data.get("MessageSid")
            NumMedia          = int(form_data.get("NumMedia", 0))
            MediaUrl0         = form_data.get("MediaUrl0")
            MediaContentType0 = form_data.get("MediaContentType0")
            OriginalRepliedMessageSid    = form_data.get("OriginalRepliedMessageSid")
            OriginalRepliedMessageSender = form_data.get("OriginalRepliedMessageSender")
            conversation_sid  = None  # No existe en Programmable Messaging
            chat_service_sid_inbound = None

            # Diagnóstico: log campos de reply para debugging
            all_keys   = list(form_data.keys())
            reply_keys = [k for k in all_keys if any(x in k.lower() for x in ("original", "reply", "context"))]
            logger.info(f"[Webhook][Form] Todos los campos: {all_keys}")
            if reply_keys:
                logger.info(f"[Webhook][Form] Campos reply: { {k: form_data.get(k) for k in reply_keys} }")

    body_preview = Body[:50] if Body else "[Sin texto]"
    logger.info(f"[Webhook] Mensaje recibido de {From}: {body_preview}... NumMedia={NumMedia}")

    if OriginalRepliedMessageSid:
        logger.info(f"[Webhook][Reply] ✅ Cliente citó mensaje: SID={OriginalRepliedMessageSid}")

    try:
        # ════════════════════════════════════════════════════════════
        # PASO 1: Validación rápida del número (< 1ms)
        # ════════════════════════════════════════════════════════════
        normalizer = PhoneNormalizer()
        validation = normalizer.normalize(From)

        if not validation.is_valid:
            logger.error(f"[Webhook] Número inválido: {From} - {validation.error_message}")
            return _create_error_response(
                "Lo siento, no pude procesar tu mensaje. Por favor intenta de nuevo."
            )

        phone_normalized = validation.normalized
        logger.info(f"[Webhook] Número normalizado: {From} → {phone_normalized}")

        # ════════════════════════════════════════════════════════════
        # PASO 2: Detección temprana del canal (< 1ms)
        # ════════════════════════════════════════════════════════════
        early_channel = detect_channel_dynamic(Body, None)
        logger.info(f"[Webhook] Canal detectado tempranamente: {early_channel}")
        # Canal real según prefijo Twilio ('whatsapp:+...' = WhatsApp directo)
        incoming_channel = "whatsapp" if From.lower().startswith("whatsapp:") else early_channel
        logger.info(f"[Webhook] Canal entrante real (From prefix): {incoming_channel}")

        # ════════════════════════════════════════════════════════════
        # PASO 3: Encolar procesamiento en background y retornar OK
        # ════════════════════════════════════════════════════════════
        logger.info(
            f"[Checkpoint] MSG_PRE_PROCESS | phone={phone_normalized} | "
            f"sid={MessageSid or 'N/A'} | channel={incoming_channel or early_channel} | "
            f"conv={conversation_sid or 'N/A'} | body={Body[:80]!r}"
        )
        background_tasks.add_task(
            _process_message_deferred,
            phone_normalized,
            From,
            Body,
            ProfileName,
            MessageSid,
            NumMedia,
            MediaUrl0,
            MediaContentType0,
            early_channel,
            OriginalRepliedMessageSid,
            incoming_channel,
            conversation_sid,
            chat_service_sid_inbound,
        )

        # Actualizar timestamps en background
        background_tasks.add_task(update_last_client_message, phone_normalized)
        background_tasks.add_task(_update_client_timestamp, phone_normalized, None)

        logger.info(f"[Webhook] ✅ Procesamiento encolado, retornando 200 OK inmediatamente")

        # TwiML vacío — la respuesta real se envía via Twilio REST API
        return Response(content="", media_type="text/xml")

    except Exception as e:
        logger.error("[Webhook] Error en validación inicial: %s", e, exc_info=True)
        return _create_error_response(
            "Disculpa, tuve un inconveniente técnico. Por favor intenta de nuevo."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# WEBHOOK ORIGINAL (DESHABILITADO - Mantenido como referencia)
# ═══════════════════════════════════════════════════════════════════════════════
# El código original del webhook se mantiene abajo comentado como referencia.
# La lógica principal ahora está en _process_message_deferred().


async def _whatsapp_webhook_original_DISABLED(
    request: Request,
    background_tasks: BackgroundTasks,
    From: str = Form(...),
    Body: str = Form(""),
    ProfileName: Optional[str] = Form(None),
    MessageSid: Optional[str] = Form(None),
    # Parámetros de multimedia (Twilio envía NumMedia, MediaUrl0, MediaContentType0)
    NumMedia: int = Form(0),
    MediaUrl0: Optional[str] = Form(None),
    MediaContentType0: Optional[str] = Form(None),
):
    """
    [DESHABILITADO] Webhook original - Mantenido como referencia.
    La lógica ha sido movida a _process_message_deferred() para respuesta diferida.
    """
    body_preview = Body[:50] if Body else "[Sin texto]"
    logger.info(f"[Webhook] Mensaje recibido de {From}: {body_preview}... NumMedia={NumMedia}")

    try:
        # ════════════════════════════════════════════════════════════
        # PASO 1: Normalización del número
        # ════════════════════════════════════════════════════════════
        normalizer = PhoneNormalizer()
        validation = normalizer.normalize(From)

        if not validation.is_valid:
            logger.error(f"[Webhook] Número inválido: {From} - {validation.error_message}")
            return _create_error_response(
                "Lo siento, no pude procesar tu mensaje. Por favor intenta de nuevo."
            )

        phone_normalized = validation.normalized
        logger.info(f"[Webhook] Número normalizado: {From} → {phone_normalized}")

        # ════════════════════════════════════════════════════════════
        # PASO 1.5: Procesamiento de Multimedia (si existe)
        # ════════════════════════════════════════════════════════════
        media_result = None
        media_url_permanent = None
        media_type = None

        if NumMedia > 0 and MediaUrl0:
            logger.info(f"[Webhook] Procesando {NumMedia} archivo(s) multimedia de {phone_normalized}")
            logger.info(f"[Webhook] ContentType: {MediaContentType0}")

            try:
                # Procesar multimedia: descarga, sube a Bunny.net, transcribe/analiza
                media_result = await media_processor.process_incoming_media(
                    media_url=MediaUrl0,
                    content_type=MediaContentType0 or "application/octet-stream",
                    phone=phone_normalized
                )

                media_url_permanent = media_result.get("permanent_url", "")
                media_type = media_result.get("media_type", "")

                # Si es audio, el Body para Sofía será la transcripción
                if media_result.get("transcription"):
                    Body = media_result.get("body_for_ai", Body)
                    logger.info(f"[Webhook] Audio transcrito: {Body[:100]}...")

                # Si es imagen, añadir análisis al contexto
                elif media_result.get("analysis"):
                    # Combinar texto original con análisis de imagen
                    if Body:
                        Body = f"{Body}\n\n{media_result.get('body_for_ai', '')}"
                    else:
                        Body = media_result.get("body_for_ai", "[Imagen recibida]")
                    logger.info(f"[Webhook] Imagen analizada: {Body[:100]}...")

            except Exception as e:
                logger.error(f"[Webhook] Error procesando multimedia: {e}")
                # Continuar con el flujo normal aunque falle el procesamiento
                Body = Body or "[El cliente envió un archivo]"

        # ════════════════════════════════════════════════════════════
        # Actualizar timestamp de último mensaje del cliente
        # ════════════════════════════════════════════════════════════
        # Necesario para calcular la ventana de 24 horas de WhatsApp
        background_tasks.add_task(update_last_client_message, phone_normalized)

        # Actualizar timestamp en ConversationMeta para TTL diferenciado
        background_tasks.add_task(_update_client_timestamp, phone_normalized, None)

        # ════════════════════════════════════════════════════════════
        # DETECCIÓN TEMPRANA DEL CANAL DE ORIGEN (para métricas correctas)
        # ════════════════════════════════════════════════════════════
        # Detectar canal ANTES de crear el contacto para que las métricas
        # de redes sociales reflejen correctamente la fuente del lead
        early_channel = detect_channel_dynamic(Body, None)
        logger.info(f"[Webhook] Canal detectado tempranamente: {early_channel}")

        # ════════════════════════════════════════════════════════════
        # DETECCIÓN DE RESPUESTA A TEMPLATE DE SEGUIMIENTO
        # ════════════════════════════════════════════════════════════
        # Si el cliente responde a un template de seguimiento post-cita,
        # activar HUMAN_ACTIVE automáticamente
        followup_detected, followup_canal = await _check_followup_response(phone_normalized)

        if followup_detected:
            logger.info(f"[Webhook] Respuesta a template de seguimiento detectada - Activando HUMAN_ACTIVE (canal: {followup_canal})")

            state_manager = get_state_manager()
            await state_manager.activate_human(
                phone_normalized=phone_normalized,
                reason="Respuesta a seguimiento post-visita",
                canal_origen=followup_canal
            )

            # Registrar mensaje en HubSpot si tenemos contacto
            contact_manager = get_contact_manager()
            try:
                # Usar canal detectado tempranamente o fallback a followup_canal
                followup_source_channel = early_channel if early_channel not in ["whatsapp", "whatsapp_directo"] else followup_canal or "whatsapp_directo"
                contact_info = await contact_manager.identify_or_create_contact(
                    phone_raw=From,
                    source_channel=followup_source_channel
                )
                if contact_info:
                    # Detectar canal dinámicamente también para followups
                    followup_final_channel = detect_channel_dynamic(Body, followup_canal)
                    background_tasks.add_task(
                        _sync_message_to_hubspot,
                        contact_info.contact_id,
                        Body,
                        "incoming",
                        phone_normalized,
                        followup_final_channel,
                        media_result,  # Pasar resultado completo de multimedia
                        MessageSid  # Para deduplicación
                    )
            except Exception as e:
                logger.warning(f"[Webhook] Error sincronizando mensaje a HubSpot: {e}")

            # Enviar respuesta indicando que un asesor le atenderá
            response_msg = (
                "¡Gracias por tu respuesta! "
                "En un momento uno de nuestros asesores te contactará para darte seguimiento."
            )
            return _create_twiml_response(response_msg)

        # ════════════════════════════════════════════════════════════
        # PASO 2: Consultar estado de la conversación
        # ════════════════════════════════════════════════════════════
        state_manager = get_state_manager()
        # NOTA: Esta consulta es solo informativa - la verificación real
        # se hace en should_bot_respond() que revisa TODOS los canales
        status = await state_manager.get_status(phone_normalized)

        # Manejar caso None de forma segura
        status_str = status.value if status else "BOT_ACTIVE (default)"
        logger.info(f"[Webhook] Estado de conversación (canal whatsapp): {status_str}")

        # ════════════════════════════════════════════════════════════
        # PASO 3: Identificar/crear contacto en HubSpot
        # ════════════════════════════════════════════════════════════
        contact_manager = get_contact_manager()

        try:
            # Usar canal detectado tempranamente para métricas correctas
            contact_info = await contact_manager.identify_or_create_contact(
                phone_raw=From,
                source_channel=early_channel
            )

            if contact_info.is_new:
                logger.info(f"[Webhook] Nuevo lead creado: {contact_info.contact_id} (canal: {early_channel})")
            else:
                logger.info(f"[Webhook] Contacto existente: {contact_info.contact_id}")
                # Para contactos existentes, actualizar canal_origen si es más específico
                if early_channel not in ["whatsapp", "whatsapp_directo"]:
                    background_tasks.add_task(
                        _update_contact_channel,
                        contact_info.contact_id,
                        early_channel
                    )

        except (ValueError, KeyError, TypeError) as e:
            logger.error("[Webhook] Error procesando contacto en HubSpot: %s", e)
            # Continuar sin HubSpot - el mensaje debe ser procesado
            contact_info = None
        except Exception as e:
            logger.error("[Webhook] Error inesperado con HubSpot: %s", e)
            contact_info = None

        # ════════════════════════════════════════════════════════════
        # PASO 4: Verificar si Sofía debe responder (Lógica Híbrida)
        # ════════════════════════════════════════════════════════════
        contact_id = contact_info.contact_id if contact_info else None
        should_respond, reason, special_message, redis_channel = await should_bot_respond(
            phone_normalized=phone_normalized,
            contact_id=contact_id
        )

        # Aplicar Detección Dinámica de Canal
        # Si el cliente envía un link de IG, el canal DEBE ser 'instagram'
        # Si no hay link, respetar el canal del Redis
        final_channel = detect_channel_dynamic(Body, redis_channel)
        logger.info(f"[Webhook] Canal final detectado: {final_channel} (redis: {redis_channel})")

        if not should_respond:
            # ═══════════════════════════════════════════════════════════════════
            # CRÍTICO: Guardar mensaje del cliente SIEMPRE (MongoDB + HubSpot)
            # Aunque el bot esté silenciado, el mensaje debe aparecer en el panel
            # ═══════════════════════════════════════════════════════════════════
            logger.info(f"[Webhook] 🔇 Bot silenciado ({reason}) - Guardando mensaje del cliente en canal {final_channel}")

            # PASO 1: Guardar en MongoDB SIEMPRE (independiente de HubSpot)
            # Esto asegura que el mensaje aparezca en el panel de asesores
            try:
                mongo_manager = get_mongo_manager()

                # Construir subdocumento media si existe
                media_dict = None
                if media_result and media_result.get("permanent_url"):
                    media_dict = {
                        "permanent_url": media_result.get("permanent_url"),
                        "type": media_result.get("media_type"),
                        "transcription": media_result.get("transcription"),
                        "analysis": media_result.get("analysis"),
                    }

                mongo_message_id = await mongo_manager.save_message(
                    phone=phone_normalized,
                    content=Body,
                    sender="client",
                    channel=final_channel,
                    hubspot_contact_id=contact_id,  # Puede ser None
                    message_sid=MessageSid,
                    media=media_dict
                )

                if mongo_message_id:
                    logger.info(f"[Webhook] ✅ Mensaje guardado en MongoDB: {mongo_message_id} (bot silenciado, canal={final_channel})")
                else:
                    logger.warning(f"[Webhook] ⚠️ MongoDB retornó None al guardar mensaje")

            except Exception as e:
                logger.error(f"[Webhook] ❌ Error guardando en MongoDB (bot silenciado): {e}")

            # PASO 2: Registrar en HubSpot si tenemos contact_info
            if contact_info:
                logger.info(f"[Webhook] 📱 Registrando mensaje del cliente en HubSpot (contact_id={contact_info.contact_id}, canal={final_channel})")
                background_tasks.add_task(
                    _sync_message_to_hubspot,
                    contact_info.contact_id,
                    Body,
                    "incoming",
                    phone_normalized,
                    final_channel,
                    media_result,  # Pasar resultado completo de multimedia
                    MessageSid  # Para deduplicación
                )
            else:
                logger.warning(f"[Webhook] ⚠️ contact_info es None para {phone_normalized} - Solo MongoDB (sin HubSpot)")

            # ════════════════════════════════════════════════════════════
            # NOTIFICAR AL PANEL VÍA WEBSOCKET (bot silenciado)
            # ════════════════════════════════════════════════════════════
            # CRÍTICO: Notificar ANTES de retornar para que el panel
            # muestre el mensaje nuevo inmediatamente con sonido y badge
            try:
                logger.info(f"[Webhook] Notificando panel (bot silenciado) - phone={phone_normalized}")
                # Fix-A: state_manager fallback resuelve assigned_owner_id desde Redis meta
                await ws_manager.notify_new_message(
                    phone=phone_normalized,
                    canal=final_channel or "whatsapp",
                    message_preview=Body[:100] if Body else "",
                    sender="client",
                    contact_name="",
                    redis_client=get_state_manager().redis,
                    state_manager=get_state_manager(),
                )
                logger.info(f"[Webhook] notify_new_message enviado (bot silenciado, cross-worker)")
            except Exception as ws_err:
                logger.error(f"[Webhook] Error notificando WebSocket (bot silenciado): {ws_err}")

            # Si hay mensaje especial (ej: PENDING_HANDOFF), enviarlo
            if special_message:
                logger.info(f"[Webhook] {reason} - Enviando mensaje especial")
                return _create_twiml_response(special_message)

            # Sin mensaje especial → respuesta vacía (bot silenciado)
            logger.info(f"[Webhook] {reason} - Bot silenciado, sin respuesta")
            return Response(content="", media_type="text/xml")

        # ════════════════════════════════════════════════════════════
        # PASO 4.1: Sofía está activa - Continuar procesamiento
        # ════════════════════════════════════════════════════════════
        logger.info(f"[Webhook] Sofía ACTIVA - Procesando mensaje")

        # ════════════════════════════════════════════════════════════
        # PASO 4.1.1: GUARDAR MENSAJE DEL CLIENTE EN MONGODB (SÍNCRONO)
        # ════════════════════════════════════════════════════════════
        # CRÍTICO: Guardar ANTES de procesar con Sofía para que el mensaje
        # aparezca en el panel incluso si Sofía falla o tarda mucho.
        # Esto garantiza que contactos nuevos sean visibles inmediatamente.
        client_mongo_id = None
        try:
            mongo_manager = get_mongo_manager()

            # Construir subdocumento media si existe
            media_dict = None
            if media_result and media_result.get("permanent_url"):
                media_dict = {
                    "permanent_url": media_result.get("permanent_url"),
                    "type": media_result.get("media_type"),
                    "transcription": media_result.get("transcription"),
                    "analysis": media_result.get("analysis"),
                }

            client_mongo_id = await mongo_manager.save_message(
                phone=phone_normalized,
                content=Body,
                sender="client",
                channel=final_channel,
                hubspot_contact_id=contact_id,
                message_sid=MessageSid,
                media=media_dict
            )

            if client_mongo_id:
                logger.info(f"[Webhook] ✅ Mensaje del cliente guardado en MongoDB: {client_mongo_id} (pre-Sofía, canal={final_channel})")
            else:
                logger.warning(f"[Webhook] ⚠️ MongoDB retornó None al guardar mensaje del cliente (pre-Sofía)")

        except Exception as e:
            logger.error(f"[Webhook] ❌ Error guardando mensaje del cliente en MongoDB (pre-Sofía): {e}")
            # Continuar con el flujo aunque falle MongoDB

        # ════════════════════════════════════════════════════════════
        # PASO 4.2: Detectar código de inmueble (alta prioridad)
        # ════════════════════════════════════════════════════════════
        property_code_result = detect_property_code(Body)
        property_code_detected = property_code_result.has_code

        if property_code_detected:
            logger.info(
                f"[Webhook] CÓDIGO DE INMUEBLE DETECTADO: {property_code_result.code} "
                f"(contexto: {property_code_result.context})"
            )

        # ════════════════════════════════════════════════════════════
        # PASO 4.2.1: Detectar links de redes sociales (alta prioridad)
        # ════════════════════════════════════════════════════════════
        link_detector = get_link_detector()
        link_result = link_detector.analizar_mensaje(Body)
        social_media_link_detected = False
        social_media_portal = None

        # Verificar si es un link de red social con contenido de inmueble
        REDES_SOCIALES = [
            PortalOrigen.INSTAGRAM,
            PortalOrigen.FACEBOOK,
            PortalOrigen.TIKTOK,
            PortalOrigen.YOUTUBE,
            PortalOrigen.LINKEDIN,
        ]

        if link_result.tiene_link and link_result.portal in REDES_SOCIALES:
            social_media_link_detected = True
            social_media_portal = link_result.portal
            logger.info(
                f"[Webhook] LINK DE RED SOCIAL DETECTADO: {link_result.portal.value} "
                f"(es_inmueble: {link_result.es_inmueble}, url: {link_result.url_original})"
            )

        # ════════════════════════════════════════════════════════════
        # PASO 4.3: Procesar mensaje con Sofía (Single-Stream)
        # ════════════════════════════════════════════════════════════
        sofia = get_sofia_brain()

        # Construir contexto adicional si hay código o link de red social detectado
        lead_context = None
        if property_code_detected:
            lead_context = {
                "property_code": property_code_result.code,
                "high_intent": True,
                "code_context": property_code_result.context
            }
        elif social_media_link_detected:
            # Link de red social con posible inmueble
            lead_context = {
                "social_media_link": True,
                "social_media_portal": social_media_portal.value if social_media_portal else None,
                "social_media_url": link_result.url_original,
                "es_inmueble": link_result.es_inmueble,
                "high_intent": True
            }

        # Procesar mensaje con análisis integrado (Single-Stream)
        result = await sofia.process_message_with_analysis(
            session_id=phone_normalized,
            user_message=Body,
            lead_context=lead_context
        )

        response_text = result.respuesta
        analysis = result.analisis

        # Si se detectó código de inmueble, forzar handoff high
        if property_code_detected and analysis.handoff_priority not in ["immediate", "high"]:
            logger.info("[Webhook] Elevando prioridad de handoff por código de inmueble detectado")
            analysis.handoff_priority = "high"
            analysis.intencion_visita = True

        # Si se detectó link de red social con contenido de inmueble, forzar handoff high
        # Los links de Instagram/Facebook/TikTok usualmente son videos de propiedades
        if social_media_link_detected and analysis.handoff_priority not in ["immediate", "high"]:
            logger.info(
                f"[Webhook] Elevando prioridad de handoff por link de {social_media_portal.value} "
                f"(es_inmueble: {link_result.es_inmueble})"
            )
            analysis.handoff_priority = "high"
            analysis.link_redes_sociales = True
            # Guardar info del link para HubSpot
            if not hasattr(analysis, 'social_media_info'):
                analysis.social_media_info = {}
            analysis.social_media_info = {
                "portal": social_media_portal.value if social_media_portal else None,
                "url": link_result.url_original,
                "es_inmueble": link_result.es_inmueble
            }

        # ════════════════════════════════════════════════════════════
        # PASO 4.4: Actuar según el análisis
        # ════════════════════════════════════════════════════════════
        state_manager = get_state_manager()

        # Log especial si hay posible origen social sin link
        if hasattr(analysis, 'posible_origen_social') and analysis.posible_origen_social:
            logger.info(
                f"[Webhook] 📱 POSIBLE ORIGEN SOCIAL detectado (sin link): "
                f"phone={phone_normalized}. Sofía solicitó link/imagen. "
                f"Canal permanece en 'whatsapp' hasta confirmar."
            )

        # Handoff inmediato si cliente enojado o lo solicita explícitamente
        if analysis.handoff_priority == "immediate":
            logger.info(
                f"[Webhook] Handoff INMEDIATO detectado: "
                f"emoción={analysis.emocion}, score={analysis.sentiment_score}"
            )
            # ✅ AGREGAR AL PANEL: Cliente urgente necesita atención humana
            await state_manager.request_handoff(
                phone_normalized,
                reason=f"Cliente urgente - Emoción: {analysis.emocion}, Score: {analysis.sentiment_score}",
                contact_id=contact_info.contact_id if contact_info else None,
                canal=final_channel
            )
            logger.info(f"[Webhook] ✅ Contacto {phone_normalized} agregado al panel (IMMEDIATE)")

        # Handoff alto - cliente listo para avanzar (tiene necesidades claras)
        elif analysis.handoff_priority == "high":
            logger.info(
                f"[Webhook] Handoff HIGH detectado: intención_visita={analysis.intencion_visita}"
            )
            # ✅ AGREGAR AL PANEL: Cliente potencial con necesidades definidas
            reason_parts = []
            if analysis.intencion_visita:
                reason_parts.append("Intención de visita")
            if analysis.link_redes_sociales:
                reason_parts.append("Link de red social")
            if hasattr(analysis, 'summary_update') and analysis.summary_update:
                reason_parts.append(analysis.summary_update)
            
            reason = ", ".join(reason_parts) if reason_parts else "Cliente potencial listo para asesor"
            
            await state_manager.request_handoff(
                phone_normalized,
                reason=reason,
                contact_id=contact_info.contact_id if contact_info else None,
                canal=final_channel
            )
            logger.info(f"[Webhook] ✅ Contacto {phone_normalized} agregado al panel (HIGH)")
            
            # Notificar también como hot lead en HubSpot
            if contact_info:
                background_tasks.add_task(
                    _notify_high_priority_lead,
                    contact_info.contact_id,
                    phone_normalized,
                    analysis
                )

        # Fallback: Detectar intención de handoff por keywords (compatibilidad)
        elif sofia.detect_handoff_intent(Body):
            logger.info(f"[Webhook] Detectada intención de handoff por keywords")
            await state_manager.request_handoff(
                phone_normalized,
                reason="Cliente solicitó hablar con asesor",
                contact_id=contact_info.contact_id if contact_info else None,
                canal=final_channel
            )

        # ── Confirmación automática de cita ──────────────────────────────────
        # Si Sofía detectó que el cliente confirmó una cita ("Sí, ahí estaré",
        # "Confirmado", etc.), actualizar el estado de la cita PENDING → CONFIRMED
        # sin intervención manual de la asesora.
        if getattr(analysis, 'cita_confirmada', False):
            try:
                appt_manager = get_appointment_manager()
                confirmed = await appt_manager.confirm_appointment(
                    phone_normalized, final_channel
                )
                if confirmed:
                    logger.info(
                        "[Webhook] ✅ Cita confirmada automáticamente: %s:%s",
                        phone_normalized, final_channel
                    )
                else:
                    logger.debug(
                        "[Webhook] cita_confirmada=True pero sin cita pendiente para %s:%s",
                        phone_normalized, final_channel
                    )
            except Exception as appt_err:
                logger.warning(
                    "[Webhook] Error en confirmación automática de cita (no crítico): %s",
                    appt_err
                )

        # Actualizar actividad
        await state_manager.update_activity(phone_normalized)

        # Inbox de no-leídos: registrar para el asesor asignado (post-Sofía)
        try:
            _inbox_meta2 = await state_manager.get_meta(phone_normalized)
            if _inbox_meta2 and _inbox_meta2.assigned_owner_id:
                await state_manager.add_to_advisor_inbox(
                    advisor_id=_inbox_meta2.assigned_owner_id,
                    phone=phone_normalized,
                    canal=final_channel or "whatsapp",
                )
        except Exception as _inbox_err2:
            logger.warning(f"[Inbox][Webhook][Post] Error escribiendo inbox (non-fatal): {_inbox_err2}")

        # Notificar al panel vía WebSocket para reordenamiento instantáneo
        # (sin esto, el panel solo se actualiza en el próximo ciclo de polling)
        try:
            # Obtener estado actual para decidir tipo de notificación
            current_status = await state_manager.get_status(phone_normalized)
            logger.info(f"[Webhook] Notificando panel - phone={phone_normalized}, status={current_status}")
            
            # Si el contacto está siendo atendido por un asesor, enviar notificación
            # de nuevo mensaje con preview para que el asesor lo vea inmediatamente
            if current_status in [
                ConversationStatus.HUMAN_ACTIVE,
                ConversationStatus.IN_CONVERSATION
            ]:
                # Notificación rica con preview del mensaje
                # Fix-A: state_manager resuelve assigned_owner_id desde Redis meta → targeted
                await ws_manager.notify_new_message(
                    phone=phone_normalized,
                    canal=final_channel or "whatsapp",
                    message_preview=Body[:100] if Body else "",
                    sender="client",
                    contact_name="",
                    redis_client=get_state_manager().redis,
                    state_manager=get_state_manager(),
                )
                logger.info(f"[Webhook] notify_new_message enviado (HUMAN_ACTIVE, cross-worker, targeted)")
            else:
                # Notificación simple para refrescar la lista
                # Fix-A: usar notify_new_message para que también haga enrutamiento targeted
                # (antes era publish_broadcast directo → broadcast global a todos los asesores)
                await ws_manager.notify_new_message(
                    phone=phone_normalized,
                    canal=final_channel or "whatsapp",
                    message_preview="",
                    sender="client",
                    contact_name="",
                    redis_client=get_state_manager().redis,
                    state_manager=get_state_manager(),
                )
                logger.info(f"[Webhook] notify_new_message enviado (BOT_ACTIVE, cross-worker, targeted)")
        except Exception as ws_err:
            logger.error(f"[Webhook] Error notificando WebSocket: {ws_err}")

        # ════════════════════════════════════════════════════════════
        # PASO 4.5: Verificar horario laboral para handoff
        # ════════════════════════════════════════════════════════════
        # Si el cliente quiere asesor y estamos fuera de horario,
        # preparar mensaje tranquilizador. NO se agrega a response_text
        # todavía — se enviará directamente en PASO 4.6 para garantizar
        # que llegue incluso cuando la race condition bloquea a Sofía.
        out_of_hours_msg: Optional[str] = None
        if should_add_out_of_hours_message(analysis.handoff_priority):
            out_of_hours_msg = get_out_of_hours_message()
            logger.info(
                f"[Webhook] Fuera de horario con handoff {analysis.handoff_priority} "
                f"— mensaje informativo preparado (se enviará antes del race condition check)"
            )

        # ════════════════════════════════════════════════════════════
        # PASO 4.6: RE-CHECK estado ANTES de enviar (anti race-condition)
        # ════════════════════════════════════════════════════════════
        # Verificar si un asesor intervino mientras Sofía procesaba.
        # Si hay mensaje de fuera de horario, enviarlo vía TwiML aunque
        # Sofía esté bloqueada — el cliente SIEMPRE debe recibir este aviso.
        final_status = await state_manager.get_status(phone_normalized)
        if final_status in [
            ConversationStatus.HUMAN_ACTIVE,
            ConversationStatus.IN_CONVERSATION,
            ConversationStatus.PENDING_HANDOFF
        ]:
            # Guardar en HubSpot pero NO enviar respuesta de Sofía
            # NOTA: El mensaje del cliente ya fue guardado en MongoDB (client_mongo_id)
            hubspot_content = f"[BOT BLOQUEADO - {final_status.value}] {response_text}"
            if out_of_hours_msg:
                hubspot_content += f"\n\n[FUERA DE HORARIO ENVIADO] {out_of_hours_msg}"

            if contact_info:
                background_tasks.add_task(
                    _sync_conversation_with_analysis_to_hubspot,
                    contact_info.contact_id,
                    Body,
                    hubspot_content,
                    phone_normalized,
                    analysis,
                    final_channel,
                    media_result,  # Pasar resultado completo de multimedia
                    client_mongo_id  # ID del mensaje ya guardado (evita duplicados)
                )

            if out_of_hours_msg:
                # Enviar solo el aviso de fuera de horario — Sofía bloqueada
                logger.info(
                    f"[Webhook] Estado {final_status.value} — Sofía bloqueada. "
                    f"Enviando mensaje de fuera de horario al cliente."
                )
                return _create_twiml_response(out_of_hours_msg)

            logger.warning(
                f"[Webhook] ⚠️ RACE CONDITION EVITADA: Estado cambió a {final_status.value} "
                f"mientras Sofía procesaba. NO se enviará respuesta del bot."
            )
            return Response(content="", media_type="text/xml")

        # Sin race condition: si hay mensaje de fuera de horario, concatenarlo a la respuesta
        if out_of_hours_msg:
            response_text = f"{response_text}\n\n{out_of_hours_msg}"

        # Sincronizar con HubSpot en background (incluye análisis)
        # NOTA: El mensaje del cliente ya fue guardado en MongoDB (client_mongo_id)
        # Solo falta guardar la respuesta de Sofía y sincronizar con HubSpot
        if contact_info:
            logger.info(f"[Webhook] 📱 Registrando conversación en HubSpot (contact_id={contact_info.contact_id}, canal={final_channel})")
            background_tasks.add_task(
                _sync_conversation_with_analysis_to_hubspot,
                contact_info.contact_id,
                Body,
                response_text,
                phone_normalized,
                analysis,
                final_channel,
                media_result,  # Pasar resultado completo de multimedia
                client_mongo_id  # ID del mensaje ya guardado (evita duplicados)
            )
        else:
            logger.warning(f"[Webhook] ⚠️ contact_info es None para {phone_normalized} - Conversación NO se guardará en HubSpot")

        return _create_twiml_response(response_text)

    except Exception as e:
        logger.error("[Webhook] Error procesando mensaje: %s", e, exc_info=True)
        return _create_error_response(
            "Disculpa, tuve un inconveniente técnico. Por favor intenta de nuevo."
        )


@router.post("/status")
async def whatsapp_status_callback(
    request: Request,
    MessageSid: str = Form(...),
    MessageStatus: str = Form(...),
    From: Optional[str] = Form(None),
    To: Optional[str] = Form(None),
    ErrorCode: Optional[str] = Form(None),
    ErrorMessage: Optional[str] = Form(None),
):
    """
    Callback de estado de mensajes de Twilio.
    
    Este endpoint actualiza el estado en MongoDB para reconciliar
    lo que muestra el panel con lo que realmente recibió el cliente.
    """
    # Normalizar el número de destino para búsqueda
    phone_normalized = None
    if To:
        normalizer = PhoneNormalizer()
        validation = normalizer.normalize(To.replace("whatsapp:", ""))
        if validation.is_valid:
            phone_normalized = validation.normalized

    # Log del estado recibido
    if MessageStatus in ["failed", "undelivered"]:
        logger.error(
            f"[StatusCallback] ❌ Mensaje {MessageSid} FALLIDO: {MessageStatus} "
            f"(Error: {ErrorCode} - {ErrorMessage}) | To: {To}"
        )
    elif MessageStatus in ["delivered", "read"]:
        logger.info(
            f"[StatusCallback] ✅ Mensaje {MessageSid}: {MessageStatus} | To: {To}"
        )
    else:
        logger.debug(
            f"[StatusCallback] Mensaje {MessageSid}: {MessageStatus} | To: {To}"
        )

    # Actualizar estado en MongoDB para reconciliación
    try:
        mongo_manager = get_mongo_manager()
        
        if MessageStatus in ["delivered", "read"]:
            # Marcar como entregado exitosamente
            await mongo_manager.update_delivery_status(
                message_sid=MessageSid,
                status="delivered",
                delivered_at=datetime.utcnow()
            )
            
        elif MessageStatus in ["failed", "undelivered"]:
            # Marcar como fallido - CRÍTICO para detectar mensajes perdidos
            await mongo_manager.update_delivery_status(
                message_sid=MessageSid,
                status="failed",
                error_code=ErrorCode,
                error_message=ErrorMessage
            )
            
            logger.warning(
                f"[StatusCallback] ⚠️ ALERTA: Mensaje a {phone_normalized or To} NO entregado. "
                f"El panel puede mostrar mensaje que el cliente NO recibió."
            )

            # Notificar al asesor en tiempo real para que sepa que la plantilla no llegó
            if phone_normalized:
                try:
                    state_mgr = get_state_manager()
                    advisor_id = None
                    for canal_hint in ("whatsapp", "finca_raiz", "metrocuadrado", "pagina_web",
                                       "whatsapp_directo", "ciencuadras", "mercado_libre",
                                       "facebook", "instagram", "linkedin", "tiktok", "youtube"):
                        meta = await state_mgr.get_meta(phone_normalized, canal_hint)
                        if meta and getattr(meta, "assigned_owner_id", None):
                            advisor_id = meta.assigned_owner_id
                            break

                    if ErrorCode == "63024":
                        user_msg = f"⚠️ Plantilla NO entregada: el número {phone_normalized} no tiene WhatsApp activo."
                    else:
                        user_msg = (
                            f"⚠️ Plantilla NO entregada al número {phone_normalized}. "
                            f"Error Twilio: {ErrorCode or 'desconocido'}."
                        )

                    notification = {
                        "type": "template_delivery_failed",
                        "phone": phone_normalized,
                        "message_sid": MessageSid,
                        "error_code": ErrorCode,
                        "user_message": user_msg,
                    }

                    redis_client = state_mgr.redis
                    if advisor_id:
                        await ws_manager.publish_to_advisor(redis_client, advisor_id, notification)
                    else:
                        await ws_manager.publish_broadcast(redis_client, notification)

                    logger.info(
                        f"[StatusCallback] Notificación WS enviada a asesor={advisor_id or 'broadcast'} "
                        f"por fallo en {phone_normalized} (error {ErrorCode})"
                    )
                except Exception as ws_err:
                    logger.error(f"[StatusCallback] Error enviando notificación WS: {ws_err}")

    except Exception as e:
        # No fallar el callback por errores de MongoDB
        logger.error(f"[StatusCallback] Error actualizando delivery status: {e}")

    return Response(content="", media_type="text/xml")


# ════════════════════════════════════════════════════════════════════
# Funciones auxiliares
# ════════════════════════════════════════════════════════════════════

async def _update_client_timestamp(phone_normalized: str, canal: Optional[str] = None):
    """
    Actualiza timestamp del mensaje del cliente en ConversationMeta.
    Usado para calcular TTL de 24h si cliente deja de responder.
    """
    try:
        state_manager = get_state_manager()
        await state_manager.update_client_message_timestamp(phone_normalized, canal)
    except Exception as e:
        logger.error("[Webhook] Error actualizando timestamp cliente: %s", e)


async def _update_contact_channel(contact_id: str, canal_origen: str):
    """
    Actualiza el canal_origen de un contacto existente en HubSpot.
    
    Se usa cuando detectamos un canal más específico (ej: Instagram, Facebook)
    para un contacto que ya existía con canal genérico (whatsapp_directo).
    
    Esto es CRÍTICO para que las métricas de redes sociales sean correctas.
    
    Args:
        contact_id: ID del contacto en HubSpot
        canal_origen: Nuevo canal detectado (instagram, facebook, etc.)
    """
    try:
        contact_manager = get_contact_manager()
        await contact_manager.update_contact_info(
            contact_id=contact_id,
            properties={"canal_origen": canal_origen}
        )
        logger.info(
            f"[Webhook] Canal actualizado en HubSpot: contact_id={contact_id}, "
            f"canal_origen={canal_origen}"
        )
    except Exception as e:
        # No fallar si HubSpot no acepta la actualización
        logger.warning(f"[Webhook] No se pudo actualizar canal_origen en HubSpot: {e}")


async def _check_followup_response(phone_normalized: str) -> tuple:
    """
    Verifica si el mensaje es respuesta a un template de seguimiento de cita.

    Busca en Redis si hay un flag de followup pendiente para este contacto.
    Si existe, lo elimina y retorna información para activar HUMAN_ACTIVE.

    Returns:
        Tupla (found: bool, canal: Optional[str]) - True y el canal si hay followup pendiente
    """
    r = None
    try:
        config = get_config()
        import redis.asyncio as redis_async
        r = redis_async.from_url(config.redis_url, encoding="utf-8", decode_responses=True)

        # Buscar si hay followup pendiente para cualquier canal
        found = False
        canal = None
        async for key in r.scan_iter(match=f"appointment_followup_pending:{phone_normalized}:*"):
            # Extraer el canal del key (formato: appointment_followup_pending:{phone}:{canal})
            parts = key.split(":")
            if len(parts) >= 3:
                canal = parts[-1]  # Último segmento es el canal

            # Encontrado - eliminar el flag y marcar como encontrado
            await r.delete(key)
            found = True
            logger.info(f"[Webhook] Followup pendiente detectado y eliminado: {key} (canal: {canal})")
            break  # Solo necesitamos encontrar uno

        return found, canal

    except Exception as e:
        logger.error("[Webhook] Error verificando followup response: %s", e)
        return False, None
    finally:
        if r:
            await r.aclose()


def _create_twiml_response(message: str) -> Response:
    """
    Crea una respuesta TwiML con un mensaje.
    """
    twiml = MessagingResponse()
    twiml.message(message)
    return Response(content=str(twiml), media_type="text/xml")


def _create_error_response(message: str) -> Response:
    """
    Crea una respuesta de error amigable.
    """
    return _create_twiml_response(message)


async def _sync_message_to_hubspot(
    contact_id: str,
    message: str,
    direction: str,
    phone: str,
    channel: str = "whatsapp",
    media_result: Optional[dict] = None,
    message_sid: Optional[str] = None
) -> None:
    """
    Sincroniza un mensaje individual.

    FLUJO v2.0:
    1. Guardar en MongoDB (~5ms) - Para visualización inmediata en panel
       (con deduplicación por message_sid si se proporciona)
    2. Registrar en HubSpot Timeline - Archivo histórico (puede demorar)

    Args:
        channel: Canal de origen del mensaje (whatsapp, instagram, facebook, etc.)
        media_result: Diccionario con info de multimedia procesada
                     (permanent_url, media_type, transcription, analysis)
        message_sid: ID del mensaje de Twilio para deduplicación (opcional)
    """
    mongo_message_id = None
    media_url = media_result.get("permanent_url") if media_result else None
    media_type = media_result.get("media_type") if media_result else None

    # =========================================================================
    # PASO 1: MongoDB - Fuente de verdad para el panel en tiempo real
    # =========================================================================
    try:
        mongo_manager = get_mongo_manager()
        sender = "client" if direction == "incoming" else "bot"

        # Construir subdocumento media si existe
        media_dict = None
        if media_result and media_url:
            media_dict = {
                "permanent_url": media_url,
                "type": media_type,
                "transcription": media_result.get("transcription"),
                "analysis": media_result.get("analysis"),
                "doc_format": media_result.get("doc_format") or None,
                "doc_icon": media_result.get("doc_icon") or None,
                "original_filename": media_result.get("original_filename") or None,
            }

        mongo_message_id = await mongo_manager.save_message(
            phone=phone,
            content=message,
            sender=sender,
            channel=channel,
            hubspot_contact_id=contact_id,
            message_sid=message_sid,  # Deduplicación por MessageSid
            media=media_dict
        )

        if mongo_message_id:
            logger.debug(f"[MongoDB] Mensaje guardado: {mongo_message_id} ({direction}, canal={channel})")
    except Exception as e:
        logger.error(f"[MongoDB] Error guardando mensaje: {e}")
        # Continuar con HubSpot aunque MongoDB falle

    # =========================================================================
    # PASO 2: HubSpot Timeline - Archivo histórico
    # =========================================================================
    try:
        timeline_logger = get_timeline_logger()

        # Construir contenido para HubSpot incluyendo link multimedia si existe
        hubspot_content = message
        if media_url:
            media_label = {"image": "📷 Imagen", "audio": "🎵 Audio", "video": "🎬 Video", "file": "📎 Archivo", "document": "📄 Documento"}.get(media_type, "📎 Archivo")
            hubspot_content = f"{message}\n\n{media_label}: {media_url}" if message else f"{media_label}: {media_url}"

        if direction == "incoming":
            await timeline_logger.log_client_message(
                contact_id=contact_id,
                content=hubspot_content,
                session_id=phone
            )
        else:
            await timeline_logger.log_bot_message(
                contact_id=contact_id,
                content=hubspot_content,
                session_id=phone
            )

        # Marcar mensaje como sincronizado en MongoDB
        if mongo_message_id:
            try:
                mongo_manager = get_mongo_manager()
                await mongo_manager.mark_as_synced_to_hubspot(mongo_message_id)
            except Exception:
                pass  # No crítico

        # Actualizar propiedad de última conversación (backup)
        # HubSpot requiere que chatbot_timestamp sea medianoche UTC (no hora exacta)
        from datetime import timezone
        midnight_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        contact_manager = get_contact_manager()
        properties = {
            "chatbot_conversation": f"[{direction.upper()}] {message[:500]}",
            "chatbot_timestamp": str(int(midnight_utc.timestamp() * 1000)),
        }

        try:
            await contact_manager.update_contact_info(contact_id, properties)
        except Exception as prop_err:
            if "PROPERTY_DOESNT_EXIST" in str(prop_err):
                logger.error(
                    f"[HubSpot Sync] ❌ Propiedades no configuradas: {list(properties.keys())}"
                )
            else:
                logger.error(f"[HubSpot Sync] Error actualizando propiedades: {prop_err}")

        logger.debug(f"[HubSpot Sync] Mensaje sincronizado en Timeline para {phone} (canal={channel})")

    except (ValueError, KeyError, TypeError) as e:
        logger.error("[HubSpot Sync] Error sincronizando mensaje: %s", e)
    except Exception as e:
        logger.error("[HubSpot Sync] Error inesperado sincronizando mensaje: %s", e)


async def _sync_conversation_to_hubspot(
    contact_id: str,
    user_message: str,
    bot_response: str,
    phone: str
) -> None:
    """
    Sincroniza una interacción completa (pregunta + respuesta) a HubSpot Timeline.

    Registra ambos mensajes en el Timeline del contacto para que los asesores
    puedan ver el historial completo de la conversación.
    """
    try:
        timeline_logger = get_timeline_logger()

        # 1. Registrar mensaje del cliente en Timeline
        await timeline_logger.log_client_message(
            contact_id=contact_id,
            content=user_message,
            session_id=phone
        )

        # 2. Registrar respuesta de Sofía en Timeline
        await timeline_logger.log_bot_message(
            contact_id=contact_id,
            content=bot_response,
            session_id=phone
        )

        # 3. Actualizar propiedades del contacto (backup/resumen)
        # HubSpot requiere que chatbot_timestamp sea medianoche UTC (no hora exacta)
        from datetime import timezone
        midnight_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        contact_manager = get_contact_manager()
        sofia = get_sofia_brain()
        summary = await sofia.get_conversation_summary(phone)

        properties = {
            "chatbot_conversation": summary[-3000:],
            "chatbot_timestamp": str(int(midnight_utc.timestamp() * 1000)),
        }

        try:
            await contact_manager.update_contact_info(contact_id, properties)
        except Exception as prop_err:
            # Loguear pero NO fallar
            if "PROPERTY_DOESNT_EXIST" in str(prop_err):
                logger.error(
                    f"[HubSpot Sync] ❌ Propiedades no configuradas en HubSpot: "
                    f"{list(properties.keys())}"
                )
            else:
                logger.error(f"[HubSpot Sync] Error actualizando propiedades: {prop_err}")

        logger.debug(f"[HubSpot Sync] Conversación sincronizada en Timeline para {phone}")

    except (ValueError, KeyError, TypeError) as e:
        logger.error("[HubSpot Sync] Error procesando conversación: %s", e)
    except Exception as e:
        logger.error("[HubSpot Sync] Error sincronizando conversación: %s", e)


async def _sync_conversation_with_analysis_to_hubspot(
    contact_id: str,
    user_message: str,
    bot_response: str,
    phone: str,
    analysis,
    channel: str = "whatsapp",
    media_result: Optional[dict] = None,
    existing_client_mongo_id: Optional[str] = None,
    lead_context: Optional[dict] = None,
) -> None:
    """
    Sincroniza una interacción completa con análisis.
    """
    mongo_client_id = existing_client_mongo_id  # Usar ID existente si fue guardado antes
    mongo_bot_id = None
    media_url = media_result.get("permanent_url") if media_result else None
    media_type = media_result.get("media_type") if media_result else None
    # Inicializar base_properties antes del try para acceso seguro en sección Deal
    base_properties = {}

    # =========================================================================
    # PASO 1: MongoDB - Fuente de verdad para el panel en tiempo real
    # =========================================================================
    try:
        mongo_manager = get_mongo_manager()

        # Construir subdocumento media si existe
        media_dict = None
        if media_result and media_url:
            media_dict = {
                "permanent_url": media_url,
                "type": media_type,
                "transcription": media_result.get("transcription"),
                "analysis": media_result.get("analysis"),
                "doc_format": media_result.get("doc_format") or None,
                "doc_icon": media_result.get("doc_icon") or None,
                "original_filename": media_result.get("original_filename") or None,
            }

        # Guardar mensaje del cliente SOLO si no fue guardado previamente
        if not existing_client_mongo_id:
            mongo_client_id = await mongo_manager.save_message(
                phone=phone,
                content=user_message,
                sender="client",
                channel=channel,
                hubspot_contact_id=contact_id,
                metadata={"analysis_emocion": analysis.emocion if analysis else None},
                media=media_dict
            )
            logger.debug(f"[MongoDB] Mensaje del cliente guardado (background): {mongo_client_id}")
        else:
            logger.debug(f"[MongoDB] Mensaje del cliente ya existente (pre-Sofía): {existing_client_mongo_id}")

        # Guardar respuesta de Sofía
        mongo_bot_id = await mongo_manager.save_message(
            phone=phone,
            content=bot_response,
            sender="bot",
            channel=channel,
            hubspot_contact_id=contact_id,
            metadata={
                "analysis_handoff": analysis.handoff_priority if analysis else None,
                "analysis_score": analysis.sentiment_score if analysis else None
            }
        )

        if mongo_client_id and mongo_bot_id:
            logger.debug(f"[MongoDB] Conversación guardada: client={mongo_client_id}, bot={mongo_bot_id}")

    except Exception as e:
        logger.error(f"[MongoDB] Error guardando conversación: {e}")
        # Continuar con HubSpot aunque MongoDB falle

    # =========================================================================
    # PASO 2: HubSpot Timeline - Archivo histórico
    # =========================================================================
    try:
        timeline_logger = get_timeline_logger()

        # Construir contenido para HubSpot incluyendo link multimedia si existe
        hubspot_client_content = user_message
        if media_url:
            media_label = {"image": "📷 Imagen", "audio": "🎵 Audio", "video": "🎬 Video", "file": "📎 Archivo", "document": "📄 Documento"}.get(media_type, "📎 Archivo")
            hubspot_client_content = f"{user_message}\n\n{media_label}: {media_url}" if user_message else f"{media_label}: {media_url}"

        # 1. Registrar mensaje del cliente en Timeline (con link multimedia si existe)
        await timeline_logger.log_client_message(
            contact_id=contact_id,
            content=hubspot_client_content,
            session_id=phone
        )

        # 2. Registrar respuesta de Sofía en Timeline
        await timeline_logger.log_bot_message(
            contact_id=contact_id,
            content=bot_response,
            session_id=phone
        )

        # Marcar mensajes como sincronizados en MongoDB
        if mongo_client_id or mongo_bot_id:
            try:
                mongo_manager = get_mongo_manager()
                if mongo_client_id:
                    await mongo_manager.mark_as_synced_to_hubspot(mongo_client_id)
                if mongo_bot_id:
                    await mongo_manager.mark_as_synced_to_hubspot(mongo_bot_id)
            except Exception:
                pass  # No crítico

        # 3. Actualizar propiedades del contacto con análisis
        # HubSpot requiere que chatbot_timestamp sea medianoche UTC (no hora exacta)
        from datetime import timezone
        midnight_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        contact_manager = get_contact_manager()
        sofia = get_sofia_brain()
        summary = await sofia.get_conversation_summary(phone)

        # ═══════════════════════════════════════════════════════════════════
        # PROPIEDADES BASE - Intentamos actualizar pero NO bloqueamos si fallan
        # ═══════════════════════════════════════════════════════════════════
        # NOTA: Aunque estas "deberían" existir, las protegemos para evitar
        # que un error de configuración en HubSpot bloquee todo el flujo.
        base_properties = {
            "chatbot_conversation": summary[-3000:],
            "chatbot_timestamp": str(int(midnight_utc.timestamp() * 1000)),
        }

        try:
            await contact_manager.update_contact_info(contact_id, base_properties)
        except Exception as base_err:
            # Loguear pero NO fallar - el mensaje ya fue guardado en MongoDB
            if "PROPERTY_DOESNT_EXIST" in str(base_err):
                logger.error(
                    f"[HubSpot Sync] ❌ PROPIEDADES BASE NO CONFIGURADAS en HubSpot: "
                    f"{list(base_properties.keys())}. Contacte al administrador de HubSpot."
                )
            else:
                logger.error(f"[HubSpot Sync] Error actualizando propiedades base: {base_err}")

        # ═══════════════════════════════════════════════════════════════════
        # PROPIEDADES OPCIONALES - Pueden no existir en HubSpot
        # ═══════════════════════════════════════════════════════════════════
        optional_properties = {}

        # Agregar summary_update si existe nueva información
        # NOTA: chatbot_summary es OPCIONAL - puede no existir en HubSpot
        if analysis.summary_update:
            optional_properties["chatbot_summary"] = analysis.summary_update

        # Registrar score de sentimiento si es bajo (para alertas)
        if analysis.sentiment_score <= 4:
            optional_properties["chatbot_sentiment_alert"] = (
                f"Score: {analysis.sentiment_score}/10 - {analysis.emocion}"
            )

        # Registrar si el cliente envió link de red social
        if analysis.link_redes_sociales:
            optional_properties["chatbot_social_media_link"] = "true"
            if hasattr(analysis, 'social_media_info') and analysis.social_media_info:
                portal = analysis.social_media_info.get("portal", "desconocido")
                optional_properties["chatbot_canal_origen"] = portal

        # Registrar si el cliente envió link de portal inmobiliario
        if lead_context and lead_context.get("portal_link"):
            optional_properties["canal_origen"] = lead_context.get("portal_id", "portal")
            if lead_context.get("portal_url"):
                optional_properties["chatbot_portal_url"] = lead_context["portal_url"][:500]

        # Registrar indicadores sospechosos si existen
        if analysis.suspicious_indicators and len(analysis.suspicious_indicators) > 0:
            optional_properties["chatbot_suspicious_indicators"] = ", ".join(analysis.suspicious_indicators)
            logger.info(
                f"[HubSpot Sync] Indicadores sospechosos detectados para {phone}: "
                f"{analysis.suspicious_indicators}"
            )

        # ═══════════════════════════════════════════════════════════════════
        # ACTUALIZAR NOMBRE DEL CLIENTE SI FUE DETECTADO (CRÍTICO)
        # ═══════════════════════════════════════════════════════════════════
        if analysis.nombre_detectado:
            # firstname es propiedad estándar de HubSpot, siempre existe
            try:
                await contact_manager.update_contact_info(contact_id, {
                    "firstname": analysis.nombre_detectado
                })
                logger.info(
                    f"[HubSpot Sync] ✅ Nombre del cliente actualizado: {analysis.nombre_detectado} "
                    f"(contact_id: {contact_id})"
                )
                # [Naming] Sincronizar display_name en Redis y notificar panel abierto.
                # Sin esto el panel queda con "Cliente Nuevo" aunque HubSpot ya tiene el nombre.
                _new_name = analysis.nombre_detectado
                try:
                    import json as _j
                    from .outbound_panel import _get_redis_client
                    _rc = await _get_redis_client()
                    # 1. Actualizar display_name en todos los canales del contacto
                    _meta_keys = await _rc.keys(f"conv_meta:{phone}:*")
                    for _mk in _meta_keys:
                        _raw = await _rc.get(_mk)
                        if _raw:
                            try:
                                _m = _j.loads(_raw)
                                _m["display_name"] = _new_name
                                _ttl = await _rc.ttl(_mk)
                                if _ttl > 0:
                                    await _rc.setex(_mk, _ttl, _j.dumps(_m))
                                else:
                                    await _rc.set(_mk, _j.dumps(_m))
                            except Exception:
                                pass
                    # 2. Invalidar cache de nombre (GET /contacts lo refresca desde HubSpot)
                    await _rc.delete(f"contact_name:{contact_id}")
                    logger.info(f"[Naming] display_name='{_new_name}' sincronizado en Redis para {phone}")
                    # 3. Notificar panel vía WebSocket (fire-and-forget)
                    # Fix-A: enrutamiento targeted por asesor dueño (evita broadcast global)
                    _name_notif = {
                        "type": "contact_updated",
                        "phone": phone,
                        "action": "name_updated",
                        "display_name": _new_name
                    }
                    try:
                        _name_meta = await get_state_manager().get_meta(phone, "whatsapp")
                        _name_owner = _name_meta.assigned_owner_id if _name_meta else None
                        if _name_owner:
                            await ws_manager.publish_to_advisor(_rc, _name_owner, _name_notif)
                        else:
                            # Contacto sin asignar — broadcast (caso excepcional)
                            await ws_manager.publish_broadcast(_rc, _name_notif)
                    except Exception as _ne:
                        logger.warning(f"[Naming] fallback broadcast name_updated: {_ne}")
                        await ws_manager.publish_broadcast(_rc, _name_notif)
                except Exception as _redis_err:
                    logger.warning(f"[Naming] Error sincronizando nombre en Redis: {_redis_err}")
            except Exception as name_err:
                logger.error(f"[HubSpot Sync] Error actualizando nombre: {name_err}")

        # Intentar actualizar propiedades opcionales (ignorar si no existen en HubSpot)
        if optional_properties:
            try:
                await contact_manager.update_contact_info(contact_id, optional_properties)
            except Exception as opt_err:
                # Ignorar errores de propiedades que no existen en HubSpot
                if "PROPERTY_DOESNT_EXIST" in str(opt_err):
                    logger.warning(
                        f"[HubSpot Sync] Propiedades opcionales no configuradas en HubSpot: "
                        f"{list(optional_properties.keys())} - Ignorando"
                    )
                else:
                    logger.warning(f"[HubSpot Sync] Error actualizando propiedades opcionales: {opt_err}")

        logger.debug(
            f"[HubSpot Sync] Conversación+Análisis sincronizado para {phone} | "
            f"Emoción: {analysis.emocion}, Score: {analysis.sentiment_score}"
        )

    except (ValueError, KeyError, TypeError) as e:
        logger.error("[HubSpot Sync] Error procesando análisis: %s", e)
    except Exception as e:
        logger.error("[HubSpot Sync] Error sincronizando conversación con análisis: %s", e)

    # ═══════════════════════════════════════════════════════════════════
    # DATOS SOFIA — escritura progresiva en Contacto (Single-Stream)
    # Solo cuando el LLM extrajo campos CRM del mensaje actual.
    # ═══════════════════════════════════════════════════════════════════
    crm_contact_props = {}
    if getattr(analysis, "tipo_propiedad", None):
        crm_contact_props["chatbot_property_type"] = analysis.tipo_propiedad
    if getattr(analysis, "tipo_operacion", None):
        crm_contact_props["chatbot_operation_type"] = analysis.tipo_operacion
    if getattr(analysis, "ubicacion", None):
        crm_contact_props["chatbot_location"] = analysis.ubicacion
    if getattr(analysis, "presupuesto", None):
        from integrations.hubspot.hubspot_utils import parse_budget_to_number
        _b = parse_budget_to_number(analysis.presupuesto)
        if _b > 0:
            crm_contact_props["chatbot_budget"] = _b
    if getattr(analysis, "caracteristicas", None) and isinstance(analysis.caracteristicas, list):
        crm_contact_props["chatbot_rooms"] = "\n".join(
            f"• {c}" for c in analysis.caracteristicas if c
        )

    if crm_contact_props:
        from integrations.hubspot.hubspot_utils import calculate_lead_score
        _score_data = {
            "firstname": lead_context.get("firstname", ""),
            "phone": phone,
            "canal_origen": lead_context.get("canal_origen", "whatsapp"),
            "metadata": {
                "tipo_propiedad": analysis.tipo_propiedad or lead_context.get("chatbot_property_type", ""),
                "ubicacion": analysis.ubicacion or lead_context.get("chatbot_location", ""),
                "presupuesto": analysis.presupuesto or "",
                "caracteristicas": analysis.caracteristicas or [],
            },
        }
        _new_score = calculate_lead_score(_score_data)
        if _new_score > 0:
            crm_contact_props["chatbot_score"] = str(_new_score)

        try:
            _cm = get_contact_manager()
            await _cm.update_contact_info(contact_id, crm_contact_props)
            logger.info(
                "[HubSpot Sync] ✅ Datos Sofia → Contacto %s: %s",
                contact_id, list(crm_contact_props.keys())
            )
        except Exception as _crm_err:
            logger.warning("[HubSpot Sync] Error escribiendo Datos Sofia en Contacto: %s", _crm_err)



async def _notify_high_priority_lead(
    contact_id: str,
    phone: str,
    analysis
) -> None:
    """
    Notifica sobre un lead de alta prioridad.

    Se llama cuando el análisis detecta handoff_priority="high",
    por ejemplo cuando el cliente expresa intención de visitar o
    envía un link de redes sociales con un inmueble.
    """
    try:
        contact_manager = get_contact_manager()

        # Construir razón del lead caliente
        reasons = []
        if analysis.intencion_visita:
            reasons.append("Intención de visita")
        if analysis.link_redes_sociales:
            reasons.append("Link de red social")
            # Si tiene info del portal, incluirla
            if hasattr(analysis, 'social_media_info') and analysis.social_media_info:
                portal = analysis.social_media_info.get("portal", "")
                if portal:
                    reasons.append(f"Portal: {portal}")

        reason_str = ", ".join(reasons) if reasons else f"Handoff: {analysis.handoff_priority}"

        # Actualizar propiedades para marcar como lead caliente
        # HubSpot requiere que chatbot_timestamp sea medianoche UTC (no hora exacta)
        from datetime import timezone
        midnight_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        # Propiedades base (deberían existir en HubSpot)
        base_properties = {
            "chatbot_timestamp": str(int(midnight_utc.timestamp() * 1000)),
        }

        # Propiedades opcionales (pueden no existir en HubSpot)
        optional_properties = {
            "chatbot_hot_lead": "true",
            "chatbot_hot_lead_reason": reason_str,
        }

        # Agregar URL del link si existe
        if hasattr(analysis, 'social_media_info') and analysis.social_media_info:
            url = analysis.social_media_info.get("url")
            if url:
                optional_properties["chatbot_social_media_url"] = url[:500]

        # Actualizar propiedades base primero
        try:
            await contact_manager.update_contact_info(contact_id, base_properties)
        except Exception as base_err:
            logger.warning(f"[Webhook] Error actualizando propiedades base: {base_err}")

        # Intentar actualizar propiedades opcionales (ignorar si no existen)
        try:
            await contact_manager.update_contact_info(contact_id, optional_properties)
        except Exception as opt_err:
            if "PROPERTY_DOESNT_EXIST" in str(opt_err):
                logger.warning(
                    f"[Webhook] Propiedades de hot lead no configuradas en HubSpot: "
                    f"{list(optional_properties.keys())} - Ignorando"
                )
            else:
                logger.warning(f"[Webhook] Error actualizando propiedades de hot lead: {opt_err}")

        logger.info(
            f"[Webhook] Lead de alta prioridad marcado: {phone} | "
            f"Razón: {reason_str}"
        )

    except (ValueError, KeyError, TypeError) as e:
        logger.error("[Webhook] Error procesando lead de alta prioridad: %s", e)
    except Exception as e:
        logger.error("[Webhook] Error notificando lead: %s", e)


# ════════════════════════════════════════════════════════════════════
# Endpoint para control de estado (admin)
# ════════════════════════════════════════════════════════════════════

@router.post("/admin/activate-human")
async def admin_activate_human(
    phone: str = Form(...),
    owner_id: Optional[str] = Form(None),
):
    """
    Activa modo humano para una conversación (admin).

    Esto se usaría cuando un asesor toma el control desde HubSpot.
    """
    try:
        normalizer = PhoneNormalizer()
        validation = normalizer.normalize(phone)

        if not validation.is_valid:
            return {"error": "Número inválido", "details": validation.error_message}

        state_manager = get_state_manager()
        await state_manager.activate_human(validation.normalized, owner_id)

        return {
            "success": True,
            "phone": validation.normalized,
            "status": ConversationStatus.HUMAN_ACTIVE.value
        }

    except ValueError as e:
        logger.error(f"[Admin] Teléfono inválido: %s", e)
        return {"error": "Teléfono inválido"}
    except Exception as e:
        logger.error("[Admin] Error al activar humano: %s", e)
        return {"error": str(e)}


@router.post("/admin/activate-bot")
async def admin_activate_bot(phone: str = Form(...)):
    """
    Reactiva el bot para una conversación (admin).

    Esto se usaría cuando el asesor termina y devuelve control a Sofía.
    """
    try:
        normalizer = PhoneNormalizer()
        validation = normalizer.normalize(phone)

        if not validation.is_valid:
            return {"error": "Número inválido", "details": validation.error_message}

        state_manager = get_state_manager()
        await state_manager.activate_bot(validation.normalized)

        return {
            "success": True,
            "phone": validation.normalized,
            "status": ConversationStatus.BOT_ACTIVE.value
        }

    except ValueError as e:
        logger.error(f"[Admin] Teléfono inválido: %s", e)
        return {"error": "Teléfono inválido"}
    except Exception as e:
        logger.error("[Admin] Error al activar bot: %s", e)
        return {"error": str(e)}


@router.get("/admin/status/{phone}")
async def admin_get_status(phone: str):
    """
    Obtiene el estado de una conversación (admin).
    """
    try:
        normalizer = PhoneNormalizer()
        validation = normalizer.normalize(phone)

        if not validation.is_valid:
            return {"error": "Número inválido", "details": validation.error_message}

        state_manager = get_state_manager()
        status = await state_manager.get_status(validation.normalized)
        meta = await state_manager.get_meta(validation.normalized)

        return {
            "phone": validation.normalized,
            "status": status.value,
            "meta": meta.to_dict() if meta else None
        }

    except ValueError as e:
        logger.error(f"[Admin] Teléfono inválido: %s", e)
        return {"error": "Teléfono inválido"}
    except Exception as e:
        logger.error("[Admin] Error al obtener estado: %s", e)
        return {"error": str(e)}


@router.post("/admin/reset-contact/{phone}")
async def admin_reset_contact(
    phone: str,
    delete_mongodb: bool = True,
    delete_redis: bool = True
):
    """
    🧹 Limpia COMPLETAMENTE un número para tests E2E.
    
    Elimina de:
    - Redis: Estados de conversación, metadata, ZSET
    - MongoDB: Historial de mensajes
    
    NO elimina de HubSpot (el contacto permanece).
    
    Uso: POST /whatsapp/admin/reset-contact/+573042652384
    """
    try:
        normalizer = PhoneNormalizer()
        validation = normalizer.normalize(phone)

        if not validation.is_valid:
            return {"error": "Número inválido", "details": validation.error_message}

        phone_norm = validation.normalized
        deleted_items = {"redis": [], "mongodb": 0}

        # === Limpiar Redis ===
        if delete_redis:
            state_manager = get_state_manager()
            redis = state_manager.redis
            
            # Buscar todas las claves relacionadas con este número
            canales = ["whatsapp", "whatsapp_directo", "instagram", "facebook", "default"]
            
            for canal in canales:
                # Estado de conversación
                state_key = f"conv_state:{phone_norm}:{canal}"
                if await redis.exists(state_key):
                    await redis.delete(state_key)
                    deleted_items["redis"].append(state_key)
                
                # Metadata
                meta_key = f"conv_meta:{phone_norm}:{canal}"
                if await redis.exists(meta_key):
                    await redis.delete(meta_key)
                    deleted_items["redis"].append(meta_key)
                
                # ZSET de contactos activos
                zset_member = f"{phone_norm}:{canal}"
                removed = await redis.zrem("active_conversations_sorted", zset_member)
                if removed:
                    deleted_items["redis"].append(f"ZSET:{zset_member}")
            
            # Buscar patrones adicionales con SCAN
            cursor = 0
            pattern = f"*{phone_norm}*"
            while True:
                cursor, keys = await redis.scan(cursor, match=pattern, count=100)
                for key in keys:
                    if key not in deleted_items["redis"]:
                        await redis.delete(key)
                        deleted_items["redis"].append(key)
                if cursor == 0:
                    break

        # === Limpiar MongoDB ===
        if delete_mongodb:
            try:
                mongo_manager = get_mongo_manager()
                if mongo_manager:
                    await mongo_manager.connect()
                    if mongo_manager.db:
                        result = await mongo_manager.db.messages.delete_many({
                            "phone": phone_norm
                        })
                        deleted_items["mongodb"] = result.deleted_count
            except Exception as e:
                logger.warning(f"[Admin] Error limpiando MongoDB: {e}")

        logger.info(f"[Admin] Contacto {phone_norm} reseteado: {len(deleted_items['redis'])} claves Redis, {deleted_items['mongodb']} mensajes MongoDB")

        return {
            "status": "success",
            "phone": phone_norm,
            "deleted": deleted_items,
            "message": f"Contacto listo para test E2E (Redis: {len(deleted_items['redis'])} claves, MongoDB: {deleted_items['mongodb']} mensajes)"
        }

    except Exception as e:
        logger.error(f"[Admin] Error reseteando contacto: {e}")
        return {"error": str(e)}


@router.post("/admin/cleanup-duplicates/{phone}")
async def admin_cleanup_duplicates(phone: str, keep_canal: Optional[str] = None):
    """
    Limpia estados duplicados para un teléfono.

    Cuando un contacto tiene múltiples estados en diferentes canales
    (ej: conv_state:+57xxx:whatsapp_directo Y conv_state:+57xxx:default),
    esta función consolida al canal especificado o al más restrictivo.
    """
    try:
        normalizer = PhoneNormalizer()
        validation = normalizer.normalize(phone)

        if not validation.is_valid:
            return {"error": "Número inválido", "details": validation.error_message}

        state_manager = get_state_manager()
        deleted = await state_manager.cleanup_duplicate_states(
            validation.normalized,
            keep_canal=keep_canal
        )

        return {
            "phone": validation.normalized,
            "duplicates_deleted": deleted,
            "keep_canal": keep_canal or "most_restrictive"
        }

    except ValueError as e:
        logger.error(f"[Admin] Teléfono inválido: %s", e)
        return {"error": "Teléfono inválido"}
    except Exception as e:
        logger.error("[Admin] Error al limpiar duplicados: %s", e)
        return {"error": str(e)}
        return {"error": str(e)}


# ════════════════════════════════════════════════════════════════════
# Endpoint para Webhooks de HubSpot (FASE 2)
# ════════════════════════════════════════════════════════════════════

@router.post("/hubspot/webhook")
async def hubspot_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """
    Endpoint para recibir webhooks de HubSpot.

    Este endpoint permite que HubSpot notifique cuando cambian propiedades
    importantes del contacto, como `sofia_activa`.
    """
    try:
        # Parsear payload (HubSpot envía array de eventos)
        payload = await request.json()
        logger.info(f"[HubSpot Webhook] Recibido payload: {payload}")

        # HubSpot envía una lista de eventos
        events = payload if isinstance(payload, list) else [payload]

        for event in events:
            property_name = event.get("propertyName", "")
            property_value = event.get("propertyValue", "")
            contact_id = str(event.get("objectId", ""))
            subscription_type = event.get("subscriptionType", "")

            # Solo procesar cambios en sofia_activa
            if property_name == "sofia_activa" and contact_id:
                logger.info(
                    f"[HubSpot Webhook] sofia_activa cambió a '{property_value}' "
                    f"para contacto {contact_id}"
                )

                # Obtener teléfono del contacto desde HubSpot
                phone = await _get_contact_phone_from_hubspot(contact_id)

                if phone:
                    state_manager = get_state_manager()

                    if property_value.lower() in ["false", "no", "0", ""]:
                        # Sofia desactivada → Activar HUMAN_ACTIVE
                        await state_manager.activate_human(
                            phone_normalized=phone,
                            contact_id=contact_id,
                            reason="Desactivado desde HubSpot CRM"
                        )
                        logger.info(f"[HubSpot Webhook] HUMAN_ACTIVE activado para {phone}")

                    elif property_value.lower() in ["true", "yes", "1", "si", "sí"]:
                        # Sofia activada → Reactivar BOT_ACTIVE
                        await state_manager.activate_bot(phone)
                        logger.info(f"[HubSpot Webhook] BOT_ACTIVE activado para {phone}")

        return {"status": "ok", "processed": len(events)}

    except (ValueError, KeyError, TypeError) as e:
        logger.error(f"[HubSpot Webhook] Datos de webhook inválidos: %s", e)
        return {"status": "error", "message": "Datos inválidos"}
    except Exception as e:
        logger.error(f"[HubSpot Webhook] Error procesando webhook: %s", e, exc_info=True)
        # Retornar 200 para evitar que HubSpot reintente
        return {"status": "error", "message": str(e)}


_hubspot_http_client: Optional["httpx.AsyncClient"] = None


def _get_hubspot_http_client():
    """Singleton httpx.AsyncClient para llamadas a HubSpot desde webhook_handler."""
    global _hubspot_http_client
    import httpx
    if _hubspot_http_client is None:
        _hubspot_http_client = httpx.AsyncClient(
            timeout=10.0,
            limits=httpx.Limits(
                max_keepalive_connections=5,
                max_connections=10,
                keepalive_expiry=30.0,
            ),
        )
    return _hubspot_http_client


async def _get_contact_phone_from_hubspot(contact_id: str) -> Optional[str]:
    """
    Obtiene el teléfono de un contacto de HubSpot.

    Args:
        contact_id: ID del contacto en HubSpot

    Returns:
        Teléfono normalizado o None si no se encuentra
    """
    hubspot_api_key = os.getenv("HUBSPOT_API_KEY")
    if not hubspot_api_key:
        logger.warning("[HubSpot Webhook] HUBSPOT_API_KEY no configurada")
        return None

    try:
        url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
        params = {"properties": "phone,whatsapp_id"}

        client = _get_hubspot_http_client()
        response = await client.get(
            url,
            headers={"Authorization": f"Bearer {hubspot_api_key}"},
            params=params,
        )

        if response.status_code == 200:
            data = response.json()
            props = data.get("properties", {})

            phone = props.get("whatsapp_id") or props.get("phone")

            if phone:
                normalizer = PhoneNormalizer()
                validation = normalizer.normalize(phone)
                if validation.is_valid:
                    return validation.normalized

            logger.warning(f"[HubSpot Webhook] Contacto {contact_id} sin teléfono válido")
            return None

        else:
            logger.warning(
                f"[HubSpot Webhook] Error obteniendo contacto {contact_id}: "
                f"{response.status_code}"
            )
            return None

    except Exception as e:
        logger.error(f"[HubSpot Webhook] Error consultando HubSpot: {e}")
        return None