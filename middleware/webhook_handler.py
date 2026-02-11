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

    Args:
        From: Número del remitente (formato: whatsapp:+573001234567)
        Body: Contenido del mensaje
        ProfileName: Nombre del perfil de WhatsApp (opcional)
        MessageSid: ID único del mensaje en Twilio (opcional)

    Returns:
        TwiML response con la respuesta de Sofía
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
        # PASO 4: Procesar según estado
        # ════════════════════════════════════════════════════════════

        if status == ConversationStatus.HUMAN_ACTIVE:
            # Humano activo → Solo espejar a HubSpot, no responder
            logger.info(f"[Webhook] HUMAN_ACTIVE - Espejando a HubSpot sin responder")

            # Registrar mensaje en HubSpot en background
            if contact_info:
                background_tasks.add_task(
                    _sync_message_to_hubspot,
                    contact_info.contact_id,
                    Body,
                    "incoming",
                    phone_normalized
                )

            # Responder vacío (sin mensaje) para que Twilio no envíe nada
            return Response(content="", media_type="text/xml")

        elif status == ConversationStatus.PENDING_HANDOFF:
            # Pendiente de handoff → Mensaje de espera
            logger.info(f"[Webhook] PENDING_HANDOFF - Enviando mensaje de espera")

            response_text = (
                "En un momento uno de nuestros asesores te atenderá. "
                "Gracias por tu paciencia. 🙏"
            )

            # Registrar en HubSpot
            if contact_info:
                background_tasks.add_task(
                    _sync_message_to_hubspot,
                    contact_info.contact_id,
                    Body,
                    "incoming",
                    phone_normalized
                )

            return _create_twiml_response(response_text)

        else:
            # BOT_ACTIVE (o estado desconocido) → Verificar si Sofía está habilitada
            logger.info(f"[Webhook] BOT_ACTIVE - Verificando si Sofía está activa")

            # ════════════════════════════════════════════════════════════
            # PASO 4.1: Verificar propiedad 'sofia_activa' en HubSpot
            # ════════════════════════════════════════════════════════════
            # Si el asesor desactivó Sofía desde HubSpot, no respondemos
            if contact_info:
                timeline_logger = get_timeline_logger()
                sofia_habilitada = await timeline_logger.is_sofia_active(contact_info.contact_id)

                if not sofia_habilitada:
                    logger.info(
                        f"[Webhook] Sofía DESACTIVADA en HubSpot para {phone_normalized} - "
                        "Solo registrando mensaje sin responder"
                    )
                    # Solo registrar el mensaje entrante, no responder
                    background_tasks.add_task(
                        _sync_message_to_hubspot,
                        contact_info.contact_id,
                        Body,
                        "incoming",
                        phone_normalized
                    )
                    # Responder vacío para que Twilio no envíe nada
                    return Response(content="", media_type="text/xml")

            # ════════════════════════════════════════════════════════════
            # PASO 4.2: Sofía está activa - Procesar mensaje
            # ════════════════════════════════════════════════════════════
            logger.info(f"[Webhook] Sofía ACTIVA - Procesando mensaje")

            sofia = get_sofia_brain()

            # Detectar intención de handoff
            if sofia.detect_handoff_intent(Body):
                logger.info(f"[Webhook] Detectada intención de handoff")
                await state_manager.request_handoff(
                    phone_normalized,
                    reason="Cliente solicitó hablar con asesor"
                )

            # Procesar mensaje con Sofía
            response_text = await sofia.process_message(
                session_id=phone_normalized,
                user_message=Body,
                lead_context=None  # TODO: Obtener de HubSpot si es necesario
            )

            # Actualizar actividad
            await state_manager.update_activity(phone_normalized)

            # Sincronizar con HubSpot en background
            if contact_info:
                background_tasks.add_task(
                    _sync_conversation_to_hubspot,
                    contact_info.contact_id,
                    Body,
                    response_text,
                    phone_normalized
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

    Args:
        message: Texto del mensaje

    Returns:
        Response con TwiML
    """
    twiml = MessagingResponse()
    twiml.message(message)
    return Response(content=str(twiml), media_type="text/xml")


def _create_error_response(message: str) -> Response:
    """
    Crea una respuesta de error amigable.

    Args:
        message: Mensaje de error para el usuario

    Returns:
        Response con TwiML
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

    Args:
        contact_id: ID del contacto en HubSpot
        message: Contenido del mensaje
        direction: "incoming" o "outgoing"
        phone: Número normalizado
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

    Args:
        contact_id: ID del contacto en HubSpot
        user_message: Mensaje del usuario
        bot_response: Respuesta de Sofía
        phone: Número normalizado
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