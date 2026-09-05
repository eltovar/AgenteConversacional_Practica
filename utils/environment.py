"""Runtime environment and staging safety gates."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Iterable


TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


def _raw(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def env_name() -> str:
    value = (
        _raw("APP_ENV")
        or _raw("ENVIRONMENT")
        or _raw("RAILWAY_ENVIRONMENT_NAME")
        or _raw("RAILWAY_ENVIRONMENT")
        or "local"
    )
    return value.lower()


def is_staging() -> bool:
    return env_name() in {
        "staging",
        "stage",
        "preprod",
        "preproduction",
        "testing",
    }


def is_production() -> bool:
    return env_name() in {"production", "prod"}


def bool_env(name: str, default: bool = False) -> bool:
    value = _raw(name)

    if not value:
        return default

    normalized = value.lower()

    if normalized in TRUE_VALUES:
        return True

    if normalized in FALSE_VALUES:
        return False

    return default


def twilio_outbound_enabled() -> bool:
    return bool_env(
        "TWILIO_OUTBOUND_ENABLED",
        default=not is_staging(),
    )


def hubspot_writes_enabled() -> bool:
    return bool_env(
        "HUBSPOT_WRITE_ENABLED",
        default=not is_staging(),
    )


def scheduler_outbound_enabled() -> bool:
    return bool_env(
        "SCHEDULER_OUTBOUND_ENABLED",
        default=not is_staging(),
    )


def bunny_uploads_enabled() -> bool:
    return bool_env(
        "BUNNY_UPLOAD_ENABLED",
        default=not is_staging(),
    )


def rag_index_on_startup_enabled() -> bool:
    return bool_env(
        "RAG_INDEX_ON_STARTUP",
        default=not is_staging(),
    )


def staging_sentinel_enabled() -> bool:
    return bool_env(
        "STAGING_SENTINEL_ENABLED",
        default=is_staging(),
    )


@lru_cache(maxsize=1)
def _twilio_allowlist() -> set[str]:
    raw = _raw("TWILIO_ALLOWED_TO_NUMBERS")

    if not raw:
        return set()

    return {
        _normalize_phone(item)
        for item in re.split(r"[\s,;]+", raw)
        if item.strip()
    }


def _normalize_phone(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def is_twilio_recipient_allowed(to: str) -> bool:
    if not is_staging():
        return True

    allowlist = _twilio_allowlist()

    return bool(
        allowlist
        and _normalize_phone(to) in allowlist
    )


def require_twilio_outbound_allowed(to: str) -> tuple[bool, str]:
    if not twilio_outbound_enabled():
        return False, "TWILIO_OUTBOUND_ENABLED=false"

    if not is_twilio_recipient_allowed(to):
        return False, "recipient_not_in_TWILIO_ALLOWED_TO_NUMBERS"

    return True, "allowed"


def require_hubspot_write_allowed() -> tuple[bool, str]:
    if not hubspot_writes_enabled():
        return False, "HUBSPOT_WRITE_ENABLED=false"

    return True, "allowed"


def require_bunny_upload_allowed() -> tuple[bool, str]:
    if not bunny_uploads_enabled():
        return False, "BUNNY_UPLOAD_ENABLED=false"

    return True, "allowed"


def apply_bunny_prefix(folder: str) -> str:
    prefix = _raw("BUNNY_UPLOAD_PREFIX")

    clean_folder = (folder or "").strip("/")
    clean_prefix = prefix.strip("/")

    if not clean_prefix:
        return clean_folder

    if (
        clean_folder.startswith(f"{clean_prefix}/")
        or clean_folder == clean_prefix
    ):
        return clean_folder

    return (
        f"{clean_prefix}/{clean_folder}"
        if clean_folder
        else clean_prefix
    )


def configured_safety_summary() -> dict:
    return {
        "environment": env_name(),
        "is_staging": is_staging(),
        "twilio_outbound_enabled": twilio_outbound_enabled(),
        "twilio_allowlist_configured": bool(_twilio_allowlist()),
        "hubspot_writes_enabled": hubspot_writes_enabled(),
        "scheduler_outbound_enabled": scheduler_outbound_enabled(),
        "bunny_uploads_enabled": bunny_uploads_enabled(),
        "rag_index_on_startup_enabled": rag_index_on_startup_enabled(),
        "staging_sentinel_enabled": staging_sentinel_enabled(),
    }


def secret_names_present(names: Iterable[str]) -> list[str]:
    return [
        name
        for name in names
        if _raw(name)
    ]
