"""
Fuente única de verdad para la configuración de canales del sistema SofIA.

Reemplaza las 7+ listas hardcodeadas en lead_assigner.py, pipeline_router.py,
hubspot_utils.py, link_detector.py y webhook_handler.py.

Agregar un canal nuevo = agregar UNA entrada aquí.
"""

import re
from typing import Dict, List, Optional
from enum import Enum


class ChannelCategory(str, Enum):
    PORTAL = "portal"
    SOCIAL = "social"
    DIRECT = "direct"
    OTHER = "other"


CHANNELS: Dict[str, dict] = {
    # ═══════════════════════════════════════════════════════════════
    # PORTALES INMOBILIARIOS
    # ═══════════════════════════════════════════════════════════════
    "finca_raiz": {
        "team": "equipo_directo",
        "owner_id": "89096380",
        "category": ChannelCategory.PORTAL,
        "score_bonus": 25,
        "link_patterns": [
            r'https?://(?:www\.)?fincaraiz\.com\.co/[^\s]+',
            r'fincaraiz\.com\.co/[^\s]+',
        ],
    },
    "metrocuadrado": {
        "team": "equipo_directo",
        "owner_id": "89096380",
        "category": ChannelCategory.PORTAL,
        "score_bonus": 25,
        "link_patterns": [
            r'https?://(?:www\.)?metrocuadrado\.com/[^\s]+',
            r'metrocuadrado\.com/[^\s]+',
        ],
    },
    "mercado_libre": {
        "team": "equipo_portales",
        "owner_id": "89096378",
        "category": ChannelCategory.PORTAL,
        "score_bonus": 20,
        "link_patterns": [
            r'https?://(?:www\.)?(?:inmuebles\.)?mercadolibre\.com\.co/[^\s]+',
            r'mercadolibre\.com\.co/[^\s]+',
        ],
    },
    "ciencuadras": {
        "team": "equipo_portales",
        "owner_id": "89096378",
        "category": ChannelCategory.PORTAL,
        "score_bonus": 20,
        "link_patterns": [
            r'https?://(?:www\.)?ciencuadras\.com/[^\s]+',
            r'ciencuadras\.com/[^\s]+',
        ],
    },
    "pagina_web": {
        "team": "equipo_directo",
        "owner_id": "89096380",
        "category": ChannelCategory.PORTAL,
        "score_bonus": 15,
        "link_patterns": [
            r'https?://(?:www\.)?inmobiliariaproteger\.com[^\s]*',
            r'https?://(?:www\.)?proteger\.com\.co[^\s]*',
        ],
    },

    # ═══════════════════════════════════════════════════════════════
    # REDES SOCIALES
    # ═══════════════════════════════════════════════════════════════
    "instagram": {
        "team": "equipo_portales",
        "owner_id": "89096378",
        "category": ChannelCategory.SOCIAL,
        "score_bonus": 10,
        "link_patterns": [
            r'https?://(?:www\.)?instagram\.com/[^\s]+',
            r'instagram\.com/[^\s]+',
            r'https?://instagr\.am/[^\s]+',
        ],
    },
    "facebook": {
        "team": "equipo_portales",
        "owner_id": "89096378",
        "category": ChannelCategory.SOCIAL,
        "score_bonus": 10,
        "link_patterns": [
            r'https?://(?:www\.)?facebook\.com/[^\s]+',
            r'https?://(?:www\.)?fb\.com/[^\s]+',
            r'https?://m\.facebook\.com/[^\s]+',
            r'facebook\.com/marketplace/[^\s]+',
        ],
    },
    "linkedin": {
        "team": "equipo_portales",
        "owner_id": "89096378",
        "category": ChannelCategory.SOCIAL,
        "score_bonus": 10,
        "link_patterns": [
            r'https?://(?:www\.)?linkedin\.com/[^\s]+',
            r'linkedin\.com/[^\s]+',
            r'https?://(?:www\.)?lnkd\.in/[^\s]+',
        ],
    },
    "youtube": {
        "team": "equipo_directo",
        "owner_id": "89096380",
        "category": ChannelCategory.SOCIAL,
        "score_bonus": 10,
        "link_patterns": [
            r'https?://(?:www\.)?youtube\.com/[^\s]+',
            r'https?://youtu\.be/[^\s]+',
            r'youtube\.com/[^\s]+',
            r'https?://(?:www\.)?youtube\.com/shorts/[^\s]+',
        ],
    },
    "tiktok": {
        "team": "equipo_portales",
        "owner_id": "89096378",
        "category": ChannelCategory.SOCIAL,
        "score_bonus": 10,
        "link_patterns": [
            r'https?://(?:www\.)?tiktok\.com/[^\s]+',
            r'https?://vm\.tiktok\.com/[^\s]+',
            r'tiktok\.com/@[^\s]+',
        ],
    },

    # ═══════════════════════════════════════════════════════════════
    # WHATSAPP DIRECTO
    # ═══════════════════════════════════════════════════════════════
    "whatsapp_directo": {
        "team": "equipo_portales",
        "owner_id": "89096378",
        "category": ChannelCategory.DIRECT,
        "score_bonus": 0,
        "link_patterns": [],
    },
    "whatsapp": {
        "team": "equipo_portales",
        "owner_id": "89096378",
        "category": ChannelCategory.DIRECT,
        "score_bonus": 0,
        "link_patterns": [],
    },

    # ═══════════════════════════════════════════════════════════════
    # FALLBACK / ROUND-ROBIN
    # ═══════════════════════════════════════════════════════════════
    "desconocido": {
        "team": "default",
        "owner_id": None,
        "category": ChannelCategory.OTHER,
        "score_bonus": 0,
        "link_patterns": [],
    },
    "google_ads": {
        "team": "default",
        "owner_id": None,
        "category": ChannelCategory.OTHER,
        "score_bonus": 0,
        "link_patterns": [],
    },
    "referido": {
        "team": "default",
        "owner_id": None,
        "category": ChannelCategory.OTHER,
        "score_bonus": 0,
        "link_patterns": [],
    },
    "default": {
        "team": "default",
        "owner_id": None,
        "category": ChannelCategory.OTHER,
        "score_bonus": 0,
        "link_patterns": [],
    },
}


# ═══════════════════════════════════════════════════════════════════════
# Funciones derivadas — reemplazan las 7 listas hardcodeadas
# ═══════════════════════════════════════════════════════════════════════

def get_all_channel_names() -> List[str]:
    """Todos los canales registrados. Reemplaza CANALES_A_VERIFICAR."""
    return list(CHANNELS.keys())


def get_channel_to_team() -> Dict[str, str]:
    """Mapeo canal → equipo. Reemplaza LeadAssigner.CHANNEL_TO_TEAM."""
    return {name: cfg["team"] for name, cfg in CHANNELS.items()}


def get_channel_to_owner() -> Dict[str, str]:
    """Mapeo canal → owner_id (solo canales con owner fijo). Reemplaza LeadAssigner.CHANNEL_TO_OWNER."""
    return {name: cfg["owner_id"] for name, cfg in CHANNELS.items() if cfg["owner_id"]}


def get_channels_by_category(category: ChannelCategory) -> List[str]:
    """Canales filtrados por categoría. Reemplaza CANALES_REDES_SOCIALES, CANALES_PORTALES."""
    return [name for name, cfg in CHANNELS.items() if cfg["category"] == category]


def get_social_media_channels() -> List[str]:
    """Canales de redes sociales. Reemplaza SOCIAL_MEDIA_CHANNELS."""
    return get_channels_by_category(ChannelCategory.SOCIAL)


def get_portal_channels() -> List[str]:
    """Canales de portales inmobiliarios. Reemplaza CANALES_PORTALES."""
    return get_channels_by_category(ChannelCategory.PORTAL)


def get_score_bonuses() -> Dict[str, int]:
    """Mapeo canal → score_bonus. Reemplaza CHANNEL_SCORE_BONUS."""
    return {name: cfg["score_bonus"] for name, cfg in CHANNELS.items()}


def get_team_for_channel(canal: str) -> str:
    """Equipo para un canal específico."""
    cfg = CHANNELS.get(canal)
    return cfg["team"] if cfg else "default"


def get_owner_for_channel(canal: str) -> Optional[str]:
    """Owner_id para un canal específico (None = round-robin)."""
    cfg = CHANNELS.get(canal)
    return cfg["owner_id"] if cfg else None


def get_link_patterns() -> Dict[str, List[re.Pattern]]:
    """Patrones de URL compilados por canal. Reemplaza LinkDetector.PATRONES_PORTALES."""
    return {
        name: [re.compile(p, re.IGNORECASE) for p in cfg["link_patterns"]]
        for name, cfg in CHANNELS.items()
        if cfg["link_patterns"]
    }
