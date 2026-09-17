"""Reference-data service for the advisor panel."""

from fastapi import Header, HTTPException

from domain.crm import lead_stage_to_payload, pipeline_stages_to_domain
from middleware import outbound_panel as _legacy
from middleware.outbound_panel import PIPELINE_STAGES_LIST


async def get_pipeline_stages(
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Retorna la lista de etapas del pipeline para el dropdown del frontend.

    Pasa por el dominio propio (orden y vocabulario) y serializa al payload
    legacy exacto [{id, name}] — salida idéntica, sin cambios para el frontend.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    stages = [lead_stage_to_payload(s) for s in pipeline_stages_to_domain(PIPELINE_STAGES_LIST)]
    return {
        "stages": stages,
        "count": len(stages)
    }
