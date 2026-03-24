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
from datetime import datetime
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
            ghosts_to_remove = []
            to_move_to_bot_set = []

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
                    base = i * 4
                    status_raw = results[base]
                    meta_raw   = results[base + 1]
                    score      = results[base + 2]
                    ttl        = results[base + 3]

                    meta = self._parse_meta_raw(meta_raw)

                    if not status_raw:
                        if not meta:
                            ghosts_to_remove.append(member)
                            continue
                        status_raw = ConversationStatus.BOT_ACTIVE.value
                        logger.debug(
                            f"[ConversationState] TTL expirado para {phone}:{canal} — "
                            f"mostrando como BOT_ACTIVE en el panel"
                        )

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
                        "activated_at": meta.created_at if meta else get_bogota_now_iso(),
                        "conversation_status": status_raw,
                        "in_priority_zset": in_priority_zset,
                        "deal_id": meta.deal_id if meta else None,
                        "deal_stage": meta.deal_stage if meta else None,
                    })

            # ── Paso 3: Escrituras pendientes en un solo pipeline ─────────────────────
            if ghosts_to_remove or to_move_to_bot_set:
                write_pipe = self.redis.pipeline(transaction=False)
                for member in ghosts_to_remove:
                    write_pipe.zrem(self.ACTIVE_CONTACTS_ZSET, member)
                for member in to_move_to_bot_set:
                    write_pipe.sadd(self.BOT_CONTROLLED_SET, member)
                    write_pipe.zrem(self.ACTIVE_CONTACTS_ZSET, member)
                await write_pipe.execute()

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

                bot_ghosts = []
                for i, member in enumerate(bot_to_process):
                    phone, canal = member.split(":", 1)
                    base = i * 3
                    meta_raw   = results2[base]
                    status_raw = results2[base + 1]
                    ttl        = results2[base + 2]

                    meta = self._parse_meta_raw(meta_raw)
                    if not meta:
                        bot_ghosts.append(member)
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
                        "activated_at": meta.created_at,
                        "conversation_status": status,
                        "in_priority_zset": False,
                        "deal_id": meta.deal_id,
                        "deal_stage": meta.deal_stage,
                    })

                if bot_ghosts:
                    clean_pipe = self.redis.pipeline(transaction=False)
                    for member in bot_ghosts:
                        clean_pipe.srem(self.BOT_CONTROLLED_SET, member)
                    await clean_pipe.execute()

        except Exception as e:
            logger.error(f"[ConversationState] Error en get_active_contacts: {e}")
        return contacts

    async def get_all_human_active_contacts(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """Alias para obtener contactos activos."""
        return await self.get_active_contacts(limit=limit, offset=offset)

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
            meta = {
                "phone_normalized": phone_num,
                "contact_id": contact_id,
                "status": ConversationStatus.HUMAN_ACTIVE.value,
                "last_activity": now_iso,
                "handoff_reason": reason,
                "assigned_owner_id": owner_id,
                "canal_origen": canal_origen,
                "display_name": display_name,
                "created_at": now_iso
            }

            meta_key = f"{self.META_PREFIX}{phone_num}:{canal_safe}"
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
                # Preservar TTL existente (puede ser PANEL_TTL_SECONDS=365d para contactos con handoff)
                ttl = await self.redis.ttl(meta_key)
                if not ttl or ttl <= 0:
                    ttl = self._calculate_dynamic_ttl()
                await self.redis.set(meta_key, json.dumps(meta), ex=ttl)

                # Actualizar score en ZSET para reordenamiento
                # Solo si el contacto ya está en el panel (in_panel=True o flag no existe = compat)
                if meta.get("in_panel", True):
                    index_member = f"{phone}:{canal_safe}"
                    score = get_bogota_now().timestamp()
                    await self.redis.zadd(self.ACTIVE_CONTACTS_ZSET, {index_member: score})

            else:
                # Contacto nuevo sin meta: crear entrada mínima temporal para que GET /contacts
                # lo retorne inmediatamente cuando el panel llama tras recibir el WS.
                # ensure_meta_with_channel() sobreescribirá con datos completos (~30s después).
                minimal_meta = {
                    "phone_normalized": phone,
                    "canal_origen": canal_safe,
                    "last_activity": get_bogota_now_iso(),
                    "in_panel": True,
                    "display_name": None,
                    "_temp_meta": True,
                }
                await self.redis.set(meta_key, json.dumps(minimal_meta), ex=self._calculate_dynamic_ttl())
                index_member = f"{phone}:{canal_safe}"
                score = get_bogota_now().timestamp()
                await self.redis.zadd(self.ACTIVE_CONTACTS_ZSET, {index_member: score})
                logger.debug(
                    f"[ConversationState] Meta temporal creado para contacto nuevo {phone}:{canal_safe}"
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
                
                # ✅ FIX: Actualizar assigned_owner_id si se proporciona y no existe en meta
                # Esto es CRÍTICO para que el panel de asesores filtre correctamente
                if owner_id and not meta.get("assigned_owner_id"):
                    meta["assigned_owner_id"] = owner_id
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

                ttl = await self.redis.ttl(meta_key)
                ex = ttl if ttl and ttl > 0 else self._calculate_dynamic_ttl()
                await self.redis.set(meta_key, json.dumps(meta), ex=ex)
                
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

                ttl = self.PANEL_TTL_SECONDS if had_panel_before else self._calculate_dynamic_ttl()
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

            # Guardar metadata actualizada preservando TTL existente
            ttl = await self.redis.ttl(meta_key)
            if not ttl or ttl <= 0:
                ttl = self._calculate_dynamic_ttl()
            await self.redis.set(meta_key, json.dumps(meta), ex=ttl)

            logger.info(
                f"[ConversationState] Contacto {phone} transferido: "
                f"{original_owner} -> {to_owner_id} (modo: {mode})"
            )

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
                # Preservar TTL existente (puede ser PANEL_TTL_SECONDS=365d para contactos con handoff)
                ttl = await self.redis.ttl(meta_key)
                if not ttl or ttl <= 0:
                    ttl = self._calculate_dynamic_ttl()
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
                meta["last_advisor_message"] = get_bogota_now_iso()
                # Preservar TTL existente (puede ser PANEL_TTL_SECONDS=365d para contactos con handoff)
                ttl = await self.redis.ttl(meta_key)
                if not ttl or ttl <= 0:
                    ttl = self._calculate_dynamic_ttl()
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