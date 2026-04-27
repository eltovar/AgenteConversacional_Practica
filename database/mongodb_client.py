# database/mongodb_client.py
"""
Cliente MongoDB para almacenamiento en tiempo real de mensajes.
"""

import asyncio
import os
import time
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import (
    ConnectionFailure,
    ServerSelectionTimeoutError,
    DuplicateKeyError,
    NetworkTimeout,
    OperationFailure,
)
from bson import ObjectId

from logging_config import logger

# Timezone Colombia
TIMEZONE = ZoneInfo("America/Bogota")


def _iso_bogota(dt_val: Optional[datetime]) -> Optional[str]:
    """
    Serializa un datetime a ISO 8601 con offset explícito de Bogotá (-05:00).

    Motor/PyMongo devuelve datetimes naive (cuando tz_aware=False) que BSON
    guardó en UTC. Sin offset en el ISO, JavaScript los interpreta como hora
    local del navegador → desfase de 5h en el panel. Normalizar aquí garantiza
    que el frontend reciba un instante inequívoco.
    """
    if not dt_val:
        return None
    if dt_val.tzinfo is None:
        dt_val = dt_val.replace(tzinfo=timezone.utc)
    return dt_val.astimezone(TIMEZONE).isoformat()

# Retry config para save_message
_MAX_SAVE_RETRIES = 3
_SAVE_RETRY_DELAY = 0.5  # segundos (crece exponencialmente: 0.5s, 1s)


class MongoDBManager:
    """
    Gestor de MongoDB para mensajes en tiempo real.

    Colecciones:
    - messages: Historial de conversaciones
    - contacts: Información de contactos (cache local)
    """

    def __init__(self):
        # Priorizar URL pública para desarrollo local (similar a Redis)
        is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
        if is_railway:
            # En Railway usar URL interna (más rápida)
            self.mongo_url = os.getenv("MONGO_URL") or os.getenv("MONGODB_URL")
        else:
            # Local: priorizar URL pública (proxy)
            self.mongo_url = (
                os.getenv("MONGO_PUBLIC_URL") or
                os.getenv("MONGODB_PUBLIC_URL") or
                os.getenv("MONGO_URL") or
                os.getenv("MONGODB_URL")
            )

        self.client: Optional[AsyncIOMotorClient] = None
        self.db: Optional[AsyncIOMotorDatabase] = None
        self._connected = False
        self._indexes_created = False
        self._last_ping: float = 0.0        # monotonic ts del último ping exitoso
        self._PING_INTERVAL: float = 30.0   # verificar conectividad cada 30s

    async def connect(self) -> bool:
        """
        Establece conexión con MongoDB.

        Returns:
            True si la conexión fue exitosa
        """
        now = time.monotonic()
        if self._connected and self.client:
            # Verificar conexión viva cada 30s con ping rápido
            if now - self._last_ping < self._PING_INTERVAL:
                return True
            try:
                await asyncio.wait_for(
                    self.client.admin.command('ping'),
                    timeout=2.0
                )
                self._last_ping = now
                return True
            except Exception as ping_err:
                logger.warning(f"[MongoDB] Ping falló ({ping_err}), reconectando...")
                self._connected = False
                self._last_ping = 0.0
                try:
                    self.client.close()
                except Exception:
                    pass
                self.client = None

        if not self.mongo_url:
            logger.warning("[MongoDB] MONGO_URL no configurada - MongoDB deshabilitado")
            return False

        try:
            self.client = AsyncIOMotorClient(
                self.mongo_url,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                socketTimeoutMS=30000,    # timeout por operación — evita queries colgadas
                maxIdleTimeMS=45000,      # cerrar conexiones idle >45s — evita stale sockets
                maxPoolSize=20,           # aumentado de 10 → más capacidad concurrente
                minPoolSize=1,
                w=1,
                journal=True,
            )

            # Verificar conexión
            await self.client.admin.command('ping')

            self.db = self.client.get_database("inmobiliaria_chat")
            self._connected = True
            self._last_ping = time.monotonic()

            # Crear índices si no existen
            await self._ensure_indexes()

            logger.info("[MongoDB] Conexión establecida exitosamente")
            return True

        except (ConnectionFailure, ServerSelectionTimeoutError) as e:
            logger.error(f"[MongoDB] Error de conexión: {e}")
            self._connected = False
            return False
        except Exception as e:
            logger.error(f"[MongoDB] Error inesperado: {e}")
            self._connected = False
            return False

    async def _ensure_indexes(self):
        """Crea índices para optimizar consultas."""
        if self._indexes_created:
            return

        try:
            # Índice compuesto para búsqueda por teléfono + timestamp
            await self.db.messages.create_index(
                [("phone", ASCENDING), ("timestamp", DESCENDING)],
                name="phone_timestamp_idx"
            )

            # Índice para búsqueda por canal
            await self.db.messages.create_index(
                [("phone", ASCENDING), ("channel", ASCENDING)],
                name="phone_channel_idx"
            )

            # Índice para contact_id de HubSpot
            await self.db.messages.create_index(
                "hubspot_contact_id",
                name="hubspot_contact_idx",
                sparse=True
            )

            # Índice TTL para auto-limpieza (mensajes > 90 días)
            await self.db.messages.create_index(
                "timestamp",
                name="timestamp_ttl_idx",
                expireAfterSeconds=90 * 24 * 60 * 60  # 90 días
            )

            # Índice único para deduplicación por MessageSid de Twilio
            # Usa partialFilterExpression en lugar de sparse para ignorar
            # documentos donde message_sid es null o no existe
            await self.db.messages.create_index(
                "message_sid",
                name="message_sid_unique_idx",
                unique=True,
                partialFilterExpression={"message_sid": {"$type": "string"}}
            )

            # Índice de texto para búsqueda fulltext en contenido de mensajes
            await self.db.messages.create_index(
                [("content", "text")],
                name="content_text_idx",
                default_language="spanish"
            )

            # Índices para colección contacts
            await self.db.contacts.create_index(
                "phone",
                name="contact_phone_idx",
                unique=True
            )

            await self.db.contacts.create_index(
                "hubspot_id",
                name="contact_hubspot_idx",
                sparse=True
            )

            # Índices para appointment_workers (equipo de campo)
            await self.db.appointment_workers.create_index(
                "name",
                name="worker_name_idx",
                unique=True
            )

            # Índices para appointments (citas agendadas)
            await self.db.appointments.create_index(
                [("contact_id", ASCENDING), ("status", ASCENDING)],
                name="appointment_contact_status_idx"
            )
            await self.db.appointments.create_index(
                "appointment_dt",
                name="appointment_dt_idx"
            )
            # Índice compuesto para métricas de citas completadas (dashboard mercadeo)
            await self.db.appointments.create_index(
                [("status", ASCENDING), ("visit_completed_at", DESCENDING)],
                name="appointment_completed_metrics_idx"
            )

            # Índice para panel_advisors — evita table scan en lookups por advisor_id
            await self.db.panel_advisors.create_index(
                "advisor_id",
                name="panel_advisor_id_idx",
                unique=True
            )

            # Índices para contact_notes (notas internas de asesores)
            await self.db.contact_notes.create_index(
                [("contact_id", ASCENDING), ("is_deleted", ASCENDING), ("created_at", DESCENDING)],
                name="note_contact_created_idx"
            )

            # Índices para scheduled_messages (mensajes programados)
            await self.db.scheduled_messages.create_index(
                [("status", ASCENDING), ("scheduled_dt", ASCENDING)],
                name="idx_sched_status_dt"
            )
            await self.db.scheduled_messages.create_index(
                [("contact_id", ASCENDING), ("scheduled_dt", ASCENDING)],
                name="idx_sched_contact_dt"
            )

            self._indexes_created = True
            logger.info("[MongoDB] Índices creados/verificados")

        except Exception as e:
            logger.warning(f"[MongoDB] Error creando índices: {e}")

    async def is_connected(self) -> bool:
        """Verifica si la conexión está activa."""
        if not self._connected or not self.client:
            return False
        try:
            await self.client.admin.command('ping')
            return True
        except Exception:
            self._connected = False
            return False

    # =========================================================================
    # OPERACIONES DE MENSAJES
    # =========================================================================

    async def save_message(
        self,
        phone: str,
        content: str,
        sender: str,
        channel: str,
        hubspot_contact_id: Optional[str] = None,
        message_sid: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        media: Optional[Dict[str, Any]] = None,
        reply_to_id: Optional[str] = None,
        reply_to_preview: Optional[Dict[str, Any]] = None,
        conversation_sid: Optional[str] = None,
        conversations_message_sid: Optional[str] = None,
        chat_service_sid: Optional[str] = None,
    ) -> Optional[str]:
        """
        Guarda un mensaje para visualización inmediata en el panel.

        Args:
            phone: Número de teléfono normalizado (+573XXXXXXXXX)
            content: Contenido del mensaje
            sender: Tipo de emisor ('client', 'advisor', 'bot')
            channel: Canal de origen ('whatsapp', 'instagram', etc.)
            hubspot_contact_id: ID del contacto en HubSpot (opcional)
            message_sid: ID del mensaje de Twilio (opcional)
            metadata: Datos adicionales (opcional)
            media: Diccionario con info de media (opcional)
            reply_to_id: ID del mensaje citado (opcional)
            reply_to_preview: Preview desnormalizado del mensaje citado (opcional)

        Returns:
            ID del documento insertado o None si falla
        """
        if not await self.connect():
            logger.warning("[MongoDB] No conectado - mensaje no guardado en MongoDB")
            return None

        # Deduplicación: si ya existe un mensaje con este MessageSid, no duplicar
        if message_sid:
            existing = await self.db.messages.find_one(
                {"message_sid": message_sid},
                {"_id": 1}
            )
            if existing:
                logger.debug(
                    f"[MongoDB] Mensaje duplicado ignorado: message_sid={message_sid}"
                )
                return str(existing["_id"])

        now = datetime.now(TIMEZONE)

        message_doc = {
            "phone": phone,
            "content": content,
            "sender": sender,
            "channel": channel,
            "hubspot_contact_id": hubspot_contact_id,
            "message_sid": message_sid,
            "timestamp": now,
            "timestamp_utc": datetime.utcnow(),
            "metadata": metadata or {},
            "synced_to_hubspot": False,
        }
        if conversation_sid:
            message_doc["conversation_sid"] = conversation_sid
        if conversations_message_sid:
            message_doc["conversations_message_sid"] = conversations_message_sid
        if chat_service_sid:
            message_doc["chat_service_sid"] = chat_service_sid
        if media:
            message_doc["media"] = media
        if reply_to_id:
            message_doc["reply_to_id"] = reply_to_id
            if reply_to_preview:
                message_doc["reply_to_preview"] = reply_to_preview

        last_error = None
        for attempt in range(1, _MAX_SAVE_RETRIES + 1):
            try:
                result = await self.db.messages.insert_one(message_doc)
                if attempt > 1:
                    logger.info(
                        f"[MongoDB] Mensaje guardado en intento {attempt}: id={result.inserted_id}"
                    )
                else:
                    logger.debug(
                        f"[MongoDB] Mensaje guardado: phone={phone}, sender={sender}, "
                        f"media={media}, id={result.inserted_id}"
                    )
                return str(result.inserted_id)

            except DuplicateKeyError:
                # El índice único de message_sid ya tiene este doc — retornar ID existente
                if message_sid:
                    existing = await self.db.messages.find_one(
                        {"message_sid": message_sid}, {"_id": 1}
                    )
                    if existing:
                        return str(existing["_id"])
                return None

            except (NetworkTimeout, OperationFailure) as e:
                last_error = e
                if attempt < _MAX_SAVE_RETRIES:
                    delay = _SAVE_RETRY_DELAY * attempt
                    logger.warning(
                        f"[MongoDB] Error transitorio intento {attempt}/{_MAX_SAVE_RETRIES}: {e}. "
                        f"Reintentando en {delay}s..."
                    )
                    self._connected = False  # forzar reconexión en siguiente intento
                    await asyncio.sleep(delay)

            except Exception as e:
                logger.error(f"[MongoDB] Error no recuperable guardando mensaje: {e}")
                return None

        logger.error(
            f"[MongoDB] Falló guardar mensaje después de {_MAX_SAVE_RETRIES} intentos: {last_error}"
        )
        return None

    async def get_history(
        self,
        phone: str,
        limit: int = 50,
        channel: Optional[str] = None,
        since: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """
        Obtiene historial de mensajes en tiempo real desde MongoDB.

        Args:
            phone: Número de teléfono normalizado
            limit: Máximo de mensajes a retornar
            channel: Filtrar por canal específico (opcional)
            since: Solo mensajes desde esta fecha (opcional)

        Returns:
            Lista de mensajes en orden cronológico (más antiguo primero)
        """
        if not await self.connect():
            logger.warning("[MongoDB] No conectado - retornando lista vacía")
            return []

        try:
            # Construir query
            query = {"phone": phone}

            if channel:
                query["channel"] = channel

            if since:
                query["timestamp"] = {"$gte": since}

            # Ejecutar consulta
            cursor = self.db.messages.find(query).sort("timestamp", DESCENDING).limit(limit)
            messages = await cursor.to_list(length=limit)

            # Formatear para el frontend
            formatted_messages = []
            for msg in reversed(messages):  # Invertir para orden cronológico
                media = msg.get("media", {})
                formatted_messages.append({
                    "id": str(msg.get("_id")),
                    "phone": msg.get("phone"),
                    "content": msg.get("content", ""),
                    "sender": msg.get("sender"),
                    "sender_name": self._get_sender_name(msg.get("sender")),
                    "channel": msg.get("channel"),
                    "timestamp": msg.get("timestamp").isoformat() if msg.get("timestamp") else None,
                    "align": "left" if msg.get("sender") == "client" else "right",
                    "message": msg.get("content", ""),  # Alias para compatibilidad
                    "metadata": msg.get("metadata", {}),
                    "media": {
                        "permanent_url": media.get("permanent_url"),
                        "type": media.get("type"),
                        "transcription": media.get("transcription"),
                        "analysis": media.get("analysis"),
                        "size_bytes": media.get("size_bytes"),
                        "format": media.get("format"),
                        "duration_seconds": media.get("duration_seconds"),
                        "processed_at": media.get("processed_at").isoformat() if isinstance(media.get("processed_at"), datetime) else media.get("processed_at"),
                    } if media else None,
                    "reply_to_id": msg.get("reply_to_id"),
                    "reply_to_preview": msg.get("reply_to_preview"),
                    "deleted": bool(msg.get("deleted", False)),
                    "edited": bool(msg.get("edited", False)),
                })

            logger.debug(f"[MongoDB] Historial obtenido: {len(formatted_messages)} mensajes para {phone}")

            return formatted_messages

        except Exception as e:
            logger.error(f"[MongoDB] Error obteniendo historial: {e}")
            # Reset para forzar reconexión en la siguiente llamada
            self._connected = False
            self._last_ping = 0.0
            return []

    async def get_history_by_contact_id(
        self,
        hubspot_contact_id: str,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Obtiene historial por ID de contacto de HubSpot.

        Útil cuando el frontend solo tiene el contact_id.
        """
        if not await self.connect():
            return []

        try:
            cursor = self.db.messages.find(
                {"hubspot_contact_id": hubspot_contact_id}
            ).sort("timestamp", DESCENDING).limit(limit)

            messages = await cursor.to_list(length=limit)

            formatted_messages = []
            for msg in reversed(messages):
                formatted_messages.append({
                    "id": str(msg.get("_id")),
                    "phone": msg.get("phone"),
                    "content": msg.get("content", ""),
                    "sender": msg.get("sender"),
                    "sender_name": self._get_sender_name(msg.get("sender")),
                    "channel": msg.get("channel"),
                    "timestamp": msg.get("timestamp").isoformat() if msg.get("timestamp") else None,
                    "align": "left" if msg.get("sender") == "client" else "right",
                    "message": msg.get("content", ""),
                    "media": {
                        # P2-A fix: leer subdocumento anidado (formato actual de save_message)
                        # Fallback a campos flat legacy para documentos MongoDB anteriores
                        "permanent_url": (msg.get("media") or {}).get("permanent_url") or msg.get("media_url"),
                        "type": (msg.get("media") or {}).get("type") or msg.get("media_type"),
                        "transcription": (msg.get("media") or {}).get("transcription"),
                        "analysis": (msg.get("media") or {}).get("analysis"),
                        "size_bytes": (msg.get("media") or {}).get("size_bytes"),
                        "doc_icon": (msg.get("media") or {}).get("doc_icon"),
                        "original_filename": (msg.get("media") or {}).get("original_filename"),
                    },
                    "reply_to_id": msg.get("reply_to_id"),
                    "reply_to_preview": msg.get("reply_to_preview"),
                    "deleted": bool(msg.get("deleted", False)),
                    "edited": bool(msg.get("edited", False)),
                })

            return formatted_messages

        except Exception as e:
            logger.error(f"[MongoDB] Error obteniendo historial por contact_id: {e}")
            self._connected = False
            self._last_ping = 0.0
            return []

    async def get_message_by_sid(self, message_sid: str) -> Optional[Dict[str, Any]]:
        """Busca un mensaje por su Twilio message_sid. Usado para reply context.

        Why: outbound del asesor guarda SMxxxx (Programmable Messaging response),
        pero Conversations API referencia el mismo mensaje como IMxxxx. Buscamos en
        ambos campos para que las citaciones del cliente resuelvan correctamente.
        """
        if not message_sid or not await self.connect():
            return None
        try:
            msg = await self.db.messages.find_one(
                {"$or": [
                    {"message_sid": message_sid},
                    {"conversations_message_sid": message_sid},
                ]},
                {"_id": 1, "content": 1, "sender": 1, "media": 1, "timestamp": 1}
            )
            if msg:
                return {
                    "id": str(msg["_id"]),
                    "content": (msg.get("content") or "")[:200],
                    "sender": msg.get("sender"),
                    "sender_name": self._get_sender_name(msg.get("sender")),
                    "media_type": (msg.get("media", {}) or {}).get("type"),
                    "timestamp": msg.get("timestamp").isoformat() if msg.get("timestamp") else None
                }
            return None
        except Exception as e:
            logger.warning(f"[MongoDB] Error buscando mensaje por SID {message_sid}: {e}")
            return None

    async def search_messages_fulltext(
        self,
        query_text: str,
        limit: int = 100
    ) -> List[str]:
        """
        Busca mensajes por texto y retorna los teléfonos únicos que coinciden.

        Usa índice de texto de MongoDB para búsqueda eficiente.

        Args:
            query_text: Texto a buscar en los mensajes
            limit: Máximo de resultados

        Returns:
            Lista de números de teléfono únicos con mensajes que coinciden
        """
        if not await self.connect():
            logger.warning("[MongoDB] No conectado - búsqueda vacía")
            return []

        if not query_text or len(query_text) < 2:
            return []

        try:
            # Búsqueda fulltext con índice de texto
            cursor = self.db.messages.find(
                {"$text": {"$search": query_text}},
                {"phone": 1, "score": {"$meta": "textScore"}}
            ).sort([("score", {"$meta": "textScore"})]).limit(limit)

            messages = await cursor.to_list(length=limit)

            # Extraer teléfonos únicos preservando orden de relevancia
            seen = set()
            unique_phones = []
            for msg in messages:
                phone = msg.get("phone")
                if phone and phone not in seen:
                    seen.add(phone)
                    unique_phones.append(phone)

            logger.info(f"[MongoDB] Búsqueda fulltext '{query_text}': {len(unique_phones)} contactos encontrados")
            return unique_phones

        except Exception as e:
            logger.error(f"[MongoDB] Error en búsqueda fulltext: {e}")
            return []

    async def mark_as_synced_to_hubspot(self, message_id: str) -> bool:
        """
        Marca un mensaje como sincronizado con HubSpot.

        Se llama después de que el BackgroundTask crea la nota en HubSpot.
        """
        if not await self.connect():
            return False

        try:
            result = await self.db.messages.update_one(
                {"_id": ObjectId(message_id)},
                {"$set": {"synced_to_hubspot": True, "synced_at": datetime.utcnow()}}
            )

            return result.modified_count > 0

        except Exception as e:
            logger.error(f"[MongoDB] Error marcando mensaje como sincronizado: {e}")
            return False

    async def get_unsynced_messages(self, limit: int = 100) -> List[Dict]:
        """
        Obtiene mensajes pendientes de sincronizar con HubSpot.

        Útil para un job de reconciliación periódico.
        """
        if not await self.connect():
            return []

        try:
            cursor = self.db.messages.find(
                {"synced_to_hubspot": False}
            ).sort("timestamp", ASCENDING).limit(limit)

            return await cursor.to_list(length=limit)

        except Exception as e:
            logger.error(f"[MongoDB] Error obteniendo mensajes no sincronizados: {e}")
            return []

    async def update_delivery_status(
        self,
        message_sid: str,
        status: str,
        delivered_at: Optional[datetime] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None
    ) -> bool:
        """
        Actualiza el estado de entrega de un mensaje basado en el callback de Twilio.
        
        Este método permite reconciliar lo que muestra el panel con lo que
        realmente recibió el cliente en WhatsApp.
        
        Args:
            message_sid: ID del mensaje de Twilio (SMxxxxxxx)
            status: Estado actual ('delivered', 'read', 'failed', 'undelivered')
            delivered_at: Timestamp de entrega (para status=delivered/read)
            error_code: Código de error de Twilio (para status=failed/undelivered)
            error_message: Mensaje de error de Twilio
            
        Returns:
            True si se actualizó el mensaje, False si no se encontró o hubo error
        """
        if not await self.connect():
            return False
        
        if not message_sid:
            logger.warning("[MongoDB] update_delivery_status llamado sin message_sid")
            return False
        
        try:
            update_data = {
                "delivery_status": status,
                "delivery_updated_at": datetime.utcnow()
            }
            
            if delivered_at:
                update_data["delivered_at"] = delivered_at
                
            if error_code:
                update_data["delivery_error_code"] = error_code
                
            if error_message:
                update_data["delivery_error_message"] = error_message
            
            result = await self.db.messages.update_one(
                {"message_sid": message_sid},
                {"$set": update_data}
            )
            
            if result.matched_count > 0:
                logger.debug(f"[MongoDB] Delivery status actualizado: {message_sid} -> {status}")
                return True
            else:
                # No es error crítico - el mensaje puede ser muy antiguo o de otro origen
                logger.debug(f"[MongoDB] Mensaje no encontrado para delivery update: {message_sid}")
                return False
                
        except Exception as e:
            logger.error(f"[MongoDB] Error actualizando delivery status: {e}")
            return False

    def _build_msg_filter(self, identifier: str) -> Dict[str, Any]:
        """Filtro que matchea por _id, message_sid (SMxxx) o conversations_message_sid (IMxxx)."""
        ors: List[Dict[str, Any]] = [
            {"message_sid": identifier},
            {"conversations_message_sid": identifier},
        ]
        try:
            ors.append({"_id": ObjectId(identifier)})
        except Exception:
            pass
        return {"$or": ors}

    async def update_message_content(self, identifier: str, new_content: str) -> Optional[Dict]:
        """
        Actualiza el contenido de un mensaje (editado por cliente vía WhatsApp o
        por asesor vía panel). `identifier` puede ser mongo _id, SMxxx o IMxxx.
        Retorna el documento actualizado (con phone e _id) para el broadcast WS.
        """
        if not await self.connect():
            return None
        try:
            result = await self.db.messages.find_one_and_update(
                self._build_msg_filter(identifier),
                {"$set": {"content": new_content, "edited": True, "edited_at": datetime.utcnow()}},
                return_document=True,
                projection={"_id": 1, "phone": 1, "content": 1, "sender": 1, "timestamp": 1, "conversation_sid": 1, "conversations_message_sid": 1, "message_sid": 1}
            )
            if result:
                logger.info(f"[MongoDB] Mensaje editado: id={identifier}")
            else:
                logger.warning(f"[MongoDB] Mensaje no encontrado para edición: {identifier}")
            return result
        except Exception as e:
            logger.error(f"[MongoDB] Error actualizando contenido de mensaje: {e}")
            return None

    async def soft_delete_message(self, identifier: str) -> Optional[Dict]:
        """
        Soft-delete de mensaje. `identifier` acepta mongo _id, SMxxx o IMxxx.
        """
        if not await self.connect():
            return None
        try:
            result = await self.db.messages.find_one_and_update(
                self._build_msg_filter(identifier),
                {"$set": {"deleted": True, "deleted_at": datetime.utcnow()}},
                return_document=True,
                projection={"_id": 1, "phone": 1, "sender": 1, "timestamp": 1, "conversation_sid": 1, "conversations_message_sid": 1, "message_sid": 1}
            )
            if result:
                logger.info(f"[MongoDB] Mensaje eliminado (soft): id={identifier}")
            else:
                logger.warning(f"[MongoDB] Mensaje no encontrado para soft-delete: {identifier}")
            return result
        except Exception as e:
            logger.error(f"[MongoDB] Error en soft-delete de mensaje: {e}")
            return None

    async def update_message_fields(self, mongo_id: str, fields: Dict[str, Any]) -> bool:
        """Update arbitrario de campos por _id. Retorna True si se modificó/encontró."""
        if not await self.connect():
            return False
        try:
            res = await self.db.messages.update_one(
                {"_id": ObjectId(mongo_id)},
                {"$set": fields},
            )
            return res.matched_count > 0
        except Exception as e:
            logger.warning(f"[MongoDB] update_message_fields {mongo_id} fallo: {e}")
            return False

    async def get_message_by_id(self, mongo_id: str) -> Optional[Dict[str, Any]]:
        """Recupera un doc completo por _id (para validaciones de auth/edad/etc)."""
        if not await self.connect():
            return None
        try:
            return await self.db.messages.find_one({"_id": ObjectId(mongo_id)})
        except Exception as e:
            logger.warning(f"[MongoDB] get_message_by_id fallo {mongo_id}: {e}")
            return None

    async def backfill_conversations_sid(
        self,
        conversation_sid: str,
        body: str,
        im_sid: str,
        max_age_seconds: int = 300,
    ) -> Optional[str]:
        """
        Backfill: cuando llega onMessageAdded(Source=API) con MessageSid=IMxxx,
        encontramos el doc del asesor recién enviado (matcheado por
        conversation_sid + body + sender=advisor en últimos 5min) y le
        agregamos el IMxxx.

        Returns: mongo _id (str) si se hizo backfill; None si no hubo match.
        """
        if not await self.connect() or not conversation_sid or not im_sid:
            return None
        try:
            cutoff = datetime.utcnow() - timedelta(seconds=max_age_seconds)
            result = await self.db.messages.find_one_and_update(
                {
                    "conversation_sid": conversation_sid,
                    "sender": "advisor",
                    "content": body or "",
                    "timestamp_utc": {"$gte": cutoff},
                    "conversations_message_sid": {"$in": [None, ""]},
                },
                {"$set": {"conversations_message_sid": im_sid}},
                sort=[("timestamp_utc", -1)],
                return_document=True,
                projection={"_id": 1},
            )
            if result:
                _id = str(result["_id"])
                logger.info(f"[MongoDB] Backfill IM SID OK: conv={conversation_sid} im={im_sid} -> {_id}")
                return _id
            logger.debug(f"[MongoDB] Backfill IM SID sin match: conv={conversation_sid} body={body[:40]!r}")
            return None
        except Exception as e:
            logger.warning(f"[MongoDB] Error en backfill_conversations_sid: {e}")
            return None

    # =========================================================================
    # OPERACIONES DE CONTACTOS
    # =========================================================================

    async def upsert_contact(
        self,
        phone: str,
        hubspot_id: Optional[str] = None,
        name: Optional[str] = None,
        channel: Optional[str] = None,
        metadata: Optional[Dict] = None
    ) -> bool:
        """
        Crea o actualiza un contacto en MongoDB.

        Útil para cache local de información de contactos.
        """
        if not await self.connect():
            return False

        try:
            update_data = {
                "phone": phone,
                "updated_at": datetime.utcnow()
            }

            if hubspot_id:
                update_data["hubspot_id"] = hubspot_id
            if name:
                update_data["name"] = name
            if channel:
                update_data["last_channel"] = channel
            if metadata:
                update_data["metadata"] = metadata

            await self.db.contacts.update_one(
                {"phone": phone},
                {
                    "$set": update_data,
                    "$setOnInsert": {"created_at": datetime.utcnow()}
                },
                upsert=True
            )

            return True

        except Exception as e:
            logger.error(f"[MongoDB] Error actualizando contacto: {e}")
            return False

    async def get_contact(self, phone: str) -> Optional[Dict]:
        """Obtiene información de un contacto."""
        if not await self.connect():
            return None

        try:
            return await self.db.contacts.find_one({"phone": phone})
        except Exception as e:
            logger.error(f"[MongoDB] Error obteniendo contacto: {e}")
            return None

    async def get_contact_by_hubspot_id(self, hubspot_id: str) -> Optional[Dict]:
        """Obtiene contacto por ID de HubSpot."""
        if not await self.connect():
            return None

        try:
            return await self.db.contacts.find_one({"hubspot_id": hubspot_id})
        except Exception as e:
            logger.error(f"[MongoDB] Error obteniendo contacto por hubspot_id: {e}")
            return None

    # =========================================================================
    # UTILIDADES
    # =========================================================================

    def _get_sender_name(self, sender: str) -> str:
        """Convierte tipo de sender a nombre legible."""
        sender_names = {
            "client": "Cliente",
            "advisor": "Asesor",
            "bot": "Sofía",
            "system": "Sistema"
        }
        return sender_names.get(sender, "Desconocido")

    async def get_message_count(self, phone: str) -> int:
        """Cuenta mensajes para un teléfono."""
        if not await self.connect():
            return 0

        try:
            return await self.db.messages.count_documents({"phone": phone})
        except Exception:
            return 0

    async def get_recent_conversations(
        self,
        hours: int = 24,
        limit: int = 50
    ) -> List[Dict]:
        """
        Obtiene conversaciones recientes (para el panel).

        Agrupa por teléfono y retorna el último mensaje de cada uno.
        """
        if not await self.connect():
            return []

        try:
            since = datetime.utcnow() - timedelta(hours=hours)

            pipeline = [
                {"$match": {"timestamp_utc": {"$gte": since}}},
                {"$sort": {"timestamp": -1}},
                {"$group": {
                    "_id": "$phone",
                    "last_message": {"$first": "$content"},
                    "last_sender": {"$first": "$sender"},
                    "last_timestamp": {"$first": "$timestamp"},
                    "channel": {"$first": "$channel"},
                    "hubspot_contact_id": {"$first": "$hubspot_contact_id"},
                    "message_count": {"$sum": 1}
                }},
                {"$sort": {"last_timestamp": -1}},
                {"$limit": limit}
            ]

            cursor = self.db.messages.aggregate(pipeline)
            return await cursor.to_list(length=limit)

        except Exception as e:
            logger.error(f"[MongoDB] Error obteniendo conversaciones recientes: {e}")
            return []

    # =========================================================================
    # OPERACIONES DE ADVISORS (ASESORES DEL PANEL)
    # =========================================================================

    # DEFAULT_ADVISORS — derivado automáticamente de lead_assigner.OWNERS_CONFIG.
    # NO editar aquí. Para cambiar nombres de asesoras, editar:
    #   integrations/hubspot/lead_assigner.py → OWNERS_CONFIG  (busca: ✏️ EDITAR AQUÍ)
    try:
        from integrations.hubspot.lead_assigner import LeadAssigner as _LA
        _seen_ids: set = set()
        DEFAULT_ADVISORS: dict = {}
        for _team_members in _LA.OWNERS_CONFIG.values():
            for _m in _team_members:
                if _m["id"] not in _seen_ids:
                    DEFAULT_ADVISORS[_m["id"]] = _m["name"]
                    _seen_ids.add(_m["id"])
        del _seen_ids, _team_members, _m, _LA
    except Exception:
        # Fallback mínimo si lead_assigner no está disponible
        DEFAULT_ADVISORS = {}

    async def init_advisors(self) -> bool:
        """Inicializa los asesores predeterminados si no existen."""
        if not await self.connect():
            return False
        try:
            for advisor_id, name in self.DEFAULT_ADVISORS.items():
                # $set para name → sincroniza siempre el nombre desde lead_assigner.OWNERS_CONFIG
                # $setOnInsert para campos de auditoría → solo al crear por primera vez
                await self.db.panel_advisors.update_one(
                    {"advisor_id": advisor_id},
                    {
                        "$set": {"name": name},
                        "$setOnInsert": {
                            "advisor_id": advisor_id,
                            "active": True,
                            "created_at": datetime.now(TIMEZONE)
                        }
                    },
                    upsert=True
                )
            logger.info(f"[MongoDB] Advisors inicializados: {len(self.DEFAULT_ADVISORS)}")
            return True
        except Exception as e:
            logger.error(f"[MongoDB] Error inicializando advisors: {e}")
            return False

    async def get_advisors(self) -> List[Dict[str, Any]]:
        """Lista todos los asesores activos."""
        if not await self.connect():
            return []
        try:
            # Asegurar que existan los advisors predeterminados
            await self.init_advisors()
            
            cursor = self.db.panel_advisors.find(
                {"active": True}
            ).sort("name", ASCENDING)
            advisors = await cursor.to_list(length=50)
            return [
                {"id": a["advisor_id"], "name": a["name"]}
                for a in advisors
            ]
        except Exception as e:
            logger.error(f"[MongoDB] Error listando advisors: {e}")
            return []

    async def get_advisor_name(self, advisor_id: str) -> str:
        """Obtiene el nombre de un asesor por su ID."""
        if not await self.connect():
            return self.DEFAULT_ADVISORS.get(advisor_id, f"Asesor {advisor_id}")
        try:
            advisor = await self.db.panel_advisors.find_one({"advisor_id": advisor_id})
            if advisor:
                return advisor["name"]
            return self.DEFAULT_ADVISORS.get(advisor_id, f"Asesor {advisor_id}")
        except Exception as e:
            logger.error(f"[MongoDB] Error obteniendo advisor {advisor_id}: {e}")
            return self.DEFAULT_ADVISORS.get(advisor_id, f"Asesor {advisor_id}")

    async def update_advisor(self, advisor_id: str, name: str) -> bool:
        """Actualiza el nombre de un asesor."""
        if not await self.connect():
            return False
        try:
            result = await self.db.panel_advisors.update_one(
                {"advisor_id": advisor_id},
                {"$set": {"name": name.strip(), "updated_at": datetime.now(TIMEZONE)}}
            )
            if result.modified_count > 0:
                logger.info(f"[MongoDB] Advisor {advisor_id} actualizado a: {name}")
                return True
            return False
        except Exception as e:
            logger.error(f"[MongoDB] Error actualizando advisor {advisor_id}: {e}")
            return False

    # =========================================================================
    # OPERACIONES DE WORKERS (EQUIPO DE CAMPO)
    # =========================================================================

    async def get_workers(self) -> List[Dict[str, Any]]:
        """Lista todos los workers activos."""
        if not await self.connect():
            return []
        try:
            cursor = self.db.appointment_workers.find(
                {"active": True}
            ).sort("name", ASCENDING)
            workers = await cursor.to_list(length=200)
            return [
                {"id": str(w["_id"]), "name": w["name"]}
                for w in workers
            ]
        except Exception as e:
            logger.error(f"[MongoDB] Error listando workers: {e}")
            return []

    async def create_worker(self, name: str) -> Optional[str]:
        """Crea un nuevo worker. Retorna el ID o None si ya existe."""
        if not await self.connect():
            return None
        try:
            result = await self.db.appointment_workers.insert_one({
                "name": name.strip(),
                "active": True,
                "created_at": datetime.now(TIMEZONE)
            })
            return str(result.inserted_id)
        except Exception as e:
            # Duplicate key → ya existe
            logger.warning(f"[MongoDB] Worker duplicado o error: {e}")
            return None

    async def update_worker(self, worker_id: str, name: str) -> bool:
        """Actualiza el nombre de un worker."""
        if not await self.connect():
            return False
        try:
            result = await self.db.appointment_workers.update_one(
                {"_id": ObjectId(worker_id)},
                {"$set": {"name": name.strip(), "updated_at": datetime.now(TIMEZONE)}}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"[MongoDB] Error actualizando worker {worker_id}: {e}")
            return False

    async def delete_worker(self, worker_id: str) -> bool:
        """Soft-delete de un worker (active=False). Preserva historial de citas."""
        if not await self.connect():
            return False
        try:
            result = await self.db.appointment_workers.update_one(
                {"_id": ObjectId(worker_id)},
                {"$set": {"active": False, "deleted_at": datetime.now(TIMEZONE)}}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"[MongoDB] Error eliminando worker {worker_id}: {e}")
            return False

    # =========================================================================
    # OPERACIONES DE CITAS (APPOINTMENTS)
    # =========================================================================

    async def create_appointment(
        self,
        contact_id: str,
        phone: str,
        advisor_id: str,
        worker_id: str,
        worker_name: str,
        appointment_dt: datetime,
        notes: str,
        hubspot_note_id: Optional[str] = None,
        contact_name: Optional[str] = None,
        canal: Optional[str] = None,
    ) -> Optional[str]:
        """Crea una cita y la persiste en MongoDB."""
        if not await self.connect():
            return None
        try:
            result = await self.db.appointments.insert_one({
                "contact_id": contact_id,
                "phone": phone,
                "advisor_id": advisor_id,
                "worker_id": worker_id,
                "worker_name": worker_name,
                "appointment_dt": appointment_dt,
                "notes": notes,
                "hubspot_note_id": hubspot_note_id,
                "contact_name": contact_name,
                "canal": canal or "whatsapp",
                "status": "scheduled",
                "created_at": datetime.now(TIMEZONE),
            })
            return str(result.inserted_id)
        except Exception as e:
            logger.error(f"[MongoDB] Error creando cita: {e}")
            return None

    async def mark_visit_completed(
        self, contact_id: str, phone: Optional[str] = None
    ) -> bool:
        """
        Marca la cita 'scheduled' más reciente del contacto como 'completed'.
        Idempotente: si no hay cita scheduled, no hace nada.
        """
        if not await self.connect():
            return False
        try:
            now = datetime.now(TIMEZONE)
            result = await self.db.appointments.find_one_and_update(
                {"contact_id": contact_id, "status": "scheduled"},
                {"$set": {
                    "status": "completed",
                    "visit_completed_at": now,
                    "status_updated_at": now,
                }},
                sort=[("appointment_dt", DESCENDING)],
            )
            if result:
                logger.info(
                    f"[MongoDB] Cita marcada como completada: contact={contact_id}, "
                    f"appointment_id={result.get('_id')}"
                )
                return True
            logger.debug(
                f"[MongoDB] No hay cita 'scheduled' para completar: contact={contact_id}"
            )
            return False
        except Exception as e:
            logger.error(f"[MongoDB] Error marcando cita completada: {e}")
            return False

    async def get_completed_appointments(
        self,
        date_from: datetime,
        date_to: datetime,
        worker_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Retorna citas completadas (status='completed') en el rango
        [date_from, date_to] por visit_completed_at.
        Filtra por worker_id (trabajador de campo que realizó la visita).
        """
        if not await self.connect():
            return []
        try:
            query: Dict[str, Any] = {
                "status": "completed",
                "visit_completed_at": {"$gte": date_from, "$lte": date_to},
            }
            if worker_id:
                query["worker_id"] = worker_id
            cursor = self.db.appointments.find(query).sort(
                "visit_completed_at", DESCENDING
            )
            return await cursor.to_list(length=None)
        except Exception as e:
            logger.error(f"[MongoDB] Error consultando citas completadas: {e}")
            return []

    async def count_completed_appointments(
        self,
        date_from: datetime,
        date_to: datetime,
        worker_id: Optional[str] = None,
    ) -> int:
        """Conteo de citas completadas en rango — útil para deltas."""
        if not await self.connect():
            return 0
        try:
            query: Dict[str, Any] = {
                "status": "completed",
                "visit_completed_at": {"$gte": date_from, "$lte": date_to},
            }
            if worker_id:
                query["worker_id"] = worker_id
            return await self.db.appointments.count_documents(query)
        except Exception as e:
            logger.error(f"[MongoDB] Error contando citas completadas: {e}")
            return 0

    async def get_appointments(self, contact_id: str) -> List[Dict[str, Any]]:
        """Obtiene todas las citas de un contacto, ordenadas por fecha."""
        if not await self.connect():
            return []
        try:
            cursor = self.db.appointments.find(
                {"contact_id": contact_id}
            ).sort("appointment_dt", ASCENDING)
            appts = await cursor.to_list(length=50)
            return [
                {
                    "id": str(a["_id"]),
                    "worker_name": a.get("worker_name", ""),
                    "appointment_dt": _iso_bogota(a.get("appointment_dt")),
                    "notes": a.get("notes", ""),
                    "status": a.get("status", "scheduled"),
                    "created_at": _iso_bogota(a.get("created_at")),
                }
                for a in appts
            ]
        except Exception as e:
            logger.error(f"[MongoDB] Error obteniendo citas de {contact_id}: {e}")
            return []

    async def get_contacts_with_appointments(self, contact_ids: List[str]) -> set:
        """
        Retorna el set de contact_ids que tienen al menos una cita futura activa.
        Usa UNA sola query para todos los contactos (eficiencia O(1) por contacto).
        """
        if not await self.connect() or not contact_ids:
            return set()
        try:
            now = datetime.now(TIMEZONE)
            cursor = self.db.appointments.find(
                {
                    "contact_id": {"$in": contact_ids},
                    "status": "scheduled",
                    "appointment_dt": {"$gte": now}
                },
                {"contact_id": 1}
            )
            docs = await cursor.to_list(length=len(contact_ids) * 10)
            return {d["contact_id"] for d in docs}
        except Exception as e:
            logger.error(f"[MongoDB] Error buscando citas activas: {e}")
            return set()

    async def get_contacts_by_worker(
        self,
        worker_id: str,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        Retorna la lista de contactos que tienen una cita activa con un worker específico.
        Usado por el filtro de worker en el panel.

        Si se proveen date_from/date_to, filtra citas dentro del rango.
        Si no, usa ventana por defecto: desde 4 horas antes de ahora en adelante.

        Returns:
            Lista de dicts con {contact_id, phone, appointment_dt (ISO str), worker_name}
        """
        if not await self.connect() or not worker_id:
            return []
        try:
            now = datetime.now(TIMEZONE)
            if date_from or date_to:
                dt_filter: Dict[str, Any] = {}
                if date_from:
                    dt_filter["$gte"] = date_from
                if date_to:
                    dt_filter["$lte"] = date_to
            else:
                # Ventana por defecto: desde 4h atrás para cubrir delay del scheduler
                dt_filter = {"$gte": now - timedelta(hours=4)}
            cursor = self.db.appointments.find(
                {
                    "worker_id": worker_id,
                    "status": "scheduled",
                    "appointment_dt": dt_filter,
                },
                {"contact_id": 1, "phone": 1, "appointment_dt": 1, "worker_name": 1}
            ).sort("appointment_dt", 1)  # ASC: más próxima primero
            docs = await cursor.to_list(length=200)
            result = []
            for d in docs:
                result.append({
                    "contact_id": d.get("contact_id", ""),
                    "phone": d.get("phone", ""),
                    "appointment_dt": _iso_bogota(d.get("appointment_dt")),
                    "worker_name": d.get("worker_name", ""),
                })
            return result
        except Exception as e:
            logger.error(f"[MongoDB] Error buscando contactos por worker {worker_id}: {e}")
            return []

    async def cancel_appointment(self, appointment_id: str) -> bool:
        """Cancela una cita (status → cancelled)."""
        if not await self.connect():
            return False
        try:
            result = await self.db.appointments.update_one(
                {"_id": ObjectId(appointment_id)},
                {"$set": {"status": "cancelled", "cancelled_at": datetime.now(TIMEZONE)}}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"[MongoDB] Error cancelando cita {appointment_id}: {e}")
            return False

    async def update_appointment(
        self,
        appointment_id: str,
        worker_id: str = None,
        worker_name: str = None,
        appointment_dt: datetime = None,
        notes: str = None
    ) -> bool:
        """Actualiza una cita existente."""
        if not await self.connect():
            return False
        try:
            update_fields = {"updated_at": datetime.now(TIMEZONE)}
            if worker_id is not None:
                update_fields["worker_id"] = worker_id
            if worker_name is not None:
                update_fields["worker_name"] = worker_name
            if appointment_dt is not None:
                update_fields["appointment_dt"] = appointment_dt
            if notes is not None:
                update_fields["notes"] = notes

            result = await self.db.appointments.update_one(
                {"_id": ObjectId(appointment_id)},
                {"$set": update_fields}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"[MongoDB] Error actualizando cita {appointment_id}: {e}")
            return False

    async def delete_appointment(self, appointment_id: str) -> bool:
        """Elimina una cita permanentemente."""
        if not await self.connect():
            return False
        try:
            result = await self.db.appointments.delete_one(
                {"_id": ObjectId(appointment_id)}
            )
            return result.deleted_count > 0
        except Exception as e:
            logger.error(f"[MongoDB] Error eliminando cita {appointment_id}: {e}")
            return False

    async def get_appointment_by_id(self, appointment_id: str) -> Optional[Dict[str, Any]]:
        """Retorna un documento de cita por su _id."""
        if not await self.connect():
            return None
        try:
            doc = await self.db.appointments.find_one({"_id": ObjectId(appointment_id)})
            if doc:
                doc["_id"] = str(doc["_id"])
            return doc
        except Exception as e:
            logger.error(f"[MongoDB] Error obteniendo cita {appointment_id}: {e}")
            return None

    # ------------------------------------------------------------------ #
    #  Contact Notes CRUD                                                  #
    # ------------------------------------------------------------------ #

    async def create_note(self, contact_id: str, content: str,
                          advisor_id: str, advisor_name: str,
                          phone: str = "") -> Optional[str]:
        """Crea una nota interna para un contacto. Retorna el _id como str."""
        if not await self.connect():
            return None
        try:
            now = datetime.now(TIMEZONE)
            doc = {
                "contact_id": contact_id,
                "phone": phone,
                "advisor_id": advisor_id,
                "advisor_name": advisor_name,
                "content": content.strip(),
                "created_at": now,
                "updated_at": now,
                "is_deleted": False,
            }
            result = await self.db.contact_notes.insert_one(doc)
            return str(result.inserted_id)
        except Exception as e:
            logger.error(f"[MongoDB] Error creando nota para {contact_id}: {e}")
            return None

    async def get_notes(self, contact_id: str) -> list:
        """Retorna notas activas de un contacto, ordenadas más recientes primero."""
        if not await self.connect():
            return []
        try:
            cursor = self.db.contact_notes.find(
                {"contact_id": contact_id, "is_deleted": False},
                sort=[("created_at", DESCENDING)]
            )
            notes = []
            async for doc in cursor:
                doc["_id"] = str(doc["_id"])
                doc["created_at"] = doc["created_at"].isoformat() if hasattr(doc.get("created_at"), "isoformat") else str(doc.get("created_at", ""))
                doc["updated_at"] = doc["updated_at"].isoformat() if hasattr(doc.get("updated_at"), "isoformat") else str(doc.get("updated_at", ""))
                notes.append(doc)
            return notes
        except Exception as e:
            logger.error(f"[MongoDB] Error obteniendo notas para {contact_id}: {e}")
            return []

    async def update_note(self, note_id: str, content: str) -> bool:
        """Actualiza el contenido de una nota. Retorna True si se modificó."""
        if not await self.connect():
            return False
        try:
            result = await self.db.contact_notes.update_one(
                {"_id": ObjectId(note_id), "is_deleted": False},
                {"$set": {"content": content.strip(), "updated_at": datetime.now(TIMEZONE)}}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"[MongoDB] Error actualizando nota {note_id}: {e}")
            return False

    async def soft_delete_note(self, note_id: str) -> bool:
        """Marca una nota como eliminada (soft delete). Retorna True si se modificó."""
        if not await self.connect():
            return False
        try:
            result = await self.db.contact_notes.update_one(
                {"_id": ObjectId(note_id), "is_deleted": False},
                {"$set": {"is_deleted": True, "updated_at": datetime.now(TIMEZONE)}}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"[MongoDB] Error eliminando nota {note_id}: {e}")
            return False

    # =========================================================================
    # SCHEDULED MESSAGES — Mensajes WhatsApp programados por asesoras
    # =========================================================================

    async def create_scheduled_message(
        self,
        contact_id: str,
        phone: str,
        canal: str,
        template_sid: str,
        template_name: str,
        template_variables: dict,
        scheduled_dt: datetime,
        advisor_id: str = "",
        notes: str = "",
    ) -> Optional[str]:
        """Persiste un mensaje programado. Retorna el _id como string."""
        if not await self.connect():
            return None
        try:
            doc = {
                "contact_id": contact_id,
                "phone": phone,
                "canal": canal,
                "template_sid": template_sid,
                "template_name": template_name,
                "template_variables": template_variables,
                "scheduled_dt": scheduled_dt,
                "status": "pending",
                "advisor_id": advisor_id,
                "notes": notes,
                "created_at": datetime.now(TIMEZONE),
                "sent_at": None,
                "error": None,
            }
            result = await self.db.scheduled_messages.insert_one(doc)
            return str(result.inserted_id)
        except Exception as e:
            logger.error(f"[MongoDB] Error creando scheduled_message: {e}")
            return None

    async def get_scheduled_messages(self, contact_id: str) -> List[Dict[str, Any]]:
        """Lista todos los mensajes programados de un contacto, ASC por fecha."""
        if not await self.connect():
            return []
        try:
            cursor = self.db.scheduled_messages.find(
                {"contact_id": contact_id}
            ).sort("scheduled_dt", ASCENDING)
            docs = await cursor.to_list(length=50)
            return [
                {
                    "id": str(d["_id"]),
                    "template_name": d.get("template_name", ""),
                    "template_sid": d.get("template_sid", ""),
                    "template_variables": d.get("template_variables", {}),
                    "scheduled_dt": _iso_bogota(d.get("scheduled_dt")),
                    "status": d.get("status", "pending"),
                    "notes": d.get("notes", ""),
                    "created_at": _iso_bogota(d.get("created_at")),
                    "sent_at": _iso_bogota(d.get("sent_at")),
                    "error": d.get("error"),
                }
                for d in docs
            ]
        except Exception as e:
            logger.error(f"[MongoDB] Error obteniendo scheduled_messages de {contact_id}: {e}")
            return []

    async def cancel_scheduled_message(self, message_id: str) -> bool:
        """Cancela un mensaje programado (status → cancelled)."""
        if not await self.connect():
            return False
        try:
            result = await self.db.scheduled_messages.update_one(
                {"_id": ObjectId(message_id), "status": "pending"},
                {"$set": {"status": "cancelled"}}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"[MongoDB] Error cancelando scheduled_message {message_id}: {e}")
            return False

    async def get_pending_messages_due(self, now_utc: datetime) -> List[Dict[str, Any]]:
        """
        Retorna mensajes pending cuya scheduled_dt ya pasó (para el scheduler).
        now_utc debe ser tz-aware UTC.
        """
        if not await self.connect():
            return []
        try:
            cursor = self.db.scheduled_messages.find(
                {"status": "pending", "scheduled_dt": {"$lte": now_utc}}
            ).sort("scheduled_dt", ASCENDING)
            docs = await cursor.to_list(length=100)
            return [
                {
                    "_id": str(d["_id"]),
                    "phone": d.get("phone", ""),
                    "template_sid": d.get("template_sid", ""),
                    "template_variables": d.get("template_variables", {}),
                    "template_name": d.get("template_name", ""),
                    "contact_id": d.get("contact_id", ""),
                }
                for d in docs
            ]
        except Exception as e:
            logger.error(f"[MongoDB] Error buscando mensajes vencidos: {e}")
            return []

    async def mark_scheduled_message_sent(self, message_id: str) -> bool:
        """Marca un mensaje como enviado."""
        if not await self.connect():
            return False
        try:
            result = await self.db.scheduled_messages.update_one(
                {"_id": ObjectId(message_id)},
                {"$set": {"status": "sent", "sent_at": datetime.now(TIMEZONE), "error": None}}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"[MongoDB] Error marcando sent {message_id}: {e}")
            return False

    async def mark_scheduled_message_failed(self, message_id: str, error: str) -> bool:
        """Marca un mensaje como fallido con el mensaje de error."""
        if not await self.connect():
            return False
        try:
            result = await self.db.scheduled_messages.update_one(
                {"_id": ObjectId(message_id)},
                {"$set": {"status": "failed", "error": error, "sent_at": datetime.now(TIMEZONE)}}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"[MongoDB] Error marcando failed {message_id}: {e}")
            return False

    async def close(self):
        """Cierra la conexión."""
        if self.client:
            self.client.close()
            self._connected = False
            logger.info("[MongoDB] Conexión cerrada")


# Instancia Singleton
_mongo_manager: Optional[MongoDBManager] = None


def get_mongo_manager() -> MongoDBManager:
    """Obtiene la instancia singleton del MongoDBManager."""
    global _mongo_manager
    if _mongo_manager is None:
        _mongo_manager = MongoDBManager()
    return _mongo_manager


# Alias para compatibilidad con código existente
mongo_manager = get_mongo_manager()