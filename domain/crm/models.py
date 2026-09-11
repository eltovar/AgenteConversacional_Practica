"""Stable domain contracts for the mini CRM.

These dataclasses are intentionally persistence-agnostic. They describe the
internal vocabulary we want to own while legacy code still reads and writes
through existing storage and provider APIs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional


class Channel(str, Enum):
    WHATSAPP = "whatsapp"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    WEB = "web"
    UNKNOWN = "unknown"


class Direction(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    SYSTEM = "system"


class MessageStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


class ConversationStatus(str, Enum):
    BOT_ACTIVE = "bot_active"
    HUMAN_ACTIVE = "human_active"
    CLOSED = "closed"
    ARCHIVED = "archived"


class AppointmentStatus(str, Enum):
    SCHEDULED = "scheduled"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"


class EventType(str, Enum):
    CONTACT_CREATED = "contact_created"
    MESSAGE_RECEIVED = "message_received"
    MESSAGE_SENT = "message_sent"
    STAGE_CHANGED = "stage_changed"
    OWNER_CHANGED = "owner_changed"
    APPOINTMENT_CREATED = "appointment_created"
    APPOINTMENT_UPDATED = "appointment_updated"
    CONVERSATION_CLOSED = "conversation_closed"
    BOT_RESET = "bot_reset"


@dataclass(frozen=True)
class CRMAdvisor:
    id: str
    name: str
    email: Optional[str] = None
    active: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CRMLeadStage:
    id: str
    name: str
    order: int = 0
    active: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CRMContact:
    id: str
    phone: Optional[str]
    firstname: str = ""
    lastname: str = ""
    email: Optional[str] = None
    channel: Channel = Channel.UNKNOWN
    owner_id: Optional[str] = None
    stage_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CRMConversation:
    id: str
    contact_id: str
    channel: Channel
    status: ConversationStatus = ConversationStatus.BOT_ACTIVE
    owner_id: Optional[str] = None
    last_message_at: Optional[datetime] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CRMMessage:
    id: str
    conversation_id: str
    contact_id: str
    direction: Direction
    text: str = ""
    status: MessageStatus = MessageStatus.PENDING
    provider_message_id: Optional[str] = None
    sent_at: Optional[datetime] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CRMAppointment:
    id: str
    contact_id: str
    advisor_id: str
    scheduled_at: datetime
    status: AppointmentStatus = AppointmentStatus.SCHEDULED
    notes: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CRMEvent:
    id: str
    type: EventType
    occurred_at: datetime
    contact_id: Optional[str] = None
    conversation_id: Optional[str] = None
    actor_id: Optional[str] = None
    payload: Mapping[str, Any] = field(default_factory=dict)
