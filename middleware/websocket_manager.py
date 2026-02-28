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
        contact_name: str = ""
    ) -> int:
        """
        Notifica a los asesores sobre un nuevo mensaje.

        Args:
            phone: Teléfono del contacto
            canal: Canal de origen
            message_preview: Primeros caracteres del mensaje
            sender: Quién envió (client/advisor/bot)
            contact_name: Nombre del contacto

        Returns:
            Número de notificaciones enviadas
        """
        notification = {
            "type": "new_message",
            "phone": phone,
            "canal": canal,
            "sender": sender,
            "preview": message_preview[:100] if message_preview else "",
            "contact_name": contact_name,
            "timestamp": datetime.now(TIMEZONE_BOGOTA).isoformat()
        }

        # Si conocemos el asesor asignado, notificar solo a él
        advisor_id = self.phone_to_advisor.get(phone)
        if advisor_id:
            return await self.send_to_advisor(advisor_id, notification)

        # Si no, broadcast a todos
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
        sent_count = 0
        failed_connections = []

        for websocket in self.all_connections.copy():
            try:
                await websocket.send_json(message)
                sent_count += 1
            except Exception as e:
                logger.warning(f"[WebSocket] Error en broadcast: {e}")
                failed_connections.append(websocket)

        # Limpiar conexiones fallidas (necesitamos encontrar el advisor_id)
        for ws in failed_connections:
            self.all_connections.discard(ws)
            # Buscar y limpiar de active_connections
            for advisor_id, connections in list(self.active_connections.items()):
                if ws in connections:
                    connections.remove(ws)
                    if not connections:
                        del self.active_connections[advisor_id]
                    break

        logger.debug(f"[WebSocket] Broadcast enviado a {sent_count} conexiones")
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


# Instancia global del manager
ws_manager = ConnectionManager()
