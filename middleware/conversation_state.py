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
    return datetime.now(TIMEZONE_BOGOTA)

def get_bogota_now_iso() -> str:
    return get_bogota_now().isoformat()

# ═══════════════════════════════════════════════════════════════════════════════
# MODELOS
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
        meta_key = f"{self.META_PREFIX}{phone}:{canal.lower()}"
        try:
            data = await self.redis.get(meta_key)
            if not data: return None
            payload = json.loads(data)
            
            # Mapeo de compatibilidad: si viene 'advisor' (CRM) o 'owner' usarlo como assigned_owner_id
            if 'advisor' in payload and not payload.get('assigned_owner_id'):
                payload['assigned_owner_id'] = payload['advisor']
            if 'phone' in payload and not payload.get('phone_normalized'):
                payload['phone_normalized'] = payload['phone']
            if 'name' in payload and not payload.get('display_name'):
                payload['display_name'] = payload['name']
                
            # Filtrar solo los campos que existen en la dataclass ConversationMeta de forma segura
            valid_fields = {f.name for f in fields(ConversationMeta)}
            filtered_payload = {k: v for k, v in payload.items() if k in valid_fields}
            
            return ConversationMeta(**filtered_payload)
        except Exception as e:
            logger.error(f"[ConversationState] Error en get_meta para {phone}: {e}")
            return None

    async def get_active_contacts(self) -> List[Dict[str, Any]]:
        """Retorna contactos asegurando que los campos de filtro existan."""
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
                
                # Construimos el diccionario con los nombres exactos que espera el filtro del panel
                contacts.append({
                    "phone": phone,
                    "canal": canal,
                    "status": status,
                    "display_name": (meta.display_name if meta else None) or "Cliente Nuevo",
                    "last_activity": meta.last_activity if meta else get_bogota_now_iso(),
                    "owner_id": meta.assigned_owner_id if meta else None,
                    "handoff_reason": meta.handoff_reason if meta else "Transferencia manual",
                    "ttl_remaining": ttl,
                    "contact_id": meta.contact_id if meta else None
                })
        except Exception as e:
            logger.error(f"[ConversationState] Error en lista: {e}")
        return contacts

    async def get_all_human_active_contacts(self) -> List[Dict[str, Any]]:
        return await self.get_active_contacts()

    async def close(self):
        await self.redis.close()