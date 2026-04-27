# utils/twilio_client.py
"""
Cliente Twilio para envío asíncrono de mensajes WhatsApp.

Necesario cuando usamos agregación de mensajes con timeout > 15 segundos,
ya que Twilio cierra la conexión del webhook después de 15 segundos.

En lugar de responder via TwiML, enviamos mensajes directamente via API.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import Optional
import httpx
from logging_config import logger

# Configuración de Twilio
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")  # Número de WhatsApp (whatsapp:+1234567890)
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID")  # MGxxx — Conversations API

# URL base de Twilio API
TWILIO_API_URL = "https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"

# Twilio llamará esta URL cuando el estado del mensaje cambie (delivered/undelivered/failed).
# Sin esto, Twilio no tiene a dónde reportar errores como el 63024.
_PANEL_BASE_URL = os.getenv("PANEL_BASE_URL", "").rstrip("/")
STATUS_CALLBACK_URL = f"{_PANEL_BASE_URL}/whatsapp/status" if _PANEL_BASE_URL else None

# Conversations API (necesaria para edit/delete de mensajes outbound desde el panel).
# Service-aware: si la conv vive en una Conversations Service específica (ChatServiceSid),
# las URLs deben incluir /Services/{ISxxx}/. Sin esto las llamadas a convs de servicios
# distintos al Default devuelven 404, causando split-brain.
TWILIO_CONV_BASE = "https://conversations.twilio.com/v1"


def _conv_url(conv_sid: str, chat_service_sid: Optional[str] = None, suffix: str = "") -> str:
    """Construye URL Conversations API respetando ChatServiceSid si se conoce."""
    if chat_service_sid:
        base = f"{TWILIO_CONV_BASE}/Services/{chat_service_sid}/Conversations/{conv_sid}"
    else:
        base = f"{TWILIO_CONV_BASE}/Conversations/{conv_sid}"
    return f"{base}{suffix}" if suffix else base


class TwilioClient:
    """Cliente para enviar mensajes WhatsApp via Twilio API."""

    def __init__(self):
        self.account_sid = TWILIO_ACCOUNT_SID
        self.auth_token = TWILIO_AUTH_TOKEN
        self.from_number = TWILIO_PHONE_NUMBER
        self.messaging_service_sid = TWILIO_MESSAGING_SERVICE_SID
        self._available = self._check_config()
        self._http_client: Optional[httpx.AsyncClient] = None

    def _get_http_client(self) -> httpx.AsyncClient:
        """Singleton httpx.AsyncClient — evita crear pool por mensaje."""
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

    def _check_config(self) -> bool:
        """Verifica que la configuración de Twilio esté completa."""
        if not all([self.account_sid, self.auth_token]):
            logger.warning(
                "[TwilioClient] Configuración incompleta. "
                "Necesitas: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN"
            )
            return False
        # Requiere TWILIO_PHONE_NUMBER o TWILIO_MESSAGING_SERVICE_SID (uno de los dos)
        if not self.from_number and not self.messaging_service_sid:
            logger.warning(
                "[TwilioClient] Configuración incompleta. "
                "Necesitas TWILIO_PHONE_NUMBER o TWILIO_MESSAGING_SERVICE_SID"
            )
            return False
        mode = "MessagingService" if self.messaging_service_sid else "PhoneNumber"
        logger.info(f"[TwilioClient] Cliente inicializado correctamente (modo: {mode})")
        return True

    def _resolve_credentials(self) -> tuple[Optional[str], Optional[str], Optional[str]]:
        # Why: Railway puede inyectar env vars con latencia tras un spawn post-SIGKILL;
        # si el singleton se construyó con None, reintentamos leer y backfillamos el cache.
        sid = self.account_sid or os.getenv("TWILIO_ACCOUNT_SID")
        token = self.auth_token or os.getenv("TWILIO_AUTH_TOKEN")
        phone = self.from_number or os.getenv("TWILIO_PHONE_NUMBER")
        msg_svc = self.messaging_service_sid or os.getenv("TWILIO_MESSAGING_SERVICE_SID")
        if sid and not self.account_sid:
            self.account_sid = sid
        if token and not self.auth_token:
            self.auth_token = token
        if phone and not self.from_number:
            self.from_number = phone
        if msg_svc and not self.messaging_service_sid:
            self.messaging_service_sid = msg_svc
        self._available = bool(self.account_sid and self.auth_token and (self.from_number or self.messaging_service_sid))
        return sid, token, phone

    @property
    def is_available(self) -> bool:
        """Indica si el cliente está disponible para enviar mensajes."""
        return self._available

    async def _resolve_conv_sid_for_phone(self, to: str) -> tuple[Optional[str], Optional[str]]:
        """Busca el (conversation_sid, chat_service_sid) activo (< 23h) en MongoDB.

        Retorna ambos campos porque Conversations API requiere el ChatServiceSid
        para resolver convs de Services no-Default. Sin esto, las URLs apuntan
        al Default y devuelven 404 para convs de otros services.
        """
        try:
            from database.mongodb_client import get_mongo_manager
            from middleware.phone_normalizer import normalize_phone

            phone = to.replace("whatsapp:", "").strip()
            try:
                phone = normalize_phone(phone)
            except Exception:
                pass

            cutoff = datetime.utcnow() - timedelta(hours=23)
            mm = get_mongo_manager()
            if not await mm.connect():
                return None, None
            doc = await mm.db.messages.find_one(
                {
                    "phone": phone,
                    "conversation_sid": {"$nin": [None, ""]},
                    "timestamp_utc": {"$gte": cutoff},
                },
                sort=[("timestamp_utc", -1)],
                projection={"conversation_sid": 1, "chat_service_sid": 1},
            )
            if not doc:
                return None, None
            return doc.get("conversation_sid"), doc.get("chat_service_sid")
        except Exception as e:
            logger.warning(f"[TwilioClient] resolve conv_sid fallo para {to}: {e}")
            return None, None

    async def _invalidate_stale_conv_sid(
        self,
        conversation_sid: str,
        chat_service_sid: Optional[str] = None,
    ) -> None:
        """Limpia un conversation_sid stale.

        Dos pasos críticos para evitar split-brain (conversaciones paralelas
        para el mismo número, lo que rompe la resolución WAMid→IM SID):
        1. Cerrar la conversación en Twilio (State=closed). Si la conv vive en
           un Conversations Service no-Default, debe usarse la URL namespaced
           con ChatServiceSid o el cierre devuelve 404.
        2. Limpiar referencias en MongoDB para que el próximo outbound no
           reintente con un SID muerto.
        """
        # Paso 1: cerrar en Twilio (service-aware)
        try:
            sid, token, _ = self._resolve_credentials()
            if sid and token:
                close_url = _conv_url(conversation_sid, chat_service_sid)
                client = self._get_http_client()
                resp = await client.post(
                    close_url,
                    auth=(sid, token),
                    data={"State": "closed"},
                )
                if resp.status_code in (200, 201):
                    logger.info(
                        f"[TwilioClient] Conversación {conversation_sid[:12]}... "
                        f"cerrada en Twilio (State=closed)"
                    )
                elif resp.status_code == 404:
                    # Ya no existe en Twilio, OK
                    logger.info(
                        f"[TwilioClient] Conversación {conversation_sid[:12]}... "
                        f"ya inexistente en Twilio (404)"
                    )
                else:
                    logger.warning(
                        f"[TwilioClient] No se pudo cerrar conv {conversation_sid[:12]}...: "
                        f"{resp.status_code} - {resp.text[:200]}"
                    )
        except Exception as e:
            logger.warning(f"[TwilioClient] Error cerrando conv en Twilio: {e}")

        # Paso 2: limpiar MongoDB
        try:
            from database.mongodb_client import get_mongo_manager
            mm = get_mongo_manager()
            if not await mm.connect():
                return
            result = await mm.db.messages.update_many(
                {"conversation_sid": conversation_sid},
                {"$unset": {"conversation_sid": "", "conversations_message_sid": ""}},
            )
            if result.modified_count:
                logger.info(
                    f"[TwilioClient] Stale conv_sid {conversation_sid[:12]}... "
                    f"limpiado en {result.modified_count} docs"
                )
        except Exception as e:
            logger.warning(f"[TwilioClient] Error limpiando stale conv_sid: {e}")

    async def _send_via_conversations(
        self,
        conversation_sid: str,
        body: Optional[str] = None,
        content_sid: Optional[str] = None,
        content_variables: Optional[dict] = None,
        chat_service_sid: Optional[str] = None,
    ) -> dict:
        """Envía mensaje vía endpoint Conversations (billing por sesión 24h).

        Endpoint: POST /v1/[Services/{ISxxx}/]Conversations/{CHsid}/Messages
        Reduce costo de $0.005/mensaje a $0.005/sesión-24h ilimitada.
        No soporta MediaUrl directo (requiere upload previo a Media Content Service).
        """
        sid, token, _ = self._resolve_credentials()
        if not self._available:
            return {"status": "error", "message": "Twilio no configurado"}

        url = _conv_url(conversation_sid, chat_service_sid, suffix="/Messages")
        payload: dict = {}
        if content_sid:
            payload["ContentSid"] = content_sid
            if content_variables:
                payload["ContentVariables"] = json.dumps(content_variables)
        elif body:
            payload["Body"] = body
        else:
            return {"status": "error", "message": "Sin body ni content_sid"}

        try:
            client = self._get_http_client()
            resp = await client.post(url, auth=(sid, token), data=payload)
            if resp.status_code in (200, 201):
                data = resp.json()
                im_sid = data.get("sid")
                logger.info(
                    f"[TwilioClient][Conv] Enviado conv={conversation_sid[:12]}... "
                    f"IM={im_sid} (billing por sesión 24h)"
                )
                return {
                    "status": "success",
                    "message_sid": im_sid,
                    "conversations_message_sid": im_sid,
                    "conversation_sid": conversation_sid,
                    "via": "conversations",
                }
            if resp.status_code == 404:
                # Conversación expirada/cerrada en Twilio → limpiar para no reintentar
                asyncio.create_task(
                    self._invalidate_stale_conv_sid(conversation_sid, chat_service_sid)
                )
            logger.warning(
                f"[TwilioClient][Conv] Error {resp.status_code} send conv={conversation_sid}: "
                f"{resp.text[:200]}"
            )
            return {"status": "error", "code": resp.status_code, "message": resp.text}
        except Exception as e:
            logger.error(f"[TwilioClient][Conv] Excepción: {e}")
            return {"status": "error", "message": str(e)}

    async def send_whatsapp_message(
        self,
        to: str,
        body: str,
        media_url: Optional[str] = None,
        content_sid: Optional[str] = None,
        content_variables: Optional[dict] = None,
        conversation_sid: Optional[str] = None,
        chat_service_sid: Optional[str] = None,
    ) -> dict:
        """
        Envía un mensaje de WhatsApp usando la API de Twilio.

        Routing:
        - Si NO hay media_url Y se conoce conv_sid (param o resolvible): usa
          endpoint /v1/Conversations/{CHsid}/Messages → billing por sesión 24h.
        - Caso contrario (media o primera interacción saliente): usa
          /Messages.json con MessagingServiceSid → autocrea conv (legacy).

        Args:
            to: Número de destino (puede ser con o sin prefijo whatsapp:)
            body: Contenido del mensaje (usado si content_sid es None)
            media_url: URL de multimedia (imagen/audio) a enviar (opcional)
            content_sid: Content-SID del template aprobado por Meta (HXxxx...).
                         Cuando se provee, se usa ContentSid en lugar de Body,
                         lo que permite enviar fuera de la ventana de 24h.
            content_variables: Dict con variables numeradas para el template,
                               ej: {"1": "Carlos", "2": "Monica"}.
            conversation_sid: Si se conoce, evita lookup en Mongo y fuerza
                              ruteo por endpoint Conversations.

        Returns:
            dict con status y mensaje_sid o error
        """
        sid, token, phone = self._resolve_credentials()
        if not self._available:
            logger.error("[TwilioClient] Cliente no disponible - configuración incompleta")
            return {"status": "error", "message": "Twilio no configurado"}

        # Asegurar formato correcto del número destino
        if not to.startswith("whatsapp:"):
            to = f"whatsapp:{to}"

        # Ruta preferida: Conversations endpoint (billing por sesión 24h)
        # Solo aplica si no hay media (Conversations requiere MCS upload para MediaSid).
        if not media_url:
            if conversation_sid:
                conv_sid = conversation_sid
                svc_sid = chat_service_sid
            else:
                conv_sid, svc_sid = await self._resolve_conv_sid_for_phone(to)
            if conv_sid:
                conv_result = await self._send_via_conversations(
                    conversation_sid=conv_sid,
                    body=body if not content_sid else None,
                    content_sid=content_sid,
                    content_variables=content_variables,
                    chat_service_sid=svc_sid,
                )
                if conv_result.get("status") == "success":
                    conv_result.setdefault("to", to)
                    conv_result.setdefault("message_status", "queued")
                    conv_result.setdefault("chat_service_sid", svc_sid)
                    return conv_result
                logger.warning(
                    f"[TwilioClient] Conversations endpoint falló "
                    f"(code={conv_result.get('code')}), fallback a /Messages.json"
                )

        url = TWILIO_API_URL.format(account_sid=sid)

        try:
            client = self._get_http_client()

            # Construir payload base:
            # Si hay MessagingServiceSid (Conversations API), usarlo como origen.
            # Si no, usar From con el número de WhatsApp (modo legacy).
            msg_svc = self.messaging_service_sid or os.getenv("TWILIO_MESSAGING_SERVICE_SID")
            if msg_svc:
                payload = {
                    "MessagingServiceSid": msg_svc,
                    "To": to,
                }
                logger.debug(f"[TwilioClient] Usando MessagingServiceSid={msg_svc[:12]}...")
            else:
                from_number = phone
                if not from_number.startswith("whatsapp:"):
                    from_number = f"whatsapp:{from_number}"
                payload = {
                    "From": from_number,
                    "To": to,
                }
            if STATUS_CALLBACK_URL:
                payload["StatusCallback"] = STATUS_CALLBACK_URL

            if content_sid:
                # Template aprobado por Meta → usa ContentSid (funciona fuera de ventana 24h)
                payload["ContentSid"] = content_sid
                if content_variables:
                    payload["ContentVariables"] = json.dumps(content_variables)
                logger.info(
                    f"[TwilioClient] Enviando con ContentSid={content_sid} "
                    f"vars={content_variables}"
                )
            else:
                # Texto libre (solo funciona con ventana de 24h abierta)
                payload["Body"] = body

            # Agregar MediaUrl si se proporciona
            if media_url:
                # VALIDACIÓN CRÍTICA: Twilio requiere URLs con https://
                if not media_url.startswith("https://"):
                    logger.error(
                        f"[TwilioClient] ❌ URL de media inválida (falta https://): "
                        f"{media_url[:80]}... - Twilio rechazará esta URL"
                    )
                    return {
                        "status": "error",
                        "code": 400,
                        "message": f"URL de media debe comenzar con https://. Recibido: {media_url[:50]}..."
                    }

                payload["MediaUrl"] = media_url
                logger.info(f"[TwilioClient] 📤 Enviando con MediaUrl: {media_url[:80]}...")
            else:
                logger.debug(f"[TwilioClient] Enviando mensaje de texto (sin multimedia)")

            response = await client.post(
                url,
                auth=(sid, token),
                data=payload
            )

            if response.status_code in (200, 201):
                data = response.json()
                msg_status = data.get('status', 'unknown')
                error_code = data.get('error_code')
                error_message = data.get('error_message')

                # Log detallado del estado
                if error_code:
                    logger.warning(
                        f"[TwilioClient] ⚠️ Mensaje aceptado pero con error: "
                        f"SID={data.get('sid')}, status={msg_status}, "
                        f"error_code={error_code}, error_message={error_message}"
                    )
                else:
                    logger.info(
                        f"[TwilioClient] Mensaje enviado exitosamente. "
                        f"SID: {data.get('sid')}, status: {msg_status}"
                    )

                return {
                    "status": "success",
                    "message_sid": data.get("sid"),
                    "message_status": msg_status,
                    "to": to,
                    "error_code": error_code,
                    "error_message": error_message
                }
            else:
                error_msg = response.text
                logger.error(f"[TwilioClient] Error enviando mensaje: {response.status_code} - {error_msg}")
                twilio_error_code = None
                try:
                    ct = response.headers.get("content-type", "")
                    if "application/json" in ct:
                        twilio_error_code = response.json().get("code")
                except Exception:
                    pass
                if twilio_error_code == 21656:
                    return {
                        "status": "error",
                        "code": 21656,
                        "message": (
                            "Las variables de la plantilla son inválidas. "
                            "Selecciona la plantilla de nuevo con / y solo reemplaza "
                            "los campos {variable} sin modificar el texto fijo."
                        )
                    }
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


    async def update_conversation_message(
        self,
        conversation_sid: str,
        message_sid: str,
        new_body: str,
        chat_service_sid: Optional[str] = None,
    ) -> dict:
        """Edita un mensaje en Conversations API.

        Endpoint: POST /v1/[Services/{ISxxx}/]Conversations/{CHsid}/Messages/{IMsid}
        Twilio limita la edición a mensajes con < ~15 min de antigüedad.
        """
        sid, token, _ = self._resolve_credentials()
        if not self._available:
            return {"status": "error", "message": "Twilio no configurado"}
        if not conversation_sid or not message_sid:
            return {"status": "error", "message": "conversation_sid y message_sid requeridos"}

        url = _conv_url(conversation_sid, chat_service_sid, suffix=f"/Messages/{message_sid}")
        try:
            client = self._get_http_client()
            resp = await client.post(
                url,
                auth=(sid, token),
                data={"Body": new_body},
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                logger.info(
                    f"[TwilioClient] Mensaje editado conv={conversation_sid} im={message_sid}"
                )
                return {"status": "success", "message_sid": data.get("sid"), "body": data.get("body")}
            logger.error(
                f"[TwilioClient] Error editando mensaje: {resp.status_code} - {resp.text}"
            )
            return {"status": "error", "code": resp.status_code, "message": resp.text}
        except Exception as e:
            logger.error(f"[TwilioClient] Excepción editando mensaje: {e}")
            return {"status": "error", "message": str(e)}

    async def delete_conversation_message(
        self,
        conversation_sid: str,
        message_sid: str,
        chat_service_sid: Optional[str] = None,
    ) -> dict:
        """Elimina un mensaje en Conversations API.

        Endpoint: DELETE /v1/[Services/{ISxxx}/]Conversations/{CHsid}/Messages/{IMsid}
        WhatsApp removerá el mensaje del cliente si está dentro de la ventana
        permitida por Meta (~ poco después del envío).
        """
        sid, token, _ = self._resolve_credentials()
        if not self._available:
            return {"status": "error", "message": "Twilio no configurado"}
        if not conversation_sid or not message_sid:
            return {"status": "error", "message": "conversation_sid y message_sid requeridos"}

        url = _conv_url(conversation_sid, chat_service_sid, suffix=f"/Messages/{message_sid}")
        try:
            client = self._get_http_client()
            resp = await client.delete(url, auth=(sid, token))
            if resp.status_code in (200, 204):
                logger.info(
                    f"[TwilioClient] Mensaje eliminado conv={conversation_sid} im={message_sid}"
                )
                return {"status": "success"}
            logger.error(
                f"[TwilioClient] Error eliminando mensaje: {resp.status_code} - {resp.text}"
            )
            return {"status": "error", "code": resp.status_code, "message": resp.text}
        except Exception as e:
            logger.error(f"[TwilioClient] Excepción eliminando mensaje: {e}")
            return {"status": "error", "message": str(e)}


    async def list_conversation_messages(
        self,
        conversation_sid: str,
        limit: int = 5,
        order: str = "desc",
        chat_service_sid: Optional[str] = None,
    ) -> dict:
        """Lista los últimos mensajes de una conversación.

        Usado para backfill activo del IM SID después de un envío del panel,
        cuando el rebote Source=API del webhook no está disponible.
        """
        sid, token, _ = self._resolve_credentials()
        if not self._available:
            return {"status": "error", "message": "Twilio no configurado"}
        if not conversation_sid:
            return {"status": "error", "message": "conversation_sid requerido"}

        url = _conv_url(conversation_sid, chat_service_sid, suffix="/Messages")
        try:
            client = self._get_http_client()
            resp = await client.get(
                url,
                auth=(sid, token),
                params={"Order": order, "PageSize": str(limit)},
            )
            if resp.status_code == 200:
                data = resp.json()
                return {"status": "success", "messages": data.get("messages", [])}
            logger.error(
                f"[TwilioClient] Error listando mensajes: {resp.status_code} - {resp.text}"
            )
            return {"status": "error", "code": resp.status_code, "message": resp.text}
        except Exception as e:
            logger.error(f"[TwilioClient] Excepción listando mensajes: {e}")
            return {"status": "error", "message": str(e)}


# Instancia global (singleton)
twilio_client = TwilioClient()