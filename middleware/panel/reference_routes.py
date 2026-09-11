"""Reference-data routes for the advisor panel."""

from fastapi import APIRouter

from services.panel.reference_service import get_pipeline_stages


router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])

router.add_api_route("/stages", get_pipeline_stages, methods=["GET"])
