"""Diagnostic routes for the advisor panel."""

from fastapi import APIRouter

from services.panel.diagnostics_service import diagnose_system, debug_redis, websocket_stats


router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])

router.add_api_route("/debug/redis", debug_redis, methods=["GET"])
router.add_api_route("/diagnose", diagnose_system, methods=["GET"])
router.add_api_route("/ws/stats", websocket_stats, methods=["GET"])
