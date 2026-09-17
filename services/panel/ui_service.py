"""Panel UI service for the advisor panel.

Moved verbatim from `middleware.outbound_panel` — no behavior changes.
"""

from fastapi import Query, Request
from fastapi.responses import HTMLResponse

from middleware import outbound_panel as _legacy
from middleware.outbound_panel import (
    STAGES_TRANSFER_TO_LUISA,
    get_lead_receiving_ids,
    get_mongo_manager,
    get_transfer_target,
    templates,
)


async def panel_ui(request: Request, x_api_key: str = Query(None, alias="key")):
    """
    Interfaz web del panel de envio para asesores - WhatsApp Web Style.

    Acceso: /whatsapp/panel/?key=TU_API_KEY
    """
    # Validar API Key via query param para acceso web
    if not _legacy._validate_api_key(x_api_key):
        return HTMLResponse(
            content="""
            <!DOCTYPE html>
            <html>
            <head><title>Acceso Denegado</title></head>
            <body style="font-family: Arial; padding: 50px; text-align: center;">
                <h1>Acceso Denegado</h1>
                <p>Se requiere API Key valida.</p>
                <p>Uso: /whatsapp/panel/?key=TU_API_KEY</p>
            </body>
            </html>
            """,
            status_code=401
        )

    # Cargar nombres de asesores desde MongoDB (editables)
    mongo_mgr = get_mongo_manager()
    advisors_list = await mongo_mgr.get_advisors()
    
    # Convertir lista a diccionario {id: name} para el template
    advisor_names = {a["id"]: a["name"] for a in advisors_list}

    # Embudos que NO se ofrecen en el filtro: los de transferencia, que sacan el
    # contacto del panel de quien los elige. Solo tienen sentido para la asesora
    # que los recibe. Se calcula aqui, donde se conocen las dos constantes, en vez
    # de repetir la lista y comparar un ID a fuego en index.js.
    # Se ocultan solo a quien RECIBE LEADS y no es el destino: para esa asesora,
    # elegir uno de esos embudos manda el contacto fuera de su panel.
    # Monica no recibe leads automaticos y si atiende transferencias manuales, asi
    # que puede tener contactos en esas etapas y necesita poder filtrarlos —
    # exactamente el comportamiento que habia antes de este refactor.
    _destino = get_transfer_target()
    _hidden_by_advisor = {
        aid: (list(STAGES_TRANSFER_TO_LUISA)
              if aid in get_lead_receiving_ids() and aid != _destino
              else [])
        for aid in advisor_names
    }

    return templates.TemplateResponse(request, "index.html", {
        "api_key": x_api_key,
        "base_url": "/whatsapp/panel",
        "advisor_names": advisor_names,
        "hidden_stages_by_advisor": _hidden_by_advisor,
    })
