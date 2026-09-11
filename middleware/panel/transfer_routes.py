"""Contact transfer routes for the CRM panel."""

from fastapi import APIRouter

from services.panel.transfer_service import (
    accept_transfer,
    reject_transfer,
    request_transfer,
    transfer_contact,
)


router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])

router.add_api_route("/contacts/{phone}/transfer", transfer_contact, methods=["POST"])
router.add_api_route("/contacts/{contact_id}/transfer-request", request_transfer, methods=["POST"])
router.add_api_route("/contacts/{contact_id}/transfer-accept", accept_transfer, methods=["POST"])
router.add_api_route("/contacts/{contact_id}/transfer-reject", reject_transfer, methods=["POST"])
