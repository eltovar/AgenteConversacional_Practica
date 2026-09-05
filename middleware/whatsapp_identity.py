"""Clasificación segura de identidades entrantes de WhatsApp/Twilio."""

from __future__ import annotations

import json
import re
from hashlib import sha256
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


# Twilio documenta BSUID con formato whatsapp:CC.BSUID:
# - CC: código de país ISO de 2 letras.
# - BSUID: hasta 128 caracteres alfanuméricos.
_BSUID_RE = re.compile(
    r"^whatsapp:[A-Z]{2}\.[A-Z0-9]{1,128}$",
    re.IGNORECASE,
)

# Mantiene la tolerancia histórica del proyecto para teléfonos recibidos con o
# sin prefijo whatsapp:, con +E.164 o formatos nacionales que luego resolverá
# PhoneNormalizer. Esta expresión solo clasifica; NO normaliza.
_PHONE_RE = re.compile(
    r"^(?:whatsapp:)?\+?\d[\d\s().-]{6,}$",
    re.IGNORECASE,
)


def is_whatsapp_bsuid(value: Optional[str]) -> bool:
    """Retorna True solo cuando ``value`` tiene forma de routing BSUID de Twilio."""
    if not value:
        return False
    return bool(_BSUID_RE.fullmatch(str(value).strip()))


def make_bsuid_identity_key(value: str) -> str:
    """Construye una llave interna estable y segura para Redis/Mongo/panel."""
    cleaned = str(value or "").strip().lower()
    digest = sha256(cleaned.encode("utf-8")).hexdigest()[:24]
    return f"bsuid_{digest}"


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


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def resolve_whatsapp_identity(
    payload: dict,
    channel_metadata_raw: Any = None,
) -> WhatsAppIdentity:
    """Clasifica la identidad entrante sin consultar ni escribir sistemas externos.

    Reglas de seguridad:
    1. ``From``/``Author`` con forma telefónica -> PHONE.
    2. ``From``/``Author`` o ``ExternalUserId`` con forma BSUID -> BSUID.
    3. ``Username`` es informativo y nunca se usa como routing address.
    4. Una identidad que no podamos demostrar como PHONE o BSUID -> UNKNOWN.

    La función no llama PhoneNormalizer y no intenta convertir dígitos de un
    identificador arbitrario en teléfono.
    """
    raw_from = _clean(payload.get("Author") or payload.get("From"))
    context = _context_from_channel_metadata(
        channel_metadata_raw or payload.get("ChannelMetadata")
    )

    wa_id = _clean(payload.get("WaId") or context.get("WaId"))
    external_user_id = _clean(
        payload.get("ExternalUserId") or context.get("ExternalUserId")
    )
    username = _clean(payload.get("Username") or context.get("Username"))

    author_attributes = payload.get("AuthorAttributes") or {}
    author_name = (
        author_attributes.get("name")
        if isinstance(author_attributes, dict)
        else None
    )
    profile_name = _clean(
        payload.get("ProfileName")
        or context.get("ProfileName")
        or author_name
    )

    # Si Twilio conserva el teléfono en From/Author, esa es la ruta histórica.
    # ExternalUserId puede traer simultáneamente el BSUID, pero no reemplaza el
    # teléfono cuando el routing recibido ya es telefónico.
    if raw_from and _PHONE_RE.fullmatch(raw_from):
        return WhatsAppIdentity(
            identity_type=WhatsAppIdentityType.PHONE,
            raw_from=raw_from,
            phone=raw_from,
            wa_id=wa_id,
            external_user_id=external_user_id,
            username=username,
            profile_name_present=bool(profile_name),
        )

    # No usar ``raw_from or external_user_id``: un From no telefónico y no-BSUID
    # no debe ocultar un ExternalUserId válido.
    for candidate in (raw_from, external_user_id):
        if is_whatsapp_bsuid(candidate):
            return WhatsAppIdentity(
                identity_type=WhatsAppIdentityType.BSUID,
                raw_from=raw_from,
                bsuid=candidate,
                wa_id=wa_id,
                external_user_id=external_user_id,
                username=username,
                profile_name_present=bool(profile_name),
            )

    # Username es metadata. Se distingue para observabilidad, pero el caller debe
    # hacer safe-stop: nunca construir whatsapp:<username> como destino.
    if username:
        return WhatsAppIdentity(
            identity_type=WhatsAppIdentityType.USERNAME,
            raw_from=raw_from,
            wa_id=wa_id,
            external_user_id=external_user_id,
            username=username,
            profile_name_present=bool(profile_name),
        )

    return WhatsAppIdentity(
        identity_type=WhatsAppIdentityType.UNKNOWN,
        raw_from=raw_from,
        wa_id=wa_id,
        external_user_id=external_user_id,
        username=username,
        profile_name_present=bool(profile_name),
    )
