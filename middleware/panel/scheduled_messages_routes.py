"""Scheduled WhatsApp message routes for the advisor panel."""

from fastapi import APIRouter

from services.panel.scheduled_messages_service import (
    cancel_scheduled_message,
    create_scheduled_message,
    get_scheduled_messages,
)


router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])

router.add_api_route("/contacts/{contact_id}/scheduled-messages", create_scheduled_message, methods=["POST"], status_code=201)
router.add_api_route("/contacts/{contact_id}/scheduled-messages", get_scheduled_messages, methods=["GET"])
router.add_api_route("/scheduled-messages/{message_id}", cancel_scheduled_message, methods=["DELETE"], status_code=200)
