# middleware/conversation_state.py
"""
Gestor de Estado de Conversación para el Middleware.

Maneja los estados BOT_ACTIVE / HUMAN_ACTIVE que determinan
quién está atendiendo la conversación en cada momento.

OPTIMIZACIONES v2.0:
- Migración automática de llaves legacy a nuevo formato
- Índice de contactos activos con Redis Set (O(1) membership check)
- Idempotencia con version_id y updated_at
- Consistencia de zona horaria America/Bogota
"""

import json
import uuid
from enum import Enum
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict, field
from zoneinfo import ZoneInfo

import redis.asyncio as redis
from logging_config import logger


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTES DE ZONA HORARIA
# ═══════════════════════════════════════════════════════════════════════════════

TIMEZONE_BOGOTA = ZoneInfo("America/Bogota")


def get_bogota_now() -> datetime:
    """Retorna datetime actual en zona horaria de Bogotá."""
    return datetime.now(TIMEZONE_BOGOTA)


def get_bogota_now_iso() -> str:
    """Retorna datetime actual en formato ISO con zona horaria explícita."""
    return datetime.now(TIMEZONE_BOGOTA).isoformat()


def parse_datetime_safe(dt_string: str) -> datetime:
    """
    Parsea un string de datetime asegurando zona horaria.

    Maneja:
    - ISO format con 'Z': 2024-01-15T10:30:00Z
    - ISO format con offset: 2024-01-15T10:30:00-05:00
    - ISO format naive: 2024-01-15T10:30:00 (asume Bogotá)
    """
    if not dt_string:
        return get_bogota_now()

    # Reemplazar Z por UTC offset
    dt_string = dt_string.replace("Z", "+00:00")

    try:
        dt = datetime.fromisoformat(dt_string)
        if dt.tzinfo is None:
            # Naive datetime: asumir Bogotá
            dt = dt.replace(tzinfo=TIMEZONE_BOGOTA)
        return dt
    except (ValueError, TypeError):
        return get_bogota_now()


# ═══════════════════════════════════════════════════════════════════════════════
# ENUMS Y DATACLASSES
# ═══════════════════════════════════════════════════════════════════════════════

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
    """
    Metadata de una conversación.

    IDEMPOTENCIA v2.0:
    - version_id: UUID único que cambia con cada actualización
    - updated_at: Timestamp de última modificación (con timezone)
    """

    phone_normalized: str
    contact_id: Optional[str] = None
    status: ConversationStatus = ConversationStatus.BOT_ACTIVE
    last_activity: Optional[str] = None
    last_bot_message: Optional[str] = None
    last_human_message: Optional[str] = None
    handoff_reason: Optional[str] = None
    assigned_owner_id: Optional[str] = None
    canal_origen: Optional[str] = None
    display_name: Optional[str] = None
    message_count: int = 0
    created_at: Optional[str] = None

    # Campos para TTL diferenciado (cliente vs asesor)
    last_client_message_at: Optional[str] = None
    last_advisor_message_at: Optional[str] = None

    # NUEVOS: Campos para idempotencia
    version_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    updated_at: Optional[str] = None

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

        # Asegurar que version_id existe (migración de datos legacy)
        if "version_id" not in filtered_data or not filtered_data.get("version_id"):
            filtered_data["version_id"] = str(uuid.uuid4())

        return cls(**filtered_data)

    def bump_version(self) -> None:
        """Incrementa la versión y actualiza timestamp."""
        self.version_id = str(uuid.uuid4())
        self.updated_at = get_bogota_now_iso()


# ═══════════════════════════════════════════════════════════════════════════════
# CONVERSATION STATE MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class ConversationStateManager:
    """
    Gestor de estado de conversaciones usando Redis.

    OPTIMIZACIONES v2.0:
    - Migración automática de llaves legacy al acceder
    - Índice de contactos activos usando Redis Set (SADD/SREM)
    - Eliminación de r.keys() en bucles de alta frecuencia
    - Idempotencia con version_id

    SEGREGACIÓN POR CANAL:
    Las keys incluyen el canal de origen para evitar colisiones.
    Formato: conv_state:{phone}:{canal}
    """

    # Prefijos de keys en Redis
    STATE_PREFIX = "conv_state:"
    META_PREFIX = "conv_meta:"

    # NUEVO: Set de Redis para indexar contactos activos (O(1) lookup)
    ACTIVE_CONTACTS_SET = "active_conversations_index"

    # NUEVO: Prefijo para locks de migración
    MIGRATION_LOCK_PREFIX = "migration_lock:"

    # Canal por defecto para compatibilidad con keys legacy
    DEFAULT_CANAL = "default"

    # TTL por defecto: 7 días (en segundos)
    DEFAULT_TTL = 7 * 24 * 60 * 60

    # TTL para HUMAN_ACTIVE: 72 horas (auto-expiración)
    HANDOFF_TTL_SECONDS = 72 * 60 * 60

    def __init__(self, redis_url: str):
        """
        Inicializa el gestor de estado.

        Args:
            redis_url: URL de conexión a Redis
        """
        self.redis_url = redis_url
        self._redis: Optional[redis.Redis] = None

    # ═══════════════════════════════════════════════════════════════════════════
    # CONEXIÓN REDIS
    # ═══════════════════════════════════════════════════════════════════════════

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

    # ═══════════════════════════════════════════════════════════════════════════
    # HELPERS PARA KEYS CON CANAL
    # ═══════════════════════════════════════════════════════════════════════════

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

    def _parse_key(self, key: str, prefix: str) -> Tuple[str, str]:
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
            return parts[0], parts[1]
        else:
            # Key legacy sin canal
            return key_without_prefix, "legacy"

    def _build_index_member(self, phone: str, canal: Optional[str] = None) -> str:
        """Construye el miembro para el Set de índice."""
        canal_safe = canal or self.DEFAULT_CANAL
        return f"{phone}:{canal_safe}"

    # ═══════════════════════════════════════════════════════════════════════════
    # MIGRACIÓN AUTOMÁTICA DE LLAVES LEGACY
    # ═══════════════════════════════════════════════════════════════════════════

    async def _migrate_legacy_key(
        self,
        r: redis.Redis,
        phone: str,
        legacy_key: str,
        legacy_value: str,
        target_canal: str,
        prefix: str
    ) -> str:
        """
        Migra una llave legacy al nuevo formato con canal.

        Esta función es llamada automáticamente cuando se detecta una llave
        legacy durante el acceso. Implementa migración atómica con lock.

        Args:
            r: Conexión Redis
            phone: Teléfono normalizado
            legacy_key: Key en formato antiguo (sin canal)
            legacy_value: Valor actual de la key legacy
            target_canal: Canal destino para la migración
            prefix: Prefijo de la key (STATE_PREFIX o META_PREFIX)

        Returns:
            Nueva key con formato canal
        """
        # Lock para evitar migraciones concurrentes
        lock_key = f"{self.MIGRATION_LOCK_PREFIX}{phone}"
        lock_acquired = await r.set(lock_key, "migrating", nx=True, ex=5)

        if not lock_acquired:
            # Otra instancia está migrando, esperar y retornar
            logger.debug(f"[ConversationState] Migración en progreso para {phone}, esperando...")
            await asyncio.sleep(0.1)
            new_key = self._build_key(prefix, phone, target_canal)
            return new_key

        try:
            new_key = self._build_key(prefix, phone, target_canal)

            # Verificar que la nueva key no exista ya
            existing_value = await r.get(new_key)

            if existing_value is None:
                # Migrar: copiar valor a nueva key
                ttl = await r.ttl(legacy_key)
                if ttl > 0:
                    await r.set(new_key, legacy_value, ex=ttl)
                else:
                    await r.set(new_key, legacy_value, ex=self.DEFAULT_TTL)

                logger.info(
                    f"[ConversationState] ✅ Migración completada: {legacy_key} → {new_key}"
                )
            else:
                logger.debug(
                    f"[ConversationState] Nueva key ya existe, descartando legacy: {legacy_key}"
                )

            # Eliminar llave legacy
            await r.delete(legacy_key)

            # Actualizar índice de contactos activos si es estado no-BOT_ACTIVE
            if prefix == self.STATE_PREFIX and legacy_value in [
                ConversationStatus.HUMAN_ACTIVE.value,
                ConversationStatus.IN_CONVERSATION.value,
                ConversationStatus.PENDING_HANDOFF.value
            ]:
                index_member = self._build_index_member(phone, target_canal)
                await r.sadd(self.ACTIVE_CONTACTS_SET, index_member)

            return new_key

        finally:
            # Liberar lock
            await r.delete(lock_key)

    async def _get_key_with_fallback(
        self,
        prefix: str,
        phone: str,
        canal: Optional[str] = None
    ) -> Tuple[Optional[str], Optional[str], bool]:
        """
        Busca una key con fallback a formato legacy Y MIGRACIÓN AUTOMÁTICA.

        OPTIMIZACIÓN v2.0:
        - Si encuentra key legacy + key con canal → consolida automáticamente
        - Si encuentra solo key legacy → migra al nuevo formato
        - Elimina el uso de scan_iter para búsquedas específicas

        Args:
            prefix: Prefijo de la key
            phone: Teléfono normalizado
            canal: Canal de origen

        Returns:
            Tupla (key_encontrada, valor, es_legacy)
        """
        r = await self._get_redis()

        # 1. Si se especifica canal, buscar esa key específica primero
        if canal:
            new_key = self._build_key(prefix, phone, canal)
            value = await r.get(new_key)
            if value is not None:
                return new_key, value, False

        # 2. Buscar key legacy (formato antiguo sin canal)
        legacy_key = f"{prefix}{phone}"
        legacy_value = await r.get(legacy_key)

        if legacy_value is not None:
            # Determinar canal destino para migración
            target_canal = canal or "whatsapp_directo"  # Default a whatsapp_directo

            # Ejecutar migración automática
            import asyncio
            new_key = await self._migrate_legacy_key(
                r, phone, legacy_key, legacy_value, target_canal, prefix
            )

            return new_key, legacy_value, False  # Ya no es legacy después de migrar

        # 3. Si no se especificó canal, buscar el más restrictivo
        # OPTIMIZACIÓN: Usamos SCAN solo cuando es estrictamente necesario
        if not canal:
            # Construir pattern para buscar todas las variantes del teléfono
            # Solo ejecutar si realmente necesitamos buscar
            found_keys = []

            # Intentar canales conocidos primero (O(k) donde k = canales conocidos)
            known_canals = ["whatsapp_directo", "instagram", "finca_raiz", "default"]

            for known_canal in known_canals:
                test_key = self._build_key(prefix, phone, known_canal)
                test_value = await r.get(test_key)
                if test_value is not None:
                    found_keys.append((test_key, test_value, known_canal))

            if found_keys:
                # Prioridad de estados (más restrictivo primero)
                priority = {
                    "HUMAN_ACTIVE": 0,
                    "IN_CONVERSATION": 1,
                    "PENDING_HANDOFF": 2,
                    "BOT_ACTIVE": 3
                }

                # Ordenar por prioridad
                found_keys.sort(key=lambda x: priority.get(x[1], 99))
                most_restrictive = found_keys[0]

                # Si hay múltiples keys, consolidar (eliminar duplicados)
                if len(found_keys) > 1:
                    await self._consolidate_duplicate_keys(
                        r, phone, found_keys, prefix
                    )

                return most_restrictive[0], most_restrictive[1], False

        return None, None, False

    async def _consolidate_duplicate_keys(
        self,
        r: redis.Redis,
        phone: str,
        found_keys: List[Tuple[str, str, str]],
        prefix: str
    ) -> None:
        """
        Consolida llaves duplicadas manteniendo la más restrictiva.

        Args:
            r: Conexión Redis
            phone: Teléfono
            found_keys: Lista de (key, value, canal)
            prefix: Prefijo de las keys
        """
        if len(found_keys) <= 1:
            return

        # La primera es la más restrictiva (ya ordenada)
        keep_key, keep_value, keep_canal = found_keys[0]

        logger.info(
            f"[ConversationState] Consolidando {len(found_keys)} keys duplicadas para {phone}. "
            f"Manteniendo: {keep_canal} ({keep_value})"
        )

        # Si es metadata, necesitamos mergear información
        if prefix == self.META_PREFIX:
            # Obtener metadata de la key a mantener
            try:
                keep_meta = ConversationMeta.from_dict(json.loads(keep_value))
            except (json.JSONDecodeError, TypeError):
                keep_meta = None

            # Mergear información de las otras keys
            for key, value, canal in found_keys[1:]:
                try:
                    other_meta = ConversationMeta.from_dict(json.loads(value))

                    # Copiar campos que no existen en keep_meta
                    if keep_meta and other_meta:
                        if not keep_meta.contact_id and other_meta.contact_id:
                            keep_meta.contact_id = other_meta.contact_id
                        if not keep_meta.display_name and other_meta.display_name:
                            keep_meta.display_name = other_meta.display_name
                        if not keep_meta.assigned_owner_id and other_meta.assigned_owner_id:
                            keep_meta.assigned_owner_id = other_meta.assigned_owner_id

                except (json.JSONDecodeError, TypeError):
                    pass

                # Eliminar key duplicada
                await r.delete(key)

                # Remover del índice si corresponde
                index_member = self._build_index_member(phone, canal)
                await r.srem(self.ACTIVE_CONTACTS_SET, index_member)

            # Guardar metadata mergeada
            if keep_meta:
                keep_meta.bump_version()
                await r.set(keep_key, json.dumps(keep_meta.to_dict()))
        else:
            # Para state keys, solo eliminar las duplicadas
            for key, value, canal in found_keys[1:]:
                await r.delete(key)

                # Remover del índice
                index_member = self._build_index_member(phone, canal)
                await r.srem(self.ACTIVE_CONTACTS_SET, index_member)

    # ═══════════════════════════════════════════════════════════════════════════
    # GESTIÓN DE ESTADO
    # ═══════════════════════════════════════════════════════════════════════════

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
            return ConversationStatus.BOT_ACTIVE

        try:
            return ConversationStatus(status_str)
        except ValueError:
            logger.warning(
                f"[ConversationState] Estado inválido en Redis: {status_str}, usando BOT_ACTIVE"
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

        OPTIMIZACIÓN v2.0: Actualiza el índice de contactos activos.
        """
        r = await self._get_redis()
        key = self._build_key(self.STATE_PREFIX, phone_normalized, canal)

        await r.set(key, status.value, ex=ttl or self.DEFAULT_TTL)

        # Actualizar índice de contactos activos
        index_member = self._build_index_member(phone_normalized, canal)

        if status in [
            ConversationStatus.HUMAN_ACTIVE,
            ConversationStatus.IN_CONVERSATION,
            ConversationStatus.PENDING_HANDOFF
        ]:
            # Agregar al índice de activos
            await r.sadd(self.ACTIVE_CONTACTS_SET, index_member)
        else:
            # Remover del índice de activos
            await r.srem(self.ACTIVE_CONTACTS_SET, index_member)

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

    # ═══════════════════════════════════════════════════════════════════════════
    # GESTIÓN DE METADATA
    # ═══════════════════════════════════════════════════════════════════════════

    async def get_meta(
        self,
        phone_normalized: str,
        canal: Optional[str] = None
    ) -> Optional[ConversationMeta]:
        """
        Obtiene metadata de una conversación.
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

        IDEMPOTENCIA v2.0: Incrementa version_id automáticamente.
        """
        r = await self._get_redis()
        canal_to_use = canal or meta.canal_origen
        key = self._build_key(self.META_PREFIX, phone_normalized, canal_to_use)

        # Bump version para idempotencia
        meta.bump_version()

        data_str = json.dumps(meta.to_dict())
        await r.set(key, data_str, ex=ttl or self.DEFAULT_TTL)

        logger.debug(
            f"[ConversationState] Meta guardada: {phone_normalized}:{canal_to_use or 'default'} "
            f"(version: {meta.version_id[:8]}...)"
        )

    async def update_activity(
        self,
        phone_normalized: str,
        canal: Optional[str] = None
    ) -> None:
        """
        Actualiza timestamp de última actividad.
        CORREGIDO: Usa timezone de Bogotá.
        """
        meta = await self.get_meta(phone_normalized, canal)

        if meta is None:
            meta = ConversationMeta(
                phone_normalized=phone_normalized,
                canal_origen=canal,
                created_at=get_bogota_now_iso()
            )

        meta.last_activity = get_bogota_now_iso()
        meta.message_count += 1

        await self.set_meta(phone_normalized, meta, canal=canal)

    # ═══════════════════════════════════════════════════════════════════════════
    # OPERACIONES DE HANDOFF
    # ═══════════════════════════════════════════════════════════════════════════

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
        """
        await self.set_status(
            phone_normalized,
            ConversationStatus.PENDING_HANDOFF,
            canal=canal_origen
        )

        now_iso = get_bogota_now_iso()
        existing_meta = await self.get_meta(phone_normalized, canal_origen)

        if existing_meta:
            meta = existing_meta
        else:
            meta = ConversationMeta(
                phone_normalized=phone_normalized,
                created_at=now_iso
            )

        meta.status = ConversationStatus.PENDING_HANDOFF
        meta.handoff_reason = reason
        meta.last_activity = now_iso

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
        TTL de 72 horas - si el asesor no escribe, Sofía retoma automáticamente.
        """
        await self.set_status(
            phone_normalized,
            ConversationStatus.HUMAN_ACTIVE,
            ttl=self.HANDOFF_TTL_SECONDS,
            canal=canal_origen
        )

        now = get_bogota_now()
        now_iso = now.isoformat()
        expires_at = now + timedelta(seconds=self.HANDOFF_TTL_SECONDS)

        existing_meta = await self.get_meta(phone_normalized, canal_origen)

        if existing_meta:
            meta = existing_meta
        else:
            meta = ConversationMeta(
                phone_normalized=phone_normalized,
                created_at=now_iso
            )

        meta.status = ConversationStatus.HUMAN_ACTIVE
        meta.assigned_owner_id = owner_id
        meta.handoff_reason = reason
        meta.last_activity = now_iso

        if contact_id:
            meta.contact_id = contact_id
        if canal_origen:
            meta.canal_origen = canal_origen
        if display_name:
            meta.display_name = display_name

        await self.set_meta(
            phone_normalized, meta,
            ttl=self.HANDOFF_TTL_SECONDS,
            canal=canal_origen
        )

        logger.info(
            f"[ConversationState] Humano activado: {phone_normalized}:{canal_origen or 'default'} "
            f"(owner: {owner_id or 'sin asignar'}, expira: {expires_at.strftime('%H:%M:%S')})"
        )

    async def activate_bot(
        self,
        phone_normalized: str,
        canal: Optional[str] = None
    ) -> None:
        """
        Reactiva el bot (después de que humano termina).
        """
        await self.set_status(phone_normalized, ConversationStatus.BOT_ACTIVE, canal=canal)

        meta = await self.get_meta(phone_normalized, canal) or ConversationMeta(
            phone_normalized=phone_normalized,
            created_at=get_bogota_now_iso()
        )
        meta.status = ConversationStatus.BOT_ACTIVE
        meta.handoff_reason = None
        meta.last_activity = get_bogota_now_iso()

        await self.set_meta(phone_normalized, meta, canal=canal)

        logger.info(f"[ConversationState] Bot reactivado: {phone_normalized}:{canal or 'default'}")

    async def refresh_human_ttl(
        self,
        phone_normalized: str,
        canal: Optional[str] = None
    ) -> bool:
        """
        Renueva el TTL del estado HUMAN_ACTIVE.
        Llamar cada vez que el asesor envía un mensaje.
        """
        r = await self._get_redis()

        state_key = self._build_key(self.STATE_PREFIX, phone_normalized, canal)
        meta_key = self._build_key(self.META_PREFIX, phone_normalized, canal)

        current_status = await r.get(state_key)

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
            meta.last_activity = get_bogota_now_iso()
            await self.set_meta(phone_normalized, meta, ttl=self.HANDOFF_TTL_SECONDS, canal=canal)

        new_expires = get_bogota_now() + timedelta(seconds=self.HANDOFF_TTL_SECONDS)
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
        """
        r = await self._get_redis()
        state_key = self._build_key(self.STATE_PREFIX, phone_normalized, canal)

        current_status = await r.get(state_key)

        if current_status != ConversationStatus.HUMAN_ACTIVE.value:
            return None

        ttl = await r.ttl(state_key)
        return ttl if ttl > 0 else None

    # ═══════════════════════════════════════════════════════════════════════════
    # TTL DIFERENCIADO (CLIENTE VS ASESOR)
    # ═══════════════════════════════════════════════════════════════════════════

    async def update_client_message_timestamp(
        self,
        phone_normalized: str,
        canal: Optional[str] = None
    ) -> None:
        """
        Actualiza timestamp del último mensaje del cliente.
        CORREGIDO: Usa timezone de Bogotá.
        """
        now_iso = get_bogota_now_iso()

        meta = await self.get_meta(phone_normalized, canal)
        if meta is None:
            meta = ConversationMeta(
                phone_normalized=phone_normalized,
                canal_origen=canal,
                created_at=now_iso
            )

        meta.last_client_message_at = now_iso
        meta.last_activity = now_iso
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
        CORREGIDO: Usa timezone de Bogotá.
        """
        now_iso = get_bogota_now_iso()

        meta = await self.get_meta(phone_normalized, canal)
        if meta is None:
            meta = ConversationMeta(
                phone_normalized=phone_normalized,
                canal_origen=canal,
                created_at=now_iso
            )

        meta.last_advisor_message_at = now_iso
        meta.last_activity = now_iso

        await self.set_meta(phone_normalized, meta, canal=canal)
        logger.debug(f"[ConversationState] Timestamp asesor actualizado: {phone_normalized}:{canal or 'default'}")

    async def check_conversation_timeout(
        self,
        phone_normalized: str,
        canal: Optional[str] = None
    ) -> Optional[str]:
        """
        Verifica si la conversación ha expirado y quién dejó de responder.
        CORREGIDO: Comparaciones consistentes con timezone de Bogotá.

        Returns:
            - "client_timeout": Cliente no respondió 24h
            - "advisor_timeout": Asesor no respondió 72h
            - None: Sin timeout
        """
        meta = await self.get_meta(phone_normalized, canal)
        if meta is None:
            return None

        status = await self.get_status(phone_normalized, canal)

        if status not in [ConversationStatus.HUMAN_ACTIVE, ConversationStatus.IN_CONVERSATION]:
            return None

        now = get_bogota_now()

        CLIENT_TIMEOUT_HOURS = 24
        ADVISOR_TIMEOUT_HOURS = 72

        advisor_time = None
        client_time = None

        if meta.last_advisor_message_at:
            advisor_time = parse_datetime_safe(meta.last_advisor_message_at)
            advisor_time = advisor_time.astimezone(TIMEZONE_BOGOTA)

        if meta.last_client_message_at:
            client_time = parse_datetime_safe(meta.last_client_message_at)
            client_time = client_time.astimezone(TIMEZONE_BOGOTA)

        # Verificar timeout de cliente (24h)
        if advisor_time and client_time:
            if advisor_time > client_time:
                hours_since_advisor = (now - advisor_time).total_seconds() / 3600
                if hours_since_advisor >= CLIENT_TIMEOUT_HOURS:
                    logger.info(
                        f"[ConversationState] Client timeout: {phone_normalized}:{canal or 'default'} "
                        f"({hours_since_advisor:.1f}h sin respuesta)"
                    )
                    return "client_timeout"

        # Verificar timeout de asesor (72h)
        if client_time:
            if advisor_time is None or client_time > advisor_time:
                hours_since_client = (now - client_time).total_seconds() / 3600
                if hours_since_client >= ADVISOR_TIMEOUT_HOURS:
                    logger.info(
                        f"[ConversationState] Advisor timeout: {phone_normalized}:{canal or 'default'} "
                        f"({hours_since_client:.1f}h sin respuesta)"
                    )
                    return "advisor_timeout"

        return None

    # ═══════════════════════════════════════════════════════════════════════════
    # UTILIDADES
    # ═══════════════════════════════════════════════════════════════════════════

    async def delete_conversation(
        self,
        phone_normalized: str,
        canal: Optional[str] = None
    ) -> None:
        """
        Elimina todos los datos de una conversación.
        """
        r = await self._get_redis()

        state_key = self._build_key(self.STATE_PREFIX, phone_normalized, canal)
        meta_key = self._build_key(self.META_PREFIX, phone_normalized, canal)
        await r.delete(state_key, meta_key)

        # Remover del índice de activos
        index_member = self._build_index_member(phone_normalized, canal)
        await r.srem(self.ACTIVE_CONTACTS_SET, index_member)

        # También eliminar key legacy si existe
        if canal:
            legacy_state = f"{self.STATE_PREFIX}{phone_normalized}"
            legacy_meta = f"{self.META_PREFIX}{phone_normalized}"
            await r.delete(legacy_state, legacy_meta)

        logger.info(f"[ConversationState] Conversación eliminada: {phone_normalized}:{canal or 'default'}")

    async def cleanup_duplicate_states(
        self,
        phone_normalized: str,
        keep_canal: Optional[str] = None
    ) -> int:
        """
        Limpia estados duplicados para un mismo teléfono.
        """
        r = await self._get_redis()

        # Buscar todas las variantes conocidas
        all_keys = []
        known_canals = ["whatsapp_directo", "instagram", "finca_raiz", "default"]

        for canal in known_canals:
            state_key = self._build_key(self.STATE_PREFIX, phone_normalized, canal)
            value = await r.get(state_key)
            if value is not None:
                all_keys.append((state_key, value, canal))

        # También verificar key legacy
        legacy_key = f"{self.STATE_PREFIX}{phone_normalized}"
        legacy_value = await r.get(legacy_key)
        if legacy_value is not None:
            all_keys.append((legacy_key, legacy_value, "legacy"))

        if len(all_keys) <= 1:
            return 0

        logger.info(
            f"[ConversationState] Encontrados {len(all_keys)} estados duplicados "
            f"para {phone_normalized}: {[(k[2], k[1]) for k in all_keys]}"
        )

        if keep_canal:
            to_keep = [k for k in all_keys if k[2] == keep_canal]
            to_delete = [k for k in all_keys if k[2] != keep_canal]
        else:
            priority = {
                "HUMAN_ACTIVE": 0,
                "IN_CONVERSATION": 1,
                "PENDING_HANDOFF": 2,
                "BOT_ACTIVE": 3
            }
            all_keys.sort(key=lambda x: priority.get(x[1], 99))
            to_keep = [all_keys[0]]
            to_delete = all_keys[1:]

        deleted_count = 0
        for key, value, canal in to_delete:
            meta_key = key.replace(self.STATE_PREFIX, self.META_PREFIX)
            await r.delete(key, meta_key)

            # Remover del índice
            if canal != "legacy":
                index_member = self._build_index_member(phone_normalized, canal)
                await r.srem(self.ACTIVE_CONTACTS_SET, index_member)

            deleted_count += 1
            logger.info(f"[ConversationState] Eliminado duplicado: {key} ({value})")

        if to_keep:
            logger.info(f"[ConversationState] Estado conservado: {to_keep[0][0]} ({to_keep[0][1]})")

        return deleted_count

    async def get_all_human_active_contacts(self) -> list:
        """
        Obtiene todos los contactos actualmente en estado HUMAN_ACTIVE.

        OPTIMIZACIÓN v2.0: Usa el índice ACTIVE_CONTACTS_SET para O(n)
        donde n = contactos activos, NO todos los contactos en Redis.
        """
        r = await self._get_redis()
        contacts = []

        try:
            # OPTIMIZACIÓN: Usar el Set de índice en lugar de SCAN
            active_members = await r.smembers(self.ACTIVE_CONTACTS_SET)

            logger.info(f"[ConversationState] Contactos en índice activo: {len(active_members)}")

            for member in active_members:
                # member formato: phone:canal
                parts = member.rsplit(":", 1)
                if len(parts) != 2:
                    continue

                phone, canal = parts

                # Obtener estado actual
                state_key = self._build_key(self.STATE_PREFIX, phone, canal)
                status = await r.get(state_key)

                if status is None:
                    # Key expiró, limpiar del índice
                    await r.srem(self.ACTIVE_CONTACTS_SET, member)
                    continue

                # Verificar que sigue siendo un estado activo
                if status not in [
                    ConversationStatus.HUMAN_ACTIVE.value,
                    ConversationStatus.IN_CONVERSATION.value,
                    ConversationStatus.PENDING_HANDOFF.value
                ]:
                    # Estado cambió, limpiar del índice
                    await r.srem(self.ACTIVE_CONTACTS_SET, member)
                    continue

                # Obtener metadata
                meta = await self.get_meta(phone, canal)

                # Obtener TTL restante
                ttl = await r.ttl(state_key)

                contact_info = {
                    "phone": phone,
                    "contact_id": meta.contact_id if meta else None,
                    "status": status,
                    "display_name": meta.display_name if meta else None,
                    "handoff_reason": meta.handoff_reason if meta else None,
                    "activated_at": meta.created_at if meta else None,
                    "last_activity": meta.last_activity if meta else None,
                    "ttl_remaining": ttl if ttl > 0 else None,
                    "is_active": True,
                    "owner_id": meta.assigned_owner_id if meta else None,
                    "canal_origen": canal,
                    "unique_id": f"{phone}:{canal}",
                    "version_id": meta.version_id if meta else None,
                }

                contacts.append(contact_info)

            logger.debug(f"[ConversationState] Retornando {len(contacts)} contactos activos")

        except Exception as e:
            logger.error(f"[ConversationState] Error obteniendo contactos activos: {e}")

        return contacts


# Necesario para la migración
import asyncio