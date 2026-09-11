"""Bulk campaign routes for the advisor panel."""

from fastapi import APIRouter

from middleware.outbound_panel import (
    create_bulk_campaign,
    get_bulk_campaign_status,
    get_last_bulk_campaign,
    preview_bulk_campaign,
)


router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])

router.add_api_route("/bulk-campaigns/preview", preview_bulk_campaign, methods=["POST"])
router.add_api_route("/bulk-campaigns", create_bulk_campaign, methods=["POST"])
router.add_api_route("/bulk-campaigns/{campaign_id}", get_bulk_campaign_status, methods=["GET"])
router.add_api_route("/bulk-campaigns/last/{stage_id}", get_last_bulk_campaign, methods=["GET"])
