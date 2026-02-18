# middleware/conversation_state.py
"""
Gestor de Estado de Conversación para el Middleware.

Maneja los estados BOT_ACTIVE / HUMAN_ACTIVE que determinan
quién está atendiendo la conversación en cada momento.
"""

import json
from enum import Enum
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict

import redis.asyncio as redis
from logging_config import logger


class ConversationStatus(str, Enum):
    """Estados posibles de una conversación."""

    # Bot activo - Sofía maneja la conversación
    BOT_ACTIVE = "BOT_ACTIVE"

    # Humano activo - Asesor tomó el control desde HubSpot
    HUMAN_ACTIVE = "HUMAN_ACTIVE"

    # Pendiente de handoff - Bot solicitó transferencia
    PENDING_HANDOFF = "PENDING_HANDOFF"

    # Conversación cerrada/inactiva
    CLOSED = "CLOSED"


@dataclass
class ConversationMeta:
    """Metadata de una conversación."""

    phone_normalized: str
    contact_id: Optional[str] = None
    status: ConversationStatus = ConversationStatus.BOT_ACTIVE
    last_activity: Optional[str] = None
    last_bot_message: Optional[str] = None
    last_human_message: Optional[str] = None
    handoff_reason: Optional[str] = None
    assigned_owner_id: Optional[str] = None
    message_count: int = 0
    created_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convierte a diccionario para serialización."""
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConversationMeta":
        """Crea instancia desde diccionario."""
        if "status" in data and isinstance(data["status"], str):
            data["status"] = ConversationStatus(data["status"])
        return cls(**data)


class ConversationStateManager:
    """
    Gestor de estado de conversaciones usando Redis.

    Responsabilidades:
    - Mantener estado BOT_ACTIVE / HUMAN_ACTIVE por conversación
    - Gestionar metadata de conversaciones
    - Proveer TTL para limpiar conversaciones inactivas
    """

    # Prefijos de keys en Redis
    STATE_PREFIX = "conv_state:"
    META_PREFIX = "conv_meta:"

    # TTL por defecto: 7 días (en segundos)
    DEFAULT_TTL = 7 * 24 * 60 * 60

    # TTL para HUMAN_ACTIVE: 2 horas (auto-expiración)
    # Si el asesor no escribe en 2h, Sofía retoma automáticamente
    HANDOFF_TTL_SECONDS = 2 * 60 * 60  # 7200 segundos = 2 horas

    def __init__(self, redis_url: str):
        """
        Inicializa el gestor de estado.

        Args:
            redis_url: URL de conexión a Redis
        """
        self.redis_url = redis_url
        self._redis: Optional[redis.Redis] = None

    async def _get_redis(self) -> redis.Redis:
        """Lazy initialization de conexión Redis."""
        if self._redis is None:
            self._redis = redis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True
            )
        return self._redis

    async def close(self):
        """Cierra la conexión Redis."""
        if self._redis:
            await self._redis.close()
            self._redis = None

    # ==================== Gestión de Estado ====================

    async def get_status(self, phone_normalized: str) -> ConversationStatus:
        """
        Obtiene el estado actual de una conversación.

        Args:
            phone_normalized: Número en formato E.164

        Returns:
            Estado actual (BOT_ACTIVE por defecto si no existe)
        """
        r = await self._get_redis()
        key = f"{self.STATE_PREFIX}{phone_normalized}"

        status_str = await r.get(key)

        if status_str is None:
            # Nueva conversación → BOT_ACTIVE por defecto
            return ConversationStatus.BOT_ACTIVE

        try:
            return ConversationStatus(status_str)
        except ValueError:
            logger.warning(
                f"[ConversationState] Estado inválido en Redis: {status_str}, "
                f"usando BOT_ACTIVE"
            )
            return ConversationStatus.BOT_ACTIVE

    async def set_status(
        self,
        phone_normalized: str,
        status: ConversationStatus,
        ttl: Optional[int] = None
    ) -> None:
        """
        Establece el estado de una conversación.

        Args:
            phone_normalized: Número en formato E.164
            status: Nuevo estado
            ttl: Tiempo de vida en segundos (opcional)
        """
        r = await self._get_redis()
        key = f"{self.STATE_PREFIX}{phone_normalized}"

        await r.set(key, status.value, ex=ttl or self.DEFAULT_TTL)

        logger.info(
            f"[ConversationState] Estado actualizado: {phone_normalized} → {status.value}"
        )

    async def is_bot_active(self, phone_normalized: str) -> bool:
        """
        Verifica si el bot debe responder.

        Returns:
            True si BOT_ACTIVE, False en cualquier otro caso
        """
        status = await self.get_status(phone_normalized)
        return status == ConversationStatus.BOT_ACTIVE

    async def is_human_active(self, phone_normalized: str) -> bool:
        """
        Verifica si un humano está atendiendo.

        Returns:
            True si HUMAN_ACTIVE, False en cualquier otro caso
        """
        status = await self.get_status(phone_normalized)
        return status == ConversationStatus.HUMAN_ACTIVE

    # ==================== Gestión de Metadata ====================

    async def get_meta(self, phone_normalized: str) -> Optional[ConversationMeta]:
        """
        Obtiene metadata de una conversación.

        Args:
            phone_normalized: Número en formato E.164

        Returns:
            ConversationMeta o None si no existe
        """
        r = await self._get_redis()
        key = f"{self.META_PREFIX}{phone_normalized}"

        data_str = await r.get(key)

        if data_str is None:
            return None

        try:
            data = json.loads(data_str)
            return ConversationMeta.from_dict(data)
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"[ConversationState] Error deserializando meta: {e}")
            return None

    async def set_meta(
        self,
        phone_normalized: str,
        meta: ConversationMeta,
        ttl: Optional[int] = None
    ) -> None:
        """
        Guarda metadata de una conversación.

        Args:
            phone_normalized: Número en formato E.164
            meta: Metadata a guardar
            ttl: Tiempo de vida en segundos (opcional)
        """
        r = await self._get_redis()
        key = f"{self.META_PREFIX}{phone_normalized}"

        data_str = json.dumps(meta.to_dict())
        await r.set(key, data_str, ex=ttl or self.DEFAULT_TTL)

        logger.debug(f"[ConversationState] Meta guardada para: {phone_normalized}")

    async def update_activity(self, phone_normalized: str) -> None:
        """
        Actualiza timestamp de última actividad.

        Args:
            phone_normalized: Número en formato E.164
        """
        meta = await self.get_meta(phone_normalized)

        if meta is None:
            meta = ConversationMeta(
                phone_normalized=phone_normalized,
                created_at=datetime.now().isoformat()
            )

        meta.last_activity = datetime.now().isoformat()
        meta.message_count += 1

        await self.set_meta(phone_normalized, meta)

    # ==================== Operaciones de Handoff ====================

    async def request_handoff(
        self,
        phone_normalized: str,
        reason: str = "Solicitud del cliente"
    ) -> None:
        """
        Solicita transferencia a un humano.

        Args:
            phone_normalized: Número en formato E.164
            reason: Razón del handoff
        """
        # Actualizar estado
        await self.set_status(phone_normalized, ConversationStatus.PENDING_HANDOFF)

        # Actualizar metadata
        meta = await self.get_meta(phone_normalized) or ConversationMeta(
            phone_normalized=phone_normalized
        )
        meta.status = ConversationStatus.PENDING_HANDOFF
        meta.handoff_reason = reason
        meta.last_activity = datetime.now().isoformat()

        await self.set_meta(phone_normalized, meta)

        logger.info(
            f"[ConversationState] Handoff solicitado: {phone_normalized} - {reason}"
        )

    async def activate_human(
        self,
        phone_normalized: str,
        owner_id: Optional[str] = None,
        reason: Optional[str] = None
    ) -> None:
        """
        Activa modo humano (asesor toma el control).

        El estado tiene TTL de 2 horas. Si el asesor no escribe
        en ese tiempo, Sofía retoma automáticamente.

        Args:
            phone_normalized: Número en formato E.164
            owner_id: ID del asesor en HubSpot (opcional)
            reason: Razón de la activación (opcional)
        """
        # Guardar estado con TTL de 2 horas
        await self.set_status(
            phone_normalized,
            ConversationStatus.HUMAN_ACTIVE,
            ttl=self.HANDOFF_TTL_SECONDS
        )

        # Calcular tiempo de expiración
        now = datetime.now()
        expires_at = now + timedelta(seconds=self.HANDOFF_TTL_SECONDS)

        meta = await self.get_meta(phone_normalized) or ConversationMeta(
            phone_normalized=phone_normalized
        )
        meta.status = ConversationStatus.HUMAN_ACTIVE
        meta.assigned_owner_id = owner_id
        meta.handoff_reason = reason
        meta.last_activity = now.isoformat()

        # Guardar metadata con mismo TTL
        await self.set_meta(phone_normalized, meta, ttl=self.HANDOFF_TTL_SECONDS)

        logger.info(
            f"[ConversationState] Humano activado: {phone_normalized} "
            f"(owner: {owner_id or 'sin asignar'}, "
            f"expira: {expires_at.strftime('%H:%M:%S')})"
        )

    async def activate_bot(self, phone_normalized: str) -> None:
        """
        Reactiva el bot (después de que humano termina).

        Args:
            phone_normalized: Número en formato E.164
        """
        await self.set_status(phone_normalized, ConversationStatus.BOT_ACTIVE)

        meta = await self.get_meta(phone_normalized) or ConversationMeta(
            phone_normalized=phone_normalized
        )
        meta.status = ConversationStatus.BOT_ACTIVE
        meta.handoff_reason = None
        meta.last_activity = datetime.now().isoformat()

        await self.set_meta(phone_normalized, meta)

        logger.info(f"[ConversationState] Bot reactivado: {phone_normalized}")

    async def refresh_human_ttl(self, phone_normalized: str) -> bool:
        """
        Renueva el TTL del estado HUMAN_ACTIVE.

        Llamar este método cada vez que el asesor envía un mensaje
        para mantener el control activo por 2 horas más.

        Args:
            phone_normalized: Número en formato E.164

        Returns:
            True si se renovó el TTL, False si no estaba en HUMAN_ACTIVE
        """
        r = await self._get_redis()
        state_key = f"{self.STATE_PREFIX}{phone_normalized}"
        meta_key = f"{self.META_PREFIX}{phone_normalized}"

        # Verificar estado actual
        current_status = await r.get(state_key)

        if current_status != ConversationStatus.HUMAN_ACTIVE.value:
            logger.debug(
                f"[ConversationState] No se renovó TTL: {phone_normalized} "
                f"no está en HUMAN_ACTIVE (estado: {current_status})"
            )
            return False

        # Renovar TTL en ambas keys
        await r.expire(state_key, self.HANDOFF_TTL_SECONDS)
        await r.expire(meta_key, self.HANDOFF_TTL_SECONDS)

        # Actualizar metadata con nueva actividad
        meta = await self.get_meta(phone_normalized)
        if meta:
            meta.last_activity = datetime.now().isoformat()
            await self.set_meta(phone_normalized, meta, ttl=self.HANDOFF_TTL_SECONDS)

        new_expires = datetime.now() + timedelta(seconds=self.HANDOFF_TTL_SECONDS)
        logger.info(
            f"[ConversationState] TTL renovado: {phone_normalized} "
            f"(nueva expiración: {new_expires.strftime('%H:%M:%S')})"
        )

        return True

    async def get_human_ttl_remaining(self, phone_normalized: str) -> Optional[int]:
        """
        Obtiene el tiempo restante del TTL de HUMAN_ACTIVE.

        Args:
            phone_normalized: Número en formato E.164

        Returns:
            Segundos restantes o None si no está en HUMAN_ACTIVE
        """
        r = await self._get_redis()
        state_key = f"{self.STATE_PREFIX}{phone_normalized}"

        # Verificar estado
        current_status = await r.get(state_key)
        if current_status != ConversationStatus.HUMAN_ACTIVE.value:
            return None

        # Obtener TTL restante
        ttl = await r.ttl(state_key)
        return ttl if ttl > 0 else None

    # ==================== Utilidades ====================

    async def delete_conversation(self, phone_normalized: str) -> None:
        """
        Elimina todos los datos de una conversación.

        Args:
            phone_normalized: Número en formato E.164
        """
        r = await self._get_redis()

        state_key = f"{self.STATE_PREFIX}{phone_normalized}"
        meta_key = f"{self.META_PREFIX}{phone_normalized}"

        await r.delete(state_key, meta_key)

        logger.info(f"[ConversationState] Conversación eliminada: {phone_normalized}")

    async def get_all_human_active_contacts(self) -> list:
        """
        Obtiene todos los contactos actualmente en estado HUMAN_ACTIVE.

        Escanea Redis buscando keys conv_state:* donde el valor es HUMAN_ACTIVE.
        Esto permite que el panel de asesores muestre automáticamente los
        contactos que necesitan atención humana.

        Returns:
            Lista de diccionarios con info de cada contacto:
            [
                {
                    "phone": "+573001234567",
                    "contact_id": "12345",
                    "status": "HUMAN_ACTIVE",
                    "display_name": None,  # Se enriquece después con HubSpot
                    "handoff_reason": "Cliente solicitó asesor",
                    "activated_at": "2024-01-20T10:30:00",
                    "ttl_remaining": 7200,
                    "is_active": True
                }
            ]
        """
        r = await self._get_redis()
        contacts = []

        # DEBUG: Log Redis URL being used
        logger.info(f"[ConversationState] Escaneando Redis: {self.redis_url}")

        try:
            # DEBUG: Verificar conexión
            await r.ping()
            logger.debug("[ConversationState] Ping a Redis exitoso")

            # DEBUG: Contar todas las keys con el patrón
            keys_found = []
            async for key in r.scan_iter(match=f"{self.STATE_PREFIX}*"):
                keys_found.append(key)

            logger.info(f"[ConversationState] Keys encontradas con patrón '{self.STATE_PREFIX}*': {len(keys_found)}")
            if keys_found:
                logger.debug(f"[ConversationState] Keys: {keys_found}")

            # Escanear todas las keys de estado usando SCAN (eficiente)
            async for key in r.scan_iter(match=f"{self.STATE_PREFIX}*"):
                status = await r.get(key)
                logger.debug(f"[ConversationState] Key: {key} -> Status: {status}")

                if status == ConversationStatus.HUMAN_ACTIVE.value:
                    logger.info(f"[ConversationState] ✅ Encontrado HUMAN_ACTIVE: {key}")
                    # Extraer teléfono del key
                    phone = key.replace(self.STATE_PREFIX, "")

                    # Obtener metadata
                    meta = await self.get_meta(phone)

                    # Obtener TTL restante
                    ttl = await r.ttl(key)

                    contact_info = {
                        "phone": phone,
                        "contact_id": meta.contact_id if meta else None,
                        "status": "HUMAN_ACTIVE",
                        "display_name": None,  # Se enriquece después con HubSpot
                        "handoff_reason": meta.handoff_reason if meta else None,
                        "activated_at": meta.last_activity if meta else None,
                        "ttl_remaining": ttl if ttl > 0 else None,
                        "is_active": True  # Flag para priorizar en UI
                    }

                    contacts.append(contact_info)

            logger.debug(
                f"[ConversationState] Encontrados {len(contacts)} contactos en HUMAN_ACTIVE"
            )

        except Exception as e:
            logger.error(f"[ConversationState] Error escaneando contactos activos: {e}")

        return contacts