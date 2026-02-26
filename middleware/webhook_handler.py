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
from typing import Optional
from datetime import datetime

from fastapi import APIRouter, Form, Request, BackgroundTasks
from fastapi.responses import Response
from twilio.twiml.messaging_response import MessagingResponse

from logging_config import logger
from .phone_normalizer import PhoneNormalizer, normalize_colombian_phone
from .conversation_state import ConversationStateManager, ConversationStatus
from .contact_manager import ContactManager
from .sofia_brain import SofiaBrain

# Importaciones para integración con HubSpot Timeline
from integrations.hubspot import get_timeline_logger

# MongoDB para almacenamiento en tiempo real
from database.mongodb_client import get_mongo_manager

# Procesador de multimedia (Cloudinary + Whisper)
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
        _contact_manager = ContactManager()
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
# LÓGICA HÍBRIDA: should_bot_respond
# ═══════════════════════════════════════════════════════════════════════════════

# Lista de canales a verificar para HUMAN_ACTIVE
CANALES_A_VERIFICAR = [
    "whatsapp", "instagram", "facebook", "whatsapp_directo",
    "pagina_web", "metrocuadrado", "finca_raiz", "ciencuadras",
    "mercado_libre", "default"
]

async def should_bot_respond(
    phone_normalized: str,
    contact_id: Optional[str] = None
) -> tuple[bool, str, Optional[str]]:
    """
    Determina si Sofía debe responder al mensaje.

    Esta función centraliza la lógica de verificación híbrida que evita
    colisión entre respuestas del bot y el asesor.

    Verificaciones:
    1. Estado en Redis EN CUALQUIER CANAL (BOT_ACTIVE / HUMAN_ACTIVE / PENDING_HANDOFF)
    2. Propiedad `sofia_activa` en HubSpot (si hay contact_id)

    FIX: Ahora verifica HUMAN_ACTIVE en TODOS los canales posibles,
    no solo en 'whatsapp' por defecto. Esto evita que Sofía responda
    cuando un asesor está atendiendo desde otro canal (ej: instagram).
    """
    state_manager = get_state_manager()

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
            return False, "HUMANO_INTERVINIENDO", None

        if status == ConversationStatus.IN_CONVERSATION:
            logger.info(
                f"🤫 [should_bot_respond] Bot silenciado: ASESOR_EN_CONVERSACION "
                f"(teléfono: {phone_normalized}, canal detectado: {canal})"
            )
            return False, "ASESOR_EN_CONVERSACION", None

        if status == ConversationStatus.PENDING_HANDOFF:
            logger.info(
                f"⏳ [should_bot_respond] Bot en espera: PENDIENTE_HANDOFF "
                f"(teléfono: {phone_normalized}, canal detectado: {canal})"
            )
            special_message = (
                "En un momento uno de nuestros asesores te atenderá. "
                "Gracias por tu paciencia. 🙏"
            )
            return False, "PENDIENTE_HANDOFF", special_message

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
            return False, "DESACTIVADO_EN_CRM", None

    # ═══════════════════════════════════════════════════════════════════════
    # Todo OK - Sofía puede responder
    # ═══════════════════════════════════════════════════════════════════════
    logger.info(
        f"✅ [should_bot_respond] Bot ACTIVO: OK (teléfono: {phone_normalized})"
    )
    return True, "OK", None


@router.post("/webhook")
async def whatsapp_webhook(
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
    Endpoint principal del webhook de Twilio.

    Recibe mensajes de WhatsApp y los procesa según el estado de la conversación.
    Soporta texto, imágenes y audios.
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
                # Procesar multimedia: descarga, sube a Cloudinary, transcribe/analiza
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
                contact_info = await contact_manager.identify_or_create_contact(
                    phone_raw=From,
                    source_channel="whatsapp_directo"
                )
                if contact_info:
                    background_tasks.add_task(
                        _sync_message_to_hubspot,
                        contact_info.contact_id,
                        Body,
                        "incoming",
                        phone_normalized,
                        media_result  # Pasar resultado completo de multimedia
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
            contact_info = await contact_manager.identify_or_create_contact(
                phone_raw=From,
                source_channel="whatsapp_directo"
            )

            if contact_info.is_new:
                logger.info(f"[Webhook] Nuevo lead creado: {contact_info.contact_id}")
            else:
                logger.info(f"[Webhook] Contacto existente: {contact_info.contact_id}")

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
        should_respond, reason, special_message = await should_bot_respond(
            phone_normalized=phone_normalized,
            contact_id=contact_id
        )

        if not should_respond:
            # ═══════════════════════════════════════════════════════════════════
            # CRÍTICO: Guardar mensaje del cliente SIEMPRE (MongoDB + HubSpot)
            # Aunque el bot esté silenciado, el mensaje debe aparecer en el panel
            # ═══════════════════════════════════════════════════════════════════
            logger.info(f"[Webhook] 🔇 Bot silenciado ({reason}) - Guardando mensaje del cliente")

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
                    channel="whatsapp",
                    hubspot_contact_id=contact_id,  # Puede ser None
                    media=media_dict
                )

                if mongo_message_id:
                    logger.info(f"[Webhook] ✅ Mensaje guardado en MongoDB: {mongo_message_id} (bot silenciado)")
                else:
                    logger.warning(f"[Webhook] ⚠️ MongoDB retornó None al guardar mensaje")

            except Exception as e:
                logger.error(f"[Webhook] ❌ Error guardando en MongoDB (bot silenciado): {e}")

            # PASO 2: Registrar en HubSpot si tenemos contact_info
            if contact_info:
                logger.info(f"[Webhook] 📱 Registrando mensaje del cliente en HubSpot (contact_id={contact_info.contact_id})")
                background_tasks.add_task(
                    _sync_message_to_hubspot,
                    contact_info.contact_id,
                    Body,
                    "incoming",
                    phone_normalized,
                    media_result  # Pasar resultado completo de multimedia
                )
            else:
                logger.warning(f"[Webhook] ⚠️ contact_info es None para {phone_normalized} - Solo MongoDB (sin HubSpot)")

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

        # Handoff inmediato si cliente enojado o lo solicita explícitamente
        if analysis.handoff_priority == "immediate":
            logger.info(
                f"[Webhook] Handoff INMEDIATO detectado: "
                f"emoción={analysis.emocion}, score={analysis.sentiment_score}"
            )

        # Handoff alto - cliente listo para avanzar
        elif analysis.handoff_priority == "high":
            logger.info(
                f"[Webhook] Handoff HIGH detectado: intención_visita={analysis.intencion_visita}"
            )
            # No cambiar estado, pero registrar para notificar al asesor
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
            )

        # Actualizar actividad
        await state_manager.update_activity(phone_normalized)

        # ════════════════════════════════════════════════════════════
        # PASO 4.5: Verificar horario laboral para handoff
        # ════════════════════════════════════════════════════════════
        # Si el cliente quiere asesor y estamos fuera de horario,
        # agregar mensaje tranquilizador (no cerramos la puerta)
        if should_add_out_of_hours_message(analysis.handoff_priority):
            out_of_hours_msg = get_out_of_hours_message()
            response_text = f"{response_text}\n\n{out_of_hours_msg}"
            logger.info(
                f"[Webhook] Mensaje de fuera de horario agregado para "
                f"handoff {analysis.handoff_priority}"
            )

        # ════════════════════════════════════════════════════════════
        # PASO 4.6: RE-CHECK estado ANTES de enviar (anti race-condition)
        # ════════════════════════════════════════════════════════════
        # Verificar si un asesor intervino mientras Sofía procesaba
        final_status = await state_manager.get_status(phone_normalized)
        if final_status in [
            ConversationStatus.HUMAN_ACTIVE,
            ConversationStatus.IN_CONVERSATION,
            ConversationStatus.PENDING_HANDOFF
        ]:
            logger.warning(
                f"[Webhook] ⚠️ RACE CONDITION EVITADA: Estado cambió a {final_status.value} "
                f"mientras Sofía procesaba. NO se enviará respuesta del bot."
            )
            # Guardar en HubSpot pero NO enviar respuesta
            if contact_info:
                background_tasks.add_task(
                    _sync_conversation_with_analysis_to_hubspot,
                    contact_info.contact_id,
                    Body,
                    f"[BOT BLOQUEADO - {final_status.value}] {response_text}",
                    phone_normalized,
                    analysis,
                    media_result  # Pasar resultado completo de multimedia
                )
            return Response(content="", media_type="text/xml")

        # Sincronizar con HubSpot en background (incluye análisis)
        if contact_info:
            logger.info(f"[Webhook] 📱 Registrando conversación en HubSpot (contact_id={contact_info.contact_id})")
            background_tasks.add_task(
                _sync_conversation_with_analysis_to_hubspot,
                contact_info.contact_id,
                Body,
                response_text,
                phone_normalized,
                analysis,
                media_result  # Pasar resultado completo de multimedia
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
):
    """
    Callback de estado de mensajes de Twilio.

    Twilio envía actualizaciones cuando el estado del mensaje cambia
    (queued, sent, delivered, read, failed).
    """
    logger.debug(
        f"[StatusCallback] Message {MessageSid}: {MessageStatus} "
        f"(From: {From}, To: {To})"
    )

    # Por ahora solo logueamos, pero se podría usar para:
    # - Detectar mensajes fallidos
    # - Confirmar entrega
    # - Analytics

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


async def _check_followup_response(phone_normalized: str) -> tuple:
    """
    Verifica si el mensaje es respuesta a un template de seguimiento de cita.

    Busca en Redis si hay un flag de followup pendiente para este contacto.
    Si existe, lo elimina y retorna información para activar HUMAN_ACTIVE.

    Returns:
        Tupla (found: bool, canal: Optional[str]) - True y el canal si hay followup pendiente
    """
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

        await r.close()
        return found, canal

    except Exception as e:
        logger.error("[Webhook] Error verificando followup response: %s", e)
        return False, None


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
    media_result: Optional[dict] = None
) -> None:
    """
    Sincroniza un mensaje individual.

    FLUJO v2.0:
    1. Guardar en MongoDB (~5ms) - Para visualización inmediata en panel
    2. Registrar en HubSpot Timeline - Archivo histórico (puede demorar)

    Args:
        media_result: Diccionario con info de multimedia procesada
                     (permanent_url, media_type, transcription, analysis)
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
            }

        mongo_message_id = await mongo_manager.save_message(
            phone=phone,
            content=message,
            sender=sender,
            channel="whatsapp",
            hubspot_contact_id=contact_id,
            media=media_dict
        )

        if mongo_message_id:
            logger.debug(f"[MongoDB] Mensaje guardado: {mongo_message_id} ({direction})")
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
            media_label = {"image": "📷 Imagen", "audio": "🎵 Audio", "file": "📎 Archivo"}.get(media_type, "📎 Archivo")
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
        await contact_manager.update_contact_info(contact_id, properties)

        logger.debug(f"[HubSpot Sync] Mensaje sincronizado en Timeline para {phone}")

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
        await contact_manager.update_contact_info(contact_id, properties)

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
    media_result: Optional[dict] = None
) -> None:
    """
    Sincroniza una interacción completa con análisis.

    FLUJO v2.0:
    1. Guardar en MongoDB (~5ms) - Para visualización inmediata en panel
    2. Registrar en HubSpot Timeline - Archivo histórico (puede demorar)

    Incluye el análisis de sentimiento y actualiza propiedades adicionales
    basadas en la información extraída del análisis Single-Stream.

    Args:
        media_result: Diccionario con info de multimedia procesada
                     (permanent_url, media_type, transcription, analysis)
    """
    mongo_client_id = None
    mongo_bot_id = None
    media_url = media_result.get("permanent_url") if media_result else None
    media_type = media_result.get("media_type") if media_result else None

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
            }

        # Guardar mensaje del cliente (con multimedia si existe)
        mongo_client_id = await mongo_manager.save_message(
            phone=phone,
            content=user_message,
            sender="client",
            channel="whatsapp",
            hubspot_contact_id=contact_id,
            metadata={"analysis_emocion": analysis.emocion if analysis else None},
            media=media_dict
        )

        # Guardar respuesta de Sofía
        mongo_bot_id = await mongo_manager.save_message(
            phone=phone,
            content=bot_response,
            sender="bot",
            channel="whatsapp",
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
            media_label = {"image": "📷 Imagen", "audio": "🎵 Audio", "file": "📎 Archivo"}.get(media_type, "📎 Archivo")
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

        properties = {
            "chatbot_conversation": summary[-3000:],
            "chatbot_timestamp": str(int(midnight_utc.timestamp() * 1000)),
        }

        # Agregar summary_update si existe nueva información
        if analysis.summary_update:
            # Acumular resúmenes en una propiedad (si existe)
            properties["chatbot_summary"] = analysis.summary_update

        # Registrar score de sentimiento si es bajo (para alertas)
        if analysis.sentiment_score <= 4:
            properties["chatbot_sentiment_alert"] = (
                f"Score: {analysis.sentiment_score}/10 - {analysis.emocion}"
            )

        # Registrar si el cliente envió link de red social
        if analysis.link_redes_sociales:
            properties["chatbot_social_media_link"] = "true"
            # Si tiene info adicional del link
            if hasattr(analysis, 'social_media_info') and analysis.social_media_info:
                portal = analysis.social_media_info.get("portal", "desconocido")
                properties["chatbot_canal_origen"] = portal

        # Registrar indicadores sospechosos si existen
        if analysis.suspicious_indicators and len(analysis.suspicious_indicators) > 0:
            # Almacenar los indicadores separados por coma
            properties["chatbot_suspicious_indicators"] = ", ".join(analysis.suspicious_indicators)
            logger.info(
                f"[HubSpot Sync] Indicadores sospechosos detectados para {phone}: "
                f"{analysis.suspicious_indicators}"
            )

        await contact_manager.update_contact_info(contact_id, properties)

        logger.debug(
            f"[HubSpot Sync] Conversación+Análisis sincronizado para {phone} | "
            f"Emoción: {analysis.emocion}, Score: {analysis.sentiment_score}"
        )

    except (ValueError, KeyError, TypeError) as e:
        logger.error("[HubSpot Sync] Error procesando análisis: %s", e)
    except Exception as e:
        logger.error("[HubSpot Sync] Error sincronizando conversación con análisis: %s", e)


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

        properties = {
            "chatbot_hot_lead": "true",
            "chatbot_hot_lead_reason": reason_str,
            "chatbot_timestamp": str(int(midnight_utc.timestamp() * 1000)),
        }

        # Agregar URL del link si existe
        if hasattr(analysis, 'social_media_info') and analysis.social_media_info:
            url = analysis.social_media_info.get("url")
            if url:
                properties["chatbot_social_media_url"] = url[:500]  # Truncar si es muy largo

        await contact_manager.update_contact_info(contact_id, properties)

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


async def _get_contact_phone_from_hubspot(contact_id: str) -> Optional[str]:
    """
    Obtiene el teléfono de un contacto de HubSpot.

    Args:
        contact_id: ID del contacto en HubSpot

    Returns:
        Teléfono normalizado o None si no se encuentra
    """
    import httpx

    hubspot_api_key = os.getenv("HUBSPOT_API_KEY")
    if not hubspot_api_key:
        logger.warning("[HubSpot Webhook] HUBSPOT_API_KEY no configurada")
        return None

    try:
        url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
        params = {"properties": "phone,whatsapp_id"}

        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {hubspot_api_key}"},
                params=params,
                timeout=10.0
            )

            if response.status_code == 200:
                data = response.json()
                props = data.get("properties", {})

                # Preferir whatsapp_id, luego phone
                phone = props.get("whatsapp_id") or props.get("phone")

                if phone:
                    # Normalizar teléfono
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