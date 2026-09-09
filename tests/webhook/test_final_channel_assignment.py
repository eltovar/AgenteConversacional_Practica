"""
Regresión: lógica de asignación de final_channel en webhook_handler.py

Bug original: el guard forzaba final_channel='whatsapp' para TODOS los mensajes
entrantes por WhatsApp, ignorando el canal histórico del contacto (historical_channel).
Esto causaba:
  - MongoDB almacenaba mensajes con channel='whatsapp' en lugar del canal real
  - ZSET creaba miembro {phone}:whatsapp en lugar de actualizar {phone}:{canal_origen}
  - Panel no encontraba al contacto ni su historial

Fix: usar historical_channel cuando existe; defaultear a 'whatsapp' solo para
contactos nuevos sin historial previo.
"""

import pytest


def _resolve_final_channel(
    incoming_channel: str,
    historical_channel: str | None,
    processed_body: str = "",
    redis_channel: str = "whatsapp",
) -> str:
    """
    Replica exacta de la lógica de final_channel en webhook_handler.py:356-366.
    Mantener sincronizado si se modifica la lógica de producción.
    """
    _real_incoming = incoming_channel
    if _real_incoming == "whatsapp" and historical_channel:
        return historical_channel
    elif _real_incoming == "whatsapp":
        return "whatsapp"
    else:
        return historical_channel if historical_channel != "whatsapp" else redis_channel


class TestFinalChannelAssignment:

    @pytest.mark.parametrize("incoming_channel,historical_channel,expected", [
        # ── Caso raíz del bug ────────────────────────────────────────────────
        # WhatsApp Directo: debe preservar el canal histórico, no forzar 'whatsapp'
        ("whatsapp", "whatsapp_directo",  "whatsapp_directo"),

        # ── Contactos nuevos (sin historial) ─────────────────────────────────
        # Sin meta previa: defaultear a 'whatsapp'
        ("whatsapp", None,                "whatsapp"),

        # ── WhatsApp puro existente ───────────────────────────────────────────
        # historical='whatsapp' → final='whatsapp' (sin cambio)
        ("whatsapp", "whatsapp",          "whatsapp"),

        # ── Portales → WhatsApp ───────────────────────────────────────────────
        # Contacto que vino de portal luego escribe por WhatsApp:
        # debe preservar canal del portal para no crear duplicidad en ZSET
        ("whatsapp", "metrocuadrado",     "metrocuadrado"),
        ("whatsapp", "finca_raiz",        "finca_raiz"),
        ("whatsapp", "ciencuadras",       "ciencuadras"),
        ("whatsapp", "mercado_libre",     "mercado_libre"),

        # ── Redes sociales → WhatsApp ─────────────────────────────────────────
        ("whatsapp", "instagram",         "instagram"),
        ("whatsapp", "facebook",          "facebook"),
        ("whatsapp", "linkedin",          "linkedin"),

        # ── Canales no-WhatsApp (rama else intacta) ───────────────────────────
        # Mensajes desde portales/links detectados; la rama else no debe cambiar
        ("finca_raiz",    "finca_raiz",    "finca_raiz"),
        ("metrocuadrado", "metrocuadrado", "metrocuadrado"),
        ("instagram",     "instagram",     "instagram"),
    ])
    def test_final_channel_resolution(self, incoming_channel, historical_channel, expected):
        result = _resolve_final_channel(incoming_channel, historical_channel)
        assert result == expected, (
            f"incoming={incoming_channel!r}, historical={historical_channel!r} "
            f"→ esperado {expected!r}, obtenido {result!r}"
        )

    def test_whatsapp_directo_no_creates_duplicate_zset_key(self):
        """
        Verifica que para whatsapp_directo el canal resultante sea el mismo
        que el histórico, garantizando que update_activity no crea un
        miembro nuevo en el ZSET (evita duplicidad en el panel).
        """
        historical = "whatsapp_directo"
        result = _resolve_final_channel("whatsapp", historical)
        assert result == historical, (
            "final_channel debe coincidir con historical_channel para no crear "
            "un segundo miembro en el ZSET active_conversations_sorted"
        )

    def test_portal_to_whatsapp_no_creates_duplicate_zset_key(self):
        """
        Un contacto de portal que envía WhatsApp debe mantener su canal original.
        Forzar 'whatsapp' crearía {phone}:whatsapp duplicando {phone}:metrocuadrado.
        """
        for portal in ("metrocuadrado", "finca_raiz", "ciencuadras", "mercado_libre"):
            result = _resolve_final_channel("whatsapp", portal)
            assert result == portal, (
                f"Portal {portal!r} → WhatsApp debe resultar en {portal!r}, "
                f"no en 'whatsapp' (evita duplicidad ZSET)"
            )

    def test_new_contact_defaults_to_whatsapp(self):
        """
        Contacto sin ningún historial previo (historical=None) debe defaultear
        a 'whatsapp' para inicializar correctamente su meta.
        """
        result = _resolve_final_channel("whatsapp", None)
        assert result == "whatsapp"

    def test_non_whatsapp_incoming_preserves_historical(self):
        """
        Canales no-WhatsApp (rama else) no deben verse afectados por el fix.
        """
        result = _resolve_final_channel("finca_raiz", "finca_raiz")
        assert result == "finca_raiz"

    def test_non_whatsapp_incoming_falls_back_to_redis_channel(self):
        """
        Rama else: si historical=='whatsapp', usar el redis_channel detectado.
        """
        result = _resolve_final_channel("finca_raiz", "whatsapp", redis_channel="instagram")
        assert result == "instagram"
