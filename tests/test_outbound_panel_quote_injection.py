"""
Tests de integración: inyección de cita inline en outbound_panel.

Validan que el body/caption que llega a twilio_client incluye el bloque
citado cuando reply_to_preview está presente.
"""

import pytest
from unittest.mock import patch

from utils.reply_quote_formatter import inject_quote, reply_audio_intro


# Estos tests son del helper a nivel de contrato — verifican que el patrón
# que aplica outbound_panel produce los outputs esperados que Twilio recibirá.


def test_text_send_with_reply_prepends_quote():
    """Endpoint JSON: body llega a Twilio con `> ...` prefix."""
    preview = {"sender": "client", "type": "text", "body": "¿precio del apto?"}
    result = inject_quote("Cuesta 350M", preview, is_caption=False)
    assert result.startswith("> 👤 Tú:\n")
    assert "¿precio del apto?" in result
    assert result.endswith("Cuesta 350M")


def test_image_send_with_reply_prepends_quote_in_caption():
    """Multipart con imagen: caption tiene cita + texto del asesor."""
    preview = {"sender": "client", "type": "image", "body": "esta cocina?"}
    result = inject_quote("Sí, esa misma", preview, is_caption=True)
    assert "📷 Imagen" in result
    assert "Sí, esa misma" in result
    # Caption respeta límite WhatsApp
    assert len(result) <= 1024


def test_video_send_with_reply_caption():
    preview = {"sender": "client", "type": "video"}
    result = inject_quote("Mira este recorrido", preview, is_caption=True)
    assert "🎥 Video" in result
    assert "Mira este recorrido" in result


def test_document_send_with_reply_caption():
    preview = {"sender": "advisor", "type": "document", "media_filename": "contrato.pdf"}
    result = inject_quote("Te paso el actualizado", preview, is_caption=True)
    assert "contrato.pdf" in result
    assert "Te paso el actualizado" in result


def test_audio_intro_for_doble_envio():
    """Audio con cita: primero un texto-cita (intro), luego el audio."""
    preview = {"sender": "client", "type": "text", "body": "¿tienes la nota de voz?"}
    intro = reply_audio_intro(preview)
    assert intro is not None
    assert "¿tienes la nota de voz?" in intro
    assert "🎵 Respondiendo con audio:" in intro


def test_send_without_reply_preserves_body():
    """Sin reply_to_preview → body intacto."""
    assert inject_quote("Mensaje normal", None) == "Mensaje normal"
    assert inject_quote("Mensaje normal", {}) == "Mensaje normal"


def test_caption_overflow_preserves_quote():
    """Si caption excede 1024, se trunca el body — la cita queda íntegra."""
    preview = {"sender": "client", "type": "text", "body": "pregunta corta"}
    long_body = "x" * 2000
    result = inject_quote(long_body, preview, is_caption=True)
    assert len(result) <= 1024
    assert "pregunta corta" in result  # quote preservada


def test_body_overflow_preserves_quote():
    """Si body excede 4096, se trunca — la cita queda íntegra."""
    preview = {"sender": "client", "type": "text", "body": "contexto"}
    huge = "y" * 5000
    result = inject_quote(huge, preview, is_caption=False)
    assert len(result) <= 4096
    assert "contexto" in result


def test_multiline_quoted_body_each_line_prefixed():
    """WhatsApp solo renderiza blockquote si CADA línea inicia con >."""
    preview = {"sender": "client", "type": "text", "body": "linea1\nlinea2"}
    result = inject_quote("respuesta", preview)
    quoted_section = result.split("\n\n")[0]
    for line in quoted_section.split("\n"):
        assert line.startswith(">")


def test_advisor_self_quote_uses_asesor_label():
    """Cuando el asesor cita un mensaje previo, el cliente lo ve como "Asesor"."""
    preview = {"sender": "advisor", "type": "text", "body": "lo confirmé"}
    result = inject_quote("y agendé visita", preview)
    assert "Asesor" in result
    assert "lo confirmé" in result


def test_bot_quote_uses_asistente_label():
    preview = {"sender": "bot", "type": "text", "body": "Bienvenido"}
    result = inject_quote("¡Hola!", preview)
    assert "Asistente" in result
