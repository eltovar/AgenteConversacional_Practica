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
        # Referencia fuerte al task del Redis listener (evita GC silencioso)
        self._redis_listener_task: Optional[asyncio.Task] = None

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
        redis_client=None,
        assigned_owner_id: Optional[str] = None,
        state_manager=None,
    ) -> int:
        """
        Notifica a los asesores sobre un nuevo mensaje.

        Enrutamiento (en cascada):
          1. assigned_owner_id explícito del caller (preferido — evita IO)
          2. self.phone_to_advisor (cache local del worker, solo poblado en transferencias)
          3. state_manager.get_meta(phone, canal).assigned_owner_id (fallback Redis)
          4. publish_broadcast global (último recurso — contacto nuevo sin asignar)

        Args:
            phone: Teléfono del contacto
            canal: Canal de origen
            message_preview: Primeros caracteres del mensaje
            sender: Quién envió (client/advisor/bot)
            contact_name: Nombre del contacto
            redis_client: Cliente Redis para publish_broadcast cross-worker.
                          Si se omite, usa broadcast local (solo este worker).
            assigned_owner_id: ID del asesor dueño del contacto (preferido, evita IO).
            state_manager: ConversationStateManager para fallback via get_meta().

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
            # Resolver advisor_id en cascada
            advisor_id = assigned_owner_id
            if not advisor_id:
                advisor_id = self.phone_to_advisor.get(phone)
            if not advisor_id and state_manager is not None:
                try:
                    _m = await state_manager.get_meta(phone, canal)
                    if _m and getattr(_m, "assigned_owner_id", None):
                        advisor_id = _m.assigned_owner_id
                except Exception as _e:
                    logger.warning(f"[WebSocket] fallback get_meta falló para {phone}: {_e}")

            if advisor_id:
                await self.publish_to_advisor(redis_client, advisor_id, notification)
                logger.info(f"[WebSocket] notify_new_message → targeted advisor={advisor_id} phone={phone}")
            else:
                logger.warning(f"[WebSocket] notify_new_message → broadcast global (sin owner) phone={phone}")
                await self.publish_broadcast(redis_client, notification)
            return 0

        return await self.broadcast(notification)

    async def notify_contact_transferred(
        self,
        phone: str,
        from_advisor: str,
        to_advisor: str,
        contact_name: str = "",
        mode: str = "exclusive",
        redis_client=None
    ) -> None:
        """
        Notifica sobre una transferencia de contacto.

        Args:
            phone: Teléfono del contacto
            from_advisor: ID del asesor origen
            to_advisor: ID del asesor destino
            contact_name: Nombre del contacto
            mode: Modo de transferencia (exclusive/collaborative)
            redis_client: Si se provee, usa Redis Pub/Sub (cross-worker safe).
                          Si es None, usa send_to_advisor directo (fallback local).
        """
        notification = {
            "type": "contact_transferred",
            "phone": phone,
            "contact_name": contact_name,
            "mode": mode,
            "timestamp": datetime.now(TIMEZONE_BOGOTA).isoformat()
        }
        msg_out = {**notification, "direction": "outgoing",
                   "message": f"Contacto {contact_name or phone} transferido"}
        msg_in  = {**notification, "direction": "incoming",
                   "message": f"Nuevo contacto recibido: {contact_name or phone}"}

        if redis_client is not None:
            await self.publish_to_advisor(redis_client, from_advisor, msg_out)
            await self.publish_to_advisor(redis_client, to_advisor, msg_in)
            # Sincronizar phone→advisor en todos los workers via Pub/Sub
            await self.publish_broadcast(redis_client, {"_route": {"phone": phone, "advisor_id": to_advisor}})
        else:
            await self.send_to_advisor(from_advisor, msg_out)
            await self.send_to_advisor(to_advisor, msg_in)

        # Actualizar registro local del worker actual
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

    async def publish_to_advisor(self, redis_client, advisor_id: str, message: dict) -> None:
        """
        Envía un mensaje a un asesor específico via Redis Pub/Sub (cross-worker safe).
        Añade _target al payload para que el listener de cada worker enrute
        solo a las conexiones de ese asesor.
        """
        payload = {**message, "_target": advisor_id}
        await self.publish_broadcast(redis_client, payload)

    async def start_redis_listener(self, get_redis_fn) -> None:
        """
        Crea (o reemplaza) el background task del Redis Pub/Sub listener.

        Guarda referencia fuerte en self._redis_listener_task para evitar
        que el GC destruya el task silenciosamente (Python asyncio warning:
        "Task was destroyed but it is pending!").

        Si ya existe un task anterior lo cancela limpiamente antes de crear uno nuevo.

        Args:
            get_redis_fn: Callable async que retorna un cliente Redis.
        """
        # Cancelar task anterior limpiamente si existe
        if self._redis_listener_task and not self._redis_listener_task.done():
            self._redis_listener_task.cancel()
            try:
                await self._redis_listener_task
            except asyncio.CancelledError:
                pass

        # Crear nuevo task y guardar referencia fuerte
        self._redis_listener_task = asyncio.create_task(
            self._run_redis_listener(get_redis_fn)
        )
        logger.info("[WebSocket] Redis listener task creado y referencia guardada")

    async def _run_redis_listener(self, get_redis_fn) -> None:
        """
        Loop interno del Redis Pub/Sub listener.
        Escucha el canal ws:broadcast y hace broadcast local a las conexiones de este worker.
        Incluye reconexión automática si Redis cae.

        NOTA: El cliente Redis se crea UNA SOLA VEZ fuera del loop para evitar el leak
        de conexiones. Antes, get_redis_fn() se llamaba dentro de while True: cada error
        de reconexión creaba un nuevo pool sin cerrar el anterior, acumulando pools
        huérfanos a lo largo del uptime del servicio.
        """
        logger.info("[WebSocket] Iniciando Redis Pub/Sub listener...")
        redis_client = await get_redis_fn()  # Un solo cliente, reutilizado en todas las reconexiones
        try:
            while True:
                pubsub = None
                try:
                    pubsub = redis_client.pubsub()  # conexión dedicada, no comparte pool
                    await pubsub.subscribe(self.PUBSUB_CHANNEL)
                    logger.info(f"[WebSocket] Suscrito al canal Redis '{self.PUBSUB_CHANNEL}'")

                    async for raw_msg in pubsub.listen():
                        if raw_msg["type"] == "message":
                            try:
                                data = json.loads(raw_msg["data"])
                                # Sincronización cross-worker de phone→advisor
                                route = data.pop("_route", None)
                                if route:
                                    self.register_phone_owner(route["phone"], route["advisor_id"])
                                target = data.pop("_target", None)
                                if not data and not target:
                                    continue  # Mensaje solo de sync, nada que enviar
                                if target:
                                    await self.send_to_advisor(target, data)
                                else:
                                    await self.broadcast(data)
                            except Exception as e:
                                logger.error(f"[WebSocket] Error procesando mensaje Redis: {e}")

                except asyncio.CancelledError:
                    logger.info("[WebSocket] Redis listener cancelado limpiamente")
                    raise
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
        finally:
            # Cerrar el cliente Redis al salir del loop (CancelledError en shutdown graceful)
            try:
                await redis_client.aclose()
            except Exception:
                pass


# Instancia global del manager
ws_manager = ConnectionManager()
