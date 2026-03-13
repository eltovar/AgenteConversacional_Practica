# database/mongodb_client.py
"""
Cliente MongoDB para almacenamiento en tiempo real de mensajes.
"""

import os
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
from bson import ObjectId

from logging_config import logger

# Timezone Colombia
TIMEZONE = ZoneInfo("America/Bogota")


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

    async def connect(self) -> bool:
        """
        Establece conexión con MongoDB.

        Returns:
            True si la conexión fue exitosa
        """
        if self._connected and self.client:
            return True

        if not self.mongo_url:
            logger.warning("[MongoDB] MONGO_URL no configurada - MongoDB deshabilitado")
            return False

        try:
            self.client = AsyncIOMotorClient(
                self.mongo_url,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                maxPoolSize=10,
                minPoolSize=1
            )

            # Verificar conexión
            await self.client.admin.command('ping')

            self.db = self.client.get_database("inmobiliaria_chat")
            self._connected = True

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

            # Índice para panel_advisors — evita table scan en lookups por advisor_id
            await self.db.panel_advisors.create_index(
                "advisor_id",
                name="panel_advisor_id_idx",
                unique=True
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
        media: Optional[Dict[str, Any]] = None
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

        Returns:
            ID del documento insertado o None si falla
        """
        if not await self.connect():
            logger.warning("[MongoDB] No conectado - mensaje no guardado en MongoDB")
            return None

        try:
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
            if media:
                message_doc["media"] = media

            result = await self.db.messages.insert_one(message_doc)

            logger.debug(
                f"[MongoDB] Mensaje guardado: phone={phone}, sender={sender}, "
                f"media={media}, id={result.inserted_id}"
            )

            return str(result.inserted_id)

        except Exception as e:
            logger.error(f"[MongoDB] Error guardando mensaje: {e}")
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
                        "processed_at": media.get("processed_at"),
                    } if media else None
                })

            logger.debug(f"[MongoDB] Historial obtenido: {len(formatted_messages)} mensajes para {phone}")

            return formatted_messages

        except Exception as e:
            logger.error(f"[MongoDB] Error obteniendo historial: {e}")
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
                    "media_url": msg.get("media_url"),
                    "media_type": msg.get("media_type")
                })

            return formatted_messages

        except Exception as e:
            logger.error(f"[MongoDB] Error obteniendo historial por contact_id: {e}")
            return []

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

    # Configuración predeterminada de asesores (ID → nombre placeholder)
    DEFAULT_ADVISORS = {
        "89096380": "Asesor Portales",
        "89096378": "Asesor Directo",
        "82598814": "Equipo de Marketing",
        "89096379": "Asesor Respaldo"
    }

    async def init_advisors(self) -> bool:
        """Inicializa los asesores predeterminados si no existen."""
        if not await self.connect():
            return False
        try:
            for advisor_id, name in self.DEFAULT_ADVISORS.items():
                # Upsert: crear solo si no existe
                await self.db.panel_advisors.update_one(
                    {"advisor_id": advisor_id},
                    {"$setOnInsert": {
                        "advisor_id": advisor_id,
                        "name": name,
                        "active": True,
                        "created_at": datetime.now(TIMEZONE)
                    }},
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
        hubspot_note_id: Optional[str] = None
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
                "status": "scheduled",
                "created_at": datetime.now(TIMEZONE)
            })
            return str(result.inserted_id)
        except Exception as e:
            logger.error(f"[MongoDB] Error creando cita: {e}")
            return None

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
                    "appointment_dt": a["appointment_dt"].isoformat() if a.get("appointment_dt") else None,
                    "notes": a.get("notes", ""),
                    "status": a.get("status", "scheduled"),
                    "created_at": a["created_at"].isoformat() if a.get("created_at") else None,
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

    async def get_contacts_by_worker(self, worker_id: str) -> List[Dict[str, Any]]:
        """
        Retorna la lista de contactos que tienen una cita futura activa con un worker específico.
        Usado por el filtro de worker en el panel.

        Returns:
            Lista de dicts con {contact_id, phone, appointment_dt (ISO str), worker_name}
        """
        if not await self.connect() or not worker_id:
            return []
        try:
            now = datetime.now(TIMEZONE)
            cursor = self.db.appointments.find(
                {
                    "worker_id": worker_id,
                    "status": "scheduled",
                    "appointment_dt": {"$gte": now}
                },
                {"contact_id": 1, "phone": 1, "appointment_dt": 1, "worker_name": 1}
            ).sort("appointment_dt", 1)  # ASC: más próxima primero
            docs = await cursor.to_list(length=200)
            result = []
            for d in docs:
                result.append({
                    "contact_id": d.get("contact_id", ""),
                    "phone": d.get("phone", ""),
                    "appointment_dt": d["appointment_dt"].isoformat() if d.get("appointment_dt") else None,
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