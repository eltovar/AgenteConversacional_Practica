"""Mini CRM domain contracts."""

from domain.crm.models import (
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


from domain.crm.translators import (
    appointment_doc_to_domain,
    contact_doc_to_domain,
    lead_stage_to_payload,
    pipeline_stages_to_domain,
)


__all__ = [
    "AppointmentStatus",
    "Channel",
    "ConversationStatus",
    "CRMAdvisor",
    "CRMAppointment",
    "CRMContact",
    "CRMConversation",
    "CRMEvent",
    "CRMLeadStage",
    "CRMMessage",
    "Direction",
    "EventType",
    "MessageStatus",
    "appointment_doc_to_domain",
    "contact_doc_to_domain",
    "lead_stage_to_payload",
    "pipeline_stages_to_domain",
]
