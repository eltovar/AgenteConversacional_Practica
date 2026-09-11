"""Realtime WebSocket routes for the advisor panel."""

from fastapi import APIRouter

from middleware.outbound_panel import websocket_endpoint


router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])

router.add_api_websocket_route("/ws/{advisor_id}", websocket_endpoint)
