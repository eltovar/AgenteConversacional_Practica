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

# ═══════════════════════════════════════════════════════════════════════════════
# GESTOR DE ESTADO
# ═══════════════════════════════════════════════════════════════════════════════

class ConversationStateManager:
    """Maneja la persistencia de estados y metadatos en Redis."""
    STATE_PREFIX = "conv_state:"
    META_PREFIX = "conv_meta:"
    ACTIVE_CONTACTS_SET = "active_conversations_index"  # Legacy SET (compatibilidad)
    ACTIVE_CONTACTS_ZSET = "active_conversations_sorted"  # Nuevo ZSET ordenado por timestamp
    
    # TTL dinámico: 24h días laborales, 72h fines de semana (para que duren hasta el lunes)
    HANDOFF_TTL_SECONDS = 86400  # Default 24h (días laborales)
    HANDOFF_TTL_WEEKEND = 259200  # 72h para fines de semana
    
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

    async def get_active_contacts(self) -> List[Dict[str, Any]]:
        """
        Retorna la lista de contactos activos ordenados por última actividad.

        Usa ZSET (sorted set) con score = timestamp Unix para ordenamiento.
        Los contactos con mensajes más recientes aparecen primero.
        """
        contacts = []
        try:
            # Usar ZSET para obtener contactos ordenados (más reciente primero)
            # zrevrange retorna en orden descendente por score
            members = await self.redis.zrevrange(self.ACTIVE_CONTACTS_ZSET, 0, -1)

            # Fallback: Si ZSET está vacío, intentar migrar desde SET legacy
            if not members:
                legacy_members = await self.redis.smembers(self.ACTIVE_CONTACTS_SET)
                if legacy_members:
                    logger.info(f"[ConversationState] Migrando {len(legacy_members)} contactos de SET a ZSET")
                    for legacy_member in legacy_members:
                        # Migrar con timestamp actual (no tenemos el original)
                        score = get_bogota_now().timestamp()
                        await self.redis.zadd(self.ACTIVE_CONTACTS_ZSET, {legacy_member: score})
                    # Ahora sí obtener del ZSET
                    members = await self.redis.zrevrange(self.ACTIVE_CONTACTS_ZSET, 0, -1)

            for member in members:
                if ":" not in member: continue
                phone, canal = member.split(":", 1)
                state_key = f"{self.STATE_PREFIX}{phone}:{canal}"
                status = await self.redis.get(state_key)
                
                meta = await self.get_meta(phone, canal)

                if not status:
                    # TTL expiró: Sofía retomará la conversación.
                    # NO eliminar del panel — las asesoras deben seguir
                    # viendo el contacto y su historial. Se muestra con
                    # estado BOT_ACTIVE para indicar que está en modo bot.
                    if not meta:
                        # Sin meta tampoco: contacto fantasma, limpiar silenciosamente
                        await self.redis.zrem(self.ACTIVE_CONTACTS_ZSET, member)
                        await self.redis.srem(self.ACTIVE_CONTACTS_SET, member)
                        continue
                    status = ConversationStatus.BOT_ACTIVE.value
                    logger.debug(
                        f"[ConversationState] TTL expirado para {phone}:{canal} — "
                        f"mostrando como BOT_ACTIVE en el panel"
                    )

                ttl = await self.redis.ttl(state_key)

                contacts.append({
                    "phone": phone,
                    "canal": canal,
                    "status": status,
                    "display_name": (meta.display_name if meta else None) or "Cliente Nuevo",
                    "last_activity": meta.last_activity if meta else get_bogota_now_iso(),
                    "owner_id": meta.assigned_owner_id if meta else None,
                    "assigned_owner_ids": meta.assigned_owner_ids if meta else [],  # Colaboradores (modo collaborative)
                    "handoff_reason": meta.handoff_reason if meta else "Transferencia manual",
                    "ttl_remaining": ttl,
                    "contact_id": meta.contact_id if meta else None,
                    # === CAMPOS CRÍTICOS PARA EL PANEL ===
                    "is_active": True,  # Marcar como activo para el panel
                    "canal_origen": (meta.canal_origen if meta else canal) or canal,  # Segregación por equipo
                    "activated_at": meta.created_at if meta else get_bogota_now_iso(),  # Filtro de tiempo
                    "conversation_status": status  # Estado real de Redis (HUMAN_ACTIVE/IN_CONVERSATION)
                })
        except Exception as e:
            logger.error(f"[ConversationState] Error en get_active_contacts: {e}")
        return contacts

    async def get_all_human_active_contacts(self) -> List[Dict[str, Any]]:
        """Alias para obtener contactos activos."""
        return await self.get_active_contacts()

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
        También elimina del índice de contactos activos.

        Returns:
            True si se reactivó correctamente
        """
        try:
            # Cambiar estado a BOT_ACTIVE
            await self.set_status(phone, ConversationStatus.BOT_ACTIVE, canal)

            # Remover del índice de contactos activos (ZSET + SET legacy)
            index_member = f"{phone}:{canal.lower()}"
            await self.redis.zrem(self.ACTIVE_CONTACTS_ZSET, index_member)
            await self.redis.srem(self.ACTIVE_CONTACTS_SET, index_member)  # Legacy cleanup

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
            # 1. Guardar estado HUMAN_ACTIVE con TTL dinámico (fin de semana = 72h)
            ttl = self._calculate_dynamic_ttl()
            state_key = f"{self.STATE_PREFIX}{phone_num}:{canal_safe}"
            await self.redis.set(state_key, ConversationStatus.HUMAN_ACTIVE.value, ex=ttl)

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
            await self.redis.set(meta_key, json.dumps(meta), ex=ttl)

            # 3. Agregar al índice de contactos activos (ZSET ordenado por timestamp)
            index_member = f"{phone_num}:{canal_safe}"
            score = get_bogota_now().timestamp()  # Unix timestamp como score
            await self.redis.zadd(self.ACTIVE_CONTACTS_ZSET, {index_member: score})
            await self.redis.sadd(self.ACTIVE_CONTACTS_SET, index_member)  # Legacy compatibilidad

            logger.info(f"[ConversationState] HUMAN_ACTIVE activado: {phone_num}:{canal_safe}")
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
            # TTL dinámico: 24h días laborales, 72h fines de semana
            ttl = self._calculate_dynamic_ttl()
            canal_safe = canal.lower() if canal else "whatsapp"
            state_key = f"{self.STATE_PREFIX}{phone}:{canal_safe}"
            await self.redis.set(state_key, ConversationStatus.PENDING_HANDOFF.value, ex=ttl)

            # Guardar metadata con la razón del handoff
            now_iso = get_bogota_now_iso()
            meta = {
                "phone_normalized": phone,
                "contact_id": contact_id,
                "status": ConversationStatus.PENDING_HANDOFF.value,
                "last_activity": now_iso,
                "handoff_reason": reason,
                "canal_origen": canal,
                "created_at": now_iso
            }
            meta_key = f"{self.META_PREFIX}{phone}:{canal_safe}"
            await self.redis.set(meta_key, json.dumps(meta), ex=ttl)

            # Agregar al índice de contactos activos (ZSET ordenado por timestamp)
            index_member = f"{phone}:{canal_safe}"
            score = get_bogota_now().timestamp()  # Unix timestamp como score
            await self.redis.zadd(self.ACTIVE_CONTACTS_ZSET, {index_member: score})
            await self.redis.sadd(self.ACTIVE_CONTACTS_SET, index_member)  # Legacy compatibilidad

            logger.info(f"[ConversationState] Handoff solicitado: {phone}:{canal_safe} - {reason}")
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
                ttl = self._calculate_dynamic_ttl()
                await self.redis.set(meta_key, json.dumps(meta), ex=ttl)

                # Actualizar score en ZSET para reordenamiento
                index_member = f"{phone}:{canal_safe}"
                score = get_bogota_now().timestamp()
                await self.redis.zadd(self.ACTIVE_CONTACTS_ZSET, {index_member: score})

            return True
        except Exception as e:
            logger.error(f"[ConversationState] Error en update_activity: {e}")
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

            # Guardar metadata actualizada
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
                ttl = self._calculate_dynamic_ttl()
                await self.redis.set(meta_key, json.dumps(meta), ex=ttl)
                
                # ✅ FIX: Actualizar score en ZSET para que contacto suba arriba
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
            # Usar ZSET como fuente principal, fallback a SET legacy
            members = await self.redis.zrange(self.ACTIVE_CONTACTS_ZSET, 0, -1)
            if not members:
                members = await self.redis.smembers(self.ACTIVE_CONTACTS_SET)

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
                    await self.redis.srem(self.ACTIVE_CONTACTS_SET, index_member)  # Legacy
                    deleted += 1

            logger.info(f"[ConversationState] Limpiados {deleted} estados duplicados para {phone}, manteniendo {canal_to_keep}")
            return deleted
        except Exception as e:
            logger.error(f"[ConversationState] Error en cleanup_duplicate_states: {e}")
            return 0

    async def close(self):
        """Cierra la conexión a Redis de forma segura."""
        try:
            if self.redis:
                await self.redis.aclose()
                logger.info("[ConversationState] Conexión a Redis cerrada correctamente")
        except Exception as e:
            logger.warning(f"[ConversationState] Error cerrando Redis: {e}")