"""Workers routes for the advisor panel.

The endpoint functions still live in `middleware.outbound_panel` during this
first extraction. This keeps behavior stable while moving route ownership into
the new panel boundary.
"""

from fastapi import APIRouter

from services.panel.workers_service import (
    create_worker,
    delete_worker,
    list_workers,
    update_worker,
)


router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])

router.add_api_route("/workers", list_workers, methods=["GET"])
router.add_api_route("/workers", create_worker, methods=["POST"], status_code=201)
router.add_api_route("/workers/{worker_id}", update_worker, methods=["PATCH"])
router.add_api_route("/workers/{worker_id}", delete_worker, methods=["DELETE"])
