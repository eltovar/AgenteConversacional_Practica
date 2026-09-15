"""Cliente WhatsApp Cloud API de Meta.

Mantiene el contrato historico de `send_whatsapp_message()` para que el resto
del CRM no dependa del proveedor de transporte.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

import httpx

from logging_config import logger
from utils.environment import require_whatsapp_outbound_allowed
from utils.safe_logging import safe_error, safe_id, safe_phone, safe_url


META_API_VERSION = os.getenv("META_API_VERSION", "v21.0").strip() or "v21.0"
META_GRAPH_BASE_URL = os.getenv("META_GRAPH_BASE_URL", "https://graph.facebook.com").rstrip("/")
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "").strip()
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "").strip()


class MetaWhatsAppClient:
    """Cliente async para enviar WhatsApp por Meta Cloud API."""

    def __init__(self) -> None:
        self.phone_number_id = META_PHONE_NUMBER_ID
        self.access_token = META_ACCESS_TOKEN
        self.api_version = META_API_VERSION
        self.graph_base_url = META_GRAPH_BASE_URL
        self._available = self._check_config()
        self._http_client: Optional[httpx.AsyncClient] = None

    def _check_config(self) -> bool:
        if not all([self.phone_number_id, self.access_token]):
            logger.warning(
                "[MetaWhatsAppClient] Configuracion incompleta. "
                "Necesitas: META_PHONE_NUMBER_ID, META_ACCESS_TOKEN"
            )
            return False
        logger.info("[MetaWhatsAppClient] Cliente inicializado correctamente")
        return True

    def _resolve_credentials(self) -> tuple[Optional[str], Optional[str]]:
        phone_number_id = self.phone_number_id or os.getenv("META_PHONE_NUMBER_ID", "").strip()
        access_token = self.access_token or os.getenv("META_ACCESS_TOKEN", "").strip()
        if phone_number_id and not self.phone_number_id:
            self.phone_number_id = phone_number_id
        if access_token and not self.access_token:
            self.access_token = access_token
        self._available = bool(self.phone_number_id and self.access_token)
        return self.phone_number_id, self.access_token

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                limits=httpx.Limits(
                    max_keepalive_connections=5,
                    max_connections=10,
                    keepalive_expiry=30.0,
                ),
            )
        return self._http_client

    @property
    def is_available(self) -> bool:
        self._resolve_credentials()
        return self._available

    def _messages_url(self, phone_number_id: str) -> str:
        return f"{self.graph_base_url}/{self.api_version}/{phone_number_id}/messages"

    @staticmethod
    def _normalize_to(to: str) -> str:
        raw = (to or "").replace("whatsapp:", "").strip()
        digits = re.sub(r"\D+", "", raw)
        return digits

    @staticmethod
    def _template_name_from_content_sid(content_sid: str) -> str:
        """Resolve template name from env mapping or a legacy identifier.

        Twilio ContentSid (HX...) no existe en Meta. Durante migracion se permite
        mapear cada HX por variable: META_TEMPLATE_<HX...>=nombre_meta.
        """
        key = f"META_TEMPLATE_{(content_sid or '').strip().upper()}"
        mapped = os.getenv(key, "").strip()
        if mapped:
            return mapped
        return (content_sid or "").strip()

    @staticmethod
    def _content_variables_to_components(content_variables: Optional[dict]) -> list[dict]:
        if not content_variables:
            return []
        parameters = []
        for key in sorted(content_variables.keys(), key=lambda item: int(item) if str(item).isdigit() else str(item)):
            value = content_variables.get(key)
            parameters.append({"type": "text", "text": "" if value is None else str(value)})
        return [{"type": "body", "parameters": parameters}] if parameters else []

    @staticmethod
    def _media_type_from_url(media_url: str, media_content_type: Optional[str] = None) -> str:
        ct = (media_content_type or "").lower()
        url = (media_url or "").lower()
        if "image/" in ct or url.endswith((".jpg", ".jpeg", ".png", ".webp")):
            return "image"
        if "audio/" in ct or url.endswith((".mp3", ".ogg", ".oga", ".m4a", ".aac", ".wav")):
            return "audio"
        if "video/" in ct or url.endswith((".mp4", ".mov")):
            return "video"
        return "document"

    async def send_whatsapp_message(
        self,
        to: str,
        body: str,
        media_url: Optional[str] = None,
        content_sid: Optional[str] = None,
        content_variables: Optional[dict] = None,
        conversation_sid: Optional[str] = None,
        chat_service_sid: Optional[str] = None,
        media_bytes: Optional[bytes] = None,
        media_content_type: Optional[str] = None,
        media_filename: Optional[str] = None,
    ) -> dict:
        del conversation_sid, chat_service_sid, media_bytes

        allowed, reason = require_whatsapp_outbound_allowed(to)
        if not allowed:
            logger.warning(
                "[MetaWhatsAppClient] Outbound bloqueado por safety gate: reason=%s to=%s",
                reason,
                safe_phone(to),
            )
            return {
                "status": "blocked",
                "message": reason,
                "blocked_by_safety_gate": True,
            }

        phone_number_id, access_token = self._resolve_credentials()
        if not self._available:
            logger.error("[MetaWhatsAppClient] Cliente no disponible - configuracion incompleta")
            return {"status": "error", "message": "Meta WhatsApp no configurado"}

        to_digits = self._normalize_to(to)
        if not to_digits:
            return {"status": "error", "code": 400, "message": "Destino WhatsApp invalido"}

        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_digits,
        }

        if content_sid:
            template_name = self._template_name_from_content_sid(content_sid)
            payload["type"] = "template"
            payload["template"] = {
                "name": template_name,
                "language": {"code": os.getenv("META_TEMPLATE_LANGUAGE", "es").strip() or "es"},
            }
            components = self._content_variables_to_components(content_variables)
            if components:
                payload["template"]["components"] = components
            logger.info(
                "[MetaWhatsAppClient] Enviando template=%s vars=%s",
                safe_id(template_name, "template"),
                safe_id(content_variables, "template_vars"),
            )
        elif media_url:
            media_type = self._media_type_from_url(media_url, media_content_type)
            payload["type"] = media_type
            media_payload: dict[str, Any] = {"link": media_url}
            if body and media_type in {"image", "video", "document"}:
                media_payload["caption"] = body
            if media_filename and media_type == "document":
                media_payload["filename"] = media_filename
            payload[media_type] = media_payload
            logger.info("[MetaWhatsAppClient] Enviando media %s: %s", media_type, safe_url(media_url))
        else:
            payload["type"] = "text"
            payload["text"] = {"preview_url": True, "body": body or ""}
            logger.debug("[MetaWhatsAppClient] Enviando texto")

        try:
            response = await self._get_http_client().post(
                self._messages_url(phone_number_id or ""),
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

            if response.status_code in (200, 201):
                data = response.json()
                messages = data.get("messages") or []
                message_id = (messages[0] or {}).get("id") if messages else None
                logger.info(
                    "[MetaWhatsAppClient] Mensaje aceptado por Meta. id=%s",
                    safe_id(message_id, "wamid"),
                )
                return {
                    "status": "success",
                    "message_sid": message_id,
                    "wamid": message_id,
                    "provider": "meta",
                    "message_status": "accepted",
                    "to": f"whatsapp:+{to_digits}",
                }

            error_code = None
            error_message = response.text
            try:
                err = response.json().get("error") or {}
                error_code = err.get("code") or err.get("error_subcode")
                error_message = err.get("message") or response.text
            except Exception:
                pass
            logger.error(
                "[MetaWhatsAppClient] Error enviando mensaje: %s - %s",
                response.status_code,
                safe_error(error_message, 220),
            )
            return {
                "status": "error",
                "code": error_code or response.status_code,
                "message": error_message,
            }
        except Exception as e:
            logger.error("[MetaWhatsAppClient] Excepcion enviando mensaje: %s", safe_error(e))
            return {"status": "error", "message": str(e)}

    async def get_conversation_message(self, *args, **kwargs) -> dict:
        return {"status": "error", "message": "Meta Cloud API no soporta lectura estilo Twilio Conversations"}

    async def list_conversation_messages(self, *args, **kwargs) -> dict:
        return {"status": "error", "message": "Meta Cloud API no soporta listado estilo Twilio Conversations"}

    async def update_conversation_message(self, *args, **kwargs) -> dict:
        return {"status": "error", "message": "Meta Cloud API no soporta editar mensajes enviados"}

    async def delete_conversation_message(self, *args, **kwargs) -> dict:
        return {"status": "error", "message": "Meta Cloud API no soporta eliminar mensajes enviados"}


meta_whatsapp_client = MetaWhatsAppClient()
