"""HTML UI routes for the advisor panel."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from middleware.outbound_panel import panel_ui


router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])

router.add_api_route("/", panel_ui, methods=["GET"], response_class=HTMLResponse)
