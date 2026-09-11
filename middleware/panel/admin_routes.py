"""Administrative recovery routes for the advisor panel."""

from fastapi import APIRouter

from services.panel.admin_service import (
    cleanup_stale_inbox,
    recover_lost_conversations,
    recover_outage,
    restore_panel_from_hubspot,
    sync_owners_mongo,
    sync_owners_with_hubspot,
)


router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])

router.add_api_route("/admin/restore-panel", restore_panel_from_hubspot, methods=["POST"])
router.add_api_route("/admin/recover-outage", recover_outage, methods=["POST"])
router.add_api_route("/admin/cleanup-stale-inbox", cleanup_stale_inbox, methods=["POST"])
router.add_api_route("/admin/sync-owners", sync_owners_with_hubspot, methods=["POST"])
router.add_api_route("/admin/sync-owners-mongo", sync_owners_mongo, methods=["POST"])
router.add_api_route("/admin/recover-lost-conversations", recover_lost_conversations, methods=["POST"])
