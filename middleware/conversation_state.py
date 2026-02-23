# middleware/conversation_state.py
"""
Gestor de Estado de Conversación para el Middleware.
Solución a errores de variables indefinidas y sincronización de Redis.
v2.1: Corregido error de argumentos faltantes en get_meta (Pylint E1120).
"""

import json
import uuid
import os
from enum import Enum
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from dataclasses import dataclass, field
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

def parse_datetime_safe(dt_str: Any) -> Optional[datetime]:
    """Convierte string ISO a objeto datetime de forma segura."""
    if not dt_str or not isinstance(dt_str, str):
        return None
    try:
        return datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
    except Exception:
        return None

# ═══════════════════════════════════════════════════════════════════════════════
# ENUMS Y MODELOS
# ═══════════════════════════════════════════════════════════════════════════════

class ConversationStatus(str, Enum):
    BOT_ACTIVE = "BOT_ACTIVE"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"
    IN_CONVERSATION = "IN_CONVERSATION"

@dataclass
class ConversationMeta:
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
    """
    Gestiona el estado (Bot/Humano) y la indexación de contactos en Redis.
    """
    STATE_PREFIX = "conv_state:"
    META_PREFIX = "conv_meta:"
    ACTIVE_CONTACTS_SET = "active_conversations_index"
    HANDOFF_TTL_SECONDS = 86400  # 24 Horas

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
        logger.info(f"[ConversationState] Conectado a Redis: {self.redis_url[:20]}...")

    async def get_status(self, phone: str, canal: str = "whatsapp") -> str:
        """Obtiene el estado actual. Canal opcional para compatibilidad."""
        state_key = f"{self.STATE_PREFIX}{phone}:{canal.lower()}"
        status = await self.redis.get(state_key)
        return status or ConversationStatus.BOT_ACTIVE

    async def is_bot_active(self, phone: str, canal: str = "whatsapp") -> bool:
        """Verifica si el bot está manejando la charla."""
        status = await self.get_status(phone, canal)
        return status == ConversationStatus.BOT_ACTIVE

    async def get_meta(self, phone: str, canal: str = "whatsapp") -> Optional[ConversationMeta]:
        """
        Obtiene la metadata de un contacto desde Redis.
        Se agrega canal="whatsapp" por defecto para evitar Pylint E1120 en app.py.
        """
        meta_key = f"{self.META_PREFIX}{phone}:{canal.lower()}"
        try:
            data = await self.redis.get(meta_key)
            if not data:
                return None
            
            payload = json.loads(data)
            return ConversationMeta(**payload)
        except Exception as e:
            logger.error(f"[ConversationState] Error leyendo meta para {phone}: {e}")
            return None

    async def activate_human(self, phone: str, canal: str, meta_data: Dict[str, Any]):
        """Activa el modo humano."""
        canal_safe = canal.lower()
        state_key = f"{self.STATE_PREFIX}{phone}:{canal_safe}"
        meta_key = f"{self.META_PREFIX}{phone}:{canal_safe}"
        index_member = f"{phone}:{canal_safe}"

        try:
            await self.redis.set(state_key, ConversationStatus.HUMAN_ACTIVE, ex=self.HANDOFF_TTL_SECONDS)
            
            if "status" not in meta_data:
                meta_data["status"] = ConversationStatus.HUMAN_ACTIVE
            if "created_at" not in meta_data:
                meta_data["created_at"] = get_bogota_now_iso()
            
            await self.redis.set(meta_key, json.dumps(meta_data), ex=self.HANDOFF_TTL_SECONDS)
            await self.redis.sadd(self.ACTIVE_CONTACTS_SET, index_member)
            
            logger.info(f"[ConversationState] {phone} pasado a HUMANO.")
            return True
        except Exception as e:
            logger.error(f"[ConversationState] Fallo al activar humano: {e}")
            return False

    async def activate_bot(self, phone: str, canal: str = "whatsapp"):
        """Devuelve el control al Bot."""
        canal_safe = canal.lower()
        state_key = f"{self.STATE_PREFIX}{phone}:{canal_safe}"
        meta_key = f"{self.META_PREFIX}{phone}:{canal_safe}"
        index_member = f"{phone}:{canal_safe}"

        try:
            await self.redis.delete(state_key)
            await self.redis.delete(meta_key)
            await self.redis.srem(self.ACTIVE_CONTACTS_SET, index_member)
            
            logger.info(f"[ConversationState] {phone} devuelto al BOT.")
            return True
        except Exception as e:
            logger.error(f"[ConversationState] Fallo al activar bot: {e}")
            return False

    async def get_active_contacts(self) -> List[Dict[str, Any]]:
        """Retorna la lista de contactos activos para el panel."""
        contacts = []
        try:
            members = await self.redis.smembers(self.ACTIVE_CONTACTS_SET)
            for member in members:
                if ":" not in member: continue
                phone, canal = member.split(":", 1)
                state_key = f"{self.STATE_PREFIX}{phone}:{canal}"
                status = await self.redis.get(state_key)
                
                if not status:
                    await self.redis.srem(self.ACTIVE_CONTACTS_SET, member)
                    continue

                meta = await self.get_meta(phone, canal)
                ttl = await self.redis.ttl(state_key)
                contacts.append({
                    "phone": phone,
                    "canal": canal,
                    "status": status,
                    "display_name": meta.display_name if meta else "Cliente",
                    "last_activity": meta.last_activity if meta else None,
                    "owner_id": meta.assigned_owner_id if meta else None,
                    "handoff_reason": meta.handoff_reason if meta else None,
                    "ttl_remaining": ttl
                })
        except Exception as e:
            logger.error(f"[ConversationState] Error en lista de contactos: {e}")
        return contacts

    async def get_all_human_active_contacts(self) -> List[Dict[str, Any]]:
        """Alias para compatibilidad con versiones anteriores de app.py."""
        return await self.get_active_contacts()

    async def close(self):
        """Cierra la conexión a Redis."""
        await self.redis.close()