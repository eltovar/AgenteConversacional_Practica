"""Template routes for the advisor panel.

Route ownership lives here; endpoint behavior remains in the legacy functions
until the next, deeper extraction.
"""

from fastapi import APIRouter

from services.panel.templates_service import (
    create_template,
    delete_template,
    get_template_by_id,
    get_template_sids,
    list_templates,
    send_template_message,
    update_template,
)


router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])

router.add_api_route("/send-template", send_template_message, methods=["POST"])
router.add_api_route("/config/template-sids", get_template_sids, methods=["GET"])
router.add_api_route("/templates", list_templates, methods=["GET"])
router.add_api_route("/templates/{template_id}", get_template_by_id, methods=["GET"])
router.add_api_route("/templates", create_template, methods=["POST"])
router.add_api_route("/templates/{template_id}", update_template, methods=["PUT"])
router.add_api_route("/templates/{template_id}", delete_template, methods=["DELETE"])
