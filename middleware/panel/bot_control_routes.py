"""Bot control and WhatsApp window routes for the CRM panel."""

from fastapi import APIRouter

from services.panel.bot_control_service import get_window_status, reset_bot_state


router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])

router.add_api_route("/reset-bot/{phone}", reset_bot_state, methods=["POST"])
router.add_api_route("/window-status/{phone}", get_window_status, methods=["GET"])
