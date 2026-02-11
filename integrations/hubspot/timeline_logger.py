# integrations/hubspot/timeline_logger.py
"""
Módulo para registrar conversaciones en el Timeline de HubSpot usando Notes API.

Permite que los asesores vean el historial de conversaciones de Sofía
directamente en la ficha del contacto en HubSpot.

Nota: Timeline Events API requiere permisos especiales (403 bloqueado).
      Este módulo usa exclusivamente Notes API (Engagements) que funciona
      con los permisos estándar de crm.objects.contacts.write.

Características:
- Registro asíncrono (no bloquea la respuesta al cliente)
- Cola de eventos para batch processing
- Diferenciación visual entre mensajes de bot/cliente/asesor
- Verificación de propiedad 'sofia_activa' para control de asesor
"""

import os
import asyncio
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from logging_config import logger


class MessageDirection(Enum):
    """Dirección del mensaje."""
    INBOUND = "inbound"    # Cliente -> Sistema
    OUTBOUND = "outbound"  # Sistema -> Cliente


class MessageSender(Enum):
    """Tipo de emisor del mensaje."""
    CLIENT = "client"      # Mensaje del cliente
    BOT = "bot"            # Mensaje de Sofía (IA)
    ADVISOR = "advisor"    # Mensaje del asesor humano


@dataclass
class TimelineEvent:
    """Representa un evento para registrar en el Timeline."""
    contact_id: str
    content: str
    sender: MessageSender
    direction: MessageDirection
    session_id: Optional[str] = None
    timestamp: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()


class TimelineLogger:
    """
    Logger de eventos para HubSpot Timeline.

    Uso:
        logger = TimelineLogger()

        # Registrar mensaje del cliente
        await logger.log_client_message(contact_id, "Hola, busco apartamento")

        # Registrar respuesta de Sofía
        await logger.log_bot_message(contact_id, "¡Hola! Soy Sofía...")

        # Registrar mensaje del asesor
        await logger.log_advisor_message(contact_id, "Hola, soy Carlos...")
    """

    def __init__(self):
        """Inicializa el logger de Timeline usando Notes API."""
        self.api_key = os.getenv("HUBSPOT_API_KEY")
        self.base_url = "https://api.hubapi.com"

        if not self.api_key:
            raise ValueError("HUBSPOT_API_KEY no está configurada")

        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        # Cola de eventos para procesamiento en batch
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._worker_running = False

        logger.info("[TimelineLogger] Inicializado con Notes API (Engagements)")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError))
    )
    async def _create_timeline_event(self, event: TimelineEvent) -> bool:
        """
        Crea una nota en el Timeline del contacto usando Notes API.

        Timeline Events API está bloqueada (403) en cuentas sin permisos especiales.
        Este método usa directamente Notes API que funciona con permisos estándar.

        Args:
            event: Evento a registrar

        Returns:
            True si se creó exitosamente
        """
        # Usar directamente Notes API (Timeline Events bloqueado por HubSpot 403)
        return await self._create_note(event)

    async def _create_note(self, event: TimelineEvent) -> bool:
        """
        Crea una nota en el contacto usando Notes API.

        Este es el método principal para registrar conversaciones en HubSpot,
        ya que Timeline Events API requiere permisos especiales (403 bloqueado).

        Args:
            event: Evento a registrar como nota

        Returns:
            True si se creó exitosamente
        """
        endpoint = f"{self.base_url}/crm/v3/objects/notes"

        # Formatear el contenido de la nota
        if event.sender == MessageSender.BOT:
            prefix = "🤖 [Sofía - IA]"
        elif event.sender == MessageSender.ADVISOR:
            prefix = "👤 [Asesor]"
        else:
            prefix = "📱 [Cliente - WhatsApp]"

        direction_icon = "⬅️" if event.direction == MessageDirection.INBOUND else "➡️"
        timestamp_str = event.timestamp.strftime("%Y-%m-%d %H:%M:%S")

        note_body = f"{prefix} {direction_icon}\n\n{event.content}\n\n---\n📅 {timestamp_str}"

        payload = {
            "properties": {
                "hs_note_body": note_body,
                "hs_timestamp": event.timestamp.isoformat() + "Z"
            },
            "associations": [
                {
                    "to": {"id": event.contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 202  # Note to Contact
                        }
                    ]
                }
            ]
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(endpoint, headers=self.headers, json=payload)

            if response.status_code == 201:
                logger.debug(
                    f"[TimelineLogger] Nota creada (fallback): contact={event.contact_id}"
                )
                return True

            else:
                logger.error(
                    f"[TimelineLogger] Error creando nota: "
                    f"{response.status_code} - {response.text}"
                )
                return False

    async def is_sofia_active(self, contact_id: str) -> bool:
        """
        Verifica si Sofía está activa para un contacto específico.

        Consulta la propiedad 'sofia_activa' del contacto en HubSpot.
        Si la propiedad no existe o es 'true', Sofía responde.
        Si es 'false', el asesor humano tiene el control.

        Args:
            contact_id: ID del contacto en HubSpot

        Returns:
            True si Sofía debe responder, False si está silenciada
        """
        endpoint = f"{self.base_url}/crm/v3/objects/contacts/{contact_id}"
        params = {"properties": "sofia_activa"}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    endpoint,
                    headers=self.headers,
                    params=params
                )

                if response.status_code == 200:
                    data = response.json()
                    sofia_activa = data.get("properties", {}).get("sofia_activa", "true")

                    # Si es "false" (string), Sofía está desactivada
                    if sofia_activa == "false":
                        logger.info(
                            f"[TimelineLogger] Sofía DESACTIVADA para contacto {contact_id}"
                        )
                        return False

                    return True

                else:
                    logger.warning(
                        f"[TimelineLogger] Error consultando sofia_activa: "
                        f"{response.status_code}"
                    )
                    # Por defecto, Sofía responde si hay error
                    return True

        except Exception as e:
            logger.error(f"[TimelineLogger] Error verificando sofia_activa: {e}")
            # Por defecto, Sofía responde si hay error
            return True

    async def set_sofia_active(self, contact_id: str, active: bool) -> bool:
        """
        Actualiza el estado de Sofía para un contacto.

        Args:
            contact_id: ID del contacto en HubSpot
            active: True para activar Sofía, False para silenciarla

        Returns:
            True si se actualizó exitosamente
        """
        endpoint = f"{self.base_url}/crm/v3/objects/contacts/{contact_id}"

        payload = {
            "properties": {
                "sofia_activa": "true" if active else "false"
            }
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.patch(
                    endpoint,
                    headers=self.headers,
                    json=payload
                )

                if response.status_code == 200:
                    estado = "ACTIVADA" if active else "DESACTIVADA"
                    logger.info(
                        f"[TimelineLogger] Sofía {estado} para contacto {contact_id}"
                    )
                    return True

                else:
                    logger.error(
                        f"[TimelineLogger] Error actualizando sofia_activa: "
                        f"{response.status_code} - {response.text}"
                    )
                    return False

        except Exception as e:
            logger.error(f"[TimelineLogger] Error en set_sofia_active: {e}")
            return False

    async def log_client_message(
        self,
        contact_id: str,
        content: str,
        session_id: Optional[str] = None
    ) -> bool:
        """
        Registra un mensaje del cliente.

        Args:
            contact_id: ID del contacto en HubSpot (vid)
            content: Contenido del mensaje
            session_id: ID de sesión de WhatsApp

        Returns:
            True si se registró exitosamente
        """
        event = TimelineEvent(
            contact_id=contact_id,
            content=content,
            sender=MessageSender.CLIENT,
            direction=MessageDirection.INBOUND,
            session_id=session_id
        )
        return await self._create_timeline_event(event)

    async def log_bot_message(
        self,
        contact_id: str,
        content: str,
        session_id: Optional[str] = None
    ) -> bool:
        """
        Registra un mensaje de Sofía (bot).

        Args:
            contact_id: ID del contacto en HubSpot (vid)
            content: Contenido del mensaje
            session_id: ID de sesión de WhatsApp

        Returns:
            True si se registró exitosamente
        """
        event = TimelineEvent(
            contact_id=contact_id,
            content=content,
            sender=MessageSender.BOT,
            direction=MessageDirection.OUTBOUND,
            session_id=session_id
        )
        return await self._create_timeline_event(event)

    async def log_advisor_message(
        self,
        contact_id: str,
        content: str,
        session_id: Optional[str] = None
    ) -> bool:
        """
        Registra un mensaje del asesor humano.

        Args:
            contact_id: ID del contacto en HubSpot (vid)
            content: Contenido del mensaje
            session_id: ID de sesión de WhatsApp

        Returns:
            True si se registró exitosamente
        """
        event = TimelineEvent(
            contact_id=contact_id,
            content=content,
            sender=MessageSender.ADVISOR,
            direction=MessageDirection.OUTBOUND,
            session_id=session_id
        )
        return await self._create_timeline_event(event)

    async def log_message(
        self,
        contact_id: str,
        content: str,
        sender: str,
        direction: str,
        session_id: Optional[str] = None
    ) -> bool:
        """
        Método genérico para registrar cualquier mensaje.

        Args:
            contact_id: ID del contacto
            content: Contenido del mensaje
            sender: "client", "bot", o "advisor"
            direction: "inbound" o "outbound"
            session_id: ID de sesión

        Returns:
            True si se registró exitosamente
        """
        sender_enum = MessageSender(sender)
        direction_enum = MessageDirection(direction)

        event = TimelineEvent(
            contact_id=contact_id,
            content=content,
            sender=sender_enum,
            direction=direction_enum,
            session_id=session_id
        )
        return await self._create_timeline_event(event)

    # =========================================================================
    # Métodos para procesamiento en background (no bloquean la respuesta)
    # =========================================================================

    def queue_event(self, event: TimelineEvent) -> None:
        """
        Agrega un evento a la cola para procesamiento en background.

        Args:
            event: Evento a encolar
        """
        try:
            self._event_queue.put_nowait(event)
            logger.debug(f"[TimelineLogger] Evento encolado: {event.sender.value}")
        except asyncio.QueueFull:
            logger.warning("[TimelineLogger] Cola llena, evento descartado")

    def queue_client_message(
        self,
        contact_id: str,
        content: str,
        session_id: Optional[str] = None
    ) -> None:
        """Encola un mensaje del cliente para registro en background."""
        event = TimelineEvent(
            contact_id=contact_id,
            content=content,
            sender=MessageSender.CLIENT,
            direction=MessageDirection.INBOUND,
            session_id=session_id
        )
        self.queue_event(event)

    def queue_bot_message(
        self,
        contact_id: str,
        content: str,
        session_id: Optional[str] = None
    ) -> None:
        """Encola un mensaje de Sofía para registro en background."""
        event = TimelineEvent(
            contact_id=contact_id,
            content=content,
            sender=MessageSender.BOT,
            direction=MessageDirection.OUTBOUND,
            session_id=session_id
        )
        self.queue_event(event)

    async def process_queue(self) -> int:
        """
        Procesa todos los eventos en la cola.

        Returns:
            Número de eventos procesados
        """
        processed = 0

        while not self._event_queue.empty():
            try:
                event = self._event_queue.get_nowait()
                await self._create_timeline_event(event)
                processed += 1
            except asyncio.QueueEmpty:
                break
            except Exception as e:
                logger.error(f"[TimelineLogger] Error procesando evento: {e}")

        return processed


# Instancia singleton
_timeline_logger: Optional[TimelineLogger] = None


def get_timeline_logger() -> TimelineLogger:
    """Obtiene la instancia singleton del TimelineLogger."""
    global _timeline_logger
    if _timeline_logger is None:
        _timeline_logger = TimelineLogger()
    return _timeline_logger