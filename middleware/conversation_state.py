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

    # Humano activo - Cliente esperando que asesora atienda (badge verde "En espera")
    HUMAN_ACTIVE = "HUMAN_ACTIVE"

    # En conversación - Asesora está chateando activamente (badge azul "En conversación")
    IN_CONVERSATION = "IN_CONVERSATION"

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
    canal_origen: Optional[str] = None  # Canal de origen del lead (para filtro por asesora)
    display_name: Optional[str] = None  # Nombre para mostrar en el panel
    message_count: int = 0
    created_at: Optional[str] = None
    # Campos para TTL diferenciado (cliente vs asesor)
    last_client_message_at: Optional[str] = None   # Timestamp último mensaje del cliente
    last_advisor_message_at: Optional[str] = None  # Timestamp último mensaje del asesor

    def to_dict(self) -> Dict[str, Any]:
        """Convierte a diccionario para serialización."""
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConversationMeta":
        """Crea instancia desde diccionario."""
        if "status" in data and isinstance(data["status"], str):
            try:
                data["status"] = ConversationStatus(data["status"])
            except ValueError:
                data["status"] = ConversationStatus.BOT_ACTIVE
        # Filtrar campos desconocidos para compatibilidad con datos legacy
        import dataclasses
        valid_fields = {f.name for f in dataclasses.fields(cls)}
        filtered_data = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered_data)


class ConversationStateManager:
    """
    Gestor de estado de conversaciones usando Redis.

    Responsabilidades:
    - Mantener estado BOT_ACTIVE / HUMAN_ACTIVE por conversación
    - Gestionar metadata de conversaciones
    - Proveer TTL para limpiar conversaciones inactivas

    SEGREGACIÓN POR CANAL:
    Las keys ahora incluyen el canal de origen para evitar colisiones
    entre el mismo teléfono llegando desde diferentes portales.

    Formato de key: conv_state:{phone}:{canal}
    Ejemplo: conv_state:+573001234567:instagram
    """

    # Prefijos de keys en Redis
    STATE_PREFIX = "conv_state:"
    META_PREFIX = "conv_meta:"

    # Canal por defecto para compatibilidad con keys legacy
    DEFAULT_CANAL = "default"

    # TTL por defecto: 7 días (en segundos)
    DEFAULT_TTL = 7 * 24 * 60 * 60

    # TTL para HUMAN_ACTIVE: 72 horas (auto-expiración)
    # Si el asesor no escribe en 72h, Sofía retoma automáticamente
    HANDOFF_TTL_SECONDS = 72 * 60 * 60  # 259200 segundos = 72 horas (3 días)

    def __init__(self, redis_url: str):
        """
        Inicializa el gestor de estado.

        Args:
            redis_url: URL de conexión a Redis
        """
        self.redis_url = redis_url
        self._redis: Optional[redis.Redis] = None

    # ==================== Helpers para Keys con Canal ====================

    def _build_key(self, prefix: str, phone: str, canal: Optional[str] = None) -> str:
        """
        Construye una key de Redis con formato segregado por canal.

        Args:
            prefix: Prefijo de la key (STATE_PREFIX o META_PREFIX)
            phone: Número de teléfono normalizado
            canal: Canal de origen (instagram, finca_raiz, etc.)

        Returns:
            Key en formato: prefix:phone:canal
        """
        canal_safe = canal or self.DEFAULT_CANAL
        return f"{prefix}{phone}:{canal_safe}"

    def _parse_key(self, key: str, prefix: str) -> tuple:
        """
        Extrae phone y canal de una key de Redis.

        Args:
            key: Key completa (ej: conv_state:+573001234567:instagram)
            prefix: Prefijo a remover

        Returns:
            Tupla (phone, canal)
        """
        key_without_prefix = key.replace(prefix, "")
        parts = key_without_prefix.rsplit(":", 1)

        if len(parts) == 2:
            # Key nueva con canal: phone:canal
            return parts[0], parts[1]
        else:
            # Key legacy sin canal: solo phone
            return key_without_prefix, "legacy"

    async def _get_key_with_fallback(
        self,
        prefix: str,
        phone: str,
        canal: Optional[str] = None
    ) -> tuple:
        """
        Busca una key con fallback a formato legacy.

        Intenta primero el formato nuevo (con canal), luego el legacy (sin canal).

        Args:
            prefix: Prefijo de la key
            phone: Teléfono normalizado
            canal: Canal de origen

        Returns:
            Tupla (key_encontrada, valor, es_legacy)
        """
        r = await self._get_redis()

        # 1. Intentar key nueva con canal
        if canal:
            new_key = self._build_key(prefix, phone, canal)
            value = await r.get(new_key)
            if value is not None:
                return new_key, value, False

        # 2. Fallback a key legacy (sin canal)
        legacy_key = f"{prefix}{phone}"
        value = await r.get(legacy_key)
        if value is not None:
            return legacy_key, value, True

        return None, None, False

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

    async def get_status(
        self,
        phone_normalized: str,
        canal: Optional[str] = None
    ) -> ConversationStatus:
        """
        Obtiene el estado actual de una conversación.

        Args:
            phone_normalized: Número en formato E.164
            canal: Canal de origen (instagram, finca_raiz, etc.)

        Returns:
            ConversationStatus actual
        """
        key, status_str, is_legacy = await self._get_key_with_fallback(
            self.STATE_PREFIX, phone_normalized, canal
        )

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
        ttl: Optional[int] = None,
        canal: Optional[str] = None
    ) -> None:
        """
        Establece el estado de una conversación.

        Args:
            phone_normalized: Número en formato E.164
            status: Nuevo estado de la conversación
            ttl: Tiempo de expiración en segundos
            canal: Canal de origen (instagram, finca_raiz, etc.)
        """
        r = await self._get_redis()
        key = self._build_key(self.STATE_PREFIX, phone_normalized, canal)

        await r.set(key, status.value, ex=ttl or self.DEFAULT_TTL)

        logger.info(
            f"[ConversationState] Estado actualizado: {phone_normalized}:{canal or 'default'} → {status.value}"
        )

    async def is_bot_active(
        self,
        phone_normalized: str,
        canal: Optional[str] = None
    ) -> bool:
        """
        Verifica si el bot debe responder.

        Returns:
            True si BOT_ACTIVE, False en cualquier otro caso
        """
        status = await self.get_status(phone_normalized, canal)
        return status == ConversationStatus.BOT_ACTIVE

    async def is_human_active(
        self,
        phone_normalized: str,
        canal: Optional[str] = None
    ) -> bool:
        """
        Verifica si un humano está atendiendo.

        Returns:
            True si HUMAN_ACTIVE, False en cualquier otro caso
        """
        status = await self.get_status(phone_normalized, canal)
        return status == ConversationStatus.HUMAN_ACTIVE

    # ==================== Gestión de Metadata ====================

    async def get_meta(
        self,
        phone_normalized: str,
        canal: Optional[str] = None
    ) -> Optional[ConversationMeta]:
        """
        Obtiene metadata de una conversación.

        Args:
            phone_normalized: Número en formato E.164
            canal: Canal de origen (instagram, finca_raiz, etc.)

        Returns:
            ConversationMeta o None si no existe
        """
        key, data_str, is_legacy = await self._get_key_with_fallback(
            self.META_PREFIX, phone_normalized, canal
        )

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
        ttl: Optional[int] = None,
        canal: Optional[str] = None
    ) -> None:
        """
        Guarda metadata de una conversación.

        Args:
            phone_normalized: Número en formato E.164
            meta: Metadata a guardar
            ttl: Tiempo de expiración en segundos
            canal: Canal de origen (instagram, finca_raiz, etc.)
        """
        r = await self._get_redis()
        # Usar canal de la metadata si no se especifica
        canal_to_use = canal or meta.canal_origen
        key = self._build_key(self.META_PREFIX, phone_normalized, canal_to_use)

        data_str = json.dumps(meta.to_dict())
        await r.set(key, data_str, ex=ttl or self.DEFAULT_TTL)

        logger.debug(f"[ConversationState] Meta guardada para: {phone_normalized}:{canal_to_use or 'default'}")

    async def update_activity(
        self,
        phone_normalized: str,
        canal: Optional[str] = None
    ) -> None:
        """
        Actualiza timestamp de última actividad.

        Args:
            phone_normalized: Número en formato E.164
            canal: Canal de origen
        """
        meta = await self.get_meta(phone_normalized, canal)

        if meta is None:
            meta = ConversationMeta(
                phone_normalized=phone_normalized,
                canal_origen=canal,
                created_at=datetime.now().isoformat()
            )

        meta.last_activity = datetime.now().isoformat()
        meta.message_count += 1

        await self.set_meta(phone_normalized, meta, canal=canal)

    # ==================== Operaciones de Handoff ====================

    async def request_handoff(
        self,
        phone_normalized: str,
        reason: str = "Solicitud del cliente",
        contact_id: Optional[str] = None,
        canal_origen: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> None:
        """
        Solicita transferencia a un humano.

        Args:
            phone_normalized: Número en formato E.164
            reason: Razón del handoff
            contact_id: ID del contacto en HubSpot
            canal_origen: Canal de origen (usado para segregación)
            display_name: Nombre para mostrar
        """
        # Actualizar estado CON CANAL
        await self.set_status(
            phone_normalized,
            ConversationStatus.PENDING_HANDOFF,
            canal=canal_origen
        )

        # Actualizar metadata CON CANAL
        now = datetime.now()
        existing_meta = await self.get_meta(phone_normalized, canal_origen)
        if existing_meta:
            meta = existing_meta
        else:
            # FIX: Establecer created_at al crear nueva metadata
            meta = ConversationMeta(
                phone_normalized=phone_normalized,
                created_at=now.isoformat()
            )
        meta.status = ConversationStatus.PENDING_HANDOFF
        meta.handoff_reason = reason
        meta.last_activity = now.isoformat()

        # Campos para filtrado por asesora
        if contact_id:
            meta.contact_id = contact_id
        if canal_origen:
            meta.canal_origen = canal_origen
        if display_name:
            meta.display_name = display_name

        await self.set_meta(phone_normalized, meta, canal=canal_origen)

        logger.info(
            f"[ConversationState] Handoff solicitado: {phone_normalized}:{canal_origen or 'default'} - {reason}"
        )

    async def activate_human(
        self,
        phone_normalized: str,
        owner_id: Optional[str] = None,
        reason: Optional[str] = None,
        contact_id: Optional[str] = None,
        canal_origen: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> None:
        """
        Activa modo humano (asesor toma el control).
        El estado tiene TTL de 72 horas. Si el asesor no escribe
        en ese tiempo, Sofía retoma automáticamente.

        Args:
            phone_normalized: Número en formato E.164
            owner_id: ID del asesor asignado
            reason: Razón del handoff
            contact_id: ID del contacto en HubSpot
            canal_origen: Canal de origen (usado para segregación)
            display_name: Nombre para mostrar
        """
        # Guardar estado con TTL y CANAL
        await self.set_status(
            phone_normalized,
            ConversationStatus.HUMAN_ACTIVE,
            ttl=self.HANDOFF_TTL_SECONDS,
            canal=canal_origen
        )

        # Calcular tiempo de expiración
        now = datetime.now()
        expires_at = now + timedelta(seconds=self.HANDOFF_TTL_SECONDS)

        # FIX: Establecer created_at al crear nueva metadata
        existing_meta = await self.get_meta(phone_normalized, canal_origen)
        if existing_meta:
            meta = existing_meta
        else:
            meta = ConversationMeta(
                phone_normalized=phone_normalized,
                created_at=now.isoformat()
            )
        meta.status = ConversationStatus.HUMAN_ACTIVE
        meta.assigned_owner_id = owner_id
        meta.handoff_reason = reason
        meta.last_activity = now.isoformat()

        # Campos para filtrado por asesora
        if contact_id:
            meta.contact_id = contact_id
        if canal_origen:
            meta.canal_origen = canal_origen
        if display_name:
            meta.display_name = display_name

        # Guardar metadata con mismo TTL y CANAL
        await self.set_meta(phone_normalized, meta, ttl=self.HANDOFF_TTL_SECONDS, canal=canal_origen)

        logger.info(
            f"[ConversationState] Humano activado: {phone_normalized}:{canal_origen or 'default'} "
            f"(owner: {owner_id or 'sin asignar'}, "
            f"expira: {expires_at.strftime('%H:%M:%S')})"
        )

    async def activate_bot(
        self,
        phone_normalized: str,
        canal: Optional[str] = None
    ) -> None:
        """
        Reactiva el bot (después de que humano termina).

        Args:
            phone_normalized: Número en formato E.164
            canal: Canal de origen
        """
        await self.set_status(phone_normalized, ConversationStatus.BOT_ACTIVE, canal=canal)

        meta = await self.get_meta(phone_normalized, canal) or ConversationMeta(
            phone_normalized=phone_normalized
        )
        meta.status = ConversationStatus.BOT_ACTIVE
        meta.handoff_reason = None
        meta.last_activity = datetime.now().isoformat()

        await self.set_meta(phone_normalized, meta, canal=canal)

        logger.info(f"[ConversationState] Bot reactivado: {phone_normalized}:{canal or 'default'}")

    async def refresh_human_ttl(
        self,
        phone_normalized: str,
        canal: Optional[str] = None
    ) -> bool:
        """
        Renueva el TTL del estado HUMAN_ACTIVE.

        Llamar este método cada vez que el asesor envía un mensaje
        para mantener el control activo por 72 horas más.

        Args:
            phone_normalized: Número en formato E.164
            canal: Canal de origen
        """
        r = await self._get_redis()

        # Construir keys con canal
        state_key = self._build_key(self.STATE_PREFIX, phone_normalized, canal)
        meta_key = self._build_key(self.META_PREFIX, phone_normalized, canal)

        # Verificar estado actual
        current_status = await r.get(state_key)

        # Si no existe con canal, intentar legacy
        if current_status is None and canal:
            legacy_state_key = f"{self.STATE_PREFIX}{phone_normalized}"
            current_status = await r.get(legacy_state_key)
            if current_status:
                state_key = legacy_state_key
                meta_key = f"{self.META_PREFIX}{phone_normalized}"

        if current_status != ConversationStatus.HUMAN_ACTIVE.value:
            logger.debug(
                f"[ConversationState] No se renovó TTL: {phone_normalized}:{canal} "
                f"no está en HUMAN_ACTIVE (estado: {current_status})"
            )
            return False

        # Renovar TTL en ambas keys
        await r.expire(state_key, self.HANDOFF_TTL_SECONDS)
        await r.expire(meta_key, self.HANDOFF_TTL_SECONDS)

        # Actualizar metadata con nueva actividad
        meta = await self.get_meta(phone_normalized, canal)
        if meta:
            meta.last_activity = datetime.now().isoformat()
            await self.set_meta(phone_normalized, meta, ttl=self.HANDOFF_TTL_SECONDS, canal=canal)

        new_expires = datetime.now() + timedelta(seconds=self.HANDOFF_TTL_SECONDS)
        logger.info(
            f"[ConversationState] TTL renovado: {phone_normalized}:{canal or 'default'} "
            f"(nueva expiración: {new_expires.strftime('%H:%M:%S')})"
        )

        return True

    async def get_human_ttl_remaining(
        self,
        phone_normalized: str,
        canal: Optional[str] = None
    ) -> Optional[int]:
        """
        Obtiene el tiempo restante del TTL de HUMAN_ACTIVE.

        Args:
            phone_normalized: Número en formato E.164
            canal: Canal de origen
        """
        r = await self._get_redis()
        state_key = self._build_key(self.STATE_PREFIX, phone_normalized, canal)

        # Verificar estado
        current_status = await r.get(state_key)

        # Fallback a legacy
        if current_status is None and canal:
            state_key = f"{self.STATE_PREFIX}{phone_normalized}"
            current_status = await r.get(state_key)

        if current_status != ConversationStatus.HUMAN_ACTIVE.value:
            return None

        # Obtener TTL restante
        ttl = await r.ttl(state_key)
        return ttl if ttl > 0 else None

    # ==================== TTL Diferenciado (Cliente vs Asesor) ====================

    async def update_client_message_timestamp(
        self,
        phone_normalized: str,
        canal: Optional[str] = None
    ) -> None:
        """
        Actualiza timestamp del último mensaje del cliente.
        Usado para calcular TTL de 24h si cliente deja de responder.

        Args:
            phone_normalized: Número en formato E.164
            canal: Canal de origen
        """
        from datetime import timezone as tz
        now = datetime.now(tz.utc)

        meta = await self.get_meta(phone_normalized, canal)
        if meta is None:
            meta = ConversationMeta(
                phone_normalized=phone_normalized,
                canal_origen=canal,
                created_at=now.isoformat()
            )

        meta.last_client_message_at = now.isoformat()
        meta.last_activity = datetime.now().isoformat()
        meta.message_count += 1

        await self.set_meta(phone_normalized, meta, canal=canal)
        logger.debug(f"[ConversationState] Timestamp cliente actualizado: {phone_normalized}:{canal or 'default'}")

    async def update_advisor_message_timestamp(
        self,
        phone_normalized: str,
        canal: Optional[str] = None
    ) -> None:
        """
        Actualiza timestamp del último mensaje del asesor.
        Usado para calcular TTL de 72h si asesor deja de responder.

        Args:
            phone_normalized: Número en formato E.164
            canal: Canal de origen
        """
        from datetime import timezone as tz
        now = datetime.now(tz.utc)

        meta = await self.get_meta(phone_normalized, canal)
        if meta is None:
            meta = ConversationMeta(
                phone_normalized=phone_normalized,
                canal_origen=canal,
                created_at=now.isoformat()
            )

        meta.last_advisor_message_at = now.isoformat()
        meta.last_activity = datetime.now().isoformat()

        await self.set_meta(phone_normalized, meta, canal=canal)
        logger.debug(f"[ConversationState] Timestamp asesor actualizado: {phone_normalized}:{canal or 'default'}")

    async def check_conversation_timeout(
        self,
        phone_normalized: str,
        canal: Optional[str] = None
    ) -> Optional[str]:
        """
        Verifica si la conversación ha expirado y quién dejó de responder.

        Reglas:
        - Si el ASESOR escribió último y el CLIENTE no responde en 24h:
          → Sofía retoma con contexto ("client_timeout")
        - Si el CLIENTE escribió último y el ASESOR no responde en 72h:
          → Sofía retoma ("advisor_timeout")

        Returns:
            - "client_timeout": Cliente no respondió 24h → Sofía retoma con contexto
            - "advisor_timeout": Asesor no respondió 72h → Sofía retoma
            - None: Sin timeout
        """
        from datetime import timezone as tz

        meta = await self.get_meta(phone_normalized, canal)
        if meta is None:
            return None

        status = await self.get_status(phone_normalized, canal)

        # Solo verificar timeouts si hay intervención humana activa
        if status not in [ConversationStatus.HUMAN_ACTIVE, ConversationStatus.IN_CONVERSATION]:
            return None

        now = datetime.now(tz.utc)

        # Constantes de timeout
        CLIENT_TIMEOUT_HOURS = 24   # Si cliente no responde en 24h
        ADVISOR_TIMEOUT_HOURS = 72  # Si asesor no responde en 72h

        # Parsear timestamps
        advisor_time = None
        client_time = None

        if meta.last_advisor_message_at:
            try:
                advisor_time = datetime.fromisoformat(
                    meta.last_advisor_message_at.replace("Z", "+00:00")
                )
                if advisor_time.tzinfo is None:
                    advisor_time = advisor_time.replace(tzinfo=tz.utc)
            except (ValueError, TypeError):
                pass

        if meta.last_client_message_at:
            try:
                client_time = datetime.fromisoformat(
                    meta.last_client_message_at.replace("Z", "+00:00")
                )
                if client_time.tzinfo is None:
                    client_time = client_time.replace(tzinfo=tz.utc)
            except (ValueError, TypeError):
                pass

        # Verificar timeout de cliente (24h)
        # Si el asesor escribió después del cliente y han pasado 24h sin respuesta del cliente
        if advisor_time and client_time:
            if advisor_time > client_time:
                hours_since_advisor = (now - advisor_time).total_seconds() / 3600
                if hours_since_advisor >= CLIENT_TIMEOUT_HOURS:
                    logger.info(
                        f"[ConversationState] Client timeout detectado: {phone_normalized}:{canal or 'default'} "
                        f"({hours_since_advisor:.1f}h sin respuesta del cliente)"
                    )
                    return "client_timeout"

        # Verificar timeout de asesor (72h)
        # Si el cliente escribió después del asesor (o el asesor nunca escribió) y han pasado 72h
        if client_time:
            # El cliente escribió y el asesor no ha respondido (o respondió hace mucho)
            if advisor_time is None or client_time > advisor_time:
                hours_since_client = (now - client_time).total_seconds() / 3600
                if hours_since_client >= ADVISOR_TIMEOUT_HOURS:
                    logger.info(
                        f"[ConversationState] Advisor timeout detectado: {phone_normalized}:{canal or 'default'} "
                        f"({hours_since_client:.1f}h sin respuesta del asesor)"
                    )
                    return "advisor_timeout"

        return None

    # ==================== Utilidades ====================

    async def delete_conversation(
        self,
        phone_normalized: str,
        canal: Optional[str] = None
    ) -> None:
        """
        Elimina todos los datos de una conversación.

        Args:
            phone_normalized: Número en formato E.164
            canal: Canal de origen (si None, elimina key legacy)
        """
        r = await self._get_redis()

        # Eliminar key con canal
        state_key = self._build_key(self.STATE_PREFIX, phone_normalized, canal)
        meta_key = self._build_key(self.META_PREFIX, phone_normalized, canal)
        await r.delete(state_key, meta_key)

        # También eliminar key legacy si existe (para limpieza completa)
        if canal:
            legacy_state = f"{self.STATE_PREFIX}{phone_normalized}"
            legacy_meta = f"{self.META_PREFIX}{phone_normalized}"
            await r.delete(legacy_state, legacy_meta)

        logger.info(f"[ConversationState] Conversación eliminada: {phone_normalized}:{canal or 'default'}")

    async def get_all_human_active_contacts(self) -> list:
        """
        Obtiene todos los contactos actualmente en estado HUMAN_ACTIVE.

        Escanea Redis buscando keys conv_state:* donde el valor es HUMAN_ACTIVE.
        Esto permite que el panel de asesores muestre automáticamente los
        contactos que necesitan atención humana.

        SEGREGACIÓN POR CANAL:
        Las keys ahora pueden tener formato:
        - Nuevo: conv_state:{phone}:{canal} (ej: conv_state:+573001234567:instagram)
        - Legacy: conv_state:{phone} (ej: conv_state:+573001234567)

        Los contactos se muestran como únicos por combinación phone+canal.
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

                # Incluir HUMAN_ACTIVE, IN_CONVERSATION y PENDING_HANDOFF (todos requieren atención)
                is_human_active = status == ConversationStatus.HUMAN_ACTIVE.value
                is_in_conversation = status == ConversationStatus.IN_CONVERSATION.value
                is_pending_handoff = status == ConversationStatus.PENDING_HANDOFF.value

                if is_human_active or is_in_conversation or is_pending_handoff:
                    if is_human_active:
                        status_label = "HUMAN_ACTIVE"
                    elif is_in_conversation:
                        status_label = "IN_CONVERSATION"
                    else:
                        status_label = "PENDING_HANDOFF"
                    logger.info(f"[ConversationState] Encontrado {status_label}: {key}")

                    # Extraer teléfono Y CANAL del key usando el helper
                    phone, canal = self._parse_key(key, self.STATE_PREFIX)

                    # Obtener metadata usando phone y canal
                    meta = await self.get_meta(phone, canal if canal != "legacy" else None)

                    # Obtener TTL restante
                    ttl = await r.ttl(key)

                    # El canal definitivo viene del key o de la metadata
                    canal_final = canal if canal != "legacy" else (meta.canal_origen if meta else None)

                    contact_info = {
                        "phone": phone,
                        "contact_id": meta.contact_id if meta else None,
                        "status": status_label,
                        "display_name": meta.display_name if meta else None,
                        "handoff_reason": meta.handoff_reason if meta else None,
                        # FIX: Usar created_at en lugar de last_activity para filtros por tiempo
                        # last_activity se actualiza con cada interacción, created_at es inmutable
                        "activated_at": meta.created_at if meta else None,
                        "last_activity": meta.last_activity if meta else None,  # Para ordenamiento
                        "ttl_remaining": ttl if ttl > 0 else None,
                        "is_active": True,
                        # Campos para filtrado por asesora
                        "owner_id": meta.assigned_owner_id if meta else None,
                        # CANAL: Ahora viene del key (segregación real)
                        "canal_origen": canal_final,
                        # ID único para la UI (phone:canal)
                        "unique_id": f"{phone}:{canal_final}" if canal_final else phone,
                    }

                    contacts.append(contact_info)

            logger.debug(
                f"[ConversationState] Encontrados {len(contacts)} contactos activos"
            )

        except Exception as e:
            logger.error(f"[ConversationState] Error escaneando contactos activos: {e}")

        return contacts