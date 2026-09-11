"""Metrics routes for the advisor panel."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from middleware.outbound_panel import (
    export_appointments_excel,
    export_metrics_csv,
    export_metrics_excel,
    get_appointments_metrics,
    get_social_media_metrics,
    metrics_dashboard_ui,
)


router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])

router.add_api_route("/metrics", get_social_media_metrics, methods=["GET"])
router.add_api_route("/metrics/export", export_metrics_csv, methods=["GET"])
router.add_api_route("/metrics/export-excel", export_metrics_excel, methods=["GET"])
router.add_api_route("/metrics/appointments", get_appointments_metrics, methods=["GET"])
router.add_api_route("/metrics/appointments/export-excel", export_appointments_excel, methods=["GET"])
router.add_api_route("/metrics/", metrics_dashboard_ui, methods=["GET"], response_class=HTMLResponse)
