"""Conversation history routes for the CRM panel."""

from fastapi import APIRouter

from services.panel.conversations_service import get_conversation_history, get_history_by_contact_id


router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])

router.add_api_route("/conversations/{phone}", get_conversation_history, methods=["GET"])
router.add_api_route("/history/{contact_id}", get_history_by_contact_id, methods=["GET"])
