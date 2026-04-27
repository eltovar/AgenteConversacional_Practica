"""Tests del formateador de citas inline para outbound WhatsApp."""

import pytest

from utils.reply_quote_formatter import (
    format_quote_prefix,
    inject_quote,
    reply_audio_intro,
    WHATSAPP_BODY_MAX,
    WHATSAPP_CAPTION_MAX,
)


def test_text_quote_format():
    preview = {"sender": "client", "type": "text", "body": "Hola, quería más info sobre el apartamento"}
    out = format_quote_prefix(preview)
    assert out.startswith("> 👤 Cliente:\n")
    assert "Hola, quería más info" in out
    assert out.endswith("\n\n")


def test_image_quote_with_caption():
    preview = {"sender": "client", "type": "image", "body": "Esta es la cocina"}
    out = format_quote_prefix(preview)
    assert "📷 Imagen" in out
    assert "cocina" in out
    assert out.startswith("> ")


def test_video_quote_no_caption():
    preview = {"sender": "client", "type": "video"}
    out = format_quote_prefix(preview)
    assert "🎥 Video" in out
    assert out.startswith("> 👤 Cliente:")


def test_audio_quote_format():
    preview = {"sender": "client", "type": "audio"}
    out = format_quote_prefix(preview)
    assert "🎵 Audio" in out


def test_document_quote_with_filename():
    preview = {"sender": "advisor", "type": "document", "media_filename": "contrato.pdf"}
    out = format_quote_prefix(preview)
    assert "Tú" in out
    assert "📄" in out
    assert "contrato.pdf" in out


def test_multiline_body_each_line_prefixed():
    preview = {"sender": "client", "type": "text", "body": "linea1\nlinea2\nlinea3"}
    out = format_quote_prefix(preview)
    # Cada línea del bloque debe iniciar con "> "
    body_lines = [l for l in out.split("\n") if l.strip()]
    assert all(l.startswith(">") for l in body_lines)


def test_truncation_preview_long_text():
    long_text = "a" * 200
    preview = {"sender": "client", "type": "text", "body": long_text}
    out = format_quote_prefix(preview)
    # Preview truncado a ~80 chars
    assert "…" in out
    assert len(out) < 200


def test_inject_quote_text_preserves_quote_over_body():
    preview = {"sender": "client", "type": "text", "body": "pregunta original"}
    body = "x" * (WHATSAPP_BODY_MAX + 100)
    out = inject_quote(body, preview, is_caption=False)
    assert len(out) <= WHATSAPP_BODY_MAX
    assert "pregunta original" in out
    assert out.endswith("…")


def test_inject_quote_caption_respects_1024():
    preview = {"sender": "client", "type": "text", "body": "ver foto"}
    body = "y" * 2000
    out = inject_quote(body, preview, is_caption=True)
    assert len(out) <= WHATSAPP_CAPTION_MAX
    assert "ver foto" in out


def test_missing_preview_returns_body_intact():
    assert inject_quote("hola", None) == "hola"
    assert inject_quote("hola", {}) == "hola"
    assert format_quote_prefix(None) == ""
    assert format_quote_prefix({}) == ""


def test_sender_labels():
    assert "Cliente" in format_quote_prefix({"sender": "client", "type": "text", "body": "x"})
    assert "Tú" in format_quote_prefix({"sender": "advisor", "type": "text", "body": "x"})
    assert "SofIA" in format_quote_prefix({"sender": "bot", "type": "text", "body": "x"})
    # Sender desconocido fallback a Cliente
    assert "Cliente" in format_quote_prefix({"sender": "alien", "type": "text", "body": "x"})


def test_inject_quote_empty_body_returns_only_quote():
    preview = {"sender": "client", "type": "text", "body": "hola"}
    out = inject_quote("", preview)
    assert out.startswith("> 👤 Cliente:")
    # Sin trailing body, pero el separador \n\n se mantiene


def test_reply_audio_intro_returns_none_without_preview():
    assert reply_audio_intro(None) is None
    assert reply_audio_intro({}) is None


def test_reply_audio_intro_includes_quote_and_indicator():
    preview = {"sender": "client", "type": "text", "body": "tienes el video?"}
    out = reply_audio_intro(preview)
    assert out is not None
    assert "tienes el video" in out
    assert "🎵 Respondiendo con audio:" in out


def test_text_quote_no_body_fallback():
    preview = {"sender": "client", "type": "text", "body": ""}
    out = format_quote_prefix(preview)
    assert "(mensaje sin texto)" in out


def test_inject_quote_combines_correctly():
    preview = {"sender": "client", "type": "text", "body": "¿precio?"}
    out = inject_quote("Cuesta 350M", preview)
    assert "> 👤 Cliente:" in out
    assert "¿precio?" in out
    assert "Cuesta 350M" in out
    # El body real va después del separador \n\n
    assert "\n\nCuesta 350M" in out
