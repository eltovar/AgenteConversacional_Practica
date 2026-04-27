"""
Formateador de citas inline para outbound WhatsApp.

Twilio Conversations Messages API no soporta `ReplyTo` nativo. Como mitigación,
prefijamos el body/caption del mensaje con un bloque citado al estilo Markdown
de WhatsApp (`> texto`), que el cliente ve renderizado como blockquote.
"""

from typing import Optional

# Límites WhatsApp
WHATSAPP_BODY_MAX = 4096
WHATSAPP_CAPTION_MAX = 1024

# Truncado del preview de la cita
QUOTE_PREVIEW_MAX_CHARS = 80

# Etiquetas de sender — desde la perspectiva del CLIENTE que recibe el mensaje.
# Si el mensaje citado lo envió el cliente, para él es "Tú".
# Si lo envió el asesor o el bot, lo ve como "Asesor" / "Asistente".
SENDER_LABELS = {
    "client": "Tú",
    "advisor": "Asesor",
    "bot": "Asistente",
}

# Iconos por tipo de mensaje original
TYPE_ICONS = {
    "image": "📷 Imagen",
    "video": "🎥 Video",
    "audio": "🎵 Audio",
    "voice": "🎵 Audio",
    "document": "📄 Documento",
    "location": "📍 Ubicación",
    "sticker": "🌟 Sticker",
}


def _truncate(text: str, limit: int) -> str:
    """Trunca preservando una elipsis al final si excede."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _prefix_each_line(text: str, prefix: str = "> ") -> str:
    """Prefija cada línea con `prefix` (regla blockquote WhatsApp)."""
    if not text:
        return ""
    return "\n".join(f"{prefix}{line}" if line else prefix.rstrip() for line in text.split("\n"))


def format_quote_prefix(reply_to_preview: Optional[dict]) -> str:
    """Genera el bloque citado para prependear al body o caption.

    Devuelve string vacío si el preview es inválido o ausente — el caller
    debe enviar el mensaje sin cita en ese caso.

    Formato:
        > 👤 Cliente:
        > texto del mensaje original truncado…

        (línea en blanco antes del body real)
    """
    if not reply_to_preview or not isinstance(reply_to_preview, dict):
        return ""

    sender = reply_to_preview.get("sender") or "client"
    # Frontend usa `media_type`, otros callers pueden enviar `type`
    msg_type = (reply_to_preview.get("media_type") or reply_to_preview.get("type") or "text").lower()
    # Frontend usa `content`, otros callers pueden enviar `body`
    body = reply_to_preview.get("content") or reply_to_preview.get("body") or ""
    filename = reply_to_preview.get("media_filename") or reply_to_preview.get("filename")

    sender_label = SENDER_LABELS.get(sender, "Tú")
    header = f"👤 {sender_label}:"

    if msg_type == "text":
        preview = _truncate(body, QUOTE_PREVIEW_MAX_CHARS) or "(mensaje sin texto)"
    elif msg_type == "document":
        label = TYPE_ICONS["document"]
        preview = f"{label} — {filename}" if filename else label
        preview = _truncate(preview, QUOTE_PREVIEW_MAX_CHARS)
    elif msg_type in TYPE_ICONS:
        label = TYPE_ICONS[msg_type]
        if body:
            preview = f"{label} — {_truncate(body, QUOTE_PREVIEW_MAX_CHARS - len(label) - 3)}"
        else:
            preview = label
    else:
        preview = _truncate(body, QUOTE_PREVIEW_MAX_CHARS) or "(mensaje)"

    block = f"{header}\n{preview}"
    return _prefix_each_line(block) + "\n\n"


def inject_quote(
    body: str,
    reply_to_preview: Optional[dict],
    is_caption: bool = False,
) -> str:
    """Inyecta la cita al inicio del body o caption, respetando límites.

    Estrategia: la cita SIEMPRE se preserva. Si el resultado excede el límite,
    se trunca el body del usuario, no el quote (el contexto pesa más que la
    cola del mensaje, que el asesor puede recomponer).

    Args:
        body: texto del asesor (puede ser vacío si solo manda media).
        reply_to_preview: dict con sender/type/body/media_filename.
        is_caption: True si el destino es caption de media (límite 1024).
    """
    quote = format_quote_prefix(reply_to_preview)
    if not quote:
        return body or ""

    body = body or ""
    limit = WHATSAPP_CAPTION_MAX if is_caption else WHATSAPP_BODY_MAX
    combined = f"{quote}{body}"

    if len(combined) <= limit:
        return combined

    # Truncar body, preservar quote íntegra
    available_for_body = limit - len(quote) - 1  # -1 para la elipsis
    if available_for_body <= 0:
        # Caso degenerado: la cita misma excede; truncamos la cita como último recurso
        return _truncate(quote.rstrip(), limit)
    return f"{quote}{body[:available_for_body].rstrip()}…"


def reply_audio_intro(reply_to_preview: Optional[dict]) -> Optional[str]:
    """Mensaje de texto previo para casos de respuesta-audio con cita.

    WhatsApp no acepta caption en audio/PTT. Cuando el asesor cita y manda
    audio, primero enviamos un texto con la cita + indicador, luego el audio.

    Devuelve None si no hay cita (no requiere doble envío).
    """
    quote = format_quote_prefix(reply_to_preview)
    if not quote:
        return None
    return f"{quote}🎵 Respondiendo con audio:"
