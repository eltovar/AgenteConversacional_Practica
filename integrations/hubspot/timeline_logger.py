# integrations/hubspot/timeline_logger.py
"""
Módulo para registrar conversaciones en el Timeline de HubSpot usando Notes API.

ARQUITECTURA v2.0:
================
MongoDB es ahora la fuente de verdad en TIEMPO REAL para el panel de asesoras.
HubSpot funciona como ARCHIVO HISTÓRICO (se actualiza en segundo plano).

Flujo actual:
1. Mensaje guardado en MongoDB (~5ms) - Panel ve inmediatamente
2. HubSpot se actualiza en BackgroundTask - No bloquea la UX
3. Cache TTL reducido (10s) - Solo para deduplicación de notas

Este módulo usa Notes API (Engagements) ya que Timeline Events API
requiere permisos especiales (403 bloqueado).

OPTIMIZACIONES:
- Idempotencia con external_id basado en MessageSid de Twilio
- Exponential backoff para errores 429 en creación de notas
- Caché Redis para evitar notas duplicadas
"""

import os
import asyncio
import json
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import httpx
import redis.asyncio as aioredis
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from logging_config import logger


# ============================================================================
# Rate Limiting Configuration
# ============================================================================

# HubSpot API limit: 100 requests per 10 seconds
# Using semaphore to limit concurrent requests
HUBSPOT_MAX_CONCURRENT_REQUESTS = 5  # Max concurrent requests
HUBSPOT_REQUEST_DELAY = 0.15  # Delay between requests (seconds)

# Cache TTL for associations
# IMPORTANTE: TTL reducido a 10 segundos para mejorar sincronización en panel
# Esto permite que los mensajes nuevos aparezcan más rápido al refrescar
ASSOCIATIONS_CACHE_TTL = 10  # seconds (reducido de 300s para mejor sincronización)

# Retry configuration for 429 errors
MAX_RETRIES_429 = 3
INITIAL_BACKOFF_429 = 2  # seconds

# Idempotencia: TTL para cache de notas procesadas (24 horas)
NOTE_IDEMPOTENCY_TTL = 86400  # seconds
NOTE_IDEMPOTENCY_PREFIX = "hs_note_processed:"


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
    """
    Representa un evento para registrar en el Timeline.

    IDEMPOTENCIA v2.0:
    - external_id: Identificador único del mensaje (MessageSid de Twilio)
    - Si external_id está presente, se verifica en Redis antes de crear la nota
    - Evita duplicados cuando llegan webhooks repetidos
    """
    contact_id: str
    content: str
    sender: MessageSender
    direction: MessageDirection
    session_id: Optional[str] = None
    timestamp: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    external_id: Optional[str] = None  # MessageSid de Twilio para idempotencia

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()


class TimelineLogger:
    """
    Logger de eventos para HubSpot Timeline.

    Incluye:
    - Rate limiting con semáforo para evitar 429
    - Caché Redis para asociaciones (5 min TTL)
    - Retry con backoff exponencial para errores 429
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

        # Rate limiting: semáforo para limitar requests concurrentes
        self._request_semaphore = asyncio.Semaphore(HUBSPOT_MAX_CONCURRENT_REQUESTS)

        # Redis para caché de asociaciones
        self._redis: Optional[aioredis.Redis] = None
        self._redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))

        logger.info("[TimelineLogger] Inicializado con Notes API (Rate Limiting + Cache)")

    async def _get_redis(self) -> aioredis.Redis:
        """Lazy initialization de conexión Redis para caché."""
        if self._redis is None:
            self._redis = aioredis.from_url(
                self._redis_url,
                encoding="utf-8",
                decode_responses=True
            )
        return self._redis

    async def _get_cached_associations(self, contact_id: str) -> Optional[List[str]]:
        """
        Obtiene asociaciones de caché Redis.

        Args:
            contact_id: ID del contacto en HubSpot

        Returns:
            Lista de note_ids o None si no está en caché
        """
        try:
            r = await self._get_redis()
            cache_key = f"hs_assoc:contact:{contact_id}:notes"
            cached = await r.get(cache_key)

            if cached:
                note_ids = json.loads(cached)
                logger.debug(f"[TimelineLogger] Cache HIT: {len(note_ids)} asociaciones para {contact_id}")
                return note_ids

        except Exception as e:
            logger.warning(f"[TimelineLogger] Error leyendo caché: {e}")

        return None

    async def _set_cached_associations(self, contact_id: str, note_ids: List[str]) -> None:
        """
        Guarda asociaciones en caché Redis con TTL de 10 segundos.

        Args:
            contact_id: ID del contacto en HubSpot
            note_ids: Lista de IDs de notas asociadas
        """
        try:
            r = await self._get_redis()
            cache_key = f"hs_assoc:contact:{contact_id}:notes"
            await r.set(cache_key, json.dumps(note_ids), ex=ASSOCIATIONS_CACHE_TTL)
            logger.debug(f"[TimelineLogger] Cache SET: {len(note_ids)} asociaciones para {contact_id}")

        except Exception as e:
            logger.warning(f"[TimelineLogger] Error guardando caché: {e}")

    async def invalidate_cache_for_contact(self, contact_id: str) -> bool:
        """
        Invalida el caché de asociaciones para un contacto específico.

        IMPORTANTE: Llamar este método después de crear una nota nueva
        para que el panel muestre el mensaje inmediatamente.

        Args:
            contact_id: ID del contacto en HubSpot

        Returns:
            True si se invalidó correctamente
        """
        try:
            r = await self._get_redis()
            cache_key = f"hs_assoc:contact:{contact_id}:notes"
            deleted = await r.delete(cache_key)

            if deleted:
                logger.info(f"[TimelineLogger] Cache INVALIDADO para contact_id={contact_id}")
            else:
                logger.debug(f"[TimelineLogger] Cache ya no existía para contact_id={contact_id}")

            return True

        except Exception as e:
            logger.warning(f"[TimelineLogger] Error invalidando caché: {e}")
            return False

    async def _rate_limited_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs
    ) -> httpx.Response:
        """
        Ejecuta una request con rate limiting y retry para 429.

        Args:
            client: Cliente httpx
            method: GET, POST, PATCH, etc.
            url: URL del endpoint
            **kwargs: Argumentos adicionales para la request

        Returns:
            Response de httpx
        """
        async with self._request_semaphore:
            # Pequeño delay entre requests para evitar ráfagas
            await asyncio.sleep(HUBSPOT_REQUEST_DELAY)

            for attempt in range(MAX_RETRIES_429):
                try:
                    if method.upper() == "GET":
                        response = await client.get(url, **kwargs)
                    elif method.upper() == "POST":
                        response = await client.post(url, **kwargs)
                    elif method.upper() == "PATCH":
                        response = await client.patch(url, **kwargs)
                    else:
                        raise ValueError(f"Método HTTP no soportado: {method}")

                    # Si es 429, aplicar backoff exponencial
                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After", INITIAL_BACKOFF_429)
                        try:
                            wait_time = int(retry_after)
                        except (ValueError, TypeError):
                            wait_time = INITIAL_BACKOFF_429 * (2 ** attempt)

                        logger.warning(
                            f"[TimelineLogger] Rate limit 429 - esperando {wait_time}s "
                            f"(intento {attempt + 1}/{MAX_RETRIES_429})"
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    return response

                except httpx.TimeoutException:
                    if attempt < MAX_RETRIES_429 - 1:
                        wait_time = INITIAL_BACKOFF_429 * (2 ** attempt)
                        logger.warning(f"[TimelineLogger] Timeout - reintentando en {wait_time}s")
                        await asyncio.sleep(wait_time)
                        continue
                    raise

            # Si llegamos aquí, agotamos los reintentos
            logger.error(f"[TimelineLogger] Agotados {MAX_RETRIES_429} reintentos para {url}")
            return response  # Retornar última respuesta (probablemente 429)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError))
    )
    async def _create_timeline_event(self, event: TimelineEvent) -> bool:
        """
        Crea una nota en el Timeline del contacto usando Notes API.
        """
        # Usar directamente Notes API (Timeline Events bloqueado por HubSpot 403)
        return await self._create_note(event)

    async def _is_note_already_processed(self, external_id: str) -> bool:
        """
        Verifica si una nota con este external_id ya fue procesada.

        IDEMPOTENCIA: Usa Redis para trackear MessageSids ya procesados.
        Evita crear notas duplicadas cuando llegan webhooks repetidos.

        Args:
            external_id: MessageSid de Twilio

        Returns:
            True si ya fue procesada, False si es nueva
        """
        if not external_id:
            return False

        try:
            r = await self._get_redis()
            cache_key = f"{NOTE_IDEMPOTENCY_PREFIX}{external_id}"
            exists = await r.exists(cache_key)

            if exists:
                logger.info(
                    f"[TimelineLogger] Nota ya procesada (idempotencia): {external_id}"
                )
                return True

            return False

        except Exception as e:
            logger.warning(f"[TimelineLogger] Error verificando idempotencia: {e}")
            # En caso de error, permitir crear (mejor duplicado que perdido)
            return False

    async def _mark_note_as_processed(self, external_id: str) -> None:
        """
        Marca un external_id como procesado en Redis.

        Args:
            external_id: MessageSid de Twilio
        """
        if not external_id:
            return

        try:
            r = await self._get_redis()
            cache_key = f"{NOTE_IDEMPOTENCY_PREFIX}{external_id}"
            await r.set(cache_key, "1", ex=NOTE_IDEMPOTENCY_TTL)
            logger.debug(f"[TimelineLogger] Nota marcada como procesada: {external_id}")

        except Exception as e:
            logger.warning(f"[TimelineLogger] Error marcando nota procesada: {e}")

    async def _create_note(self, event: TimelineEvent) -> bool:
        """
        Crea una nota en el contacto usando Notes API.

        OPTIMIZACIONES v2.0:
        - Idempotencia: Verifica external_id (MessageSid) antes de crear
        - Rate Limiting: Usa semáforo y backoff exponencial para 429
        - Caché: Marca notas procesadas en Redis (TTL 24h)

        Este es el método principal para registrar conversaciones en HubSpot,
        ya que Timeline Events API requiere permisos especiales (403 bloqueado).
        """
        # ═══════════════════════════════════════════════════════════════════
        # PASO 1: Verificar idempotencia con external_id
        # ═══════════════════════════════════════════════════════════════════
        if event.external_id:
            if await self._is_note_already_processed(event.external_id):
                logger.info(
                    f"[TimelineLogger] Nota duplicada ignorada: external_id={event.external_id}"
                )
                return True  # Retornar True porque ya se procesó exitosamente antes

        # ═══════════════════════════════════════════════════════════════════
        # PASO 2: Preparar payload de la nota
        # ═══════════════════════════════════════════════════════════════════
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

        # ═══════════════════════════════════════════════════════════════════
        # PASO 3: Crear nota con rate limiting y retry para 429
        # ═══════════════════════════════════════════════════════════════════
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await self._rate_limited_request(
                client, "POST", endpoint,
                headers=self.headers,
                json=payload
            )

            if response.status_code == 201:
                # Marcar como procesada para idempotencia futura
                if event.external_id:
                    await self._mark_note_as_processed(event.external_id)

                # IMPORTANTE: Invalidar caché para que el panel muestre el mensaje inmediatamente
                # Esto resuelve el problema de sincronización donde los mensajes no aparecían
                await self.invalidate_cache_for_contact(event.contact_id)

                logger.info(
                    f"[TimelineLogger] ✅ Nota creada: contact={event.contact_id}, "
                    f"sender={event.sender.value}, external_id={event.external_id or 'N/A'}"
                )
                return True

            else:
                logger.error(
                    f"[TimelineLogger] ❌ Error creando nota para contact={event.contact_id}: "
                    f"{response.status_code} - {response.text[:200]}"
                )
                return False

    async def is_sofia_active(self, contact_id: str) -> bool:
        """
        Verifica si Sofía está activa para un contacto específico.

        Consulta la propiedad 'sofia_activa' del contacto en HubSpot.
        Si la propiedad no existe o es 'true', Sofía responde.
        Si es 'false', el asesor humano tiene el control.
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
        session_id: Optional[str] = None,
        external_id: Optional[str] = None
    ) -> bool:
        """
        Registra un mensaje del cliente.

        Args:
            contact_id: ID del contacto en HubSpot
            content: Contenido del mensaje
            session_id: ID de sesión de WhatsApp
            external_id: MessageSid de Twilio (para idempotencia)

        Returns:
            True si se registró exitosamente
        """
        event = TimelineEvent(
            contact_id=contact_id,
            content=content,
            sender=MessageSender.CLIENT,
            direction=MessageDirection.INBOUND,
            session_id=session_id,
            external_id=external_id
        )
        return await self._create_timeline_event(event)

    async def log_bot_message(
        self,
        contact_id: str,
        content: str,
        session_id: Optional[str] = None,
        external_id: Optional[str] = None
    ) -> bool:
        """
        Registra un mensaje de Sofía (bot).

        Args:
            contact_id: ID del contacto en HubSpot (vid)
            content: Contenido del mensaje
            session_id: ID de sesión de WhatsApp
            external_id: MessageSid de Twilio (para idempotencia)

        Returns:
            True si se registró exitosamente
        """
        event = TimelineEvent(
            contact_id=contact_id,
            content=content,
            sender=MessageSender.BOT,
            direction=MessageDirection.OUTBOUND,
            session_id=session_id,
            external_id=external_id
        )
        return await self._create_timeline_event(event)

    async def log_advisor_message(
        self,
        contact_id: str,
        content: str,
        session_id: Optional[str] = None,
        external_id: Optional[str] = None
    ) -> bool:
        """
        Registra un mensaje del asesor humano.

        Args:
            contact_id: ID del contacto en HubSpot
            content: Contenido del mensaje
            session_id: ID de sesión de WhatsApp
            external_id: MessageSid de Twilio (para idempotencia)
        """
        event = TimelineEvent(
            contact_id=contact_id,
            content=content,
            sender=MessageSender.ADVISOR,
            direction=MessageDirection.OUTBOUND,
            session_id=session_id,
            external_id=external_id
        )
        return await self._create_timeline_event(event)

    async def log_message(
        self,
        contact_id: str,
        content: str,
        sender: str,
        direction: str,
        session_id: Optional[str] = None,
        external_id: Optional[str] = None
    ) -> bool:
        """
        Método genérico para registrar cualquier mensaje.

        Args:
            contact_id: ID del contacto en HubSpot
            content: Contenido del mensaje
            sender: Tipo de emisor ("client", "bot", "advisor")
            direction: Dirección ("inbound", "outbound")
            session_id: ID de sesión de WhatsApp
            external_id: MessageSid de Twilio (para idempotencia)
        """
        sender_enum = MessageSender(sender)
        direction_enum = MessageDirection(direction)

        event = TimelineEvent(
            contact_id=contact_id,
            content=content,
            sender=sender_enum,
            direction=direction_enum,
            session_id=session_id,
            external_id=external_id
        )
        return await self._create_timeline_event(event)

    # =========================================================================
    # Métodos para procesamiento en background (no bloquean la respuesta)
    # =========================================================================

    def queue_event(self, event: TimelineEvent) -> None:
        """
        Agrega un evento a la cola para procesamiento en background.
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
        session_id: Optional[str] = None,
        external_id: Optional[str] = None
    ) -> None:
        """
        Encola un mensaje del cliente para registro en background.

        Args:
            contact_id: ID del contacto en HubSpot
            content: Contenido del mensaje
            session_id: ID de sesión de WhatsApp
            external_id: MessageSid de Twilio (para idempotencia)
        """
        event = TimelineEvent(
            contact_id=contact_id,
            content=content,
            sender=MessageSender.CLIENT,
            direction=MessageDirection.INBOUND,
            session_id=session_id,
            external_id=external_id
        )
        self.queue_event(event)

    def queue_bot_message(
        self,
        contact_id: str,
        content: str,
        session_id: Optional[str] = None,
        external_id: Optional[str] = None
    ) -> None:
        """
        Encola un mensaje de Sofía para registro en background.

        Args:
            contact_id: ID del contacto en HubSpot
            content: Contenido del mensaje
            session_id: ID de sesión de WhatsApp
            external_id: MessageSid de Twilio (para idempotencia)
        """
        event = TimelineEvent(
            contact_id=contact_id,
            content=content,
            sender=MessageSender.BOT,
            direction=MessageDirection.OUTBOUND,
            session_id=session_id,
            external_id=external_id
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


    # =========================================================================
    # Métodos para consultar historial de conversación
    # =========================================================================

    async def get_notes_for_contact(
        self,
        contact_id: str,
        limit: int = 50,
        since: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """
        Obtiene las notas asociadas a un contacto.

        Incluye:
        - Caché de asociaciones (5 min TTL) para evitar requests repetidas
        - Rate limiting con semáforo para evitar 429
        - Retry con backoff exponencial si recibe 429
        """
        logger.info(f"[TimelineLogger] Buscando notas para contact_id={contact_id}, limit={limit}")

        try:
            # 1. Verificar caché de asociaciones primero
            note_ids = await self._get_cached_associations(contact_id)

            async with httpx.AsyncClient(timeout=15.0) as client:
                # Si no está en caché, obtener de HubSpot con rate limiting
                if note_ids is None:
                    assoc_endpoint = f"{self.base_url}/crm/v4/objects/contacts/{contact_id}/associations/notes"

                    logger.debug(f"[TimelineLogger] GET {assoc_endpoint} (con rate limiting)")

                    assoc_response = await self._rate_limited_request(
                        client,
                        "GET",
                        assoc_endpoint,
                        headers=self.headers,
                        params={"limit": limit}
                    )

                    logger.info(f"[TimelineLogger] Asociaciones response: {assoc_response.status_code}")

                    if assoc_response.status_code != 200:
                        logger.warning(
                            f"[TimelineLogger] Error obteniendo asociaciones: "
                            f"{assoc_response.status_code} - {assoc_response.text[:200]}"
                        )
                        return []

                    assoc_data = assoc_response.json()
                    note_ids = [
                        str(result["toObjectId"])
                        for result in assoc_data.get("results", [])
                    ]

                    # Guardar en caché para próximas consultas (incluso si está vacío)
                    # Esto evita hacer queries repetidas sin resultados
                    await self._set_cached_associations(contact_id, note_ids)

                logger.info(f"[TimelineLogger] Notas asociadas encontradas: {len(note_ids)}")

                if not note_ids:
                    logger.info(f"[TimelineLogger] No hay notas asociadas al contacto {contact_id}")
                    return []

                # 2. Obtener detalles de notas usando BATCH API (evita múltiples requests)
                # HubSpot permite hasta 100 IDs por batch request
                notes = []
                batch_size = 100
                note_ids_limited = note_ids[:limit]

                for i in range(0, len(note_ids_limited), batch_size):
                    batch_ids = note_ids_limited[i:i + batch_size]

                    batch_endpoint = f"{self.base_url}/crm/v3/objects/notes/batch/read"
                    batch_payload = {
                        "inputs": [{"id": str(nid)} for nid in batch_ids],
                        "properties": ["hs_note_body", "hs_timestamp"]
                    }

                    logger.debug(f"[TimelineLogger] Batch request para {len(batch_ids)} notas (con rate limiting)")

                    # Usar rate limiting también para batch
                    batch_response = await self._rate_limited_request(
                        client,
                        "POST",
                        batch_endpoint,
                        headers=self.headers,
                        json=batch_payload
                    )

                    if batch_response.status_code == 200:
                        batch_data = batch_response.json()

                        for result in batch_data.get("results", []):
                            props = result.get("properties", {})
                            note_id = result.get("id")

                            # Log del contenido de la nota para debug
                            body_preview = (props.get("hs_note_body", "") or "")[:100]
                            logger.debug(f"[TimelineLogger] Nota {note_id}: '{body_preview}...'")

                            # Filtrar por fecha si se especificó
                            if since and props.get("hs_timestamp"):
                                try:
                                    note_time = datetime.fromisoformat(
                                        props["hs_timestamp"].replace("Z", "+00:00")
                                    )
                                    if note_time < since:
                                        continue
                                except (ValueError, TypeError):
                                    pass

                            notes.append({
                                "id": note_id,
                                "body": props.get("hs_note_body", ""),
                                "timestamp": props.get("hs_timestamp"),
                                "created_at": result.get("createdAt")
                            })
                    else:
                        logger.warning(
                            f"[TimelineLogger] Error en batch request: "
                            f"{batch_response.status_code} - {batch_response.text[:200]}"
                        )

                logger.info(f"[TimelineLogger] Notas obtenidas con contenido: {len(notes)}")

                # 3. Formatear como burbujas de chat
                return self._format_notes_as_chat(notes)

        except Exception as e:
            logger.error(f"[TimelineLogger] Error obteniendo notas: {e}", exc_info=True)
            return []

    def _format_notes_as_chat(self, notes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Convierte notas de HubSpot a formato de burbujas de chat.

        ROBUSTEZ v2.1: Detección mejorada que no depende solo de emojis.
        Usa múltiples indicadores para identificar el sender correctamente.
        """
        bubbles = []

        logger.info(f"[TimelineLogger] Procesando {len(notes) if notes else 0} notas")

        for note in notes:
            try:
                # Validación de seguridad: obtener el cuerpo de la nota
                body = note.get("body") if note else None
                if not body:
                    logger.debug(f"[TimelineLogger] Nota sin body: {note.get('id')}")
                    continue

                # Limpiar HTML básico de HubSpot si existe
                body = body.replace('<p>', '').replace('</p>', '').replace('<br>', '\n').strip()

                timestamp = note.get("timestamp")

                # Detectar sender usando múltiples indicadores (orden de prioridad)
                sender, sender_name, align = self._detect_sender_from_body(body)

                # Limpiar prefijo y metadata del cuerpo
                clean_body = self._clean_note_body(body)

                # Agregar si hay contenido después de limpiar
                if clean_body and clean_body.strip():
                    bubbles.append({
                        "id": note.get("id"),
                        "sender": sender,
                        "sender_name": sender_name,
                        "message": clean_body,
                        "timestamp": timestamp,
                        "align": align
                    })

            except Exception as e:
                logger.warning(f"[TimelineLogger] Error formateando nota: {e}")
                continue

        logger.info(f"[TimelineLogger] Burbujas generadas: {len(bubbles)}")

        # Ordenar por timestamp (más antiguo primero)
        try:
            bubbles.sort(key=lambda x: x.get("timestamp") or "")
        except Exception as e:
            logger.warning(f"[TimelineLogger] Error ordenando burbujas: {e}")

        return bubbles

    def _detect_sender_from_body(self, body: str) -> tuple:
        """
        Detecta el sender de un mensaje basándose en múltiples indicadores.

        ROBUSTEZ v2.1: No depende solo de emojis, usa múltiples criterios.

        Returns:
            Tupla (sender, sender_name, align)
        """
        if not body:
            return ("unknown", "Desconocido", "left")

        # Obtener prefijo para análisis (primeros 100 chars para mayor contexto)
        body_lower = body[:100].lower() if len(body) >= 100 else body.lower()
        body_prefix = body[:50] if len(body) >= 50 else body

        # === CLIENTE (Mensaje entrante) ===
        # Indicadores: emoji 📱, texto "[cliente", "whatsapp", dirección ⬅️ con contexto cliente
        client_indicators = [
            "📱" in body_prefix,
            "[cliente" in body_lower,
            "cliente - whatsapp" in body_lower,
            "cliente]" in body_lower,
            # Dirección inbound con indicador de cliente
            ("⬅️" in body_prefix and "cliente" in body_lower),
        ]
        if any(client_indicators):
            return ("client", "Cliente", "left")

        # === SOFÍA / BOT (Respuesta automática) ===
        # Indicadores: emoji 🤖, texto "[sofía", "sofia", "[ia]", "bot"
        bot_indicators = [
            "🤖" in body_prefix,
            "[sofía" in body_lower,
            "sofía - ia" in body_lower,
            "[sofia" in body_lower,
            "sofia - ia" in body_lower,
            "[ia]" in body_lower,
            "- ia]" in body_lower,
            # Bot con dirección outbound
            ("➡️" in body_prefix and ("sofia" in body_lower or "bot" in body_lower)),
        ]
        if any(bot_indicators):
            return ("bot", "Sofía", "right")

        # === ASESOR (Mensaje manual) ===
        # Indicadores: emoji 👤, texto "[asesor", templates
        advisor_indicators = [
            "👤" in body_prefix,
            "[asesor" in body_lower,
            "asesor]" in body_lower,
            "[template:" in body_lower,
            "[template " in body_lower,
            "[panel" in body_lower,
            "manual via panel" in body_lower,
            "template via panel" in body_lower,
        ]
        if any(advisor_indicators):
            # Detectar si es template
            if "[template" in body_lower:
                return ("advisor", "Asesor (Template)", "right")
            return ("advisor", "Asesor", "right")

        # === SISTEMA (Notificaciones automáticas) ===
        system_indicators = [
            "[sistema" in body_lower,
            "[system" in body_lower,
            "handoff" in body_lower and ("activado" in body_lower or "transferido" in body_lower),
        ]
        if any(system_indicators):
            return ("system", "Sistema", "left")

        # === FALLBACK: Nota manual de HubSpot ===
        # Si no coincide con ningún patrón conocido, es una nota manual
        logger.debug(f"[TimelineLogger] Nota sin patrón reconocido: '{body[:60]}...'")
        return ("manual_note", "📝 Nota HubSpot", "left")

    def _clean_note_body(self, body: str) -> str:
        """
        Limpia el cuerpo de la nota removiendo prefijos y metadata.
        """
        if not body:
            return ""

        lines = body.split("\n")
        clean_lines = []

        for line in lines:
            # Saltar líneas de prefijo
            if any(emoji in line for emoji in ["📱", "🤖", "👤", "⬅️", "➡️"]):
                if len(line) < 30:  # Es línea de prefijo, no contenido
                    continue

            # Saltar separadores y timestamps
            if line.strip() == "---":
                continue
            if line.strip().startswith("📅"):
                continue

            clean_lines.append(line)

        # Unir y limpiar espacios extra
        result = "\n".join(clean_lines).strip()

        # Si el resultado empieza con prefijo, limpiarlo
        for prefix in ["[Sofía - IA]", "[Asesor]", "[Cliente - WhatsApp]"]:
            if result.startswith(prefix):
                result = result[len(prefix):].strip()

        return result

    async def get_contacts_with_advisor_activity(
        self,
        since: datetime,
        until: Optional[datetime] = None,
        limit: int = 50,
        after: Optional[str] = None  # Cursor para paginación
    ) -> Dict[str, Any]:
        """
        Busca contactos que han tenido interacción con asesor.

        Estrategia: Buscar notas con prefijo 👤 (asesor) y obtener
        los contactos asociados.

        Incluye rate limiting para evitar errores 429.
        """
        endpoint = f"{self.base_url}/crm/v3/objects/notes/search"

        since_ms = int(since.timestamp() * 1000)
        until_ms = int((until or datetime.utcnow()).timestamp() * 1000)

        # Cache Redis 5 min — los contactos históricos no cambian por segundo.
        # Redondeamos a cubos de 5 min para que requests cercanas compartan clave.
        _since_bucket = (since_ms // 300000) * 300000
        _until_bucket = (until_ms // 300000) * 300000
        _cache_key = f"timeline_activity:{_since_bucket}:{_until_bucket}:{limit}"
        try:
            _r = await self._get_redis()
            _cached = await _r.get(_cache_key)
            if _cached:
                import json as _json
                logger.debug(f"[TimelineLogger] get_contacts_with_advisor_activity cache HIT")
                return _json.loads(_cached)
        except Exception:
            pass

        payload = {
            "filterGroups": [{
                "filters": [
                    {
                        "propertyName": "hs_timestamp",
                        "operator": "GTE",
                        "value": str(since_ms)
                    },
                    {
                        "propertyName": "hs_timestamp",
                        "operator": "LTE",
                        "value": str(until_ms)
                    }
                ]
            }],
            "sorts": [
                {"propertyName": "hs_timestamp", "direction": "DESCENDING"}
            ],
            "properties": ["hs_note_body", "hs_timestamp"],
            "limit": 100
        }

        if after:
            payload["after"] = after

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                # Búsqueda con rate limiting
                response = await self._rate_limited_request(
                    client, "POST", endpoint,
                    headers=self.headers,
                    json=payload
                )

                if response.status_code != 200:
                    logger.warning(
                        f"[TimelineLogger] Error buscando notas: "
                        f"{response.status_code} - {response.text}"
                    )
                    return {"contacts": [], "paging": {"next_after": None}}

                data = response.json()
                notes = data.get("results", [])
                next_after = data.get("paging", {}).get("next", {}).get("after")

                # Filtrar notas de asesor (contienen 👤)
                advisor_note_ids = [
                    note["id"] for note in notes
                    if "👤" in note.get("properties", {}).get("hs_note_body", "")
                ]

                if not advisor_note_ids:
                    return {"contacts": [], "paging": {"next_after": next_after}}

                # Obtener contactos asociados con rate limiting
                contact_ids = set()
                for note_id in advisor_note_ids[:limit]:
                    assoc_endpoint = (
                        f"{self.base_url}/crm/v4/objects/notes/{note_id}/associations/contacts"
                    )
                    assoc_response = await self._rate_limited_request(
                        client, "GET", assoc_endpoint,
                        headers=self.headers
                    )

                    if assoc_response.status_code == 200:
                        assoc_data = assoc_response.json()
                        for result in assoc_data.get("results", []):
                            contact_ids.add(str(result["toObjectId"]))

                if not contact_ids:
                    return {"contacts": [], "paging": {"next_after": next_after}}

                # Obtener detalles de contactos usando BATCH API
                contacts = []
                contact_ids_list = list(contact_ids)[:limit]

                batch_endpoint = f"{self.base_url}/crm/v3/objects/contacts/batch/read"
                batch_payload = {
                    "inputs": [{"id": cid} for cid in contact_ids_list],
                    "properties": ["firstname", "lastname", "phone", "email"]
                }

                batch_response = await self._rate_limited_request(
                    client, "POST", batch_endpoint,
                    headers=self.headers,
                    json=batch_payload
                )

                if batch_response.status_code == 200:
                    batch_data = batch_response.json()
                    for result in batch_data.get("results", []):
                        props = result.get("properties", {})
                        contacts.append({
                            "id": result.get("id"),
                            "firstname": props.get("firstname", ""),
                            "lastname": props.get("lastname", ""),
                            "phone": props.get("phone", ""),
                            "email": props.get("email", ""),
                        })

                _result = {
                    "contacts": contacts,
                    "paging": {"next_after": next_after}
                }
                try:
                    import json as _json
                    _r = await self._get_redis()
                    await _r.set(_cache_key, _json.dumps(_result), ex=300)
                    logger.debug(f"[TimelineLogger] get_contacts_with_advisor_activity cache WRITE ({len(contacts)} contactos)")
                except Exception:
                    pass
                return _result

        except Exception as e:
            logger.error(f"[TimelineLogger] Error buscando contactos: {e}")
            return {"contacts": [], "paging": {"next_after": None}}


# Instancia singleton
_timeline_logger: Optional[TimelineLogger] = None


def get_timeline_logger() -> TimelineLogger:
    """Obtiene la instancia singleton del TimelineLogger."""
    global _timeline_logger
    if _timeline_logger is None:
        _timeline_logger = TimelineLogger()
    return _timeline_logger