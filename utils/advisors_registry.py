"""
Fuente única de verdad para la identidad de las asesoras del panel.

Espejo de `channels_registry.py`, que hace lo mismo con los canales. Antes esto
vivía en `integrations/hubspot/lead_assigner.py:OWNERS_CONFIG`, y para saber el
nombre de una asesora había que importar el asignador de leads entero — que a su
vez importa el registro de canales. La identidad va debajo, no dentro del
asignador.

Agregar o quitar una asesora = tocar UNA entrada aquí.

`lead_assigner.OWNERS_CONFIG` se deriva de este módulo, así que sigue funcionando
igual y sus consumidores no se enteran.
"""

from typing import Dict, List, Optional
#   name              Nombre visible en el panel y en los informes.
#   team              Equipo. Debe coincidir con el `team` de channels_registry:
#                     de ahí sale qué canales atiende.
#   receives_leads    Recibe asignación automática de leads nuevos.
#   uses_panel        Tiene bandeja en el panel. Distinto de receives_leads:
#                     Monica no recibe leads automáticos pero sí atiende los que
#                     le transfieren a mano, así que tiene bandeja.
#   receives_transfers  Destino de las transferencias por embudo (los 10 de
#                     STAGES_TRANSFER_TO_LUISA). Solo una asesora puede serlo.
#   panel_templates   Ids de las plantillas PREDEFINIDAS que ve en el picker del
#                     chat. None = sin reparto, ve el catalogo entero. Las que la
#                     asesora crea a mano desde el modal del panel viven en Redis
#                     y NUNCA se ven afectadas por este campo.

ADVISORS: Dict[str, dict] = {
    "89096378": {
        "name": "Jubeny",
        "team": "equipo_portales",
        "receives_leads": True,
        "uses_panel": True,
        "receives_transfers": False,
        "panel_templates": [
            "saludo_reactivador_inmueble",
            "cita_confirmacion",
            "seguimiento_personalizado",
        ],
    },
    "89096380": {
        "name": "Luisa",
        "team": "equipo_directo",
        "receives_leads": True,
        "uses_panel": True,
        "receives_transfers": True,
        "panel_templates": [
            "cita_confirmacion",
            "seguimiento_personalizado",
        ],
    },
    "89096379": {
        "name": "Monica",
        "team": "equipo_respaldo",
        "receives_leads": False,
        "uses_panel": True,
        "receives_transfers": False,
        "panel_templates": None,
    },
    "82598814": {
        "name": "Equipo de Marketing",
        "team": "equipo_marketing",
        "receives_leads": False,
        "uses_panel": False,
        "receives_transfers": False,
        "panel_templates": None,
    },
}

# Equipo al que caen los canales sin clasificar. lead_assigner lo necesita.
DEFAULT_TEAM = "equipo_portales"


def get_advisor_name(advisor_id: str) -> str:
    """Nombre visible. Nunca vacío: sin registro devuelve 'Asesor {id}'."""
    cfg = ADVISORS.get(str(advisor_id))
    return cfg["name"] if cfg else f"Asesor {advisor_id}"


def get_advisor_names() -> Dict[str, str]:
    """{id: nombre} de TODAS las asesoras. Reemplaza los OWNER_NAMES duplicados."""
    return {aid: cfg["name"] for aid, cfg in ADVISORS.items()}


def get_panel_advisor_ids() -> List[str]:
    """
    Quienes tienen bandeja en el panel. Reemplaza los ACTIVE_ADVISORS sueltos.

    NO es lo mismo que quien recibe leads: Monica no recibe asignación automática
    pero atiende transferencias, así que su inbox existe y hay que mantenerlo.
    """
    return [aid for aid, cfg in ADVISORS.items() if cfg["uses_panel"]]


def get_lead_receiving_ids() -> List[str]:
    """Quienes reciben asignación automática de leads nuevos."""
    return [aid for aid, cfg in ADVISORS.items() if cfg["receives_leads"]]


def get_transfer_target() -> Optional[str]:
    """
    Destino de las transferencias por embudo. None si nadie está marcado — en
    ese caso quien llame debe abstenerse de transferir, no adivinar.
    """
    for aid, cfg in ADVISORS.items():
        if cfg["receives_transfers"]:
            return aid
    return None


def get_panel_templates(advisor_id: str) -> Optional[List[str]]:
    """
    Ids de las plantillas predefinidas que esta asesora ve en el picker del chat.

    None significa "sin reparto": ve el catálogo entero. Es distinto de `[]`, que
    sería un reparto vacío y le dejaría el picker sin ninguna predefinida. Una
    asesora que no esté en el registro también devuelve None: mejor que vea de más
    a que se quede sin poder reabrir una ventana de 24h cerrada.
    """
    cfg = ADVISORS.get(str(advisor_id))
    return cfg.get("panel_templates") if cfg else None


def get_team(advisor_id: str) -> Optional[str]:
    cfg = ADVISORS.get(str(advisor_id))
    return cfg["team"] if cfg else None


def get_owners_config() -> Dict[str, List[dict]]:
    """
    Forma que espera `lead_assigner.OWNERS_CONFIG`: {equipo: [{name, id, active}]}.

    `active` ahí significa "recibe asignación automática", que es
    `receives_leads` aquí. El equipo `default` apunta al mismo grupo que
    DEFAULT_TEAM para que un canal sin clasificar caiga donde toca.
    """
    config: Dict[str, List[dict]] = {}
    for aid, cfg in ADVISORS.items():
        config.setdefault(cfg["team"], []).append({
            "name": cfg["name"],
            "id": aid,
            "active": cfg["receives_leads"],
        })
    config["default"] = list(config.get(DEFAULT_TEAM, []))
    return config
