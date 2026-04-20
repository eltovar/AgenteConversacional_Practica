# integrations/hubspot/lead_assigner.py
"""
Sistema de asignación automática de leads por Round Robin.
Soporta múltiples canales y equipos con persistencia en Redis.
"""

import os
import redis
from typing import Optional, Dict, List, Any
from logging_config import logger
from datetime import datetime, timezone, timedelta


class LeadAssigner:
    """
    Asignador de leads por Round Robin con soporte para múltiples canales.
    """
    # ═══════════════════════════════════════════════════════════════════════════
    # CONFIGURACIÓN DE EQUIPOS Y CANALES
    # ═══════════════════════════════════════════════════════════════════════════
    
    # ✏️ EDITAR AQUÍ para cambiar los nombres de las asesoras en el panel.
    # Este es el ÚNICO lugar donde se definen los nombres — mongodb_client.py y
    # outbound_panel.py los leen automáticamente desde aquí.
    OWNERS_CONFIG = {
        # === PORTALES INMOBILIARIOS + WHATSAPP DIRECTO (ID: 89096378) ===
        # Portales: Mercado Libre, LinkedIn, Facebook, Instagram, Ciencuadras, TikTok, Charly + WhatsApp Directo
        "equipo_portales": [
            {"name": "Jubeny", "id": "89096378", "active": True},
        ],

        # === DIRECTO + PORTALES ESPECIALES (ID: 89096380) ===
        # Directo: Página Web, YouTube + Portales: MetroCuadrado, Finca Raíz
        "equipo_directo": [
            {"name": "Luisa", "id": "89096380", "active": True},
        ],

        # === EQUIPO DE MARKETING (Solo métricas - NO responde) ===
        # NOTA: Este ID no se usa para asignación de leads, solo para filtrar métricas
        "equipo_marketing": [
            {"name": "Equipo de Marketing", "id": "82598814", "active": False},  # Inactivo para asignación
        ],

        # === RESPALDO (Solo transferencias manuales, ID: 89096379) ===
        # No recibe asignación automática, solo contactos transferidos manualmente
        "equipo_respaldo": [
            {"name": "Monica", "id": "89096379", "active": False},  # Inactivo para asignación automática
        ],

        # Equipo default (fallback - round robin entre portales y directo)
        "default": [
            {"name": "Jubeny", "id": "89096378", "active": True},
            {"name": "Luisa",  "id": "89096380", "active": True},
        ],
    }

    # Mapeo de canal_origen → equipo
    # Clave: identificador del canal (se detecta del mensaje o metadata)
    # Valor: nombre del equipo en OWNERS_CONFIG
    CHANNEL_TO_TEAM = {
        # === PORTALES INMOBILIARIOS + WHATSAPP DIRECTO (ID: 89096378) ===
        "mercado_libre": "equipo_portales",
        "linkedin": "equipo_portales",
        "whatsapp_directo": "equipo_portales",
        "whatsapp": "equipo_portales",  # Alias para compatibilidad con webhook

        # === DIRECTO + PORTALES ESPECIALES (ID: 89096380) ===
        "pagina_web": "equipo_directo",
        "youtube": "equipo_directo",
        "finca_raiz": "equipo_directo",  # ✓ CAMBIO: Finca Raíz → 89096380 (Asesor Directo)
        "metrocuadrado": "equipo_directo",  # ✓ CAMBIO: MetroCuadrado → 89096380 (Asesor Directo)

        # === REDES SOCIALES + CIENCUADRAS (ID: 89096378) ===
        "facebook": "equipo_portales",
        "instagram": "equipo_portales",
        "ciencuadras": "equipo_portales",
        "tiktok": "equipo_portales",
        "charly": "equipo_portales",

        # === FALLBACK (Round robin entre portales y directo) ===
        "desconocido": "default",
        "google_ads": "default",
        "referido": "default",
    }

    # ═══════════════════════════════════════════════════════════════════════════
    # MAPEO DIRECTO CANAL → OWNER ID (para filtro del panel)
    # ═══════════════════════════════════════════════════════════════════════════
    CHANNEL_TO_OWNER = {
        # Asesor Portales + WhatsApp Directo (89096378)
        "mercado_libre": "89096378",
        "linkedin": "89096378",
        "whatsapp_directo": "89096378",
        "whatsapp": "89096378",  # Alias para compatibilidad con webhook

        # Asesor Directo + Portales Especiales (89096380)
        "pagina_web": "89096380",
        "youtube": "89096380",
        "finca_raiz": "89096380",  # ✓ CAMBIO: Finca Raíz → 89096380 (Asesor Directo)
        "metrocuadrado": "89096380",  # ✓ CAMBIO: MetroCuadrado → 89096380 (Asesor Directo)

        # Redes Sociales + Ciencuadras (89096378)
        "facebook": "89096378",
        "instagram": "89096378",
        "ciencuadras": "89096378",
        "tiktok": "89096378",
        "charly": "89096378",
    }

    # Canales para métricas de analista de redes sociales
    SOCIAL_MEDIA_CHANNELS = ["facebook", "instagram", "linkedin", "youtube", "tiktok"]

    # Prefijo para claves de Redis
    REDIS_KEY_PREFIX = "lead_assigner"

    def __init__(self, redis_client: Optional[redis.Redis] = None):
        """
        Inicializa el asignador con cliente Redis opcional.
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
                self.redis = redis.from_url(
                    redis_url,
                    decode_responses=True,
                    max_connections=3,           # Cap explícito — evita pool sin límite
                    socket_timeout=3.0,
                    socket_connect_timeout=3.0,
                    retry_on_timeout=True
                )
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

    def get_next_owner(self, channel_origin: str = "whatsapp_directo") -> Optional[str]:
        """
        Retorna el ID del siguiente owner en rotación para el canal especificado.
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
        return "whatsapp_directo"

    def reset_index(self, team: str = "default") -> bool:
        """
        Reinicia el índice de rotación para un equipo (útil para testing).
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
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
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


# ═══════════════════════════════════════════════════════════════════════════════
# MONITOR PROACTIVO DE LEADS HUÉRFANOS (BÚSQUEDA EN HUBSPOT)
# ═══════════════════════════════════════════════════════════════════════════════

class OrphanLeadMonitor:
    """
    Monitorea activamente leads sin owner en HubSpot y envía alertas.
    """

    REDIS_KEY_ORPHANS = "lead_assigner:orphan_leads_detected"

    def __init__(self, hubspot_client, redis_client: Optional[redis.Redis] = None):
        """
        Inicializa el monitor con cliente HubSpot y Redis.
        """
        self.hubspot = hubspot_client
        self.redis = redis_client
        self._redis_available = redis_client is not None

        # URL del webhook para alertas (Slack, Discord, Teams, etc.)
        self.webhook_url = os.getenv("ORPHAN_LEAD_WEBHOOK_URL")

        logger.info(
            f"[OrphanLeadMonitor] Inicializado. "
            f"Webhook: {'Configurado' if self.webhook_url else 'No configurado'}"
        )

    async def check_orphan_leads(self, hours_window: int = 24) -> List[Dict]:
        """
        Busca leads sin owner asignado en HubSpot.

        Args:
            hours_window: Ventana de tiempo en horas para buscar leads recientes

        Returns:
            Lista de leads huérfanos encontrados
        """
        try:
            # Calcular timestamp límite
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours_window) 
            cutoff_timestamp_ms = int(cutoff_time.timestamp() * 1000)

            logger.info(f"[OrphanLeadMonitor] Buscando leads sin asignar (últimas {hours_window}h)...")

            # Construir filtros para búsqueda
            filters = {
                "filterGroups": [
                    {
                        "filters": [
                            {
                                "propertyName": "hubspot_owner_id",
                                "operator": "NOT_HAS_PROPERTY"
                            },
                            {
                                "propertyName": "chatbot_timestamp",
                                "operator": "HAS_PROPERTY"  # Solo leads del chatbot
                            },
                            {
                                "propertyName": "chatbot_timestamp",
                                "operator": "GTE",
                                "value": cutoff_timestamp_ms
                            }
                        ]
                    }
                ],
                "properties": [
                    "firstname",
                    "lastname",
                    "phone",
                    "canal_origen",
                    "chatbot_score",
                    "chatbot_location",
                    "chatbot_urgency",
                    "chatbot_timestamp"
                ],
                "limit": 100
            }

            # Ejecutar búsqueda en HubSpot
            endpoint = "/crm/v3/objects/contacts/search"
            response = await self.hubspot._request("POST", endpoint, filters)

            orphans = response.get("results", [])

            if orphans:
                logger.warning(f"[OrphanLeadMonitor] ⚠️ {len(orphans)} leads sin asignar detectados")
                await self._send_alert(orphans, hours_window)
            else:
                logger.info("[OrphanLeadMonitor] ✅ No se encontraron leads sin asignar")

            return orphans

        except Exception as e:
            logger.error(f"[OrphanLeadMonitor] Error buscando leads huérfanos: {e}", exc_info=True)
            return []

    async def _send_alert(self, orphan_leads: List[Dict], hours_window: int):
        """
        Envía alertas sobre leads huérfanos por múltiples canales.
        """
        # 1. Log a consola (siempre)
        logger.warning(
            f"[OrphanLeadMonitor] 🚨 ALERTA: {len(orphan_leads)} leads sin asignar "
            f"en las últimas {hours_window}h"
        )

        # 2. Guardar en Redis para consulta posterior
        if self._redis_available:
            await self._store_in_redis(orphan_leads)

        # 3. Webhook (Slack/Discord/Teams)
        if self.webhook_url:
            await self._send_webhook_alert(orphan_leads, hours_window)
        else:
            logger.warning(
                "[OrphanLeadMonitor] ORPHAN_LEAD_WEBHOOK_URL no configurada. "
                "No se enviarán notificaciones externas."
            )

    async def _store_in_redis(self, orphan_leads: List[Dict]):
        """
        Almacena leads huérfanos en Redis con TTL.

        Args:
            orphan_leads: Lista de leads a almacenar
        """
        try:
            import json

            for lead in orphan_leads:
                lead_data = {
                    "contact_id": lead["id"],
                    "properties": lead.get("properties", {}),
                    "detected_at": datetime.now(timezone.utc).isoformat() + "Z"
                }

                # Guardar con key única por contact_id
                redis_key = f"{self.REDIS_KEY_ORPHANS}:{lead['id']}"
                self.redis.setex(
                    redis_key,
                    86400,  # TTL 24 horas
                    json.dumps(lead_data)
                )

            logger.info(f"[OrphanLeadMonitor] {len(orphan_leads)} leads almacenados en Redis")

        except Exception as e:
            logger.error(f"[OrphanLeadMonitor] Error guardando en Redis: {e}")

    async def _send_webhook_alert(self, orphan_leads: List[Dict], hours_window: int):
        """
        Envía notificación a Slack/Discord via webhook.

        Args:
            orphan_leads: Lista de leads sin asignar
            hours_window: Ventana de tiempo
        """
        try:
            import httpx

            # Construir mensaje
            message = self._format_webhook_message(orphan_leads, hours_window)

            # Reusar singleton si existe, sino crear uno efímero (cold path)
            if not hasattr(self, '_http_client') or self._http_client is None:
                self._http_client = httpx.AsyncClient(timeout=10.0)
            response = await self._http_client.post(self.webhook_url, json=message)
            response.raise_for_status()

            logger.info(f"[OrphanLeadMonitor] ✅ Alerta enviada a webhook ({response.status_code})")

        except Exception as e:
            logger.error(f"[OrphanLeadMonitor] Error enviando webhook: {e}")

    def _format_webhook_message(self, orphan_leads: List[Dict], hours_window: int) -> Dict:
        """
        Formatea mensaje para Slack/Discord.

        Args:
            orphan_leads: Lista de leads
            hours_window: Ventana de tiempo

        Returns:
            Payload del webhook
        """
        # Slack Webhook Format (compatible con Discord también)
        message = {
            "text": f"⚠️ *Alerta: {len(orphan_leads)} leads sin asignar*",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"⚠️ {len(orphan_leads)} Leads Sin Asignar",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"Se detectaron *{len(orphan_leads)} leads* del chatbot "
                            f"sin trabajador asignado en las últimas *{hours_window} horas*.\n\n"
                            f"*Acción requerida:* Revisar y asignar manualmente en HubSpot."
                        )
                    }
                },
                {
                    "type": "divider"
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": self._format_leads_list(orphan_leads)
                    }
                }
            ]
        }

        return message

    def _format_leads_list(self, orphan_leads: List[Dict]) -> str:
        """
        Formatea lista de leads para mensaje.

        Args:
            orphan_leads: Lista de leads

        Returns:
            String formateado con leads
        """
        lines = ["*Lista de Leads:*\n"]

        # Mostrar máximo 10 leads
        for i, lead in enumerate(orphan_leads[:10], 1):
            props = lead.get("properties", {})
            name = f"{props.get('firstname', '')} {props.get('lastname', '')}".strip() or "Sin nombre"
            canal = props.get("canal_origen", "desconocido").replace("_", " ").title()
            score = props.get("chatbot_score", "N/A")
            urgency = props.get("chatbot_urgency", "N/A")

            lines.append(
                f"{i}. *{name}* | Canal: {canal} | Score: {score} | Urgencia: {urgency}"
            )

        # Si hay más, indicarlo
        if len(orphan_leads) > 10:
            lines.append(f"\n... y *{len(orphan_leads) - 10} más*")

        return "\n".join(lines)

    def get_cached_orphans(self) -> List[Dict]:
        """
        Obtiene leads huérfanos desde Redis (cache).

        Returns:
            Lista de leads en cache
        """
        if not self._redis_available:
            return []

        try:
            import json

            # Buscar todas las keys de orphan leads
            pattern = f"{self.REDIS_KEY_ORPHANS}:*"
            keys = self.redis.keys(pattern)

            orphans = []
            for key in keys:
                data = self.redis.get(key)
                if data:
                    orphans.append(json.loads(data))

            return orphans

        except Exception as e:
            logger.error(f"[OrphanLeadMonitor] Error leyendo cache: {e}")
            return []


# Instancia global (Singleton)
lead_assigner = LeadAssigner()
orphan_alert_system = OrphanLeadAlert()