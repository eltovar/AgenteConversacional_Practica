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