"""Manual advisor message routes for the CRM panel."""

from fastapi import APIRouter

from services.panel.messages_service import (
    delete_advisor_message,
    edit_advisor_message,
    send_message,
    send_message_json,
)


router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])

router.add_api_route("/send-message", send_message, methods=["POST"])
router.add_api_route("/messages/{mongo_id}", edit_advisor_message, methods=["PATCH"])
router.add_api_route("/messages/{mongo_id}", delete_advisor_message, methods=["DELETE"])
router.add_api_route("/send-message-json", send_message_json, methods=["POST"])
