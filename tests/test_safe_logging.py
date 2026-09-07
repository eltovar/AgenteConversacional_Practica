from utils.safe_logging import obs_event, safe_error, safe_id, safe_mapping, safe_phone, safe_text, safe_url
from logging_config import SensitiveDataFilter
import logging


def test_safe_phone_masks_digits_but_keeps_correlation():
    raw = "whatsapp:+573138405930"
    masked = safe_phone(raw)

    assert "573138405930" not in masked
    assert "3138405930" not in masked
    assert masked.startswith("phone:***5930#")
    assert safe_phone(raw) == masked


def test_safe_text_does_not_emit_content():
    body = "quiero informacion del apartamento"
    masked = safe_text(body)

    assert body not in masked
    assert "apartamento" not in masked
    assert "len=34" in masked
    assert "hash=" in masked


def test_safe_id_masks_external_identifier():
    raw = "236935263571"
    masked = safe_id(raw, "contact")

    assert raw not in masked
    assert masked.startswith("contact:***3571#")


def test_safe_error_masks_phone_like_sequences():
    masked = safe_error("fallo enviando a +57 313 840 5930 por timeout")

    assert "313" not in masked
    assert "573138405930" not in masked
    assert "id:***5930#" in masked
    assert "timeout" in masked


def test_safe_url_removes_query_tokens():
    masked = safe_url("https://cdn.example.com/private/audio-file.ogg?token=secret")

    assert "token" not in masked
    assert "secret" not in masked
    assert "cdn.example.com" in masked
    assert "#" in masked


def test_safe_error_masks_urls_and_secret_pairs():
    masked = safe_error(
        "fallo GET https://cdn.example.com/private/audio.ogg?token=secret api_key=abc123"
    )

    assert "https://cdn.example.com/private/audio.ogg?token=secret" not in masked
    assert "secret" not in masked
    assert "abc123" not in masked
    assert "url:https://cdn.example.com/" in masked
    assert "api_key={masked}" in masked


def test_safe_mapping_logs_only_keys():
    masked = safe_mapping({"nombre": "Luisa", "telefono": "+573138405930", "vacio": ""}, "meta")

    assert "Luisa" not in masked
    assert "+573" not in masked
    assert "nombre" in masked
    assert "telefono" in masked


def test_sensitive_data_filter_masks_unwrapped_log_messages():
    record = logging.LogRecord(
        name="agent_system",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="fallo para whatsapp:+573138405930 url=https://x.test/file?token=secret",
        args=(),
        exc_info=None,
    )

    assert SensitiveDataFilter().filter(record)
    rendered = record.getMessage()

    assert "573138405930" not in rendered
    assert "secret" not in rendered
    assert "phone:***5930#" in rendered
    assert "token={masked}" in rendered


def test_obs_event_has_operational_fields():
    rendered = obs_event(
        "hubspot",
        "panel",
        "batch_read",
        status=207,
        duration_ms=349,
        contact_count=0,
    )

    assert rendered.startswith("[OBS]")
    assert "source=hubspot" in rendered
    assert "component=panel" in rendered
    assert "op=batch_read" in rendered
    assert "status=207" in rendered
    assert "duration_ms=349" in rendered
    assert "contact_count=0" in rendered


def test_obs_event_sanitizes_sensitive_fields():
    rendered = obs_event(
        "twilio",
        "status_callback",
        "delivery",
        status="failed",
        phone="whatsapp:+573138405930",
        message="quiero informacion del apartamento",
        signed_url="https://cdn.example.com/private/audio.ogg?token=secret",
        error="fallo enviando a +57 313 840 5930 por timeout",
        MessageSid="SM12345678901234567890",
    )

    assert "573138405930" not in rendered
    assert "3138405930" not in rendered
    assert "apartamento" not in rendered
    assert "secret" not in rendered
    assert "SM12345678901234567890" not in rendered
    assert "phone:***5930#" in rendered
    assert "message=text:len=34" in rendered
    assert "signed_url=url:https://cdn.example.com/" in rendered
    assert "error=fallo enviando a id:***5930#" in rendered
    assert "messagesid=messagesid:***7890#" in rendered


def test_obs_event_unknown_source_falls_back_to_server():
    rendered = obs_event("unknown-system", "api", "failure", status="error")

    assert "source=server" in rendered
    assert "component=api" in rendered
    assert "op=failure" in rendered
