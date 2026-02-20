# integrations/hubspot/lead_counter.py
"""
Sistema de conteo de leads pendientes por trabajador.
Genera notificaciones tipo "Tienes 4 nuevos leads por responder"
"""

from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from logging_config import logger


class LeadCounter:
    """
    Cuenta leads sin responder agrupados por trabajador.
    """

    # Emojis para cada canal
    CANAL_EMOJIS = {
        "whatsapp_directo": "📱",
        "instagram": "📸",
        "facebook": "👤",
        "finca_raiz": "🏠",
        "metrocuadrado": "📐",
        "mercado_libre": "🛒",
        "ciencuadras": "🔲",
        "pagina_web": "🌐",
        "google_ads": "🔍",
        "referido": "🤝",
        "desconocido": "📌"
    }

    def __init__(self, hubspot_client):
        """
        Inicializa el contador con cliente HubSpot.

        Args:
            hubspot_client: Instancia de HubSpotClient
        """
        self.hubspot = hubspot_client
        logger.info("[LeadCounter] Inicializado")

    async def get_pending_leads_count(
        self,
        owner_id: str,
        hours_window: int = 24
    ) -> Dict[str, Any]:
        """
        Obtiene conteo de leads pendientes para un trabajador.
        """
        try:
            # Calcular timestamp límite (hace X horas)
            cutoff_timestamp = int(
                (datetime.now(timezone.utc) - timedelta(hours=hours_window)).timestamp() * 1000
            )

            # Buscar contactos con las siguientes condiciones:
            # 1. Asignados a este owner
            # 2. Tienen chatbot_timestamp (son del chatbot)
            # 3. chatbot_timestamp es reciente (últimas X horas)
            # 4. Sin actividad registrada por el trabajador

            leads = await self._search_pending_leads(owner_id, cutoff_timestamp)

            # Agrupar por canal
            por_canal = {}
            for lead in leads:
                canal = lead.get("canal", "desconocido")
                por_canal[canal] = por_canal.get(canal, 0) + 1

            logger.info(
                f"[LeadCounter] Owner {owner_id}: {len(leads)} leads pendientes "
                f"en las últimas {hours_window}h"
            )

            return {
                "total": len(leads),
                "por_canal": por_canal,
                "leads": leads
            }

        except Exception as e:
            logger.error(f"[LeadCounter] Error obteniendo leads pendientes para {owner_id}: {e}")
            return {
                "total": 0,
                "por_canal": {},
                "leads": [],
                "error": str(e)
            }

    async def get_unassigned_leads_count(
        self,
        hours_window: int = 168  # 7 días por defecto
    ) -> Dict[str, Any]:
        """
        Obtiene conteo de leads huérfanos (sin owner asignado).
        """
        try:
            cutoff_timestamp = int(
                (datetime.now(timezone.utc) - timedelta(hours=hours_window)).timestamp() * 1000
            )

            # Buscar contactos sin owner que tengan chatbot_timestamp
            leads = await self._search_unassigned_leads(cutoff_timestamp)

            # Agrupar por canal
            por_canal = {}
            for lead in leads:
                canal = lead.get("canal", "desconocido")
                por_canal[canal] = por_canal.get(canal, 0) + 1

            logger.warning(
                f"[LeadCounter] ⚠️ {len(leads)} leads sin asignar "
                f"en las últimas {hours_window}h"
            )

            return {
                "total": len(leads),
                "por_canal": por_canal,
                "leads": leads
            }

        except Exception as e:
            logger.error(f"[LeadCounter] Error obteniendo leads sin asignar: {e}")
            return {
                "total": 0,
                "por_canal": {},
                "leads": [],
                "error": str(e)
            }

    async def generate_notification_message(
        self,
        owner_id: str,
        hours_window: int = 24
    ) -> str:
        """
        Genera mensaje de notificación para el trabajador.
        """
        data = await self.get_pending_leads_count(owner_id, hours_window)

        if data["total"] == 0:
            return "✅ No tienes leads pendientes por responder"

        # Construir mensaje
        plural_leads = "s" if data["total"] > 1 else ""
        msg = (
            f"🔔 **Tienes {data['total']} nuevo{plural_leads} "
            f"lead{plural_leads} por responder**\n\n"
        )

        # Agregar desglose por canal
        for canal, count in sorted(data["por_canal"].items(), key=lambda x: x[1], reverse=True):
            emoji = self._get_canal_emoji(canal)
            canal_name = canal.replace('_', ' ').title()
            plural_canal = "s" if count > 1 else ""
            msg += f"{emoji} {canal_name}: {count} lead{plural_canal}\n"

        msg += f"\n⏰ Leads recibidos en las últimas {hours_window} horas"

        return msg

    async def generate_unassigned_alert(
        self,
        hours_window: int = 168
    ) -> str:
        """
        Genera alerta de leads sin asignar para administradores.

        Args:
            hours_window: Ventana de tiempo en horas

        Returns:
            Mensaje de alerta formateado
        """
        data = await self.get_unassigned_leads_count(hours_window)

        if data["total"] == 0:
            return "✅ No hay leads sin asignar"

        # Construir mensaje de alerta
        plural = "s" if data["total"] > 1 else ""
        msg = (
            f"⚠️ **ALERTA: {data['total']} lead{plural} sin asignar**\n\n"
            f"Estos leads no tienen trabajador asignado:\n\n"
        )

        # Desglose por canal
        for canal, count in sorted(data["por_canal"].items(), key=lambda x: x[1], reverse=True):
            emoji = self._get_canal_emoji(canal)
            canal_name = canal.replace('_', ' ').title()
            msg += f"{emoji} {canal_name}: {count}\n"

        msg += (
            f"\n⏰ Recibidos en los últimos {hours_window // 24} días\n"
            f"🔧 Acción requerida: Asignar manualmente en HubSpot"
        )

        return msg

    # ═══════════════════════════════════════════════════════════════════════════
    # MÉTODOS PRIVADOS - BÚSQUEDA EN HUBSPOT
    # ═══════════════════════════════════════════════════════════════════════════

    async def _search_pending_leads(
        self,
        owner_id: str,
        cutoff_timestamp: int
    ) -> List[Dict[str, Any]]:
        """
        Busca leads pendientes de respuesta para un owner específico.
        """
        try:
            # Construir filtros para búsqueda
            filters = {
                "filterGroups": [
                    {
                        "filters": [
                            {
                                "propertyName": "hubspot_owner_id",
                                "operator": "EQ",
                                "value": owner_id
                            },
                            {
                                "propertyName": "chatbot_timestamp",
                                "operator": "GTE",
                                "value": cutoff_timestamp
                            },
                            {
                                "propertyName": "hs_lead_status",
                                "operator": "NEQ",
                                "value": "OPEN"  # Excluir leads ya abiertos
                            }
                        ]
                    }
                ],
                "properties": [
                    "firstname",
                    "lastname",
                    "canal_origen",
                    "chatbot_timestamp",
                    "phone",
                    "chatbot_location",
                    "chatbot_urgency"
                ],
                "limit": 100
            }

            # Ejecutar búsqueda
            endpoint = "/crm/v3/objects/contacts/search"
            response = await self.hubspot._request("POST", endpoint, filters)

            # Procesar resultados
            leads = []
            for contact in response.get("results", []):
                props = contact.get("properties", {})
                leads.append({
                    "id": contact["id"],
                    "name": f"{props.get('firstname', '')} {props.get('lastname', '')}".strip(),
                    "canal": props.get("canal_origen", "desconocido"),
                    "timestamp": props.get("chatbot_timestamp", ""),
                    "phone": props.get("phone", ""),
                    "location": props.get("chatbot_location", ""),
                    "urgency": props.get("chatbot_urgency", "")
                })

            return leads

        except Exception as e:
            logger.error(f"[LeadCounter] Error en búsqueda de leads pendientes: {e}")
            return []

    async def _search_unassigned_leads(
        self,
        cutoff_timestamp: int
    ) -> List[Dict[str, Any]]:
        """
        Busca leads sin owner asignado (huérfanos).
        """
        try:
            # Filtros para leads sin owner
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
                                "operator": "GTE",
                                "value": cutoff_timestamp
                            }
                        ]
                    }
                ],
                "properties": [
                    "firstname",
                    "lastname",
                    "canal_origen",
                    "chatbot_timestamp",
                    "phone"
                ],
                "limit": 100
            }

            # Ejecutar búsqueda
            endpoint = "/crm/v3/objects/contacts/search"
            response = await self.hubspot._request("POST", endpoint, filters)

            # Procesar resultados
            leads = []
            for contact in response.get("results", []):
                props = contact.get("properties", {})
                leads.append({
                    "id": contact["id"],
                    "name": f"{props.get('firstname', '')} {props.get('lastname', '')}".strip(),
                    "canal": props.get("canal_origen", "desconocido"),
                    "timestamp": props.get("chatbot_timestamp", ""),
                    "phone": props.get("phone", "")
                })

            return leads

        except Exception as e:
            logger.error(f"[LeadCounter] Error en búsqueda de leads sin asignar: {e}")
            return []

    def _get_canal_emoji(self, canal: str) -> str:
        """
        Retorna el emoji correspondiente a un canal.

        Args:
            canal: Nombre del canal

        Returns:
            Emoji string
        """
        return self.CANAL_EMOJIS.get(canal, "📌")


# ═══════════════════════════════════════════════════════════════════════════
# FUNCIONES DE UTILIDAD
# ═══════════════════════════════════════════════════════════════════════════

async def generate_daily_summary(
    counter: LeadCounter,
    owner_ids: List[str]
) -> Dict[str, str]:
    """
    Genera resumen diario de leads pendientes para múltiples trabajadores.

    Args:
        counter: Instancia de LeadCounter
        owner_ids: Lista de IDs de owners

    Returns:
        Diccionario {owner_id: mensaje_notificacion}
    """
    summaries = {}

    for owner_id in owner_ids:
        try:
            message = await counter.generate_notification_message(owner_id, hours_window=24)
            summaries[owner_id] = message
        except Exception as e:
            logger.error(f"[LeadCounter] Error generando resumen para {owner_id}: {e}")
            summaries[owner_id] = f"❌ Error generando resumen: {e}"

    return summaries


async def check_orphan_leads_threshold(
    counter: LeadCounter,
    threshold: int = 5
) -> Optional[str]:
    """
    Verifica si el número de leads huérfanos supera un umbral.
    """
    data = await counter.get_unassigned_leads_count(hours_window=168)  # 7 días

    if data["total"] >= threshold:
        return await counter.generate_unassigned_alert(hours_window=168)

    return None