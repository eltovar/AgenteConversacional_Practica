# middleware/conversation_state.py
"""
Gestor de Estado de Conversación para el Middleware.
Maneja los estados BOT_ACTIVE / HUMAN_ACTIVE con conexión unificada a Redis.

OPTIMIZACIONES v2.1:
- Conexión unificada (Internal/Public URL) para evitar Split-Brain en Railway.
- Garantía de indexación en active_conversations_index.
- Consistencia total con CRMAgent.
"""

import json
import uuid
import os
from enum import Enum
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict, field
from zoneinfo import ZoneInfo

import redis.asyncio as redis
from logging_config import logger

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTES Y CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════════════════════

TIMEZONE_BOGOTA = ZoneInfo("America/Bogota")

def get_bogota_now() -> datetime:
    """Retorna datetime actual en zona horaria de Bogotá."""
    return datetime.now(TIMEZONE_BOGOTA)

class ConversationStatus(str, Enum):
    BOT_ACTIVE = "BOT_ACTIVE"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"
    IN_CONVERSATION = "IN_CONVERSATION"

@dataclass
class ConversationMeta:
    phone_normalized: str
    contact_id: Optional[str] = None
    status: str = "BOT_ACTIVE"
    last_activity: str = field(default_factory=lambda: get_bogota_now().isoformat())
    handoff_reason: Optional[str] = None
    assigned_owner_id: Optional[str] = None
    canal_origen: str = "whatsapp"
    display_name: Optional[str] = None
    version_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: get_bogota_now().isoformat())

class ConversationStateManager:
    """
    Gestiona el estado y la metadata de las conversaciones en Redis.
    """
    STATE_PREFIX = "conversation_state:"
    META_PREFIX = "conv_meta:"
    ACTIVE_CONTACTS_SET = "active_conversations_index"

    def __init__(self, redis_url: str = None):
        # LÓGICA DE CONEXIÓN UNIFICADA (Mismo protocolo que CRMAgent)
        if not redis_url:
            is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
            # En Railway preferimos la URL interna, fuera de ella la pública
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
        logger.info(f"[ConversationState] Inicializado con URL: {self.redis_url[:15]}...")

    async def activate_human(self, phone: str, canal: str, meta_data: Dict[str, Any], ex: int = 86400):
        """
        Pone al contacto en modo humano y lo indexa para el panel.
        """
        state_key = f"{self.STATE_PREFIX}{phone}:{canal}"
        meta_key = f"{self.META_PREFIX}{phone}:{canal}"
        index_member = f"{phone}:{canal}"

        try:
            # 1. Estado
            await self.redis.set(state_key, ConversationStatus.HUMAN_ACTIVE, ex=ex)
            
            # 2. Metadata
            if "status" not in meta_data:
                meta_data["status"] = ConversationStatus.HUMAN_ACTIVE
            await self.redis.set(meta_key, json.dumps(meta_data), ex=ex)
            
            # 3. ÍNDICE (CRÍTICO para visibilidad en panel)
            await self.redis.sadd(self.ACTIVE_CONTACTS_SET, index_member)
            
            logger.info(f"[ConversationState] {phone}:{canal} activado para Humano e indexado.")
            return True
        except Exception as e:
            logger.error(f"[ConversationState] Error en activate_human para {phone}: {e}")
            return False

    async def get_active_contacts(self) -> List[Dict[str, Any]]:
        """
        Obtiene todos los contactos indexados que tienen estado activo.
        """
        contacts = []
        try:
            # Obtener todos los miembros del índice
            members = await self.redis.smembers(self.ACTIVE_CONTACTS_SET)
            
            for member in members:
                if ":" not in member: continue
                phone, canal = member.split(":", 1)
                
                state_key = f"{self.STATE_PREFIX}{phone}:{canal}"
                status = await self.redis.get(state_key)
                
                # Si no hay estado, el contacto expiró; lo limpiamos del índice
                if not status:
                    await self.redis.srem(self.ACTIVE_CONTACTS_SET, member)
                    continue

                # Obtener meta
                meta_raw = await self.redis.get(f"{self.META_PREFIX}{phone}:{canal}")
                meta = json.loads(meta_raw) if meta_raw else {}
                
                contacts.append({
                    "phone": phone,
                    "canal": canal,
                    "status": status,
                    "display_name": meta.get("display_name", "Cliente"),
                    "last_activity": meta.get("last_activity"),
                    "owner_id": meta.get("assigned_owner_id"),
                    "contact_id": meta.get("contact_id")
                })
        except Exception as e:
            logger.error(f"[ConversationState] Error listando contactos: {e}")
        
        return contacts

    async def close(self):
        await self.redis.close()