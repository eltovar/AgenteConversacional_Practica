from datetime import datetime, timezone
from pathlib import Path

from domain.crm import (
    AppointmentStatus,
    Channel,
    ConversationStatus,
    CRMAdvisor,
    CRMAppointment,
    CRMContact,
    CRMConversation,
    CRMEvent,
    CRMLeadStage,
    CRMMessage,
    Direction,
    EventType,
    MessageStatus,
)


def test_crm_domain_contracts_define_core_entities():
    now = datetime.now(timezone.utc)

    advisor = CRMAdvisor(id="owner-1", name="Asesora")
    stage = CRMLeadStage(id="stage-1", name="Nuevo", order=1)
    contact = CRMContact(id="contact-1", phone="+573001234567", firstname="Sofia", channel=Channel.WHATSAPP)
    conversation = CRMConversation(
        id="conversation-1",
        contact_id=contact.id,
        channel=Channel.WHATSAPP,
        status=ConversationStatus.HUMAN_ACTIVE,
        owner_id=advisor.id,
    )
    message = CRMMessage(
        id="message-1",
        conversation_id=conversation.id,
        contact_id=contact.id,
        direction=Direction.OUTBOUND,
        text="Hola",
        status=MessageStatus.SENT,
        sent_at=now,
    )
    appointment = CRMAppointment(
        id="appointment-1",
        contact_id=contact.id,
        advisor_id=advisor.id,
        scheduled_at=now,
        status=AppointmentStatus.SCHEDULED,
    )
    event = CRMEvent(
        id="event-1",
        type=EventType.STAGE_CHANGED,
        occurred_at=now,
        contact_id=contact.id,
        payload={"stage_id": stage.id},
    )

    assert advisor.active is True
    assert stage.active is True
    assert conversation.owner_id == advisor.id
    assert message.contact_id == contact.id
    assert appointment.contact_id == contact.id
    assert event.payload["stage_id"] == stage.id


def test_crm_domain_contracts_are_infrastructure_agnostic():
    source = Path("domain/crm/models.py").read_text(encoding="utf-8")

    forbidden_imports = (
        "fastapi",
        "motor",
        "pymongo",
        "redis",
        "hubspot",
        "twilio",
        "bunny",
        "openai",
        "middleware",
        "integrations",
        "database",
    )

    lower_source = source.lower()
    for forbidden in forbidden_imports:
        assert forbidden not in lower_source
