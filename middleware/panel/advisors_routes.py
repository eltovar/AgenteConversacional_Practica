"""Advisor routes for the advisor panel."""

from fastapi import APIRouter

from services.panel.advisors_service import list_advisors, update_advisor


router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])

router.add_api_route("/advisors", list_advisors, methods=["GET"])
router.add_api_route("/advisors/{advisor_id}", update_advisor, methods=["PATCH"])
