# middleware/conversation_state.py
"""
Gestor de Estado de Conversación para el Middleware.
v2.3: Corrección de error de introspección en get_meta y limpieza de argumentos.
"""

import json
import uuid
import os
from enum import Enum
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from dataclasses import dataclass, field, fields
from zoneinfo import ZoneInfo

import redis.asyncio as redis
from logging_config import logger

# ═══════════════════════════════════════════════════════════════════════════════
# UTILIDADES DE TIEMPO
# ═══════════════════════════════════════════════════════════════════════════════

TIMEZONE_BOGOTA = ZoneInfo("America/Bogota")

def get_bogota_now() -> datetime:
    """Retorna datetime actual en zona horaria de Bogotá."""
    return datetime.now(TIMEZONE_BOGOTA)

def get_bogota_now_iso() -> str:
    """Retorna datetime actual en formato ISO."""
    return get_bogota_now().isoformat()


def parse_datetime_safe(date_str: str) -> datetime:
    """
    Parsea un string ISO datetime de forma segura.

    Maneja varios formatos:
    - ISO con timezone: 2026-02-25T10:30:00-05:00
    - ISO sin timezone: 2026-02-25T10:30:00
    - Solo fecha: 2026-02-25

    Siempre retorna datetime con timezone de Bogotá.
    """
    if not date_str:
        return get_bogota_now()

    try:
        # Intentar parsear ISO con timezone
        dt = datetime.fromisoformat(date_str)

        # Si no tiene timezone, asumir Bogotá
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TIMEZONE_BOGOTA)
        else:
            # Convertir a Bogotá para comparaciones consistentes
            dt = dt.astimezone(TIMEZONE_BOGOTA)

        return dt
    except (ValueError, TypeError):
        # Si falla, retornar ahora (fail-safe)
        return get_bogota_now()

# ═══════════════════════════════════════════════════════════════════════════════
# MODELOS
# ═══════════════════════════════════════════════════════════════════════════════

class ConversationStatus(str, Enum):
    """Estados posibles de una conversación."""
    BOT_ACTIVE = "BOT_ACTIVE"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"
    IN_CONVERSATION = "IN_CONVERSATION"
    PENDING_HANDOFF = "PENDING_HANDOFF"

@dataclass
class ConversationMeta:
    """Metadatos persistidos en Redis para cada conversación."""
    phone_normalized: str
    contact_id: Optional[str] = None
    status: str = "BOT_ACTIVE"
    last_activity: str = field(default_factory=get_bogota_now_iso)
    handoff_reason: Optional[str] = None
    assigned_owner_id: Optional[str] = None
    canal_origen: str = "whatsapp"
    display_name: Optional[str] = None
    version_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=get_bogota_now_iso)
    # === CAMPOS PARA TRANSFERENCIA DE CONTACTOS ===
    assigned_owner_ids: Optional[List[str]] = None  # Colaboradores (modo collaborative)
    primary_owner_id: Optional[str] = None  # Owner principal en HubSpot
    transfer_history: Optional[List[dict]] = None  # Historial de transferencias
    # === CAMPOS DE DEAL HUBSPOT (parchados por patch_redis_deal_ids.py) ===
    deal_id: Optional[str] = None
    deal_stage: Optional[str] = None
    last_advisor_message: Optional[str] = None
    canal_display: Optional[str] = None  # Canal visible en UI (editable por asesor), no afecta routing
    seguimiento_origin: Optional[str] = None  # Timestamp ISO de cuándo se movió al embudo Seguimiento

# ═══════════════════════════════════════════════════════════════════════════════
# GESTOR DE ESTADO
# ═══════════════════════════════════════════════════════════════════════════════

class ConversationStateManager:
    """Maneja la persistencia de estados y metadatos en Redis."""
    STATE_PREFIX = "conv_state:"
    META_PREFIX = "conv_meta:"
    ACTIVE_CONTACTS_ZSET = "active_conversations_sorted"  # ZSET principal ordenado por timestamp
    BOT_CONTROLLED_SET = "bot_controlled_conversations"  # SET para contactos en modo BOT que salen del ZSET
    
    # TTL dinámico: 48h días laborales, 72h fines de semana (para que duren hasta el lunes)
    HANDOFF_TTL_SECONDS = 172800  # Default 48h
    HANDOFF_TTL_WEEKEND = 259200  # 72h para fines de semana
    INACTIVITY_THRESHOLD = 172800  # 48h — umbral para mover BOT_ACTIVE del ZSET a BOT_CONTROLLED_SET
    PANEL_TTL_SECONDS = 365 * 86400  # 1 año — meta_key de contactos que tuvieron handoff no expira en la práctica
    HUMAN_PANEL_STATE_TTL = 7 * 86400  # 7 días — state_key de HUMAN_ACTIVE/IN_CONVERSATION sobrevive el fin de semana
    STATE_REFRESH_THRESHOLD = 2 * 86400  # 2 días — umbral para refresh-on-read de state_key en get_active_contacts
    
    # Horario laboral: Lunes-Viernes 8:00-18:00 (Bogotá)
    WORK_HOURS_START = 8
    WORK_HOURS_END = 18
    
    def _calculate_dynamic_ttl(self) -> int:
        """
        Calcula TTL dinámico basado en día y hora actual.
        
        - Viernes después de las 18h → TTL hasta lunes 9AM
        - Sábado/Domingo → TTL hasta lunes 9AM
        - Días laborales → 24h estándar
        
        Esto garantiza que los contactos del fin de semana persistan
        hasta que las asesoras puedan atenderlos el lunes.
        """
        now = get_bogota_now()
        weekday = now.weekday()  # 0=Lunes, 4=Viernes, 5=Sábado, 6=Domingo
        hour = now.hour
        
        # Calcular si estamos en periodo de fin de semana extendido
        is_friday_evening = (weekday == 4 and hour >= self.WORK_HOURS_END)
        is_weekend = (weekday in [5, 6])
        
        if is_friday_evening or is_weekend:
            # Calcular segundos hasta el próximo lunes 9AM
            days_until_monday = (7 - weekday) % 7
            if days_until_monday == 0:
                days_until_monday = 7  # Si es lunes, esperar al siguiente
            
            # Para viernes/sábado ajustar días
            if weekday == 4:  # Viernes
                days_until_monday = 3
            elif weekday == 5:  # Sábado
                days_until_monday = 2
            elif weekday == 6:  # Domingo
                days_until_monday = 1
            
            # Calcular TTL hasta lunes 9AM
            from datetime import timedelta
            monday_9am = now.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=days_until_monday)
            ttl_seconds = int((monday_9am - now).total_seconds())
            
            # Mínimo 24 horas, máximo 72 horas
            ttl_seconds = max(self.HANDOFF_TTL_SECONDS, min(ttl_seconds, self.HANDOFF_TTL_WEEKEND))
            
            logger.info(
                f"[ConversationState] TTL FIN DE SEMANA: {ttl_seconds}s "
                f"({ttl_seconds // 3600}h) - weekday={weekday}, hour={hour}"
            )
            return ttl_seconds
        
        # Días laborales: TTL estándar de 24h
        return self.HANDOFF_TTL_SECONDS

    def __init__(self, redis_url: str = None):
        if not redis_url:
            is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
            self.redis_url = os.getenv("REDIS_URL") if is_railway else (
                os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
            )
        else:
            self.redis_url = redis_url
            
        # Configuración optimizada de Redis con timeouts
        self.redis = redis.from_url(
            self.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=10,          # Cap explícito — componente de mayor frecuencia
            socket_timeout=3.0,          # Timeout para operaciones (antes 5.0)
            socket_connect_timeout=3.0,  # Timeout para conexión
            retry_on_timeout=True,       # Reintentar en timeout
            health_check_interval=30     # Health check cada 30s
        )

    def _parse_meta_raw(self, raw_json: Optional[str]) -> Optional[ConversationMeta]:
        """Parsea JSON de meta desde Redis sin IO. Replica la lógica de get_meta()."""
        if not raw_json:
            return None
        try:
            payload = json.loads(raw_json)
            if 'advisor' in payload and not payload.get('assigned_owner_id'):
                payload['assigned_owner_id'] = payload['advisor']
            if 'owner_id' in payload and not payload.get('assigned_owner_id'):
                payload['assigned_owner_id'] = payload['owner_id']
            if 'phone' in payload and not payload.get('phone_normalized'):
                payload['phone_normalized'] = payload['phone']
            if 'name' in payload and not payload.get('display_name'):
                payload['display_name'] = payload['name']
            valid_fields = {f.name for f in fields(ConversationMeta)}
            filtered = {k: v for k, v in payload.items() if k in valid_fields}
            return ConversationMeta(**filtered)
        except Exception as e:
            logger.error(f"[ConversationState] Error parseando meta JSON: {e}")
            return None

    async def get_meta(self, phone: str, canal: str = "whatsapp") -> Optional[ConversationMeta]:
        """Recupera metadatos de Redis filtrando campos inválidos de forma segura."""
        meta_key = f"{self.META_PREFIX}{phone}:{canal.lower()}"
        try:
            data = await self.redis.get(meta_key)
            if not data: return None
            payload = json.loads(data)
            
            # Mapeos de compatibilidad para datos legacy
            if 'advisor' in payload and not payload.get('assigned_owner_id'):
                payload['assigned_owner_id'] = payload['advisor']
            # ✅ FIX: Mapear owner_id → assigned_owner_id (campo usado por ensure_meta_with_channel)
            if 'owner_id' in payload and not payload.get('assigned_owner_id'):
                payload['assigned_owner_id'] = payload['owner_id']
            if 'phone' in payload and not payload.get('phone_normalized'):
                payload['phone_normalized'] = payload['phone']
            if 'name' in payload and not payload.get('display_name'):
                payload['display_name'] = payload['name']
                
            # --- SOLUCIÓN AL ERROR DE INTROSPECCIÓN ---
            # Usamos la función fields() de dataclasses para obtener los nombres válidos
            valid_fields = {f.name for f in fields(ConversationMeta)}
            filtered_payload = {k: v for k, v in payload.items() if k in valid_fields}
            
            return ConversationMeta(**filtered_payload)
        except Exception as e:
            logger.error(f"[ConversationState] Error en get_meta para {phone}: {e}")
            return None

    async def get_active_contacts(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """
        Retorna la lista de contactos activos ordenados por última actividad.

        Args:
            limit:  Máximo de contactos del ZSET a retornar (default 100).
            offset: Posición inicial en el ZSET (para paginación).

        v2: Optimizado con Redis pipeline — reduce N*4 round-trips seriales a 3 round-trips totales.
        v3: Paginación via zrevrange(offset, offset+limit-1) — escala con >100 contactos.
        """
        contacts = []
        try:
            # ── Paso 1: Obtener miembros del ZSET con paginación (1 round-trip) ──────
            members = await self.redis.zrevrange(
                self.ACTIVE_CONTACTS_ZSET, offset, offset + limit - 1
            )

            # ── Paso 2: Pipeline de lectura para todos los miembros ZSET (1 round-trip) ─
            valid_members = [m for m in members if ":" in m]
            now_ts = get_bogota_now().timestamp()
            ghosts_detected = []  # ⚠️ 2026-05-30: ya NO se borran automáticamente del ZSET.
                                  # Se logean y se delegan a job nocturno rebuild_zset_from_conversations
                                  # que valida contra MongoDB `conversations` antes de hacer zrem.
                                  # Causa: el ghost cleanup automático estaba eliminando del inbox
                                  # cualquier conversación cuyo conv_meta expirara (48-72h sin handoff),
                                  # haciendo que conversaciones de >1 semana desaparecieran del panel.
            to_move_to_bot_set = []
            state_keys_to_refresh = []  # Refresh-on-read: state_keys cerca de expirar

            # Estados del panel que justifican refresh-on-read (no BOT_ACTIVE)
            _PANEL_ACTIVE_STATUSES = {
                ConversationStatus.HUMAN_ACTIVE.value,
                ConversationStatus.IN_CONVERSATION.value,
                ConversationStatus.PENDING_HANDOFF.value,
            }

            if valid_members:
                pipe = self.redis.pipeline(transaction=False)
                for member in valid_members:
                    phone, canal = member.split(":", 1)
                    c = canal.lower()
                    pipe.get(f"{self.STATE_PREFIX}{phone}:{c}")   # status
                    pipe.get(f"{self.META_PREFIX}{phone}:{c}")    # meta JSON
                    pipe.zscore(self.ACTIVE_CONTACTS_ZSET, member)  # score
                    pipe.ttl(f"{self.STATE_PREFIX}{phone}:{c}")   # TTL
                results = await pipe.execute()

                for i, member in enumerate(valid_members):
                    phone, canal = member.split(":", 1)
                    c = canal.lower()
                    base = i * 4
                    status_raw = results[base]
                    meta_raw   = results[base + 1]
                    score      = results[base + 2]
                    ttl        = results[base + 3]

                    meta = self._parse_meta_raw(meta_raw)

                    if not status_raw:
                        if not meta:
                            # ⚠️ 2026-05-30: NO borrar del ZSET aquí.
                            # Antes hacíamos zrem inmediato → conversaciones perdidas para siempre.
                            # Ahora: logear + dejar entrada visible con fallback mínimo.
                            # El job nocturno valida contra MongoDB `conversations` y solo
                            # purga miembros cuya conversación NO existe en MongoDB.
                            ghosts_detected.append(member)
                            # Re-hidratación mínima para que el panel pueda mostrar la fila
                            # con datos básicos hasta que el job de rebuild la enriquezca
                            # o el cliente vuelva a escribir.
                            status_raw = ConversationStatus.BOT_ACTIVE.value
                        else:
                            status_raw = ConversationStatus.BOT_ACTIVE.value
                            logger.debug(
                                f"[ConversationState] TTL state expirado para {phone}:{canal} — "
                                f"mostrando como BOT_ACTIVE en el panel"
                            )

                    # Refresh-on-read: si el contacto está activo en panel y su state_key
                    # está a punto de expirar, re-extender TTL para evitar que "baje"
                    # visualmente a BOT_ACTIVE tras inactividad natural del cliente.
                    if (
                        status_raw in _PANEL_ACTIVE_STATUSES
                        and meta is not None
                        and isinstance(ttl, int)
                        and 0 < ttl < self.STATE_REFRESH_THRESHOLD
                    ):
                        state_keys_to_refresh.append(f"{self.STATE_PREFIX}{phone}:{c}")

                    in_priority_zset = True
                    if status_raw == ConversationStatus.BOT_ACTIVE.value:
                        if score is not None and (now_ts - float(score)) > self.INACTIVITY_THRESHOLD:
                            to_move_to_bot_set.append(member)
                            in_priority_zset = False
                            logger.debug(
                                f"[ConversationState] {phone}:{canal} BOT_ACTIVE "
                                f">48h inactivo, movido a BOT_CONTROLLED_SET (sigue visible)"
                            )

                    contacts.append({
                        "phone": phone,
                        "canal": canal,
                        "status": status_raw,
                        "display_name": (meta.display_name if meta else None) or "Cliente Nuevo",
                        "last_activity": meta.last_activity if meta else get_bogota_now_iso(),
                        "owner_id": meta.assigned_owner_id if meta else None,
                        "assigned_owner_ids": meta.assigned_owner_ids if meta else [],
                        "handoff_reason": meta.handoff_reason if meta else "Transferencia manual",
                        "ttl_remaining": ttl,
                        "contact_id": meta.contact_id if meta else None,
                        "is_active": True,
                        "canal_origen": (meta.canal_origen if meta else canal) or canal,
                        "canal_display": meta.canal_display if meta else None,
                        "activated_at": meta.created_at if meta else get_bogota_now_iso(),
                        "conversation_status": status_raw,
                        "in_priority_zset": in_priority_zset,
                        "deal_id": meta.deal_id if meta else None,
                        "deal_stage": meta.deal_stage if meta else None,
                        "last_advisor_message": meta.last_advisor_message if meta else None,
                    })

            # ── Paso 3: Escrituras pendientes en un solo pipeline ─────────────────────
            # ⚠️ 2026-05-30: ghosts_detected ya NO se zrem aquí — el job nocturno
            # rebuild_zset_from_conversations valida contra MongoDB antes de purgar.
            if to_move_to_bot_set or state_keys_to_refresh:
                write_pipe = self.redis.pipeline(transaction=False)
                for member in to_move_to_bot_set:
                    write_pipe.sadd(self.BOT_CONTROLLED_SET, member)
                    write_pipe.zrem(self.ACTIVE_CONTACTS_ZSET, member)
                # Refresh-on-read: re-extender TTL del state_key al máximo (7 días).
                # expire() es no-op si la key no existe → safe.
                for state_key in state_keys_to_refresh:
                    write_pipe.expire(state_key, self.HUMAN_PANEL_STATE_TTL)
                await write_pipe.execute()
                if state_keys_to_refresh:
                    logger.debug(
                        f"[ConversationState] Refresh-on-read: {len(state_keys_to_refresh)} "
                        f"state_keys re-extendidos a {self.HUMAN_PANEL_STATE_TTL}s"
                    )

            if ghosts_detected:
                logger.info(
                    "[ConversationState] ZSET: %d ghosts sin meta (job nocturno purgará)",
                    len(ghosts_detected)
                )

            # ── Paso 4: BOT_CONTROLLED_SET — pipeline separado (1 round-trip) ─────────
            # Contactos BOT_ACTIVE >48h: visibles pero sin notificaciones
            bot_controlled = await self.redis.smembers(self.BOT_CONTROLLED_SET)
            processed_keys = {f"{c['phone']}:{c['canal']}" for c in contacts}
            bot_to_process = [m for m in bot_controlled if ":" in m and m not in processed_keys]

            if bot_to_process:
                pipe2 = self.redis.pipeline(transaction=False)
                for member in bot_to_process:
                    phone, canal = member.split(":", 1)
                    c = canal.lower()
                    pipe2.get(f"{self.META_PREFIX}{phone}:{c}")   # meta JSON
                    pipe2.get(f"{self.STATE_PREFIX}{phone}:{c}")  # status
                    pipe2.ttl(f"{self.STATE_PREFIX}{phone}:{c}")  # TTL
                results2 = await pipe2.execute()

                bot_ghost_count = 0
                for i, member in enumerate(bot_to_process):
                    phone, canal = member.split(":", 1)
                    base = i * 3
                    meta_raw   = results2[base]
                    status_raw = results2[base + 1]
                    ttl        = results2[base + 2]

                    meta = self._parse_meta_raw(meta_raw)
                    if not meta:
                        bot_ghost_count += 1
                        continue

                    status = status_raw or ConversationStatus.BOT_ACTIVE.value
                    contacts.append({
                        "phone": phone,
                        "canal": canal,
                        "status": status,
                        "display_name": meta.display_name or "Cliente Nuevo",
                        "last_activity": meta.last_activity,
                        "owner_id": meta.assigned_owner_id,
                        "assigned_owner_ids": meta.assigned_owner_ids or [],
                        "handoff_reason": meta.handoff_reason or "Sofía atiende",
                        "ttl_remaining": ttl,
                        "contact_id": meta.contact_id,
                        "is_active": True,
                        "canal_origen": meta.canal_origen or canal,
                        "canal_display": meta.canal_display,
                        "activated_at": meta.created_at,
                        "conversation_status": status,
                        "in_priority_zset": False,
                        "deal_id": meta.deal_id,
                        "deal_stage": meta.deal_stage,
                        "last_advisor_message": meta.last_advisor_message,
                        "seguimiento_origin": meta.seguimiento_origin,
                    })

                if bot_ghost_count:
                    logger.info(
                        "[ConversationState] BOT_CONTROLLED: %d ghosts sin meta (job nocturno purgará)",
                        bot_ghost_count
                    )

        except Exception as e:
            logger.error(f"[ConversationState] Error en get_active_contacts: {e}")
        return contacts

    async def get_all_human_active_contacts(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """Alias para obtener contactos activos."""
        return await self.get_active_contacts(limit=limit, offset=offset)

    async def get_archived_conversations_from_mongo(
        self,
        owner_id: Optional[str] = None,
        before_ts: Optional[datetime] = None,
        limit: int = 30,
        exclude_phones: Optional[set] = None,
    ) -> List[Dict[str, Any]]:
        """
        ⚠️ 2026-05-30: Fallback inbox desde MongoDB `conversations`.

        Retorna conversaciones del owner ordenadas por last_message_at desc,
        EXCLUYENDO teléfonos que ya están en el ZSET Redis (para no duplicar).
        Formato compatible con get_active_contacts() para que el panel los
        mezcle transparentemente.

        Usado por outbound_panel.GET /contacts cuando el ZSET tiene menos
        contactos que el `limit` solicitado o cuando el usuario pagina hacia
        conversaciones antiguas.
        """
        try:
            from database.mongodb_client import get_mongo_manager
            mongo = get_mongo_manager()
            if owner_id:
                conv_docs = await mongo.find_conversations_by_owner(
                    owner_id=owner_id,
                    before_ts=before_ts,
                    limit=limit * 2,  # over-fetch para compensar dedup contra ZSET
                    include_archived=False,
                )
            else:
                # Sin owner_id, usar query genérica reciente
                since = before_ts or get_bogota_now()
                conv_docs = await mongo.find_recent_conversations(
                    since=since - timedelta(days=365),
                    limit=limit * 2,
                )

            exclude = exclude_phones or set()
            result = []
            for d in conv_docs:
                phone = d.get("phone", "")
                if phone in exclude:
                    continue
                last_msg_dt = d.get("last_message_at")
                if isinstance(last_msg_dt, datetime):
                    last_activity_iso = last_msg_dt.astimezone(TIMEZONE_BOGOTA).isoformat()
                else:
                    last_activity_iso = str(last_msg_dt) if last_msg_dt else get_bogota_now_iso()

                result.append({
                    "phone": phone,
                    "canal": d.get("canal", "whatsapp"),
                    "status": d.get("status") or ConversationStatus.BOT_ACTIVE.value,
                    "display_name": d.get("display_name") or phone or "Cliente",
                    "last_activity": last_activity_iso,
                    "owner_id": d.get("owner_id"),
                    "assigned_owner_ids": [],
                    "handoff_reason": None,
                    "ttl_remaining": None,
                    "contact_id": d.get("contact_id"),
                    "is_active": True,
                    "canal_origen": d.get("canal_origen") or d.get("canal", "whatsapp"),
                    "canal_display": None,
                    "activated_at": (d.get("created_at").astimezone(TIMEZONE_BOGOTA).isoformat()
                                     if isinstance(d.get("created_at"), datetime)
                                     else get_bogota_now_iso()),
                    "conversation_status": d.get("status") or ConversationStatus.BOT_ACTIVE.value,
                    "in_priority_zset": False,
                    "deal_id": None,
                    "deal_stage": None,
                    "last_advisor_message": None,
                    "_from_mongo_fallback": True,  # marca para debugging/logs
                    "last_message_preview": d.get("last_message_preview", ""),
                    "message_count": d.get("message_count", 0),
                })
                if len(result) >= limit:
                    break
            if result:
                logger.info(
                    f"[ConversationState][MongoFallback] {len(result)} conversaciones recuperadas "
                    f"desde MongoDB para owner={owner_id or 'all'} (excluyendo {len(exclude)} ya en ZSET)"
                )
            return result
        except Exception as e:
            logger.warning(f"[ConversationState][MongoFallback] Error: {e}")
            return []

    # ═══════════════════════════════════════════════════════════════════════════════
    # MÉTODOS DE COMPATIBILIDAD PARA app.py Y outbound_panel.py
    # ═══════════════════════════════════════════════════════════════════════════════

    async def get_status(self, phone: str, canal: str = "whatsapp") -> Optional[ConversationStatus]:
        """
        Obtiene el estado actual de una conversación.

        Args:
            phone: Número de teléfono normalizado
            canal: Canal de origen (default: whatsapp)

        Returns:
            ConversationStatus o None si no existe
        """
        state_key = f"{self.STATE_PREFIX}{phone}:{canal.lower()}"
        try:
            status_str = await self.redis.get(state_key)
            if not status_str:
                return None
            return ConversationStatus(status_str)
        except ValueError:
            # Estado no reconocido, retornar None
            logger.warning(f"[ConversationState] Estado no reconocido: {status_str}")
            return None
        except Exception as e:
            logger.error(f"[ConversationState] Error en get_status: {e}")
            return None

    async def set_status(
        self,
        phone: str,
        status: ConversationStatus,
        canal: str = "whatsapp",
        ttl: int = None
    ) -> bool:
        """
        Establece el estado de una conversación.

        Args:
            phone: Número de teléfono normalizado
            status: Nuevo estado
            canal: Canal de origen
            ttl: Tiempo de vida en segundos (opcional)

        Returns:
            True si se guardó correctamente
        """
        state_key = f"{self.STATE_PREFIX}{phone}:{canal.lower()}"
        try:
            ttl = ttl or self._calculate_dynamic_ttl()
            await self.redis.set(state_key, status.value, ex=ttl)
            logger.debug(f"[ConversationState] Estado actualizado: {phone}:{canal} -> {status.value}")
            return True
        except Exception as e:
            logger.error(f"[ConversationState] Error en set_status: {e}")
            return False

    async def is_bot_active(self, phone: str, canal: str = "whatsapp") -> bool:
        """
        Verifica si el bot está activo para esta conversación.

        Returns:
            True si el bot está activo o no hay estado (default BOT_ACTIVE)
        """
        status = await self.get_status(phone, canal)
        # Si no hay estado o es BOT_ACTIVE, el bot está activo
        return status is None or status == ConversationStatus.BOT_ACTIVE

    async def activate_bot(self, phone: str, canal: str = "whatsapp") -> bool:
        """
        Reactiva el bot para una conversación (cierra sesión de asesor).
        El contacto permanece en el ZSET con su score original; la limpieza
        por inactividad ocurre en get_active_contacts() tras INACTIVITY_THRESHOLD.

        Returns:
            True si se reactivó correctamente
        """
        try:
            # Cambiar estado a BOT_ACTIVE (TTL dinámico)
            await self.set_status(phone, ConversationStatus.BOT_ACTIVE, canal)

            # Actualizar campo status en meta sin tocar last_activity
            # (el score del ZSET ya registra el momento de la última interacción humana)
            meta_key = f"{self.META_PREFIX}{phone}:{canal.lower()}"
            raw = await self.redis.get(meta_key)
            if raw:
                try:
                    meta_dict = json.loads(raw)
                    meta_dict["status"] = ConversationStatus.BOT_ACTIVE.value
                    ttl = await self.redis.ttl(meta_key)
                    ex = ttl if ttl and ttl > 0 else self._calculate_dynamic_ttl()
                    await self.redis.set(meta_key, json.dumps(meta_dict), ex=ex)
                except Exception:
                    pass  # Meta update is best-effort

            logger.info(f"[ConversationState] Bot reactivado para {phone}:{canal}")
            return True
        except Exception as e:
            logger.error(f"[ConversationState] Error en activate_bot: {e}")
            return False

    async def activate_human(
        self,
        phone_normalized: str = None,
        canal_origen: str = "whatsapp",
        owner_id: str = None,
        reason: str = None,
        display_name: str = None,
        contact_id: str = None,
        # Alias para compatibilidad con llamadas posicionales
        phone: str = None
    ) -> bool:
        """
        Activa el modo humano para una conversación.
        El contacto aparecerá en el panel de asesores.

        Args:
            phone_normalized: Número de teléfono normalizado (también acepta 'phone')
            canal_origen: Canal de origen para segregación
            owner_id: ID del asesor asignado
            reason: Razón del handoff
            display_name: Nombre para mostrar
            contact_id: ID del contacto en HubSpot

        Returns:
            True si se activó correctamente
        """
        # Compatibilidad: aceptar phone_normalized o phone
        phone_num = phone_normalized or phone
        if not phone_num:
            logger.error("[ConversationState] activate_human: Se requiere phone_normalized o phone")
            return False

        canal_safe = canal_origen.lower() if canal_origen else "whatsapp"

        try:
            # 1. Guardar estado HUMAN_ACTIVE con TTL de 7 días — sobrevive fines de semana completos
            state_key = f"{self.STATE_PREFIX}{phone_num}:{canal_safe}"
            await self.redis.set(state_key, ConversationStatus.HUMAN_ACTIVE.value, ex=self.HUMAN_PANEL_STATE_TTL)

            # 2. Guardar metadata
            now_iso = get_bogota_now_iso()

            # Merge con meta existente para preservar campos acumulados
            # (assigned_owner_id, assigned_owner_ids, transfer_history, etc.)
            meta_key = f"{self.META_PREFIX}{phone_num}:{canal_safe}"
            existing_meta = {}
            try:
                existing_raw = await self.redis.get(meta_key)
                if existing_raw:
                    existing_meta = json.loads(existing_raw)
            except Exception:
                pass

            # Preservar last_activity real del último mensaje
            existing_last_activity = existing_meta.get("last_activity") or now_iso

            # Resolver assigned_owner_id: parámetro explícito > existente > None
            resolved_owner = owner_id or existing_meta.get("assigned_owner_id")
            if owner_id and existing_meta.get("assigned_owner_id") and owner_id != existing_meta["assigned_owner_id"]:
                logger.info(
                    f"[ConversationState] activate_human cambió owner: "
                    f"{existing_meta['assigned_owner_id']} → {owner_id} ({phone_num})"
                )

            meta = {
                **existing_meta,
                "phone_normalized": phone_num,
                "contact_id": contact_id or existing_meta.get("contact_id"),
                "status": ConversationStatus.HUMAN_ACTIVE.value,
                "last_activity": existing_last_activity,
                "handoff_reason": reason,
                "assigned_owner_id": resolved_owner,
                "canal_origen": canal_origen or existing_meta.get("canal_origen") or "whatsapp",
                "display_name": display_name or existing_meta.get("display_name"),
                "created_at": existing_meta.get("created_at") or now_iso,
                "in_panel": True,
            }
            # meta_key usa TTL largo (365d): contacto permanece visible en panel aunque state_key expire a 48h
            await self.redis.set(meta_key, json.dumps(meta), ex=self.PANEL_TTL_SECONDS)
            # Marcador permanente: garantiza re-aparición en panel aunque meta_key expire tras 365 días
            marker_key = f"conv_was_panel:{phone_num}:{canal_safe}"
            await self.redis.set(marker_key, "1", ex=self.PANEL_TTL_SECONDS)

            # 3. Agregar al índice de contactos activos (ZSET ordenado por timestamp)
            # Y remover del BOT_CONTROLLED_SET si estaba ahí (re-activación)
            index_member = f"{phone_num}:{canal_safe}"
            score = get_bogota_now().timestamp()  # Unix timestamp como score
            await self.redis.zadd(self.ACTIVE_CONTACTS_ZSET, {index_member: score})
            await self.redis.srem(self.BOT_CONTROLLED_SET, index_member)  # Sale del modo bot

            logger.info(f"[ConversationState] HUMAN_ACTIVE activado: {phone_num}:{canal_safe} (re-agregado al ZSET)")

            # ⚠️ 2026-05-30: Sincronizar metadatos a MongoDB `conversations` (fuente permanente).
            # Fire-and-forget — si falla no rompe el handoff.
            try:
                import asyncio as _asyncio
                from database.mongodb_client import get_mongo_manager as _gmm
                _asyncio.create_task(
                    _gmm().update_conversation_meta(
                        phone=phone_num,
                        canal=canal_safe,
                        owner_id=resolved_owner,
                        display_name=display_name or existing_meta.get("display_name"),
                        status=ConversationStatus.HUMAN_ACTIVE.value,
                        contact_id=contact_id or existing_meta.get("contact_id"),
                        canal_origen=canal_origen or existing_meta.get("canal_origen") or "whatsapp",
                    )
                )
            except Exception as _sync_err:
                logger.debug(f"[ConversationState] sync MongoDB conv no crítico: {_sync_err}")

            return True

        except Exception as e:
            logger.error(f"[ConversationState] Error en activate_human: {e}")
            return False

    async def request_handoff(
        self,
        phone: str,
        reason: str = None,
        contact_id: str = None,
        canal: str = "whatsapp"
    ) -> bool:
        """
        Solicita handoff a un asesor humano.
        Cambia el estado a PENDING_HANDOFF.
        """
        try:
            # TTL de 7 días — PENDING_HANDOFF también debe sobrevivir el fin de semana
            canal_safe = canal.lower() if canal else "whatsapp"
            state_key = f"{self.STATE_PREFIX}{phone}:{canal_safe}"
            await self.redis.set(state_key, ConversationStatus.PENDING_HANDOFF.value, ex=self.HUMAN_PANEL_STATE_TTL)

            # Guardar metadata con la razón del handoff.
            # Se lee el meta existente para preservar assigned_owner_id, display_name
            # y otros campos que se pierden si se sobrescribe con un dict vacío.
            now_iso = get_bogota_now_iso()
            meta_key = f"{self.META_PREFIX}{phone}:{canal_safe}"
            existing_raw = await self.redis.get(meta_key)
            if existing_raw:
                try:
                    meta = json.loads(existing_raw)
                except Exception:
                    meta = {}
                meta.update({
                    "status": ConversationStatus.PENDING_HANDOFF.value,
                    "last_activity": now_iso,
                    "handoff_reason": reason,
                    "in_panel": True,  # Resetea in_panel=False de un cierre previo
                })
                if contact_id:
                    meta["contact_id"] = contact_id
            else:
                meta = {
                    "phone_normalized": phone,
                    "contact_id": contact_id,
                    "status": ConversationStatus.PENDING_HANDOFF.value,
                    "last_activity": now_iso,
                    "handoff_reason": reason,
                    "canal_origen": canal,
                    "created_at": now_iso,
                }
            # meta_key usa TTL largo (365d): contacto permanece visible en panel aunque state_key expire a 48h
            await self.redis.set(meta_key, json.dumps(meta), ex=self.PANEL_TTL_SECONDS)
            # Marcador permanente: garantiza re-aparición en panel aunque meta_key expire tras 365 días
            marker_key = f"conv_was_panel:{phone}:{canal_safe}"
            await self.redis.set(marker_key, "1", ex=self.PANEL_TTL_SECONDS)

            # Agregar al índice de contactos activos (ZSET ordenado por timestamp)
            # Y remover del BOT_CONTROLLED_SET si estaba ahí (re-activación)
            index_member = f"{phone}:{canal_safe}"
            score = get_bogota_now().timestamp()  # Unix timestamp como score
            await self.redis.zadd(self.ACTIVE_CONTACTS_ZSET, {index_member: score})
            await self.redis.srem(self.BOT_CONTROLLED_SET, index_member)  # Sale del modo bot

            logger.info(f"[ConversationState] Handoff solicitado: {phone}:{canal_safe} - {reason} (ZSET índice 0)")

            # ⚠️ 2026-05-30: Sincronizar a MongoDB `conversations`.
            try:
                import asyncio as _asyncio
                from database.mongodb_client import get_mongo_manager as _gmm
                _asyncio.create_task(
                    _gmm().update_conversation_meta(
                        phone=phone,
                        canal=canal_safe,
                        status=ConversationStatus.PENDING_HANDOFF.value,
                        contact_id=contact_id,
                        canal_origen=canal,
                    )
                )
            except Exception as _sync_err:
                logger.debug(f"[ConversationState] sync MongoDB conv no crítico: {_sync_err}")

            _owner = meta.get("assigned_owner_id") if isinstance(meta, dict) else None
            if _owner:
                await self.add_to_advisor_inbox(_owner, phone, canal_safe)

            return True
        except Exception as e:
            logger.error(f"[ConversationState] Error en request_handoff: {e}")
            return False

    async def update_activity(self, phone: str, canal: str = "whatsapp") -> bool:
        """
        Actualiza el timestamp de última actividad.

        También actualiza el score en el ZSET para reordenamiento automático.
        Contactos con actividad reciente suben al inicio de la lista.
        """
        try:
            canal_safe = canal.lower() if canal else "whatsapp"
            meta_key = f"{self.META_PREFIX}{phone}:{canal_safe}"

            data = await self.redis.get(meta_key)
            if data:
                meta = json.loads(data)
                meta["last_activity"] = get_bogota_now_iso()
                # ⚠️ 2026-05-30: TTL meta unificado a PANEL_TTL_SECONDS (365d).
                # Antes: TTL dinámico 48-72h hacía que meta expirara antes del handoff
                # y luego el ghost cleanup borraba el contacto del ZSET para siempre.
                # Ahora: meta siempre 365d. El state_key sigue con TTL corto (lifecycle real).
                ttl = await self.redis.ttl(meta_key)
                if not ttl or ttl <= 0 or ttl < self.PANEL_TTL_SECONDS:
                    ttl = self.PANEL_TTL_SECONDS
                await self.redis.set(meta_key, json.dumps(meta), ex=ttl)

                # Actualizar score en ZSET para reordenamiento
                # Solo si el contacto ya está en el panel (in_panel=True o flag no existe = compat)
                _did_zadd = False
                if meta.get("in_panel", True):
                    index_member = f"{phone}:{canal_safe}"
                    score = get_bogota_now().timestamp()
                    await self.redis.zadd(self.ACTIVE_CONTACTS_ZSET, {index_member: score})
                    _did_zadd = True

            else:
                # Contacto nuevo sin meta: crear entrada mínima temporal para que GET /contacts
                # lo retorne inmediatamente cuando el panel llama tras recibir el WS.
                # _hydrate_contact_and_ensure_panel() (dispara async abajo) sobreescribirá con
                # datos completos desde HubSpot (~1-2s después).
                #
                # CRÍTICO: display_name = phone (NUNCA None) para que el panel nunca muestre
                # "Cliente Nuevo" o vacío mientras llega la hidratación async.
                minimal_meta = {
                    "phone_normalized": phone,
                    "canal_origen": canal_safe,
                    "last_activity": get_bogota_now_iso(),
                    "in_panel": True,
                    "display_name": phone,  # Fallback presentable — jamás None
                    "_temp_meta": True,
                    "_needs_hydration": True,
                }
                # ⚠️ 2026-05-30: TTL unificado a 365d incluso para meta temporal.
                # La hidratación async reescribirá con datos reales pero preserva TTL largo.
                await self.redis.set(meta_key, json.dumps(minimal_meta), ex=self.PANEL_TTL_SECONDS)
                index_member = f"{phone}:{canal_safe}"
                score = get_bogota_now().timestamp()
                await self.redis.zadd(self.ACTIVE_CONTACTS_ZSET, {index_member: score})
                _did_zadd = True
                logger.debug(
                    f"[ConversationState] Meta temporal creado para contacto nuevo {phone}:{canal_safe}"
                )

                # Disparar hidratación async (fire-and-forget) para enriquecer con nombre de
                # HubSpot, owner, current_stage. Lazy import para evitar ciclo con outbound_panel.
                # Si existe marker conv_was_panel, es un contacto que ya tuvo handoff — prioritario.
                try:
                    marker_key = f"conv_was_panel:{phone}:{canal_safe}"
                    had_panel_before = await self.redis.exists(marker_key)
                    if had_panel_before:
                        logger.info(
                            f"[ConversationState] Marker conv_was_panel presente para {phone} "
                            f"— disparando _hydrate_contact_and_ensure_panel async"
                        )

                    async def _hydrate_async():
                        try:
                            from .outbound_panel import _hydrate_contact_and_ensure_panel
                            await _hydrate_contact_and_ensure_panel(phone, canal_hint=canal_safe)
                        except Exception as _he:
                            logger.warning(
                                f"[ConversationState] Hydrate async falló para {phone}: {_he}"
                            )

                    import asyncio as _asyncio
                    _asyncio.create_task(_hydrate_async())
                except Exception as _e:
                    logger.debug(
                        f"[ConversationState] No se pudo disparar hydrate async para {phone}: {_e}"
                    )

            # P3-D: Fusionar identidad de canal — cuando llega mensaje WhatsApp, eliminar
            # entradas ZSET de otros canales (ej. phone:instagram) para el mismo teléfono.
            # Evita duplicados en el panel para contactos que cambiaron de canal.
            # GUARD: Si el canal stale tiene sesión activa de asesor (HUMAN_ACTIVE /
            # IN_CONVERSATION / PENDING_HANDOFF), NO mergearlo — preservar la sesión.
            # P3-D solo aplica a canales BOT_ACTIVE o expirados.
            if _did_zadd and canal_safe == "whatsapp":
                whatsapp_member = f"{phone}:whatsapp"
                cursor = 0
                _stale_candidates = []
                while True:
                    cursor, results = await self.redis.zscan(
                        self.ACTIVE_CONTACTS_ZSET, cursor, match=f"{phone}:*", count=20
                    )
                    for member, _ in results:
                        m = member if isinstance(member, str) else member.decode()
                        if m != whatsapp_member:
                            _stale_candidates.append(m)
                    if cursor == 0:
                        break

                _SKIP_STATUSES = {
                    ConversationStatus.HUMAN_ACTIVE.value,
                    ConversationStatus.IN_CONVERSATION.value,
                    ConversationStatus.PENDING_HANDOFF.value,
                }
                stale_members = []
                if _stale_candidates:
                    # Batch-read state keys de todos los candidatos (1 round-trip)
                    _pipe_check = self.redis.pipeline(transaction=False)
                    for _cand in _stale_candidates:
                        _cand_canal = _cand.split(":", 1)[1] if ":" in _cand else "whatsapp"
                        _pipe_check.get(f"{self.STATE_PREFIX}{phone}:{_cand_canal}")
                    _cand_states = await _pipe_check.execute()

                    for _cand, _cand_state_raw in zip(_stale_candidates, _cand_states):
                        _cand_status = _cand_state_raw if isinstance(_cand_state_raw, str) else (
                            _cand_state_raw.decode() if _cand_state_raw else None
                        )
                        if _cand_status in _SKIP_STATUSES:
                            logger.info(
                                f"[P3-D] Skip merge {_cand}: canal activo con asesor "
                                f"({_cand_status}), preservando sesión"
                            )
                            continue
                        stale_members.append(_cand)

                if stale_members:
                    await self.redis.zrem(self.ACTIVE_CONTACTS_ZSET, *stale_members)
                    logger.info(
                        f"[ConversationState][P3-D] Canal merge {phone}: zrem {stale_members}"
                        + (f" (skipped_active={len(_stale_candidates) - len(stale_members)})"
                           if len(_stale_candidates) > len(stale_members) else "")
                    )

                    # ── P3-D v2: Migrar datos, status, markers e inbox de canales stale ──
                    wa_meta_key = f"{self.META_PREFIX}{phone}:whatsapp"
                    wa_state_key = f"{self.STATE_PREFIX}{phone}:whatsapp"
                    wa_marker_key = f"conv_was_panel:{phone}:whatsapp"

                    # Leer meta whatsapp actual para merge
                    wa_meta_raw = await self.redis.get(wa_meta_key)
                    wa_meta = json.loads(wa_meta_raw) if wa_meta_raw else {}
                    wa_status_raw = await self.redis.get(wa_state_key)
                    wa_status = wa_status_raw if isinstance(wa_status_raw, str) else (
                        wa_status_raw.decode() if wa_status_raw else None
                    )

                    _STATUS_PRIO = {"IN_CONVERSATION": 4, "HUMAN_ACTIVE": 3, "PENDING_HANDOFF": 2, "BOT_ACTIVE": 1}
                    best_status = wa_status
                    best_prio = _STATUS_PRIO.get(wa_status, 0)
                    stale_advisor_id = None

                    for stale_m in stale_members:
                        stale_canal = stale_m.split(":", 1)[1] if ":" in stale_m else stale_m
                        s_meta_key = f"{self.META_PREFIX}{phone}:{stale_canal}"
                        s_state_key = f"{self.STATE_PREFIX}{phone}:{stale_canal}"
                        s_marker_key = f"conv_was_panel:{phone}:{stale_canal}"

                        # 1) Meta merge: copiar campos faltantes del stale al whatsapp
                        s_meta_raw = await self.redis.get(s_meta_key)
                        if s_meta_raw:
                            s_meta = json.loads(s_meta_raw)
                            for fld in ("contact_id", "display_name", "assigned_owner_id",
                                        "assigned_owner_ids", "primary_owner_id", "handoff_reason",
                                        "deal_id", "deal_stage", "transfer_history"):
                                if s_meta.get(fld) and not wa_meta.get(fld):
                                    wa_meta[fld] = s_meta[fld]
                            # canal_origen: preservar portal específico sobre "whatsapp" genérico.
                            # wa_meta siempre tiene canal_origen (default "whatsapp"), pero si el
                            # stale tiene un portal real (finca_raiz, instagram, etc.), ese es el
                            # verdadero origen del lead y debe prevalecer.
                            s_canal = s_meta.get("canal_origen", "")
                            wa_canal = wa_meta.get("canal_origen", "whatsapp")
                            if s_canal and s_canal != "whatsapp" and wa_canal in ("whatsapp", "whatsapp_directo", ""):
                                wa_meta["canal_origen"] = s_canal
                                logger.info(f"[P3-D v2] canal_origen preservado: {wa_canal} → {s_canal}")
                            # Preservar created_at más antiguo
                            s_created = s_meta.get("created_at", "")
                            if s_created and (not wa_meta.get("created_at") or s_created < wa_meta["created_at"]):
                                wa_meta["created_at"] = s_created
                            if s_meta.get("assigned_owner_id"):
                                stale_advisor_id = s_meta["assigned_owner_id"]

                        # 2) Status promote: si stale tiene mayor prioridad
                        s_status_raw = await self.redis.get(s_state_key)
                        s_status = s_status_raw if isinstance(s_status_raw, str) else (
                            s_status_raw.decode() if s_status_raw else None
                        )
                        s_prio = _STATUS_PRIO.get(s_status, 0)
                        if s_prio > best_prio:
                            best_status = s_status
                            best_prio = s_prio

                        # 3) Marker: copiar marcador de re-entrada al panel
                        if await self.redis.exists(s_marker_key):
                            await self.redis.set(wa_marker_key, "1", ex=self.PANEL_TTL_SECONDS)

                        # 4) Cleanup: eliminar keys stale
                        await self.redis.delete(s_meta_key, s_state_key, s_marker_key)

                    # Guardar meta mergeado en whatsapp
                    wa_meta.pop("_temp_meta", None)
                    wa_meta["in_panel"] = True
                    # ⚠️ 2026-05-30: TTL unificado a 365d (antes: dinámico si no había handoff).
                    wa_ttl = await self.redis.ttl(wa_meta_key)
                    if not wa_ttl or wa_ttl <= 0 or wa_ttl < self.PANEL_TTL_SECONDS:
                        wa_ttl = self.PANEL_TTL_SECONDS
                    await self.redis.set(wa_meta_key, json.dumps(wa_meta), ex=wa_ttl)

                    # Promover status si el stale tenía prioridad mayor
                    if best_status and best_status != wa_status:
                        await self.redis.set(wa_state_key, best_status, ex=self.HUMAN_PANEL_STATE_TTL)
                        logger.info(f"[P3-D v2] Status promovido {phone}: {wa_status} → {best_status}")

                    # 5) Inbox migration: mover entry stale al canal whatsapp
                    if stale_advisor_id:
                        inbox_key = f"advisor_inbox:{stale_advisor_id}"
                        for stale_m in stale_members:
                            score = await self.redis.zscore(inbox_key, stale_m)
                            if score is not None:
                                await self.redis.zadd(inbox_key, {whatsapp_member: score})
                                await self.redis.zrem(inbox_key, stale_m)

                    logger.info(
                        f"[P3-D v2] Canal merge completo {phone}: "
                        f"meta={'merged' if wa_meta_raw else 'created'}, "
                        f"status={best_status}, stale_cleaned={len(stale_members)}, "
                        f"skipped_active={len(_stale_candidates) - len(stale_members)}"
                    )

            return True
        except Exception as e:
            logger.error(f"[ConversationState] Error en update_activity: {e}")
            return False

    async def ensure_meta_with_channel(
        self,
        phone: str,
        canal: str = "whatsapp",
        canal_origen: str = None,
        contact_id: str = None,
        display_name: str = None,
        owner_id: str = None,
        add_to_zset: bool = True
    ) -> bool:
        """
        Asegura que exista conv_meta y actualiza el canal_origen si es específico.
        
        IMPORTANTE para asignación de asesores:
        - Si el contacto NO tiene meta → crea uno con el canal detectado
        - Si el contacto YA tiene meta con canal específico (no whatsapp) → lo preserva
        - Si el contacto tiene meta con canal "whatsapp" Y llega uno específico → actualiza
        
        Esto garantiza que los leads de portales/redes sociales siempre tengan
        su canal_origen correcto para la asignación de asesores.
        
        El owner_id (hubspot_owner_id) se guarda en el meta para que el panel
        de asesores pueda filtrar correctamente por propietario.
        
        Args:
            phone: Teléfono normalizado
            canal: Canal de la conversación (para la key de Redis)
            canal_origen: Canal de origen detectado del mensaje
            contact_id: ID del contacto en HubSpot
            display_name: Nombre para mostrar
            owner_id: ID del propietario en HubSpot (para filtrado en panel)
            
        Returns:
            True si se creó/actualizó correctamente
        """
        try:
            canal_safe = canal.lower() if canal else "whatsapp"
            meta_key = f"{self.META_PREFIX}{phone}:{canal_safe}"
            
            data = await self.redis.get(meta_key)
            now_iso = get_bogota_now_iso()
            
            if data:
                # Meta ya existe - verificar si actualizar canal
                meta = json.loads(data)
                current_canal = meta.get("canal_origen", "whatsapp")
                
                # Solo actualizar si:
                # 1. El canal actual es genérico (whatsapp/whatsapp_directo)
                # 2. Y tenemos un canal específico nuevo
                canales_genericos = ("whatsapp", "whatsapp_directo", "", None)
                if current_canal in canales_genericos and canal_origen and canal_origen not in canales_genericos:
                    meta["canal_origen"] = canal_origen
                    logger.info(
                        f"[ConversationState] Canal actualizado: {current_canal} → {canal_origen} "
                        f"(teléfono: {phone})"
                    )
                
                # Actualizar otros campos si se proporcionan
                if contact_id and not meta.get("contact_id"):
                    meta["contact_id"] = contact_id
                if display_name and not meta.get("display_name"):
                    meta["display_name"] = display_name
                
                # Sincronizar assigned_owner_id con HubSpot (fuente de verdad)
                if owner_id and meta.get("assigned_owner_id") != owner_id:
                    old_owner = meta.get("assigned_owner_id")
                    meta["assigned_owner_id"] = owner_id
                    if old_owner:
                        logger.info(
                            f"[ConversationState] assigned_owner_id actualizado: "
                            f"{old_owner} → {owner_id} (teléfono: {phone})"
                        )
                    else:
                        logger.info(
                            f"[ConversationState] assigned_owner_id asignado en meta: {owner_id} "
                            f"(teléfono: {phone})"
                        )
                
                meta["last_activity"] = now_iso

                # Si add_to_zset=False y la meta fue creada temporalmente por update_activity
                # (señal _temp_meta=True), limpiar el ZSET entry para no mostrar contactos
                # sin señal comercial en el panel.
                # CRÍTICO: solo limpiar si _temp_meta=True — nunca tocar metas de contactos
                # existentes (in_panel=True sin _temp_meta), para no sacar del ZSET un
                # contacto HUMAN_ACTIVE/IN_CONVERSATION que ya estaba en atención.
                if not add_to_zset and meta.get("_temp_meta", False):
                    meta["in_panel"] = False
                    meta.pop("_temp_meta", None)
                    index_member = f"{phone}:{canal_safe}"
                    await self.redis.zrem(self.ACTIVE_CONTACTS_ZSET, index_member)
                    logger.debug(
                        f"[ConversationState] {phone} removido del ZSET (señal baja, sin intent comercial)"
                    )

                # ⚠️ 2026-05-30: TTL unificado a 365d (antes: dinámico 48-72h).
                ttl = await self.redis.ttl(meta_key)
                if not ttl or ttl <= 0 or ttl < self.PANEL_TTL_SECONDS:
                    ttl = self.PANEL_TTL_SECONDS
                await self.redis.set(meta_key, json.dumps(meta), ex=ttl)

            else:
                # Meta no existe - crear nuevo
                meta = {
                    "phone_normalized": phone,
                    "contact_id": contact_id,
                    "status": ConversationStatus.BOT_ACTIVE.value,
                    "last_activity": now_iso,
                    "canal_origen": canal_origen or canal_safe,
                    "display_name": display_name,
                    "created_at": now_iso,
                    # ✅ FIX: Incluir assigned_owner_id para filtrado correcto en panel de asesores
                    "assigned_owner_id": owner_id,
                    # Flag que controla si este contacto debe aparecer en el panel de asesoras
                    "in_panel": add_to_zset,
                }

                # Respetar marcador permanente: contacto que tuvo handoff siempre re-aparece
                marker_key = f"conv_was_panel:{phone}:{canal_safe}"
                had_panel_before = await self.redis.exists(marker_key)
                if had_panel_before:
                    add_to_zset = True
                    meta["in_panel"] = True
                    logger.info(f"[ConversationState] ↩ Re-entrada al panel (fue handoff antes): {phone}")

                # ⚠️ 2026-05-30: TTL meta unificado a 365d incluso para meta nuevo sin handoff.
                # Causa: contactos que entraban al panel via update_activity (lead nuevo)
                # tenían meta de 48-72h y al expirar el ghost cleanup los borraba.
                ttl = self.PANEL_TTL_SECONDS
                await self.redis.set(meta_key, json.dumps(meta), ex=ttl)

                # Agregar al ZSET solo si tiene intención comercial (add_to_zset=True)
                # Contactos de consulta general (info) no deben aparecer en el panel
                if add_to_zset:
                    index_member = f"{phone}:{canal_safe}"
                    score = get_bogota_now().timestamp()
                    await self.redis.zadd(self.ACTIVE_CONTACTS_ZSET, {index_member: score})

                logger.info(
                    f"[ConversationState] Meta creado con canal_origen={canal_origen or canal_safe}, "
                    f"assigned_owner_id={owner_id} (teléfono: {phone})"
                )
            
            return True
            
        except Exception as e:
            logger.error(f"[ConversationState] Error en ensure_meta_with_channel: {e}")
            return False

    async def transfer_contact(
        self,
        phone: str,
        to_owner_id: str,
        from_owner_id: str = None,
        canal: str = "whatsapp",
        mode: str = "exclusive",
        reason: str = None
    ) -> dict:
        """
        Transfiere un contacto a otro asesor.

        Args:
            phone: Teléfono del contacto
            to_owner_id: ID del asesor destino
            from_owner_id: ID del asesor origen (opcional, se obtiene de metadata)
            canal: Canal de la conversación
            mode: "exclusive" (cambio total) o "collaborative" (ambos ven)
            reason: Motivo de la transferencia

        Returns:
            dict con status, from_owner, to_owner, mode
        """
        try:
            canal_safe = canal.lower() if canal else "whatsapp"
            meta_key = f"{self.META_PREFIX}{phone}:{canal_safe}"

            data = await self.redis.get(meta_key)
            if not data:
                logger.warning(f"[ConversationState] No hay metadata para transferir: {phone}")
                return {"status": "error", "message": "Contacto no encontrado en sesión activa"}

            meta = json.loads(data)
            original_owner = from_owner_id or meta.get("assigned_owner_id")

            # Registrar en historial de transferencias
            transfer_record = {
                "from": original_owner,
                "to": to_owner_id,
                "mode": mode,
                "reason": reason,
                "timestamp": get_bogota_now_iso()
            }

            history = meta.get("transfer_history") or []
            history.append(transfer_record)
            meta["transfer_history"] = history

            if mode == "exclusive":
                # Transferencia exclusiva: cambiar owner completamente
                meta["assigned_owner_id"] = to_owner_id
                meta["primary_owner_id"] = to_owner_id
                meta["assigned_owner_ids"] = [to_owner_id]
            else:
                # Modo colaborativo: agregar al equipo
                owners = meta.get("assigned_owner_ids") or []
                if original_owner and original_owner not in owners:
                    owners.append(original_owner)
                if to_owner_id not in owners:
                    owners.append(to_owner_id)
                meta["assigned_owner_ids"] = owners
                meta["primary_owner_id"] = to_owner_id  # El nuevo es el principal

            # ⚠️ 2026-05-30: TTL unificado a 365d (antes: dinámico 48-72h).
            # transfer_contact siempre debe extender meta a TTL largo — es operación humana explícita.
            ttl = await self.redis.ttl(meta_key)
            if not ttl or ttl <= 0 or ttl < self.PANEL_TTL_SECONDS:
                ttl = self.PANEL_TTL_SECONDS
            await self.redis.set(meta_key, json.dumps(meta), ex=ttl)

            logger.info(
                f"[ConversationState] Contacto {phone} transferido: "
                f"{original_owner} -> {to_owner_id} (modo: {mode})"
            )

            # ⚠️ 2026-05-30: Sincronizar transferencia a MongoDB `conversations`.
            try:
                import asyncio as _asyncio
                from database.mongodb_client import get_mongo_manager as _gmm
                _asyncio.create_task(
                    _gmm().update_conversation_meta(
                        phone=phone,
                        canal=canal_safe,
                        owner_id=to_owner_id,
                    )
                )
            except Exception as _sync_err:
                logger.debug(f"[ConversationState] sync MongoDB conv no crítico: {_sync_err}")

            return {
                "status": "success",
                "from_owner": original_owner,
                "to_owner": to_owner_id,
                "mode": mode,
                "phone": phone,
                "transfer_history": history
            }

        except Exception as e:
            logger.error(f"[ConversationState] Error en transfer_contact: {e}")
            return {"status": "error", "message": str(e)}

    async def update_client_message_timestamp(self, phone: str, canal: str = "whatsapp") -> bool:
        """
        Actualiza el timestamp del último mensaje del cliente.
        También actualiza el score en ZSET para que el contacto suba al principio de la lista.
        """
        try:
            canal_safe = canal.lower() if canal else "whatsapp"
            meta_key = f"{self.META_PREFIX}{phone}:{canal_safe}"

            data = await self.redis.get(meta_key)
            if data:
                meta = json.loads(data)
                meta["last_client_message"] = get_bogota_now_iso()
                # ⚠️ 2026-05-30: TTL unificado a 365d (antes: dinámico 48-72h).
                ttl = await self.redis.ttl(meta_key)
                if not ttl or ttl <= 0 or ttl < self.PANEL_TTL_SECONDS:
                    ttl = self.PANEL_TTL_SECONDS
                await self.redis.set(meta_key, json.dumps(meta), ex=ttl)

                # ✅ FIX: Actualizar score en ZSET para que contacto suba arriba
                # Solo si el contacto ya está en el panel (in_panel=True o flag no existe = compat)
                if meta.get("in_panel", True):
                    index_member = f"{phone}:{canal_safe}"
                    score = get_bogota_now().timestamp()
                    await self.redis.zadd(self.ACTIVE_CONTACTS_ZSET, {index_member: score})
                    logger.info(f"[ConversationState] ↑ Contacto {phone} reordenado al principio (mensaje cliente)")

            return True
        except Exception as e:
            logger.error(f"[ConversationState] Error en update_client_message_timestamp: {e}")
            return False

    async def update_advisor_message_timestamp(self, phone: str, canal: str = "whatsapp") -> bool:
        """
        Actualiza el timestamp del último mensaje del asesor.
        También actualiza el score en ZSET para que el contacto suba al principio de la lista.
        """
        try:
            canal_safe = canal.lower() if canal else "whatsapp"
            meta_key = f"{self.META_PREFIX}{phone}:{canal_safe}"

            data = await self.redis.get(meta_key)
            if data:
                meta = json.loads(data)
                _now_iso = get_bogota_now_iso()
                meta["last_advisor_message"] = _now_iso
                meta["last_activity"] = _now_iso      # FIX: actualizar last_activity para sort correcto en GET /contacts
                # ⚠️ 2026-05-30: TTL unificado a 365d (antes: dinámico 48-72h).
                ttl = await self.redis.ttl(meta_key)
                if not ttl or ttl <= 0 or ttl < self.PANEL_TTL_SECONDS:
                    ttl = self.PANEL_TTL_SECONDS
                await self.redis.set(meta_key, json.dumps(meta), ex=ttl)

                # ✅ FIX: Actualizar score en ZSET para que contacto suba arriba
                index_member = f"{phone}:{canal_safe}"
                score = get_bogota_now().timestamp()
                await self.redis.zadd(self.ACTIVE_CONTACTS_ZSET, {index_member: score})
                logger.info(f"[ConversationState] ↑ Contacto {phone} reordenado al principio (mensaje asesor)")
                
            return True
        except Exception as e:
            logger.error(f"[ConversationState] Error en update_advisor_message_timestamp: {e}")
            return False

    # ─── Inbox de no-leídos por asesor ────────────────────────────────────────
    # Estructura: ZSET advisor_inbox:{advisor_id} → {phone:canal: unix_ts}
    # score = timestamp cuando se generó el no-leído (diagnóstico + ordenamiento)
    ADVISOR_INBOX_PREFIX = "advisor_inbox:"
    ADVISOR_INBOX_TTL    = 90 * 86400  # 90 días

    async def add_to_advisor_inbox(
        self,
        advisor_id: str,
        phone: str,
        canal: str,
        score: float = None
    ) -> None:
        """
        Agrega o actualiza un contacto en el inbox de no-leídos del asesor.
        ZADD es idempotente — si ya estaba, solo actualiza el score.

        También asegura que el contacto esté en el ZSET principal y que
        in_panel=True. Sin esto, contactos cerrados que reciben un nuevo
        mensaje quedan con badge en el inbox pero invisibles en el panel
        (el ZSET no los incluye si in_panel=False).
        """
        try:
            if not advisor_id or not phone:
                return
            canal_safe = (canal or "whatsapp").lower()
            member = f"{phone}:{canal_safe}"
            _score = score if score is not None else get_bogota_now().timestamp()
            key = f"{self.ADVISOR_INBOX_PREFIX}{advisor_id}"
            await self.redis.zadd(key, {member: _score})
            await self.redis.expire(key, self.ADVISOR_INBOX_TTL)

            # Asegurar visibilidad en el panel: re-agregar al ZSET + in_panel=True
            await self.redis.zadd(self.ACTIVE_CONTACTS_ZSET, {member: _score})
            meta_key = f"{self.META_PREFIX}{phone}:{canal_safe}"
            raw_meta = await self.redis.get(meta_key)
            if raw_meta:
                try:
                    meta = json.loads(raw_meta)
                    if not meta.get("in_panel", True):
                        meta["in_panel"] = True
                        ttl = await self.redis.ttl(meta_key)
                        if not ttl or ttl <= 0:
                            ttl = self.PANEL_TTL_SECONDS
                        await self.redis.set(meta_key, json.dumps(meta), ex=ttl)
                        logger.info(f"[Inbox][Add] Re-activado in_panel=True para {phone}")
                except Exception:
                    pass

            logger.info(
                f"[Inbox][Add] advisor={advisor_id} phone={phone} "
                f"canal={canal_safe} ts={_score:.0f}"
            )
        except Exception as e:
            logger.error(f"[Inbox][Error][Add] advisor={advisor_id} phone={phone}: {e}")

    async def remove_from_advisor_inbox(
        self,
        advisor_id: str,
        phone: str,
        canal: str = None
    ) -> None:
        """
        Marca un contacto como leído removiéndolo del inbox del asesor.

        Fix-D (Abr 2026): CANAL-AGNOSTIC. Hace ZRANGE del inbox y remueve
        TODOS los members que coincidan con el phone, sin importar el canal.
        Evita badge fantasma cuando canal-escritura ≠ canal-lectura
        (ej: contacto guardado como whatsapp pero panel lo devuelve como mercado_libre).
        """
        try:
            if not advisor_id or not phone:
                return
            key = f"{self.ADVISOR_INBOX_PREFIX}{advisor_id}"
            prefix = f"{phone}:"
            all_members = await self.redis.zrange(key, 0, -1)
            targets = []
            for m in all_members:
                if isinstance(m, bytes):
                    m = m.decode("utf-8", errors="ignore")
                if m == phone or m.startswith(prefix):
                    targets.append(m)

            removed = 0
            if targets:
                removed = await self.redis.zrem(key, *targets)

            logger.info(
                f"[Inbox][Clear] advisor={advisor_id} phone={phone} "
                f"removed={removed} targets={targets}"
            )
        except Exception as e:
            logger.error(f"[Inbox][Error][Clear] advisor={advisor_id} phone={phone}: {e}")

    async def get_inbox_unread_map(
        self,
        advisor_id: str,
        contacts: list
    ) -> dict:
        """
        Retorna {phone: bool} donde True = contacto tiene actividad no leída.
        contacts: lista de dicts con al menos {phone, canal}.

        Fix-D (Abr 2026): Lookup CANAL-AGNOSTIC.
        Antes usaba ZMSCORE con key `{phone}:{canal}`, pero el canal del inbox
        (canal de mensajería, ej: whatsapp) puede diferir del canal en
        get_active_contacts (canal_origen, ej: mercado_libre) → ZMSCORE=None
        → badge fantasma / badge faltante.

        Ahora hace ZRANGE 0 -1 (1 round-trip) y extrae solo los teléfonos,
        ignorando el canal del member. Para <~500 contactos por asesor es O(N)
        trivial (<5ms) y elimina el mismatch de canal por completo.

        Fail-safe: si Redis falla retorna {} → UI sigue funcionando sin badges.
        """
        if not contacts or not advisor_id:
            return {}
        try:
            key = f"{self.ADVISOR_INBOX_PREFIX}{advisor_id}"
            all_members = await self.redis.zrange(key, 0, -1)
            # Extraer solo el teléfono del member "{phone}:{canal}"
            unread_phones = set()
            for m in all_members:
                # Member puede venir como bytes o str según decode_responses
                if isinstance(m, bytes):
                    m = m.decode("utf-8", errors="ignore")
                if ":" in m:
                    unread_phones.add(m.split(":", 1)[0])
                else:
                    unread_phones.add(m)

            unread_map = {}
            for contact in contacts:
                phone = contact.get("phone", "")
                unread_map[phone] = (phone in unread_phones)

            no_leidos = sum(1 for v in unread_map.values() if v)
            leidos    = sum(1 for v in unread_map.values() if not v)
            logger.info(
                f"[Inbox][Batch] advisor={advisor_id} "
                f"no_leidos={no_leidos} leidos={leidos} total={len(contacts)} "
                f"inbox_size={len(unread_phones)}"
            )
            return unread_map
        except Exception as e:
            logger.error(f"[Inbox][Error][Batch] advisor={advisor_id}: {e}")
            return {}  # Fail-safe: sin badges antes que romper la UI

    async def get_all_inbox_phones(self, advisor_id: str) -> set:
        """Retorna set de phones con mensajes no leídos del asesor. O(N), 1 round-trip Redis."""
        try:
            key = f"{self.ADVISOR_INBOX_PREFIX}{advisor_id}"
            all_members = await self.redis.zrange(key, 0, -1)
            phones = set()
            for m in all_members:
                if isinstance(m, bytes):
                    m = m.decode("utf-8", errors="ignore")
                phones.add(m.split(":", 1)[0] if ":" in m else m)
            return phones
        except Exception as e:
            logger.error(f"[Inbox][Error][GetPhones] advisor={advisor_id}: {e}")
            return set()

    # ─── Notificaciones persistentes por asesor ───────────────────────────────
    # advisor_notifications:{advisor_id} → ZSET score=timestamp member=JSON
    ADVISOR_NOTIF_PREFIX  = "advisor_notifications:"
    ADVISOR_NOTIF_TTL     = 30 * 86400   # 30 días
    NOTIF_IDEM_PREFIX     = "notif_24h_sent:"
    NOTIF_IDEM_TTL        = 25 * 3600    # 25h — evita duplicados entre ciclos horarios
    NOTIF_APROBADOS_PREFIX = "notif_aprobados_sent:"

    async def add_advisor_notification(
        self,
        advisor_id: str,
        notif_type: str,
        payload: dict
    ) -> str:
        """
        Crea una notificación persistente para el asesor.
        notif_type: 'inactivity_24h' | 'aprobados_daily'
        Retorna el notif_id generado.
        """
        try:
            notif_id = str(uuid.uuid4())
            now_ts = get_bogota_now().timestamp()
            member = json.dumps(
                {"id": notif_id, "type": notif_type, "created_at": get_bogota_now_iso(), **payload},
                ensure_ascii=False
            )
            key = f"{self.ADVISOR_NOTIF_PREFIX}{advisor_id}"
            await self.redis.zadd(key, {member: now_ts})
            await self.redis.expire(key, self.ADVISOR_NOTIF_TTL)
            logger.info(f"[Notif][Add] advisor={advisor_id} type={notif_type} id={notif_id}")
            return notif_id
        except Exception as e:
            logger.error(f"[Notif][Error][Add] advisor={advisor_id}: {e}")
            return ""

    async def get_advisor_notifications(self, advisor_id: str) -> list:
        """Retorna notificaciones activas del asesor (más recientes primero)."""
        try:
            key = f"{self.ADVISOR_NOTIF_PREFIX}{advisor_id}"
            members = await self.redis.zrevrange(key, 0, -1)
            result = []
            for m in members:
                if isinstance(m, bytes):
                    m = m.decode("utf-8", errors="ignore")
                try:
                    result.append(json.loads(m))
                except json.JSONDecodeError:
                    pass
            return result
        except Exception as e:
            logger.error(f"[Notif][Error][Get] advisor={advisor_id}: {e}")
            return []

    async def remove_advisor_notification(self, advisor_id: str, notif_id: str) -> bool:
        """Elimina una notificación por su ID."""
        try:
            key = f"{self.ADVISOR_NOTIF_PREFIX}{advisor_id}"
            members = await self.redis.zrange(key, 0, -1)
            for m in members:
                if isinstance(m, bytes):
                    m = m.decode("utf-8", errors="ignore")
                try:
                    if json.loads(m).get("id") == notif_id:
                        await self.redis.zrem(key, m)
                        logger.info(f"[Notif][Remove] advisor={advisor_id} id={notif_id}")
                        return True
                except json.JSONDecodeError:
                    pass
            return False
        except Exception as e:
            logger.error(f"[Notif][Error][Remove] advisor={advisor_id} id={notif_id}: {e}")
            return False

    async def clear_inactivity_notifications_for_contact(
        self, advisor_id: str, phone: str
    ) -> int:
        """
        Elimina notificaciones 'inactivity_24h' de un contacto específico.
        Se llama cuando el asesor responde al contacto.
        """
        try:
            key = f"{self.ADVISOR_NOTIF_PREFIX}{advisor_id}"
            members = await self.redis.zrange(key, 0, -1)
            to_remove = []
            for m in members:
                if isinstance(m, bytes):
                    m = m.decode("utf-8", errors="ignore")
                try:
                    data = json.loads(m)
                    if data.get("type") == "inactivity_24h" and data.get("phone") == phone:
                        to_remove.append(m)
                except json.JSONDecodeError:
                    pass
            if to_remove:
                removed = await self.redis.zrem(key, *to_remove)
                logger.info(f"[Notif][ClearContact] advisor={advisor_id} phone={phone} removed={removed}")
                return removed
            return 0
        except Exception as e:
            logger.error(f"[Notif][Error][ClearContact] advisor={advisor_id} phone={phone}: {e}")
            return 0

    async def get_conversation_state(self, phone: str, canal: str = "whatsapp") -> Optional[Dict[str, Any]]:
        """
        Obtiene el estado completo de una conversación (status + metadata).

        Returns:
            dict con status y metadata, o None si no existe
        """
        try:
            canal_safe = canal.lower() if canal else "whatsapp"
            state_key = f"{self.STATE_PREFIX}{phone}:{canal_safe}"
            meta_key = f"{self.META_PREFIX}{phone}:{canal_safe}"

            status = await self.redis.get(state_key)
            meta_data = await self.redis.get(meta_key)

            if not status and not meta_data:
                return None

            result = {
                "status": status or "BOT_ACTIVE",
                "phone": phone,
                "canal": canal_safe
            }

            if meta_data:
                meta = json.loads(meta_data)
                result.update(meta)

            return result
        except Exception as e:
            logger.error(f"[ConversationState] Error en get_conversation_state: {e}")
            return None

    async def cleanup_duplicate_states(self, phone: str, keep_canal: str = None) -> int:
        """
        Limpia estados duplicados para un teléfono, manteniendo solo un canal.

        Args:
            phone: Número de teléfono normalizado
            keep_canal: Canal a mantener (si es None, mantiene el más restrictivo)

        Returns:
            Número de estados eliminados
        """
        try:
            deleted = 0
            members = await self.redis.zrange(self.ACTIVE_CONTACTS_ZSET, 0, -1)

            # Encontrar todos los canales para este teléfono
            phone_channels = []
            for member in members:
                if member.startswith(f"{phone}:"):
                    canal = member.split(":", 1)[1] if ":" in member else "whatsapp"
                    phone_channels.append(canal)

            if len(phone_channels) <= 1:
                return 0  # No hay duplicados

            # Determinar qué canal mantener
            if keep_canal and keep_canal in phone_channels:
                canal_to_keep = keep_canal
            else:
                # Mantener el más restrictivo (HUMAN_ACTIVE > PENDING_HANDOFF > IN_CONVERSATION > BOT_ACTIVE)
                priority = {"HUMAN_ACTIVE": 4, "PENDING_HANDOFF": 3, "IN_CONVERSATION": 2, "BOT_ACTIVE": 1}
                best_canal = phone_channels[0]
                best_priority = 0

                for canal in phone_channels:
                    state_key = f"{self.STATE_PREFIX}{phone}:{canal}"
                    status = await self.redis.get(state_key)
                    p = priority.get(status, 0)
                    if p > best_priority:
                        best_priority = p
                        best_canal = canal

                canal_to_keep = best_canal

            # Eliminar los demás
            for canal in phone_channels:
                if canal != canal_to_keep:
                    state_key = f"{self.STATE_PREFIX}{phone}:{canal}"
                    meta_key = f"{self.META_PREFIX}{phone}:{canal}"
                    index_member = f"{phone}:{canal}"

                    await self.redis.delete(state_key)
                    await self.redis.delete(meta_key)
                    await self.redis.zrem(self.ACTIVE_CONTACTS_ZSET, index_member)
                    deleted += 1

            logger.info(f"[ConversationState] Limpiados {deleted} estados duplicados para {phone}, manteniendo {canal_to_keep}")
            return deleted
        except Exception as e:
            logger.error(f"[ConversationState] Error en cleanup_duplicate_states: {e}")
            return 0

    async def cleanup_legacy_set(self):
        """
        Migración única: mueve cualquier miembro restante del SET legacy
        (active_conversations_index) al ZSET principal y elimina la clave.
        Se llama una sola vez en startup. Después de esto el SET no existe.
        """
        try:
            legacy_key = "active_conversations_index"
            legacy_members = await self.redis.smembers(legacy_key)
            if legacy_members:
                score = get_bogota_now().timestamp()
                pipe = self.redis.pipeline(transaction=False)
                for member in legacy_members:
                    # Solo migrar si no existe ya en el ZSET
                    pipe.zadd(self.ACTIVE_CONTACTS_ZSET, {member: score}, nx=True)
                await pipe.execute()
                logger.info(
                    f"[ConversationState] cleanup_legacy_set: "
                    f"migrados {len(legacy_members)} miembros de SET a ZSET"
                )
            await self.redis.delete(legacy_key)
            logger.info("[ConversationState] SET legacy 'active_conversations_index' eliminado de Redis")
        except Exception as e:
            logger.warning(f"[ConversationState] cleanup_legacy_set: {e} (no crítico)")

    async def close(self):
        """Cierra la conexión a Redis de forma segura."""
        try:
            if self.redis:
                await self.redis.aclose()
                logger.info("[ConversationState] Conexión a Redis cerrada correctamente")
        except Exception as e:
            logger.warning(f"[ConversationState] Error cerrando Redis: {e}")
