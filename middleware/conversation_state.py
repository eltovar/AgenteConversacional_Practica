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

# ═══════════════════════════════════════════════════════════════════════════════
# GESTOR DE ESTADO
# ═══════════════════════════════════════════════════════════════════════════════

class ConversationStateManager:
    """Maneja la persistencia de estados y metadatos en Redis."""
    STATE_PREFIX = "conv_state:"
    META_PREFIX = "conv_meta:"
    ACTIVE_CONTACTS_SET = "active_conversations_index"
    HANDOFF_TTL_SECONDS = 86400

    def __init__(self, redis_url: str = None):
        if not redis_url:
            is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
            self.redis_url = os.getenv("REDIS_URL") if is_railway else (
                os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
            )
        else:
            self.redis_url = redis_url
            
        self.redis = redis.from_url(
            self.redis_url, 
            encoding="utf-8", 
            decode_responses=True,
            socket_timeout=5.0
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
        """Retorna la lista de contactos activos procesando el índice de Redis."""
        contacts = []
        try:
            members = await self.redis.smembers(self.ACTIVE_CONTACTS_SET)
            for member in members:
                if ":" not in member: continue
                phone, canal = member.split(":", 1)
                state_key = f"{self.STATE_PREFIX}{phone}:{canal}"
                status = await self.redis.get(state_key)
                
                if not status:
                    # Limpieza automática si la llave de estado expiró
                    await self.redis.srem(self.ACTIVE_CONTACTS_SET, member)
                    continue

                meta = await self.get_meta(phone, canal)
                ttl = await self.redis.ttl(state_key)
                
                contacts.append({
                    "phone": phone,
                    "canal": canal,
                    "status": status,
                    "display_name": (meta.display_name if meta else None) or "Cliente Nuevo",
                    "last_activity": meta.last_activity if meta else get_bogota_now_iso(),
                    "owner_id": meta.assigned_owner_id if meta else None,
                    "handoff_reason": meta.handoff_reason if meta else "Transferencia manual",
                    "ttl_remaining": ttl,
                    "contact_id": meta.contact_id if meta else None,
                    # === CAMPOS CRÍTICOS PARA EL PANEL ===
                    "is_active": True,  # Marcar como activo para el panel
                    "canal_origen": (meta.canal_origen if meta else canal) or canal,  # Segregación por equipo
                    "activated_at": meta.created_at if meta else get_bogota_now_iso(),  # Filtro de tiempo
                    "conversation_status": "active"  # Badge "En espera" del panel
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
            ttl = ttl or self.HANDOFF_TTL_SECONDS
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

            # Remover del índice de contactos activos
            index_member = f"{phone}:{canal.lower()}"
            await self.redis.srem(self.ACTIVE_CONTACTS_SET, index_member)

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
            # 1. Guardar estado HUMAN_ACTIVE
            state_key = f"{self.STATE_PREFIX}{phone_num}:{canal_safe}"
            await self.redis.set(state_key, ConversationStatus.HUMAN_ACTIVE.value, ex=self.HANDOFF_TTL_SECONDS)

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
            await self.redis.set(meta_key, json.dumps(meta), ex=self.HANDOFF_TTL_SECONDS)

            # 3. Agregar al índice de contactos activos
            index_member = f"{phone_num}:{canal_safe}"
            await self.redis.sadd(self.ACTIVE_CONTACTS_SET, index_member)

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
            canal_safe = canal.lower() if canal else "whatsapp"
            state_key = f"{self.STATE_PREFIX}{phone}:{canal_safe}"
            await self.redis.set(state_key, ConversationStatus.PENDING_HANDOFF.value, ex=self.HANDOFF_TTL_SECONDS)

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
            await self.redis.set(meta_key, json.dumps(meta), ex=self.HANDOFF_TTL_SECONDS)

            # Agregar al índice de contactos activos
            index_member = f"{phone}:{canal_safe}"
            await self.redis.sadd(self.ACTIVE_CONTACTS_SET, index_member)

            logger.info(f"[ConversationState] Handoff solicitado: {phone}:{canal_safe} - {reason}")
            return True
        except Exception as e:
            logger.error(f"[ConversationState] Error en request_handoff: {e}")
            return False

    async def update_activity(self, phone: str, canal: str = "whatsapp") -> bool:
        """Actualiza el timestamp de última actividad."""
        try:
            canal_safe = canal.lower() if canal else "whatsapp"
            meta_key = f"{self.META_PREFIX}{phone}:{canal_safe}"

            data = await self.redis.get(meta_key)
            if data:
                meta = json.loads(data)
                meta["last_activity"] = get_bogota_now_iso()
                await self.redis.set(meta_key, json.dumps(meta), ex=self.HANDOFF_TTL_SECONDS)
            return True
        except Exception as e:
            logger.error(f"[ConversationState] Error en update_activity: {e}")
            return False

    async def update_client_message_timestamp(self, phone: str, canal: str = "whatsapp") -> bool:
        """Actualiza el timestamp del último mensaje del cliente."""
        try:
            canal_safe = canal.lower() if canal else "whatsapp"
            meta_key = f"{self.META_PREFIX}{phone}:{canal_safe}"

            data = await self.redis.get(meta_key)
            if data:
                meta = json.loads(data)
                meta["last_client_message"] = get_bogota_now_iso()
                await self.redis.set(meta_key, json.dumps(meta), ex=self.HANDOFF_TTL_SECONDS)
            return True
        except Exception as e:
            logger.error(f"[ConversationState] Error en update_client_message_timestamp: {e}")
            return False

    async def update_advisor_message_timestamp(self, phone: str, canal: str = "whatsapp") -> bool:
        """Actualiza el timestamp del último mensaje del asesor."""
        try:
            canal_safe = canal.lower() if canal else "whatsapp"
            meta_key = f"{self.META_PREFIX}{phone}:{canal_safe}"

            data = await self.redis.get(meta_key)
            if data:
                meta = json.loads(data)
                meta["last_advisor_message"] = get_bogota_now_iso()
                await self.redis.set(meta_key, json.dumps(meta), ex=self.HANDOFF_TTL_SECONDS)
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
                    await self.redis.srem(self.ACTIVE_CONTACTS_SET, index_member)
                    deleted += 1

            logger.info(f"[ConversationState] Limpiados {deleted} estados duplicados para {phone}, manteniendo {canal_to_keep}")
            return deleted
        except Exception as e:
            logger.error(f"[ConversationState] Error en cleanup_duplicate_states: {e}")
            return 0

    async def close(self):
        """Cierra la conexión a Redis."""
        await self.redis.close()