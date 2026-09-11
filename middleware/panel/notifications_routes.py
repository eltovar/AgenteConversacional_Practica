"""Notification routes for the advisor panel."""

from fastapi import APIRouter

from middleware.outbound_panel import (
    get_advisor_notifications,
    mark_all_notifications_read,
    mark_notification_read,
)


router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])

router.add_api_route("/notifications", get_advisor_notifications, methods=["GET"])
router.add_api_route("/notifications/{notif_id}/read", mark_notification_read, methods=["POST"])
router.add_api_route("/notifications/read-all", mark_all_notifications_read, methods=["POST"])
