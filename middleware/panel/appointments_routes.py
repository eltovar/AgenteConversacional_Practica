"""Appointment routes for the advisor panel."""

from fastapi import APIRouter

from services.panel.appointments_service import (
    cancel_appointment,
    create_appointment,
    delete_appointment,
    get_appointments,
    update_appointment,
)


router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])

router.add_api_route("/contacts/{contact_id}/appointments", create_appointment, methods=["POST"], status_code=201)
router.add_api_route("/contacts/{contact_id}/appointments", get_appointments, methods=["GET"])
router.add_api_route("/appointments/{appointment_id}/cancel", cancel_appointment, methods=["PATCH"])
router.add_api_route("/appointments/{appointment_id}", update_appointment, methods=["PATCH"])
router.add_api_route("/appointments/{appointment_id}", delete_appointment, methods=["DELETE"])
