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

async def should_bot_respond(
    phone_normalized: str,
    contact_id: Optional[str] = None
) -> tuple[bool, str, Optional[str]]:
    """
    Determina si Sofía debe responder al mensaje.

    Esta función centraliza la lógica de verificación híbrida que evita
    colisión entre respuestas del bot y el asesor.

    Verificaciones:
    1. Estado en Redis (BOT_ACTIVE / HUMAN_ACTIVE / PENDING_HANDOFF)
    2. Propiedad `sofia_activa` en HubSpot (si hay contact_id)
    """
    state_manager = get_state_manager()

    # ═══════════════════════════════════════════════════════════════════════
    # 1. Verificar estado en Redis (flag temporal de intervención humana)
    # ═══════════════════════════════════════════════════════════════════════
    status = await state_manager.get_status(phone_normalized)

    if status == ConversationStatus.HUMAN_ACTIVE:
        logger.info(
            f"🤫 [should_bot_respond] Bot silenciado: HUMANO_INTERVINIENDO "
            f"(teléfono: {phone_normalized})"
        )
        return False, "HUMANO_INTERVINIENDO", None

    if status == ConversationStatus.PENDING_HANDOFF:
        logger.info(
            f"⏳ [should_bot_respond] Bot en espera: PENDIENTE_HANDOFF "
            f"(teléfono: {phone_normalized})"
        )
        special_message = (
            "En un momento uno de nuestros asesores te atenderá. "
            "Gracias por tu paciencia. 🙏"
        )
        return False, "PENDIENTE_HANDOFF", special_message

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
    logger.debug(
        f"✅ [should_bot_respond] Bot activo: OK (teléfono: {phone_normalized})"
    )
    return True, "OK", None


@router.post("/webhook")
async def whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    From: str = Form(...),
    Body: str = Form(...),
    ProfileName: Optional[str] = Form(None),
    MessageSid: Optional[str] = Form(None),
):
    """
    Endpoint principal del webhook de Twilio.

    Recibe mensajes de WhatsApp y los procesa según el estado de la conversación.
    """
    logger.info(f"[Webhook] Mensaje recibido de {From}: {Body[:50]}...")

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
        # Actualizar timestamp de último mensaje del cliente
        # ════════════════════════════════════════════════════════════
        # Necesario para calcular la ventana de 24 horas de WhatsApp
        background_tasks.add_task(update_last_client_message, phone_normalized)

        # ════════════════════════════════════════════════════════════
        # PASO 2: Consultar estado de la conversación
        # ════════════════════════════════════════════════════════════
        state_manager = get_state_manager()
        status = await state_manager.get_status(phone_normalized)

        logger.info(f"[Webhook] Estado de conversación: {status.value}")

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

        except Exception as e:
            logger.error(f"[Webhook] Error con HubSpot: {e}")
            # Continuar sin HubSpot - el mensaje debe ser procesado
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
            # Registrar mensaje entrante en HubSpot (siempre)
            if contact_info:
                background_tasks.add_task(
                    _sync_message_to_hubspot,
                    contact_info.contact_id,
                    Body,
                    "incoming",
                    phone_normalized
                )

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

        # Sincronizar con HubSpot en background (incluye análisis)
        if contact_info:
            background_tasks.add_task(
                _sync_conversation_with_analysis_to_hubspot,
                contact_info.contact_id,
                Body,
                response_text,
                phone_normalized,
                analysis
            )

        return _create_twiml_response(response_text)

    except Exception as e:
        logger.error(f"[Webhook] Error procesando mensaje: {e}", exc_info=True)
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
    phone: str
) -> None:
    """
    Sincroniza un mensaje individual a HubSpot Timeline.
    """
    try:
        # 1. Registrar en Timeline (visual para asesores)
        timeline_logger = get_timeline_logger()

        if direction == "incoming":
            await timeline_logger.log_client_message(
                contact_id=contact_id,
                content=message,
                session_id=phone
            )
        else:
            await timeline_logger.log_bot_message(
                contact_id=contact_id,
                content=message,
                session_id=phone
            )

        # 2. Actualizar propiedad de última conversación (backup)
        contact_manager = get_contact_manager()
        properties = {
            "chatbot_conversation": f"[{direction.upper()}] {message[:500]}",
            "chatbot_timestamp": datetime.now().isoformat(),
        }
        await contact_manager.update_contact_info(contact_id, properties)

        logger.debug(f"[HubSpot Sync] Mensaje sincronizado en Timeline para {phone}")

    except Exception as e:
        logger.error(f"[HubSpot Sync] Error sincronizando mensaje: {e}")


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
        contact_manager = get_contact_manager()
        sofia = get_sofia_brain()
        summary = await sofia.get_conversation_summary(phone)

        properties = {
            "chatbot_conversation": summary[-3000:],
            "chatbot_timestamp": datetime.now().isoformat(),
        }
        await contact_manager.update_contact_info(contact_id, properties)

        logger.debug(f"[HubSpot Sync] Conversación sincronizada en Timeline para {phone}")

    except Exception as e:
        logger.error(f"[HubSpot Sync] Error sincronizando conversación: {e}")


async def _sync_conversation_with_analysis_to_hubspot(
    contact_id: str,
    user_message: str,
    bot_response: str,
    phone: str,
    analysis
) -> None:
    """
    Sincroniza una interacción completa con análisis a HubSpot Timeline.

    Incluye el análisis de sentimiento y actualiza propiedades adicionales
    basadas en la información extraída del análisis Single-Stream.
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

        # 3. Actualizar propiedades del contacto con análisis
        contact_manager = get_contact_manager()
        sofia = get_sofia_brain()
        summary = await sofia.get_conversation_summary(phone)

        properties = {
            "chatbot_conversation": summary[-3000:],
            "chatbot_timestamp": datetime.now().isoformat(),
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

    except Exception as e:
        logger.error(f"[HubSpot Sync] Error sincronizando conversación con análisis: {e}")


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
        properties = {
            "chatbot_hot_lead": "true",
            "chatbot_hot_lead_reason": reason_str,
            "chatbot_timestamp": datetime.now().isoformat(),
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

    except Exception as e:
        logger.error(f"[Webhook] Error notificando lead de alta prioridad: {e}")


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

    except Exception as e:
        logger.error(f"[Admin] Error activando humano: {e}")
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

    except Exception as e:
        logger.error(f"[Admin] Error activando bot: {e}")
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

    except Exception as e:
        logger.error(f"[Admin] Error obteniendo estado: {e}")
        return {"error": str(e)}