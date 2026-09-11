"""Contact routes for the CRM panel."""

from fastapi import APIRouter

from services.panel.contacts_service import (
    close_conversation,
    create_manual_contact,
    get_active_contacts,
    get_contact_detail,
    hydrate_contact_endpoint,
    mark_contact_read,
    search_contacts_by_keyword,
    take_control_of_conversation,
    update_contact_canal_display,
    update_contact_name,
    update_contact_stage,
)


router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])

router.add_api_route("/contacts/create", create_manual_contact, methods=["POST"])
router.add_api_route("/contacts/{contact_id}/name", update_contact_name, methods=["PATCH"])
router.add_api_route("/contacts/{phone}/close", close_conversation, methods=["DELETE"])
router.add_api_route("/contacts/{phone}/mark-read", mark_contact_read, methods=["POST"])
router.add_api_route("/contacts/{contact_id}/stage", update_contact_stage, methods=["PATCH"])
router.add_api_route("/contacts/{phone}/canal", update_contact_canal_display, methods=["PATCH"])
router.add_api_route("/contacts/{phone}/detail", get_contact_detail, methods=["GET"])
router.add_api_route("/contacts/{phone}/hydrate", hydrate_contact_endpoint, methods=["GET"])
router.add_api_route("/contacts/{phone}/take-control", take_control_of_conversation, methods=["POST"])
router.add_api_route("/contacts/search", search_contacts_by_keyword, methods=["GET"])
router.add_api_route("/contacts", get_active_contacts, methods=["GET"])
