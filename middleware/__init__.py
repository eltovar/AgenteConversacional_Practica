# middleware/__init__.py
"""
Middleware Inteligente - Cerebro del Sistema Multi-Agente

Este módulo actúa como puente entre:
- Twilio (WhatsApp)
- HubSpot (CRM)
- Modelos de IA (LangChain + OpenAI)

Gestiona estados BOT_ACTIVE / HUMAN_ACTIVE para unificar atención
automatizada y humana bajo un solo número de WhatsApp.
"""

# Imports básicos que no requieren conexiones externas
from .phone_normalizer import PhoneNormalizer, normalize_colombian_phone
from .conversation_state import ConversationStatus, ConversationStateManager


def get_contact_manager():
    """Lazy import de ContactManager para evitar errores de conexión al importar."""
    from .contact_manager import ContactManager
    return ContactManager


def get_contact_info():
    """Lazy import de ContactInfo."""
    from .contact_manager import ContactInfo
    return ContactInfo


def get_sofia_brain():
    """Lazy import de SofiaBrain para evitar errores de dependencias."""
    from .sofia_brain import SofiaBrain
    return SofiaBrain


def get_whatsapp_router():
    """Lazy import del router de WhatsApp."""
    from .webhook_handler import router
    return router


__all__ = [
    # Normalización (sin dependencias externas)
    "PhoneNormalizer",
    "normalize_colombian_phone",
    # Estado de conversación
    "ConversationStatus",
    "ConversationStateManager",
    # Lazy loaders
    "get_contact_manager",
    "get_contact_info",
    "get_sofia_brain",
    "get_whatsapp_router",
]