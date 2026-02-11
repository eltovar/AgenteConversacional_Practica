# integrations/hubspot/__init__.py
"""
Módulo de integración con HubSpot CRM API v3.

Componentes:
- hubspot_client.py: Cliente HTTP para interactuar con API de HubSpot
- hubspot_utils.py: Utilidades para normalización y validación de datos
- contact_finder.py: Búsqueda robusta de contactos por teléfono
- timeline_logger.py: Registro de eventos en Timeline de HubSpot
- outbound_handler.py: Webhook para mensajes HubSpot -> WhatsApp
"""

from .hubspot_client import HubSpotClient
from .hubspot_utils import normalize_phone_e164, calculate_lead_score

# Instancia global del cliente HubSpot
hubspot_client = HubSpotClient()


# ═══════════════════════════════════════════════════════════════════════════════
# LAZY IMPORTS para nuevos módulos (evitar errores de conexión al importar)
# ═══════════════════════════════════════════════════════════════════════════════

def get_contact_finder():
    """Obtiene el ContactFinder para búsqueda de contactos."""
    from .contact_finder import get_contact_finder as _get_finder
    return _get_finder()


def get_timeline_logger():
    """Obtiene el TimelineLogger para registro de eventos."""
    from .timeline_logger import get_timeline_logger as _get_logger
    return _get_logger()


def get_outbound_handler():
    """Obtiene el OutboundHandler para mensajes salientes."""
    from .outbound_handler import get_outbound_handler as _get_handler
    return _get_handler()


def get_outbound_router():
    """Obtiene el router de FastAPI para webhooks de salida."""
    from .outbound_handler import router
    return router


__all__ = [
    # Cliente principal
    "HubSpotClient",
    "hubspot_client",
    # Utilidades
    "normalize_phone_e164",
    "calculate_lead_score",
    # Nuevos módulos (lazy)
    "get_contact_finder",
    "get_timeline_logger",
    "get_outbound_handler",
    "get_outbound_router",
]
