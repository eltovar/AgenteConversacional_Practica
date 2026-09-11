"""Contact note routes for the advisor panel."""

from fastapi import APIRouter

from services.panel.notes_service import (
    create_contact_note,
    delete_contact_note,
    get_contact_notes,
    update_contact_note,
)


router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])

router.add_api_route("/contacts/{contact_id}/notes", get_contact_notes, methods=["GET"])
router.add_api_route("/contacts/{contact_id}/notes", create_contact_note, methods=["POST"], status_code=201)
router.add_api_route("/contacts/{contact_id}/notes/{note_id}", update_contact_note, methods=["PATCH"])
router.add_api_route("/contacts/{contact_id}/notes/{note_id}", delete_contact_note, methods=["DELETE"])
