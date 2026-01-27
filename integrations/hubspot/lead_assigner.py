# integrations/hubspot/lead_assigner.py
"""
Sistema de asignación automática de leads por Round Robin.
Soporta múltiples canales y equipos con persistencia en Redis.
"""

import os
import redis
from typing import Optional, Dict, List, Any
from logging_config import logger
from datetime import datetime, timezone


class LeadAssigner:
    """
    Asignador de leads por Round Robin con soporte para múltiples canales.

    Características:
    - Persistencia del índice en Redis
    - Mapeo canal_origen → equipo de asignación
    - Fallback seguro si Redis no está disponible
    - Sistema de alertas para leads huérfanos
    """

    # ═══════════════════════════════════════════════════════════════════════════
    # CONFIGURACIÓN DE EQUIPOS Y CANALES
    # ═══════════════════════════════════════════════════════════════════════════

    # IDs de owners de HubSpot (obtener de Settings > Users & Teams)
    # Formato: {"name": "Nombre", "id": "hubspot_owner_id", "active": True/False}
    #
    # CONFIGURACIÓN ACTUALIZADA:
    # - Trabajador1 (ID: 87367331): Facebook, Mercado Libre, Ciencuadras, Metrocuadrado
    # - Trabajador2 (ID: 87367331): WhatsApp, Finca Raíz, Página Web, Instagram
    OWNERS_CONFIG = {
        # Equipo para Trabajador 1
        "equipo_trabajador1": [
            {"name": "Trabajador1", "id": "87367331", "active": True},
        ],

        # Equipo para Trabajador 2
        "equipo_trabajador2": [
            {"name": "Trabajador2", "id": "87367331", "active": True},
        ],

        # Equipo default (fallback - ambos trabajadores en round robin)
        "default": [
            {"name": "Trabajador1", "id": "87367331", "active": True},
            {"name": "Trabajador2", "id": "87367331", "active": True},
        ],
    }

    # Mapeo de canal_origen → equipo
    # Clave: identificador del canal (se detecta del mensaje o metadata)
    # Valor: nombre del equipo en OWNERS_CONFIG
    CHANNEL_TO_TEAM = {
        # === TRABAJADOR 2 ===
        "whatsapp_direct": "equipo_trabajador2",
        "finca_raiz": "equipo_trabajador2",
        "pagina_web": "equipo_trabajador2",
        "instagram": "equipo_trabajador2",

        # === TRABAJADOR 1 ===
        "facebook": "equipo_trabajador1",
        "mercado_libre": "equipo_trabajador1",
        "ciencuadras": "equipo_trabajador1",
        "metrocuadrado": "equipo_trabajador1",

        # === FALLBACK ===
        "desconocido": "default",
        "google_ads": "default",
        "referido": "default",
    }

    # Prefijo para claves de Redis
    REDIS_KEY_PREFIX = "lead_assigner"

    def __init__(self, redis_client: Optional[redis.Redis] = None):
        """
        Inicializa el asignador con cliente Redis opcional.

        Args:
            redis_client: Cliente Redis existente. Si es None, intentará crear uno.
        """
        self.redis = redis_client
        self._redis_available = False

        # Intentar conectar a Redis si no se proporciona cliente
        if self.redis is None:
            self._init_redis()
        else:
            self._redis_available = True

        logger.info(f"[LeadAssigner] Inicializado. Redis disponible: {self._redis_available}")

    def _init_redis(self):
        """Inicializa conexión a Redis de forma segura."""
        try:
            # Detectar entorno
            is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
            redis_url = os.getenv("REDIS_URL") if is_railway else (
                os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL")
            )

            if redis_url:
                self.redis = redis.from_url(redis_url, decode_responses=True)
                self.redis.ping()
                self._redis_available = True
                logger.info("[LeadAssigner] Conexión a Redis establecida")
            else:
                logger.warning("[LeadAssigner] REDIS_URL no configurada. Usando asignación sin persistencia.")

        except Exception as e:
            logger.warning(f"[LeadAssigner] No se pudo conectar a Redis: {e}. Usando asignación sin persistencia.")
            self._redis_available = False

    def _get_redis_key(self, team: str) -> str:
        """Genera la clave de Redis para un equipo específico."""
        return f"{self.REDIS_KEY_PREFIX}:index:{team}"

    def _get_active_owners(self, team: str) -> List[Dict[str, Any]]:
        """
        Retorna lista de owners activos para un equipo.

        Args:
            team: Nombre del equipo

        Returns:
            Lista de owners activos con sus IDs
        """
        owners = self.OWNERS_CONFIG.get(team, self.OWNERS_CONFIG["default"])
        return [o for o in owners if o.get("active", True)]

    def get_next_owner(self, channel_origin: str = "whatsapp_direct") -> Optional[str]:
        """
        Retorna el ID del siguiente owner en rotación para el canal especificado.

        Args:
            channel_origin: Identificador del canal de origen del lead

        Returns:
            hubspot_owner_id del siguiente owner, o None si no hay owners disponibles
        """
        # Determinar equipo basado en canal
        team = self.CHANNEL_TO_TEAM.get(channel_origin, "default")
        active_owners = self._get_active_owners(team)

        if not active_owners:
            logger.error(f"[LeadAssigner] No hay owners activos para el equipo '{team}'")
            return None

        # Si solo hay un owner, retornarlo directamente
        if len(active_owners) == 1:
            owner_id = active_owners[0]["id"]
            logger.info(f"[LeadAssigner] Asignando a único owner: {active_owners[0]['name']} (ID: {owner_id})")
            return owner_id

        # Obtener índice actual de Redis (o inicializar en 0)
        current_index = 0
        redis_key = self._get_redis_key(team)

        if self._redis_available:
            try:
                stored_index = self.redis.get(redis_key)
                if stored_index is not None:
                    current_index = int(stored_index)
            except Exception as e:
                logger.warning(f"[LeadAssigner] Error leyendo índice de Redis: {e}")

        # Calcular owner actual usando módulo
        owner_index = current_index % len(active_owners)
        owner = active_owners[owner_index]

        # Incrementar índice para la próxima asignación
        next_index = current_index + 1

        if self._redis_available:
            try:
                self.redis.set(redis_key, next_index)
            except Exception as e:
                logger.warning(f"[LeadAssigner] Error guardando índice en Redis: {e}")

        logger.info(
            f"[LeadAssigner] Asignación Round Robin: {owner['name']} "
            f"(ID: {owner['id']}, Canal: {channel_origin}, Equipo: {team}, Index: {owner_index})"
        )

        return owner["id"]

    def get_owner_name(self, owner_id: str) -> str:
        """
        Retorna el nombre del owner dado su ID.

        Args:
            owner_id: ID del owner en HubSpot

        Returns:
            Nombre del owner o "Desconocido" si no se encuentra
        """
        for team_owners in self.OWNERS_CONFIG.values():
            for owner in team_owners:
                if owner["id"] == owner_id:
                    return owner["name"]
        return "Desconocido"

    def detect_channel_origin(self, metadata: Dict[str, Any], session_id: str) -> str:
        """
        Detecta el canal de origen basado en metadata y session_id.

        Args:
            metadata: Diccionario con metadata del lead
            session_id: ID de sesión (puede contener información del canal)

        Returns:
            Identificador del canal de origen
        """
        # 1. Verificar si hay canal explícito en metadata
        explicit_channel = metadata.get("canal_origen") or metadata.get("source") or metadata.get("utm_source")
        if explicit_channel:
            explicit_channel = explicit_channel.lower().replace(" ", "_")
            if explicit_channel in self.CHANNEL_TO_TEAM:
                return explicit_channel

        # 2. Detectar por patrones en metadata
        referrer = (metadata.get("referrer") or "").lower()

        if "fincaraiz" in referrer or "finca raiz" in referrer:
            return "finca_raiz"
        elif "metrocuadrado" in referrer:
            return "metrocuadrado"
        elif "facebook" in referrer or "fb.com" in referrer:
            return "facebook"
        elif "instagram" in referrer:
            return "instagram"
        elif "google" in referrer:
            return "google_ads"

        # 3. Default: WhatsApp directo
        return "whatsapp_direct"

    def reset_index(self, team: str = "default") -> bool:
        """
        Reinicia el índice de rotación para un equipo (útil para testing).

        Args:
            team: Nombre del equipo

        Returns:
            True si se reinició correctamente, False en caso de error
        """
        if not self._redis_available:
            logger.warning("[LeadAssigner] Redis no disponible para reiniciar índice")
            return False

        try:
            redis_key = self._get_redis_key(team)
            self.redis.set(redis_key, 0)
            logger.info(f"[LeadAssigner] Índice reiniciado para equipo '{team}'")
            return True
        except Exception as e:
            logger.error(f"[LeadAssigner] Error reiniciando índice: {e}")
            return False

    def get_assignment_stats(self) -> Dict[str, Any]:
        """
        Retorna estadísticas de asignación (útil para monitoreo).

        Returns:
            Diccionario con estadísticas por equipo
        """
        stats = {
            "redis_available": self._redis_available,
            "teams": {}
        }

        for team in self.OWNERS_CONFIG.keys():
            active_owners = self._get_active_owners(team)
            current_index = 0

            if self._redis_available:
                try:
                    stored_index = self.redis.get(self._get_redis_key(team))
                    if stored_index:
                        current_index = int(stored_index)
                except Exception:
                    pass

            stats["teams"][team] = {
                "active_owners_count": len(active_owners),
                "current_index": current_index,
                "next_owner": active_owners[current_index % len(active_owners)]["name"] if active_owners else None
            }

        return stats


# ═══════════════════════════════════════════════════════════════════════════════
# SISTEMA DE ALERTAS PARA LEADS HUÉRFANOS
# ═══════════════════════════════════════════════════════════════════════════════

class OrphanLeadAlert:
    """
    Sistema de alertas para leads que no pudieron ser asignados.
    """

    REDIS_KEY = "lead_assigner:orphan_alerts"

    def __init__(self, redis_client: Optional[redis.Redis] = None):
        self.redis = redis_client
        self._redis_available = redis_client is not None

    def log_orphan_lead(
        self,
        contact_id: str,
        phone: str,
        reason: str,
        metadata: Optional[Dict] = None
    ):
        """
        Registra un lead huérfano para revisión manual.

        Args:
            contact_id: ID del contacto en HubSpot
            phone: Teléfono del lead
            reason: Razón por la que no se pudo asignar
            metadata: Metadata adicional del lead
        """
        alert_data = {
            "contact_id": contact_id,
            "phone": phone,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": metadata or {}
        }

        # Log siempre a consola
        logger.warning(
            f"[OrphanLeadAlert] ⚠️ LEAD HUÉRFANO - "
            f"ContactID: {contact_id}, Phone: {phone}, Reason: {reason}"
        )

        # Intentar guardar en Redis si está disponible
        if self._redis_available:
            try:
                import json
                self.redis.lpush(self.REDIS_KEY, json.dumps(alert_data))
                # Mantener solo las últimas 100 alertas
                self.redis.ltrim(self.REDIS_KEY, 0, 99)
            except Exception as e:
                logger.error(f"[OrphanLeadAlert] Error guardando alerta en Redis: {e}")

    def get_pending_alerts(self, limit: int = 10) -> List[Dict]:
        """
        Retorna las alertas pendientes de leads huérfanos.

        Args:
            limit: Número máximo de alertas a retornar

        Returns:
            Lista de alertas
        """
        if not self._redis_available:
            return []

        try:
            import json
            alerts_raw = self.redis.lrange(self.REDIS_KEY, 0, limit - 1)
            return [json.loads(a) for a in alerts_raw]
        except Exception as e:
            logger.error(f"[OrphanLeadAlert] Error leyendo alertas: {e}")
            return []


# Instancia global (Singleton)
lead_assigner = LeadAssigner()
orphan_alert_system = OrphanLeadAlert()