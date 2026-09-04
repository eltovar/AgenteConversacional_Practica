# middleware/whatsapp_identity.py
"""Clasificacion segura de identidades entrantes de WhatsApp/Twilio."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class WhatsAppIdentityType(str, Enum):
    PHONE = "phone"
    BSUID = "bsuid"
    USERNAME = "username"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WhatsAppIdentity:
    identity_type: WhatsAppIdentityType
    raw_from: Optional[str]
    phone: Optional[str] = None
    bsuid: Optional[str] = None
    wa_id: Optional[str] = None
    external_user_id: Optional[str] = None
    username: Optional[str] = None
    profile_name_present: bool = False


_BSUID_RE = re.compile(r"^whatsapp:CO\.\d{8,}$", re.IGNORECASE)
_PHONE_RE = re.compile(r"^(?:whatsapp:)?\+?\d[\d\s().-]{6,}$", re.IGNORECASE)
_USERNAME_RE = re.compile(r"^whatsapp:[A-Za-z][A-Za-z0-9_.]{2,}$", re.IGNORECASE)
_BARE_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{2,}$")


def _as_dict(value: Any) -> Optional[dict]:
    if not value:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _context_from_channel_metadata(channel_metadata_raw: Any) -> dict:
    meta = _as_dict(channel_metadata_raw) or {}
    data = meta.get("data")
    if not isinstance(data, dict):
        return {}
    context = data.get("context")
    return context if isinstance(context, dict) else {}


def resolve_whatsapp_identity(payload: dict, channel_metadata_raw: Any = None) -> WhatsAppIdentity:
    """Clasifica la identidad entrante sin escribir ni consultar sistemas externos."""
    raw_from = payload.get("Author") or payload.get("From") or None
    raw_from = str(raw_from).strip() if raw_from is not None else None
    context = _context_from_channel_metadata(channel_metadata_raw or payload.get("ChannelMetadata"))

    wa_id = payload.get("WaId") or context.get("WaId")
    external_user_id = payload.get("ExternalUserId") or context.get("ExternalUserId")
    username = payload.get("Username") or context.get("Username")
    author_attributes = payload.get("AuthorAttributes") or {}
    author_name = author_attributes.get("name") if isinstance(author_attributes, dict) else None
    profile_name = payload.get("ProfileName") or context.get("ProfileName") or author_name

    if raw_from and _PHONE_RE.match(raw_from):
        return WhatsAppIdentity(
            identity_type=WhatsAppIdentityType.PHONE,
            raw_from=raw_from,
            phone=raw_from,
            wa_id=str(wa_id) if wa_id else None,
            external_user_id=str(external_user_id) if external_user_id else None,
            username=str(username) if username else None,
            profile_name_present=bool(profile_name),
        )

    bsuid_candidate = raw_from or external_user_id
    if bsuid_candidate and _BSUID_RE.match(str(bsuid_candidate).strip()):
        return WhatsAppIdentity(
            identity_type=WhatsAppIdentityType.BSUID,
            raw_from=raw_from,
            bsuid=str(bsuid_candidate).strip(),
            wa_id=str(wa_id) if wa_id else None,
            external_user_id=str(external_user_id) if external_user_id else None,
            username=str(username) if username else None,
            profile_name_present=bool(profile_name),
        )

    username_candidate = username or raw_from
    if username_candidate and (
        _USERNAME_RE.match(str(username_candidate).strip())
        or (username and _BARE_USERNAME_RE.match(str(username_candidate).strip()))
    ):
        raw_username = str(username_candidate).strip()
        display_username = raw_username.removeprefix("whatsapp:")
        return WhatsAppIdentity(
            identity_type=WhatsAppIdentityType.USERNAME,
            raw_from=raw_from,
            username=display_username,
            wa_id=str(wa_id) if wa_id else None,
            external_user_id=str(external_user_id) if external_user_id else None,
            profile_name_present=bool(profile_name),
        )

    return WhatsAppIdentity(
        identity_type=WhatsAppIdentityType.UNKNOWN,
        raw_from=raw_from,
        wa_id=str(wa_id) if wa_id else None,
        external_user_id=str(external_user_id) if external_user_id else None,
        username=str(username) if username else None,
        profile_name_present=bool(profile_name),
    )
