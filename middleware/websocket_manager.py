# middleware/websocket_manager.py
"""
Gestor de conexiones WebSocket para notificaciones en tiempo real.

Permite a los asesores recibir notificaciones instantáneas cuando:
- Un cliente envía un nuevo mensaje
- Un contacto es transferido
- Hay cambios de estado en una conversación
"""

import asyncio
import json
from typing import Dict, List, Optional, Set
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import WebSocket, WebSocketDisconnect
from logging_config import logger

TIMEZONE_BOGOTA = ZoneInfo("America/Bogota")


class ConnectionManager:
    """
    Gestiona las conexiones WebSocket activas de los asesores.

    Permite:
    - Múltiples conexiones por asesor (ej: panel en PC y móvil)
    - Broadcast a todos los asesores
    - Mensajes dirigidos a un asesor específico
    - Notificaciones por teléfono de contacto
    """

    def __init__(self):
        # advisor_id -> lista de websockets activos
        self.active_connections: Dict[str, List[WebSocket]] = {}
        # phone -> advisor_id (para saber a quién notificar)
        self.phone_to_advisor: Dict[str, str] = {}
        # Todas las conexiones activas (para broadcast global)
        self.all_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket, advisor_id: str) -> None:
        """
        Registra una nueva conexión WebSocket para un asesor.

        Args:
            websocket: Conexión WebSocket
            advisor_id: ID del asesor (owner_id de HubSpot)
        """
        await websocket.accept()

        if advisor_id not in self.active_connections:
            self.active_connections[advisor_id] = []

        self.active_connections[advisor_id].append(websocket)
        self.all_connections.add(websocket)

        logger.info(
            f"[WebSocket] Asesor {advisor_id} conectado. "
            f"Total conexiones: {len(self.all_connections)}"
        )

    def disconnect(self, websocket: WebSocket, advisor_id: str) -> None:
        """
        Elimina una conexión WebSocket de un asesor.

        Args:
            websocket: Conexión a remover
            advisor_id: ID del asesor
        """
        if advisor_id in self.active_connections:
            try:
                self.active_connections[advisor_id].remove(websocket)
                # Si no quedan conexiones, eliminar la entrada
                if not self.active_connections[advisor_id]:
                    del self.active_connections[advisor_id]
            except ValueError:
                pass

        self.all_connections.discard(websocket)

        logger.info(
            f"[WebSocket] Asesor {advisor_id} desconectado. "
            f"Total conexiones: {len(self.all_connections)}"
        )

    def register_phone_owner(self, phone: str, advisor_id: str) -> None:
        """
        Registra qué asesor atiende un teléfono específico.

        Args:
            phone: Número de teléfono normalizado
            advisor_id: ID del asesor asignado
        """
        self.phone_to_advisor[phone] = advisor_id
        logger.debug(f"[WebSocket] Teléfono {phone} asignado a asesor {advisor_id}")

    def unregister_phone(self, phone: str) -> None:
        """
        Elimina el registro de un teléfono (cuando se cierra la conversación).
        """
        if phone in self.phone_to_advisor:
            del self.phone_to_advisor[phone]

    async def send_to_advisor(self, advisor_id: str, message: dict) -> int:
        """
        Envía un mensaje a todas las conexiones de un asesor.

        Args:
            advisor_id: ID del asesor
            message: Diccionario con el mensaje a enviar

        Returns:
            Número de conexiones a las que se envió
        """
        sent_count = 0
        connections = self.active_connections.get(advisor_id, [])

        # Lista de conexiones fallidas para limpieza
        failed_connections = []

        for websocket in connections:
            try:
                await websocket.send_json(message)
                sent_count += 1
            except Exception as e:
                logger.warning(f"[WebSocket] Error enviando a asesor {advisor_id}: {e}")
                failed_connections.append(websocket)

        # Limpiar conexiones fallidas
        for ws in failed_connections:
            self.disconnect(ws, advisor_id)

        return sent_count

    async def notify_new_message(
        self,
        phone: str,
        canal: str,
        message_preview: str = "",
        sender: str = "client",
        contact_name: str = "",
        redis_client=None
    ) -> int:
        """
        Notifica a los asesores sobre un nuevo mensaje.

        Args:
            phone: Teléfono del contacto
            canal: Canal de origen
            message_preview: Primeros caracteres del mensaje
            sender: Quién envió (client/advisor/bot)
            contact_name: Nombre del contacto
            redis_client: Cliente Redis para publish_broadcast cross-worker.
                          Si se omite, usa broadcast local (solo este worker).

        Returns:
            Número de notificaciones enviadas (siempre 0 cuando usa Pub/Sub)
        """
        notification = {
            "type": "contact_updated",
            "action": "new_message",
            "phone": phone,
            "canal": canal,
            "sender": sender,
            "preview": message_preview[:100] if message_preview else "",
            "contact_name": contact_name,
            "timestamp": datetime.now(TIMEZONE_BOGOTA).isoformat()
        }

        logger.info(f"[WebSocket] notify_new_message: phone={phone}, preview={message_preview[:30] if message_preview else 'N/A'}")

        if redis_client is not None:
            await self.publish_broadcast(redis_client, notification)
            return 0  # Pub/Sub no retorna conteo local

        return await self.broadcast(notification)

    async def notify_contact_transferred(
        self,
        phone: str,
        from_advisor: str,
        to_advisor: str,
        contact_name: str = "",
        mode: str = "exclusive"
    ) -> None:
        """
        Notifica sobre una transferencia de contacto.

        Args:
            phone: Teléfono del contacto
            from_advisor: ID del asesor origen
            to_advisor: ID del asesor destino
            contact_name: Nombre del contacto
            mode: Modo de transferencia (exclusive/collaborative)
        """
        notification = {
            "type": "contact_transferred",
            "phone": phone,
            "contact_name": contact_name,
            "mode": mode,
            "timestamp": datetime.now(TIMEZONE_BOGOTA).isoformat()
        }

        # Notificar al asesor origen (el contacto salió de su panel)
        await self.send_to_advisor(from_advisor, {
            **notification,
            "direction": "outgoing",
            "message": f"Contacto {contact_name or phone} transferido"
        })

        # Notificar al asesor destino (tiene nuevo contacto)
        await self.send_to_advisor(to_advisor, {
            **notification,
            "direction": "incoming",
            "message": f"Nuevo contacto recibido: {contact_name or phone}"
        })

        # Actualizar registro
        self.register_phone_owner(phone, to_advisor)

    async def notify_status_change(
        self,
        phone: str,
        canal: str,
        old_status: str,
        new_status: str,
        contact_name: str = ""
    ) -> int:
        """
        Notifica sobre un cambio de estado en una conversación.
        """
        notification = {
            "type": "status_change",
            "phone": phone,
            "canal": canal,
            "old_status": old_status,
            "new_status": new_status,
            "contact_name": contact_name,
            "timestamp": datetime.now(TIMEZONE_BOGOTA).isoformat()
        }

        advisor_id = self.phone_to_advisor.get(phone)
        if advisor_id:
            return await self.send_to_advisor(advisor_id, notification)

        return await self.broadcast(notification)

    async def broadcast(self, message: dict) -> int:
        """
        Envía un mensaje a TODAS las conexiones activas.

        Args:
            message: Diccionario con el mensaje

        Returns:
            Número de conexiones a las que se envió
        """
        connections = self.all_connections.copy()
        if not connections:
            logger.info("[WebSocket] Broadcast: sin conexiones activas")
            return 0

        async def _send_one(websocket):
            try:
                await asyncio.wait_for(websocket.send_json(message), timeout=2.0)
                return (websocket, True)
            except Exception as e:
                logger.warning(f"[WebSocket] Error en broadcast a conexión: {e}")
                return (websocket, False)

        results = await asyncio.gather(
            *[_send_one(ws) for ws in connections],
            return_exceptions=True
        )

        sent_count = 0
        failed_connections = []
        for result in results:
            if isinstance(result, Exception):
                continue
            ws, success = result
            if success:
                sent_count += 1
            else:
                failed_connections.append(ws)

        # Limpiar conexiones fallidas
        for ws in failed_connections:
            self.all_connections.discard(ws)
            for advisor_id, conns in list(self.active_connections.items()):
                if ws in conns:
                    conns.remove(ws)
                    if not conns:
                        del self.active_connections[advisor_id]
                    break

        logger.info(f"[WebSocket] Broadcast enviado a {sent_count} conexiones activas")
        return sent_count

    async def send_ping(self) -> int:
        """
        Envía un ping a todas las conexiones para mantenerlas vivas.

        Returns:
            Número de conexiones activas
        """
        return await self.broadcast({"type": "ping", "timestamp": datetime.now(TIMEZONE_BOGOTA).isoformat()})

    def get_stats(self) -> dict:
        """
        Retorna estadísticas de conexiones activas.
        """
        return {
            "total_connections": len(self.all_connections),
            "advisors_connected": len(self.active_connections),
            "phones_tracked": len(self.phone_to_advisor),
            "connections_by_advisor": {
                advisor_id: len(connections)
                for advisor_id, connections in self.active_connections.items()
            }
        }

    # ------------------------------------------------------------------
    # Redis Pub/Sub — broadcast cross-worker
    # ------------------------------------------------------------------

    PUBSUB_CHANNEL = "ws:broadcast"

    async def publish_broadcast(self, redis_client, message: dict) -> None:
        """
        Publica un mensaje en el canal Redis para que TODOS los workers
        lo reciban y hagan broadcast local a sus conexiones.

        Reemplaza las llamadas directas a broadcast() en endpoints que
        necesitan notificar a asesoras en cualquier worker.
        """
        await redis_client.publish(self.PUBSUB_CHANNEL, json.dumps(message))

    async def start_redis_listener(self, get_redis_fn) -> None:
        """
        Background task que debe iniciarse una vez por worker.
        Escucha el canal Redis ws:broadcast y reenvía cada mensaje
        a las conexiones locales de este worker.

        Incluye reconexión automática si Redis cae o el worker se reinicia.

        Args:
            get_redis_fn: Callable async que retorna un cliente Redis.
                          Se llama de nuevo en cada reconexión para obtener
                          una conexión fresca.
        """
        logger.info("[WebSocket] Iniciando Redis Pub/Sub listener...")
        while True:
            pubsub = None
            try:
                redis = await get_redis_fn()
                pubsub = redis.pubsub()  # conexión dedicada, no comparte pool
                await pubsub.subscribe(self.PUBSUB_CHANNEL)
                logger.info(f"[WebSocket] Suscrito al canal Redis '{self.PUBSUB_CHANNEL}'")

                async for raw_msg in pubsub.listen():
                    if raw_msg["type"] == "message":
                        try:
                            data = json.loads(raw_msg["data"])
                            await self.broadcast(data)
                        except Exception as e:
                            logger.error(f"[WebSocket] Error procesando mensaje Redis: {e}")

            except Exception as e:
                logger.error(f"[WebSocket] Redis listener caído, reconectando en 2s: {e}")
            finally:
                # Cerrar el pubsub explícitamente para evitar
                # RuntimeError('aclose(): asynchronous generator is already running')
                if pubsub is not None:
                    try:
                        await pubsub.unsubscribe()
                        await pubsub.aclose()
                    except Exception:
                        pass

            await asyncio.sleep(2)  # fuera del try/except — siempre espera antes de reconectar


# Instancia global del manager
ws_manager = ConnectionManager()
