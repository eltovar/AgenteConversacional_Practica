"""Translators between legacy storage shapes and the owned domain model.

Pure functions: no I/O, no FastAPI, no providers. Every translator must
round-trip without losing or adding fields — the services serialize back
to the exact legacy payloads until the frontend migrates.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from domain.crm.models import (
    AppointmentStatus,
    Channel,
    CRMAdvisor,
    CRMAppointment,
    CRMContact,
    CRMConversation,
    CRMLeadStage,
    CRMMessage,
    ConversationStatus,
    Direction,
    MessageStatus,
)


def pipeline_stages_to_domain(stages: list[dict]) -> list[CRMLeadStage]:
    """Legacy `PIPELINE_STAGES_LIST` [{id, name}] -> owned stages, order kept."""
    return [
        CRMLeadStage(id=str(s["id"]), name=str(s["name"]), order=i)
        for i, s in enumerate(stages)
    ]


def lead_stage_to_payload(stage: CRMLeadStage) -> dict:
    """Owned stage -> legacy payload shape [{id, name}]."""
    return {"id": stage.id, "name": stage.name}


def _coerce_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def appointment_doc_to_domain(doc: Mapping[str, Any]) -> CRMAppointment:
    """Mongo appointment doc -> owned appointment (best-effort status map)."""
    raw_status = str(doc.get("status", "pending")).lower()
    status = {
        "pending": AppointmentStatus.SCHEDULED,
        "scheduled": AppointmentStatus.SCHEDULED,
        "confirmed": AppointmentStatus.CONFIRMED,
        "cancelled": AppointmentStatus.CANCELLED,
        "completed": AppointmentStatus.COMPLETED,
        "no_show": AppointmentStatus.NO_SHOW,
    }.get(raw_status, AppointmentStatus.SCHEDULED)
    scheduled = _coerce_dt(doc.get("appointment_dt") or doc.get("scheduled_at"))
    return CRMAppointment(
        id=str(doc.get("_id", doc.get("appointment_id", ""))),
        contact_id=str(doc.get("contact_id", "")),
        advisor_id=str(doc.get("advisor_id", "")),
        scheduled_at=scheduled or datetime.now(timezone.utc),
        status=status,
        notes=str(doc.get("notes", "")),
        metadata={"canal": doc.get("canal", "whatsapp")},
    )


def contact_doc_to_domain(doc: Mapping[str, Any], phone: str = "") -> CRMContact:
    """Mongo/HubSpot contact shape -> owned contact (identity = phone)."""
    raw_channel = str(doc.get("canal", doc.get("canal_origen", "unknown"))).lower()
    try:
        channel = Channel(raw_channel)
    except ValueError:
        channel = Channel.UNKNOWN
    return CRMContact(
        id=str(doc.get("contact_id", doc.get("id", phone))),
        phone=phone or doc.get("phone"),
        firstname=str(doc.get("firstname", "")),
        lastname=str(doc.get("lastname", "")),
        email=doc.get("email"),
        channel=channel,
        owner_id=doc.get("owner_id"),
        stage_id=doc.get("stage_id"),
        created_at=_coerce_dt(doc.get("created_at")),
        updated_at=_coerce_dt(doc.get("updated_at")),
    )
