# utils/twilio_client.py
"""
Cliente Twilio para envío asíncrono de mensajes WhatsApp.

Necesario cuando usamos agregación de mensajes con timeout > 15 segundos,
ya que Twilio cierra la conexión del webhook después de 15 segundos.

En lugar de responder via TwiML, enviamos mensajes directamente via API.
"""

import os
import httpx
from logging_config import logger

# Configuración de Twilio
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")  # Número de WhatsApp (whatsapp:+1234567890)

# URL base de Twilio API
TWILIO_API_URL = "https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"


class TwilioClient:
    """Cliente para enviar mensajes WhatsApp via Twilio API."""

    def __init__(self):
        self.account_sid = TWILIO_ACCOUNT_SID
        self.auth_token = TWILIO_AUTH_TOKEN
        self.from_number = TWILIO_PHONE_NUMBER
        self._available = self._check_config()

    def _check_config(self) -> bool:
        """Verifica que la configuración de Twilio esté completa."""
        if not all([self.account_sid, self.auth_token, self.from_number]):
            logger.warning(
                "[TwilioClient] Configuración incompleta. "
                "Necesitas: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER"
            )
            return False
        logger.info("[TwilioClient] Cliente inicializado correctamente")
        return True

    @property
    def is_available(self) -> bool:
        """Indica si el cliente está disponible para enviar mensajes."""
        return self._available

    async def send_whatsapp_message(self, to: str, body: str) -> dict:
        """
        Envía un mensaje de WhatsApp usando la API de Twilio.

        Args:
            to: Número de destino (puede ser con o sin prefijo whatsapp:)
            body: Contenido del mensaje

        Returns:
            dict con status y mensaje_sid o error
        """
        if not self._available:
            logger.error("[TwilioClient] Cliente no disponible - configuración incompleta")
            return {"status": "error", "message": "Twilio no configurado"}

        # Asegurar formato correcto del número
        if not to.startswith("whatsapp:"):
            to = f"whatsapp:{to}"

        # Asegurar formato correcto del from_number
        from_number = self.from_number
        if not from_number.startswith("whatsapp:"):
            from_number = f"whatsapp:{from_number}"

        url = TWILIO_API_URL.format(account_sid=self.account_sid)

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    auth=(self.account_sid, self.auth_token),
                    data={
                        "From": from_number,
                        "To": to,
                        "Body": body
                    }
                )

                if response.status_code in (200, 201):
                    data = response.json()
                    logger.info(f"[TwilioClient] Mensaje enviado exitosamente. SID: {data.get('sid')}")
                    return {
                        "status": "success",
                        "message_sid": data.get("sid"),
                        "to": to
                    }
                else:
                    error_msg = response.text
                    logger.error(f"[TwilioClient] Error enviando mensaje: {response.status_code} - {error_msg}")
                    return {
                        "status": "error",
                        "code": response.status_code,
                        "message": error_msg
                    }

        except Exception as e:
            logger.error(f"[TwilioClient] Excepción enviando mensaje: {e}")
            return {
                "status": "error",
                "message": str(e)
            }


# Instancia global (singleton)
twilio_client = TwilioClient()