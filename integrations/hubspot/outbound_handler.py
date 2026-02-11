# integrations/hubspot/outbound_handler.py
"""
Webhook de salida: HubSpot -> Twilio.

Maneja los mensajes que los asesores envían desde HubSpot Inbox
y los reenvía al cliente por WhatsApp.

Características:
- Validación de origen (evitar loops)
- Mapeo de ThreadID a número de teléfono
- Pausa automática de Sofía cuando el asesor interviene
- Sincronización de estado en Redis y HubSpot
"""

import os
import json
import hmac
import hashlib
from typing import Optional, Dict, Any
from dataclasses import dataclass
from datetime import datetime

import redis
from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from twilio.rest import Client as TwilioClient

from logging_config import logger
from .contact_finder import get_contact_finder
from .timeline_logger import get_timeline_logger


@dataclass
class OutboundMessage:
    """Mensaje saliente desde HubSpot."""
    thread_id: str
    contact_id: str
    message_text: str
    sender_id: Optional[str] = None  # ID del asesor que envía
    sender_email: Optional[str] = None
    timestamp: Optional[datetime] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()


class OutboundHandler:
    """
    Manejador de mensajes salientes desde HubSpot.

    Flujo:
    1. Asesor responde en HubSpot Inbox
    2. HubSpot dispara webhook a /hubspot/outbound
    3. Este handler:
       a. Valida que el mensaje viene de HubSpot
       b. Obtiene el número de teléfono del contacto
       c. Pausa a Sofía para ese contacto
       d. Envía el mensaje por Twilio/WhatsApp
       e. Registra el mensaje en Timeline
    """

    # Prefijo para mapeo de threads en Redis
    THREAD_PREFIX = "hubspot:thread:"

    # TTL del mapeo de threads (7 días)
    THREAD_TTL = 7 * 24 * 60 * 60

    def __init__(self, redis_url: Optional[str] = None):
        """
        Inicializa el handler de mensajes salientes.

        Args:
            redis_url: URL de conexión a Redis
        """
        # Configuración de Twilio
        self.twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        self.twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        self.twilio_number = os.getenv("TWILIO_WHATSAPP_NUMBER") or os.getenv("TWILIO_NUMBER")

        if not all([self.twilio_account_sid, self.twilio_auth_token, self.twilio_number]):
            logger.warning(
                "[OutboundHandler] Configuración de Twilio incompleta. "
                "Los mensajes salientes no funcionarán."
            )
            self.twilio_client = None
        else:
            self.twilio_client = TwilioClient(self.twilio_account_sid, self.twilio_auth_token)

        # Configuración de HubSpot (para verificación de firma)
        self.hubspot_client_secret = os.getenv("HUBSPOT_CLIENT_SECRET")

        # Configuración de Redis
        self.redis_url = redis_url or os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL")
        self._redis_client: Optional[redis.Redis] = None

    def _get_redis(self) -> Optional[redis.Redis]:
        """Obtiene el cliente Redis con lazy initialization."""
        if self._redis_client is None and self.redis_url:
            try:
                self._redis_client = redis.from_url(self.redis_url)
                self._redis_client.ping()
            except Exception as e:
                logger.warning(f"[OutboundHandler] Redis no disponible: {e}")
                self._redis_client = None
        return self._redis_client

    def verify_hubspot_signature(self, request_body: bytes, signature: str) -> bool:
        """
        Verifica que el webhook viene de HubSpot.

        Args:
            request_body: Cuerpo de la solicitud en bytes
            signature: Firma del header X-HubSpot-Signature

        Returns:
            True si la firma es válida
        """
        if not self.hubspot_client_secret:
            logger.warning(
                "[OutboundHandler] HUBSPOT_CLIENT_SECRET no configurado. "
                "Skipping verificación de firma."
            )
            return True

        expected_signature = hmac.new(
            self.hubspot_client_secret.encode(),
            request_body,
            hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(expected_signature, signature)

    # =========================================================================
    # Mapeo de Thread ID <-> Número de teléfono
    # =========================================================================

    def save_thread_mapping(
        self,
        thread_id: str,
        phone_e164: str,
        contact_id: str
    ) -> None:
        """
        Guarda el mapeo entre ThreadID de HubSpot y número de teléfono.

        Args:
            thread_id: ID del hilo de conversación en HubSpot
            phone_e164: Número de teléfono en formato E.164
            contact_id: ID del contacto en HubSpot
        """
        redis_client = self._get_redis()
        if not redis_client:
            logger.warning("[OutboundHandler] No se puede guardar mapeo sin Redis")
            return

        key = f"{self.THREAD_PREFIX}{thread_id}"
        data = {
            "phone": phone_e164,
            "contact_id": contact_id,
            "created_at": datetime.utcnow().isoformat()
        }

        try:
            redis_client.setex(key, self.THREAD_TTL, json.dumps(data))
            logger.debug(f"[OutboundHandler] Mapeo guardado: thread={thread_id} -> phone={phone_e164}")
        except Exception as e:
            logger.error(f"[OutboundHandler] Error guardando mapeo: {e}")

    def get_phone_from_thread(self, thread_id: str) -> Optional[Dict[str, str]]:
        """
        Obtiene el número de teléfono asociado a un ThreadID.

        Args:
            thread_id: ID del hilo de conversación

        Returns:
            Dict con 'phone' y 'contact_id', o None si no existe
        """
        redis_client = self._get_redis()
        if not redis_client:
            return None

        key = f"{self.THREAD_PREFIX}{thread_id}"

        try:
            data = redis_client.get(key)
            if data:
                return json.loads(data)
        except Exception as e:
            logger.error(f"[OutboundHandler] Error obteniendo mapeo: {e}")

        return None

    # =========================================================================
    # Envío de mensajes
    # =========================================================================

    async def send_whatsapp_message(
        self,
        to_phone: str,
        message: str
    ) -> bool:
        """
        Envía un mensaje de WhatsApp vía Twilio.

        Args:
            to_phone: Número de destino en formato E.164
            message: Texto del mensaje

        Returns:
            True si se envió correctamente
        """
        if not self.twilio_client:
            logger.error("[OutboundHandler] Cliente Twilio no inicializado")
            return False

        try:
            # Asegurar formato de WhatsApp
            from_number = f"whatsapp:{self.twilio_number}" if not self.twilio_number.startswith("whatsapp:") else self.twilio_number
            to_number = f"whatsapp:{to_phone}" if not to_phone.startswith("whatsapp:") else to_phone

            message_obj = self.twilio_client.messages.create(
                from_=from_number,
                body=message,
                to=to_number
            )

            logger.info(
                f"[OutboundHandler] Mensaje enviado: SID={message_obj.sid}, "
                f"to={to_phone}"
            )
            return True

        except Exception as e:
            logger.error(f"[OutboundHandler] Error enviando mensaje: {e}")
            return False

    # =========================================================================
    # Pausa de Sofía
    # =========================================================================

    async def pause_sofia(
        self,
        contact_id: str,
        phone_e164: str,
        reason: str = "Intervención de asesor"
    ) -> bool:
        """
        Pausa a Sofía para un contacto específico.

        Sincroniza el estado en:
        1. Redis (middleware/conversation_state)
        2. HubSpot (propiedad sofia_status)

        Args:
            contact_id: ID del contacto en HubSpot
            phone_e164: Número de teléfono
            reason: Razón de la pausa

        Returns:
            True si se pausó correctamente
        """
        success = True

        # 1. Actualizar en Redis (estado de conversación del middleware)
        redis_client = self._get_redis()
        if redis_client:
            try:
                # Usar la misma estructura que ConversationStateManager
                from middleware.conversation_state import ConversationStatus

                state_key = f"conv_state:{phone_e164}"
                state_data = redis_client.get(state_key)

                if state_data:
                    state = json.loads(state_data)
                    state["status"] = ConversationStatus.HUMAN_ACTIVE.value
                    state["handoff_reason"] = reason
                    state["human_active_since"] = datetime.utcnow().isoformat()
                    redis_client.set(state_key, json.dumps(state))
                else:
                    # Crear nuevo estado
                    new_state = {
                        "status": ConversationStatus.HUMAN_ACTIVE.value,
                        "handoff_reason": reason,
                        "human_active_since": datetime.utcnow().isoformat()
                    }
                    redis_client.set(state_key, json.dumps(new_state))

                logger.info(f"[OutboundHandler] Sofía pausada en Redis para {phone_e164}")

            except Exception as e:
                logger.error(f"[OutboundHandler] Error pausando en Redis: {e}")
                success = False

        # 2. Actualizar en HubSpot
        try:
            contact_finder = get_contact_finder()
            await contact_finder.update_sofia_status(
                vid=contact_id,
                status="pausada",
                phone_e164=phone_e164
            )
            logger.info(f"[OutboundHandler] Sofía pausada en HubSpot para contact={contact_id}")

        except Exception as e:
            logger.error(f"[OutboundHandler] Error pausando en HubSpot: {e}")
            success = False

        return success

    # =========================================================================
    # Procesamiento del webhook
    # =========================================================================

    async def process_outbound_webhook(
        self,
        payload: Dict[str, Any],
        background_tasks: BackgroundTasks
    ) -> Dict[str, Any]:
        """
        Procesa el webhook de salida desde HubSpot.

        Args:
            payload: Datos del webhook
            background_tasks: FastAPI BackgroundTasks para registro async

        Returns:
            Dict con resultado del procesamiento
        """
        logger.info(f"[OutboundHandler] Webhook recibido: {json.dumps(payload, default=str)[:500]}")

        # Extraer datos del payload (ajustar según formato real de HubSpot)
        # El formato depende de cómo configures el webhook en HubSpot
        message_text = payload.get("body") or payload.get("text") or payload.get("message")
        thread_id = payload.get("threadId") or payload.get("thread_id")
        contact_id = payload.get("contactId") or payload.get("contact_id")
        sender_email = payload.get("senderEmail") or payload.get("sender_email")

        if not message_text:
            logger.warning("[OutboundHandler] Webhook sin mensaje de texto")
            return {"status": "ignored", "reason": "no_message"}

        # Obtener número de teléfono
        phone_e164 = None

        # Opción 1: Desde el payload directo
        phone_e164 = payload.get("recipientPhone") or payload.get("recipient_phone")

        # Opción 2: Desde el mapeo de thread
        if not phone_e164 and thread_id:
            mapping = self.get_phone_from_thread(thread_id)
            if mapping:
                phone_e164 = mapping.get("phone")
                contact_id = contact_id or mapping.get("contact_id")

        # Opción 3: Buscar por contact_id
        if not phone_e164 and contact_id:
            try:
                contact_finder = get_contact_finder()
                # Necesitamos obtener el teléfono del contacto
                # Esto requiere una llamada adicional a HubSpot
                phone_e164 = await self._get_phone_from_contact(contact_id)
            except Exception as e:
                logger.error(f"[OutboundHandler] Error obteniendo teléfono: {e}")

        if not phone_e164:
            logger.error(
                "[OutboundHandler] No se pudo determinar el número de destino. "
                f"thread_id={thread_id}, contact_id={contact_id}"
            )
            return {"status": "error", "reason": "no_phone"}

        # Pausar a Sofía (el asesor está interviniendo)
        if contact_id:
            await self.pause_sofia(
                contact_id=contact_id,
                phone_e164=phone_e164,
                reason=f"Mensaje de asesor: {sender_email or 'desconocido'}"
            )

        # Enviar mensaje por WhatsApp
        sent = await self.send_whatsapp_message(phone_e164, message_text)

        if not sent:
            return {"status": "error", "reason": "send_failed"}

        # Registrar en Timeline (background)
        if contact_id:
            timeline_logger = get_timeline_logger()
            background_tasks.add_task(
                timeline_logger.log_advisor_message,
                contact_id=contact_id,
                content=message_text,
                session_id=phone_e164
            )

        return {
            "status": "sent",
            "to": phone_e164,
            "contact_id": contact_id,
            "sofia_paused": True
        }

    async def _get_phone_from_contact(self, contact_id: str) -> Optional[str]:
        """
        Obtiene el teléfono de un contacto por su ID.

        Args:
            contact_id: ID del contacto en HubSpot

        Returns:
            Número de teléfono en formato E.164 o None
        """
        import httpx

        api_key = os.getenv("HUBSPOT_API_KEY")
        endpoint = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        params = {
            "properties": "phone,mobilephone,whatsapp_id,hs_whatsapp_phone_number"
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(endpoint, headers=headers, params=params)

            if response.status_code == 200:
                props = response.json().get("properties", {})
                # Prioridad: whatsapp_id > hs_whatsapp > mobilephone > phone
                return (
                    props.get("whatsapp_id") or
                    props.get("hs_whatsapp_phone_number") or
                    props.get("mobilephone") or
                    props.get("phone")
                )

        return None


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTER DE FASTAPI
# ═══════════════════════════════════════════════════════════════════════════════

router = APIRouter(prefix="/hubspot", tags=["HubSpot Outbound"])

# Instancia singleton del handler
_outbound_handler: Optional[OutboundHandler] = None


def get_outbound_handler() -> OutboundHandler:
    """Obtiene la instancia singleton del OutboundHandler."""
    global _outbound_handler
    if _outbound_handler is None:
        _outbound_handler = OutboundHandler()
    return _outbound_handler


@router.post("/outbound")
async def hubspot_outbound_webhook(
    request: Request,
    background_tasks: BackgroundTasks
):
    """
    Endpoint para recibir mensajes salientes desde HubSpot.

    Cuando un asesor responde en HubSpot Inbox, este endpoint:
    1. Recibe el mensaje
    2. Pausa a Sofía
    3. Envía el mensaje por WhatsApp
    4. Registra la actividad

    Headers esperados:
    - X-HubSpot-Signature: Firma de verificación
    """
    handler = get_outbound_handler()

    # Verificar firma (opcional pero recomendado)
    signature = request.headers.get("X-HubSpot-Signature", "")
    body = await request.body()

    if not handler.verify_hubspot_signature(body, signature):
        logger.warning("[OutboundWebhook] Firma inválida")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    result = await handler.process_outbound_webhook(payload, background_tasks)

    return result


@router.post("/thread-mapping")
async def create_thread_mapping(
    thread_id: str,
    phone: str,
    contact_id: str
):
    """
    Crea un mapeo manual entre ThreadID y número de teléfono.

    Útil para testing o cuando el mapeo no se crea automáticamente.
    """
    handler = get_outbound_handler()
    handler.save_thread_mapping(thread_id, phone, contact_id)

    return {
        "status": "created",
        "thread_id": thread_id,
        "phone": phone,
        "contact_id": contact_id
    }