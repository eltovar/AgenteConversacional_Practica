# integrations/hubspot/__init__.py
"""
Módulo de integración con HubSpot CRM API v3.

Componentes:
- hubspot_client.py: Cliente HTTP para interactuar con API de HubSpot
- hubspot_utils.py: Utilidades para normalización y validación de datos
"""

from .hubspot_client import HubSpotClient
from .hubspot_utils import normalize_phone_e164, calculate_lead_score

# Instancia global del cliente HubSpot
hubspot_client = HubSpotClient()

__all__ = [
    "HubSpotClient",
    "hubspot_client",
    "normalize_phone_e164",
    "calculate_lead_score"
]
