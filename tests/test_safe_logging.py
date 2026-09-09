from utils.safe_logging import safe_error, safe_id, safe_mapping, safe_phone, safe_text, safe_url
from logging_config import SensitiveDataFilter
import logging


def test_safe_phone_masks_digits_but_keeps_correlation():
    raw = "whatsapp:+573001234567"
    masked = safe_phone(raw)

    assert "573001234567" not in masked
    assert "3001234567" not in masked
    assert masked.startswith("phone:***4567#")
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
    masked = safe_error("fallo enviando a +57 300 123 4567 por timeout")

    assert "313" not in masked
    assert "573001234567" not in masked
    assert "id:***4567#" in masked
    assert "timeout" in masked


def test_safe_url_removes_query_tokens():
    masked = safe_url("https://cdn.example.com/private/audio-file.ogg?token=secret")

    assert "token" not in masked
    assert "secret" not in masked
    assert "cdn.example.com" in masked
    assert "#" in masked


def test_safe_mapping_logs_only_keys():
    masked = safe_mapping({"nombre": "Luisa", "telefono": "+573001234567", "vacio": ""}, "meta")

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
        msg="fallo para whatsapp:+573001234567 url=https://x.test/file?token=secret",
        args=(),
        exc_info=None,
    )

    assert SensitiveDataFilter().filter(record)
    rendered = record.getMessage()

    assert "573001234567" not in rendered
    assert "secret" not in rendered
    assert "phone:***4567#" in rendered
    assert "token={masked}" in rendered
