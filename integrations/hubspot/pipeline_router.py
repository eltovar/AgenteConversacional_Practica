# integrations/hubspot/pipeline_router.py
"""
Router de Pipelines basado en Canal de Origen.
Determina el pipeline, stage y owner correcto para cada lead según su fuente.
"""

import os
from typing import Dict, Any, Optional
from logging_config import logger


# ═══════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN DE CANALES
# ═══════════════════════════════════════════════════════════════════════════

# Canales que van al Pipeline de Redes Sociales
CANALES_REDES_SOCIALES = [
    "instagram",
    "facebook",
    "linkedin",
    "youtube",
    "tiktok",
]

# Canales que van al Pipeline de Ventas General (Portales Inmobiliarios)
CANALES_PORTALES = [
    "finca_raiz",
    "metrocuadrado",
    "mercado_libre",
    "ciencuadras",
    "pagina_web",
]

# Mapeo de canal a categoría de HubSpot Analytics
CANAL_TO_ANALYTICS_SOURCE = {
    # Redes Sociales
    "instagram": "SOCIAL_MEDIA",
    "facebook": "SOCIAL_MEDIA",
    "linkedin": "SOCIAL_MEDIA",
    "youtube": "SOCIAL_MEDIA",
    "tiktok": "SOCIAL_MEDIA",
    # Portales / Orgánico
    "finca_raiz": "ORGANIC_SEARCH",
    "metrocuadrado": "ORGANIC_SEARCH",
    "mercado_libre": "ORGANIC_SEARCH",
    "ciencuadras": "ORGANIC_SEARCH",
    "pagina_web": "DIRECT_TRAFFIC",
    # Otros
    "whatsapp_directo": "DIRECT_TRAFFIC",
    "google_ads": "PAID_SEARCH",
    "referido": "REFERRALS",
    "desconocido": "OTHER_CAMPAIGNS",
}


# ═══════════════════════════════════════════════════════════════════════════
# FUNCIONES DE ROUTING
# ═══════════════════════════════════════════════════════════════════════════

def get_target_pipeline(channel: str) -> Dict[str, Any]:
    """
    Determina Pipeline, Stage y Owner basándose en el canal de origen.
    """
    channel_clean = channel.lower().strip() if channel else "desconocido"

    # Determinar analytics source
    analytics_source = CANAL_TO_ANALYTICS_SOURCE.get(channel_clean, "OTHER_CAMPAIGNS")

    if channel_clean in CANALES_REDES_SOCIALES:
        pipeline_id = os.getenv("HUBSPOT_PIPELINE_REDES_ID")
        stage_id = os.getenv("HUBSPOT_STAGE_NUEVO_RS")
        owner_id = os.getenv("OWNER_ID_REDES")

        # Validar que las variables estén configuradas
        if not pipeline_id:
            logger.warning("[PipelineRouter] HUBSPOT_PIPELINE_REDES_ID no configurado. Usando pipeline general.")
            return _get_fallback_pipeline(channel_clean, analytics_source)

        logger.info(f"[PipelineRouter] Canal '{channel_clean}' → Pipeline Redes Sociales")
        return {
            "pipeline_id": pipeline_id,
            "stage_id": stage_id,
            "owner_id": owner_id,
            "analytics_source": analytics_source,
            "is_social_media": True,
        }

    # Fallback: Pipeline de Ventas General
    return _get_fallback_pipeline(channel_clean, analytics_source)


def _get_fallback_pipeline(channel: str, analytics_source: str) -> Dict[str, Any]:
    """
    Retorna configuración del pipeline general (Ventas/Portales).
    """
    logger.info(f"[PipelineRouter] Canal '{channel}' → Pipeline General")
    return {
        "pipeline_id": os.getenv("HUBSPOT_PIPELINE_ID"),
        "stage_id": os.getenv("HUBSPOT_DEAL_STAGE"),
        "owner_id": os.getenv("HUBSPOT_DEFAULT_OWNER"),
        "analytics_source": analytics_source,
        "is_social_media": False,
    }


def is_social_media_channel(channel: str) -> bool:
    """
    Verifica si un canal pertenece a redes sociales.

    Args:
        channel: Identificador del canal

    Returns:
        True si es red social, False en caso contrario
    """
    if not channel:
        return False
    return channel.lower().strip() in CANALES_REDES_SOCIALES


def get_analytics_source(channel: str) -> str:
    """
    Obtiene el valor de hs_analytics_source para HubSpot.
    """
    if not channel:
        return "OTHER_CAMPAIGNS"
    return CANAL_TO_ANALYTICS_SOURCE.get(channel.lower().strip(), "OTHER_CAMPAIGNS")


def get_display_name(channel: str) -> str:
    """
    Obtiene el nombre para mostrar de un canal.
    """
    display_names = {
        "instagram": "Instagram",
        "facebook": "Facebook",
        "linkedin": "LinkedIn",
        "youtube": "YouTube",
        "tiktok": "TikTok",
        "finca_raiz": "Finca Raíz",
        "metrocuadrado": "Metrocuadrado",
        "mercado_libre": "Mercado Libre",
        "ciencuadras": "Ciencuadras",
        "pagina_web": "Página Web",
        "whatsapp_directo": "WhatsApp Directo",
        "google_ads": "Google Ads",
        "referido": "Referido",
        "desconocido": "Desconocido",
    }
    return display_names.get(channel.lower().strip() if channel else "", "Desconocido")