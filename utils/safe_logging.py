"""Helpers para logs sin PII.

Estos helpers solo cambian representacion en observabilidad. No deben usarse
para payloads hacia Twilio, HubSpot, MongoDB, Redis ni respuestas al panel.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping
from urllib.parse import urlsplit


_DIGITS_RE = re.compile(r"\d+")
_OBS_TOKEN_RE = re.compile(r"[^a-zA-Z0-9_.:-]+")
_URL_RE = re.compile(r"https?://[^\s)>\"]+")
_SECRET_PAIR_RE = re.compile(
    r"(?i)\b(token|api[_-]?key|access[_-]?key|secret|signature|sig)=([^&\s]+)"
)
OBS_SOURCES = frozenset({
    "twilio",
    "hubspot",
    "bunny",
    "mongo",
    "redis",
    "openai",
    "panel",
    "scheduler",
    "server",
})


def safe_phone(value: Any) -> str:
    """Representa un telefono sin exponerlo completo.

    Mantiene solo ultimos 4 digitos para poder correlacionar soporte humano, y
    agrega hash corto estable para distinguir numeros con mismo sufijo.
    """
    if value is None:
        return "phone:none"

    raw = str(value)
    digits = "".join(_DIGITS_RE.findall(raw))
    if not digits:
        return "phone:invalid"

    suffix = digits[-4:] if len(digits) >= 4 else digits
    digest = hashlib.sha256(digits.encode("utf-8")).hexdigest()[:8]
    return f"phone:***{suffix}#{digest}"


def safe_id(value: Any, label: str = "id") -> str:
    """Representa ids externos sin imprimir el valor completo."""
    if value is None or value == "":
        return f"{label}:none"

    raw = str(value)
    suffix = raw[-4:] if len(raw) >= 4 else raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{label}:***{suffix}#{digest}"


def safe_text(value: Any, max_len: int = 80) -> str:
    """Resumen seguro de texto libre sin conservar contenido literal."""
    if value is None:
        return "text:none"

    raw = str(value)
    normalized_len = len(raw.strip())
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    clipped = max(0, min(max_len, normalized_len))
    return f"text:len={normalized_len},preview_len={clipped},hash={digest}"


def safe_error(value: Any, max_len: int = 120) -> str:
    """Enmascara numeros largos dentro de errores manteniendo correlacion."""
    if value is None:
        return "error:none"
    raw = str(value)

    def _mask_identifier(match: re.Match[str]) -> str:
        digits = "".join(_DIGITS_RE.findall(match.group(0)))
        if len(digits) < 6:
            return match.group(0)
        suffix = digits[-4:] if len(digits) >= 4 else digits
        digest = hashlib.sha256(digits.encode("utf-8")).hexdigest()[:8]
        return f"id:***{suffix}#{digest}"

    masked = _URL_RE.sub(lambda match: safe_url(match.group(0)), raw)
    masked = _SECRET_PAIR_RE.sub(lambda match: f"{match.group(1)}={{masked}}", masked)
    masked = re.sub(r"\+?\d[\d\s().-]{5,}\d", _mask_identifier, masked)
    if len(masked) > max_len:
        return f"{masked[:max_len]}..."
    return masked


def safe_url(value: Any) -> str:
    """Representa URLs sin querystring ni tokens firmados."""
    if value is None:
        return "url:none"
    raw = str(value)
    try:
        parsed = urlsplit(raw)
        host = parsed.netloc or "no-host"
        path = parsed.path or ""
        suffix = path.rsplit("/", 1)[-1][-16:] if path else ""
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
        return f"url:{parsed.scheme or 'unknown'}://{host}/...{suffix}#{digest}"
    except Exception:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
        return f"url:invalid#{digest}"


def safe_mapping(value: Any, label: str = "mapping") -> str:
    """Resume un mapping por sus claves, sin imprimir valores."""
    if not isinstance(value, Mapping):
        return f"{label}:not_mapping"
    keys = sorted(str(key) for key, item in value.items() if item is not None and str(item).strip())
    digest = hashlib.sha256(repr(sorted(str(key) for key in value.keys())).encode("utf-8")).hexdigest()[:8]
    return f"{label}:keys={keys},hash={digest}"


def _obs_token(value: Any, default: str) -> str:
    if value is None or value == "":
        return default
    token = _OBS_TOKEN_RE.sub("_", str(value).strip().lower()).strip("_")
    return token or default


def _obs_value(key: str, value: Any) -> str:
    """Sanitiza valores de observabilidad sin cambiar los helpers existentes."""
    key_norm = _obs_token(key, "field")
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Mapping):
        return safe_mapping(value, key_norm)

    raw = str(value)
    lower_key = key_norm.lower()
    if any(part in lower_key for part in ("phone", "telefono", "number", "from", "to")):
        return safe_phone(raw)
    if any(part in lower_key for part in ("url", "uri", "endpoint", "link")):
        return safe_url(raw)
    if lower_key == "slow_steps":
        return re.sub(r"[^a-zA-Z0-9_.:=;-]+", "_", raw.replace("\r", " ").replace("\n", " ")).strip("_") or "none"
    if any(part in lower_key for part in ("sid", "id", "contact", "advisor", "owner", "session", "conversation")):
        return safe_id(raw, key_norm)
    if any(part in lower_key for part in ("body", "text", "message", "content", "preview")):
        return safe_text(raw)
    if any(part in lower_key for part in ("error", "exception", "reason", "detail")):
        return safe_error(raw)

    safe = raw.replace("\r", " ").replace("\n", " ").strip()
    if len(safe) > 80:
        return safe_text(safe)
    return _OBS_TOKEN_RE.sub("_", safe).strip("_") or "empty"


def obs_event(source: str, component: str, op: str, status: Any = "ok", **fields: Any) -> str:
    """Construye logs `[OBS]` clasificados por origen sin exponer PII."""
    source_token = _obs_token(source, "server")
    if source_token not in OBS_SOURCES:
        source_token = "server"

    parts = [
        "[OBS]",
        f"source={source_token}",
        f"component={_obs_token(component, 'unknown')}",
        f"op={_obs_token(op, 'unknown')}",
        f"status={_obs_value('status', status)}",
    ]
    for key, value in fields.items():
        parts.append(f"{_obs_token(key, 'field')}={_obs_value(key, value)}")
    return " ".join(parts)
