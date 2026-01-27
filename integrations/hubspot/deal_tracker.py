# integrations/hubspot/deal_tracker.py
"""
Sistema de seguimiento automático de etapas de Deals.
Monitorea actividad de contactos y actualiza el Deal stage automáticamente.
"""

import os
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone, timedelta
from logging_config import logger


class DealStageTracker:
    """
    Monitorea actividad en contactos y actualiza etapas de deals automáticamente.

    Flujo típico:
    1. Nuevo lead → Deal creado en "Nuevo Lead"
    2. Trabajador responde → Deal se mueve a "En Conversación"
    3. Se agenda visita → Deal se mueve a "Visita Agendada"
    4. Se completa visita → Deal se mueve a "Visita Realizada"
    """

    # ═══════════════════════════════════════════════════════════════════════════
    # CONFIGURACIÓN DE ETAPAS
    # ═══════════════════════════════════════════════════════════════════════════

    # IDs de las etapas del pipeline (obtener de HubSpot Settings > Objects > Deals > Pipelines)
    # Para obtener IDs:
    # 1. Ve a Settings > Objects > Deals > Pipelines
    # 2. Selecciona tu pipeline
    # 3. Los IDs están en la URL o puedes usar la API:
    #    GET https://api.hubapi.com/crm/v3/pipelines/deals

    STAGE_IDS = {
        "nuevo_lead": os.getenv("HUBSPOT_DEAL_STAGE", "appointmentscheduled"),  # Default desde .env
        "en_conversacion": "1275156340",  # TODO: Reemplazar con ID real de HubSpot
        "visita_agendada": "1275156341",  # TODO: Reemplazar con ID real
        "visita_realizada": "1275156342",  # TODO: Reemplazar con ID real
        "propuesta_enviada": "1275156343",  # TODO: Reemplazar con ID real
        "negociacion": "1275156344",  # TODO: Reemplazar con ID real
        "ganado": "closedwon",  # ID estándar de HubSpot
        "perdido": "closedlost",  # ID estándar de HubSpot
    }

    # Tipos de actividad que consideramos como "interacción activa"
    ACTIVITY_TYPES_ENGAGEMENT = ["CALL", "EMAIL", "MEETING", "NOTE", "TASK"]

    # Palabras clave que indican agendamiento de visita (en notas o emails)
    VISIT_KEYWORDS = [
        "visita", "agendar", "agendada", "cita", "reunión",
        "ver el inmueble", "conocer la propiedad", "mostrar"
    ]

    def __init__(self, hubspot_client):
        """
        Inicializa el tracker con cliente HubSpot.

        Args:
            hubspot_client: Instancia de HubSpotClient
        """
        self.hubspot = hubspot_client
        logger.info("[DealStageTracker] Inicializado")

    async def check_and_update_stage(
        self,
        deal_id: str,
        contact_id: str,
        force_check: bool = False
    ) -> Optional[str]:
        """
        Verifica si hay actividad reciente y actualiza la etapa del deal si corresponde.

        Args:
            deal_id: ID del deal en HubSpot
            contact_id: ID del contacto asociado
            force_check: Si es True, verifica incluso si el deal ya fue actualizado hoy

        Returns:
            Nuevo stage_id si hubo cambio, None si no se actualizó
        """
        try:
            # 1. Obtener etapa actual del deal
            current_stage = await self._get_deal_stage(deal_id)
            logger.info(f"[DealStageTracker] Deal {deal_id} en etapa: {current_stage}")

            # 2. Solo actualizar si está en "Nuevo Lead"
            if current_stage != self.STAGE_IDS["nuevo_lead"]:
                logger.debug(f"[DealStageTracker] Deal {deal_id} ya no está en 'Nuevo Lead', omitiendo")
                return None

            # 3. Verificar actividades recientes del contacto
            has_activity = await self._has_recent_activity(contact_id, hours=24)

            if has_activity:
                # Hay actividad → mover a "En Conversación"
                new_stage = self.STAGE_IDS["en_conversacion"]
                await self._update_deal_stage(deal_id, new_stage)
                logger.info(f"[DealStageTracker] ✅ Deal {deal_id} movido: Nuevo Lead → En Conversación")
                return new_stage

            logger.debug(f"[DealStageTracker] No hay actividad reciente para deal {deal_id}")
            return None

        except Exception as e:
            logger.error(f"[DealStageTracker] Error verificando deal {deal_id}: {e}", exc_info=True)
            return None

    async def check_for_scheduled_visit(
        self,
        deal_id: str,
        contact_id: str
    ) -> bool:
        """
        Verifica si se agendó una visita y actualiza a "Visita Agendada".

        Args:
            deal_id: ID del deal
            contact_id: ID del contacto

        Returns:
            True si se detectó visita agendada y se actualizó
        """
        try:
            current_stage = await self._get_deal_stage(deal_id)

            # Solo actualizar si está en "En Conversación"
            if current_stage != self.STAGE_IDS["en_conversacion"]:
                return False

            # Buscar actividades que mencionen visita
            activities = await self._get_contact_activities(
                contact_id,
                activity_types=["NOTE", "EMAIL", "MEETING"],
                since_hours=48
            )

            for activity in activities:
                content = activity.get("body", "").lower()
                if any(keyword in content for keyword in self.VISIT_KEYWORDS):
                    # Encontramos mención de visita → actualizar
                    new_stage = self.STAGE_IDS["visita_agendada"]
                    await self._update_deal_stage(deal_id, new_stage)
                    logger.info(f"[DealStageTracker] ✅ Deal {deal_id} movido: En Conversación → Visita Agendada")
                    return True

            return False

        except Exception as e:
            logger.error(f"[DealStageTracker] Error verificando visita para deal {deal_id}: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════════════════
    # MÉTODOS PRIVADOS - INTERACCIÓN CON HUBSPOT API
    # ═══════════════════════════════════════════════════════════════════════════

    async def _get_deal_stage(self, deal_id: str) -> Optional[str]:
        """
        Obtiene la etapa actual de un deal.

        Args:
            deal_id: ID del deal

        Returns:
            stage_id del deal o None si falla
        """
        try:
            endpoint = f"/crm/v3/objects/deals/{deal_id}"
            params = {"properties": "dealstage"}

            # Usar método _request del cliente HubSpot
            response = await self.hubspot._request("GET", f"{endpoint}?properties=dealstage")
            stage_id = response.get("properties", {}).get("dealstage")

            return stage_id

        except Exception as e:
            logger.error(f"[DealStageTracker] Error obteniendo etapa de deal {deal_id}: {e}")
            return None

    async def _update_deal_stage(self, deal_id: str, new_stage_id: str) -> bool:
        """
        Actualiza la etapa de un deal.

        Args:
            deal_id: ID del deal
            new_stage_id: Nuevo stage_id

        Returns:
            True si se actualizó correctamente
        """
        try:
            endpoint = f"/crm/v3/objects/deals/{deal_id}"
            payload = {
                "properties": {
                    "dealstage": new_stage_id
                }
            }

            await self.hubspot._request("PATCH", endpoint, payload)
            logger.info(f"[DealStageTracker] Deal {deal_id} actualizado a etapa {new_stage_id}")
            return True

        except Exception as e:
            logger.error(f"[DealStageTracker] Error actualizando deal {deal_id}: {e}", exc_info=True)
            return False

    async def _has_recent_activity(self, contact_id: str, hours: int = 24) -> bool:
        """
        Verifica si un contacto tiene actividad reciente.

        Args:
            contact_id: ID del contacto
            hours: Ventana de tiempo en horas

        Returns:
            True si hay actividad en la ventana de tiempo
        """
        try:
            activities = await self._get_contact_activities(
                contact_id,
                activity_types=self.ACTIVITY_TYPES_ENGAGEMENT,
                since_hours=hours
            )

            return len(activities) > 0

        except Exception as e:
            logger.error(f"[DealStageTracker] Error verificando actividad de contacto {contact_id}: {e}")
            return False

    async def _get_contact_activities(
        self,
        contact_id: str,
        activity_types: List[str],
        since_hours: int = 24
    ) -> List[Dict[str, Any]]:
        """
        Obtiene actividades recientes de un contacto.

        Args:
            contact_id: ID del contacto
            activity_types: Lista de tipos de actividad (CALL, EMAIL, NOTE, etc.)
            since_hours: Ventana de tiempo en horas

        Returns:
            Lista de actividades
        """
        try:
            # Calcular timestamp de inicio
            since_timestamp = int(
                (datetime.now(timezone.utc) - timedelta(hours=since_hours)).timestamp() * 1000
            )

            # Endpoint de engagements (actividades)
            endpoint = "/crm/v3/objects/contacts/{}/associations/engagements".format(contact_id)

            # Nota: HubSpot v3 API tiene limitaciones en filtrado por fecha
            # Alternativa: usar v1 API o implementar filtrado post-fetch
            response = await self.hubspot._request("GET", endpoint)

            # TODO: Implementar filtrado por timestamp y tipo
            # Por ahora retornamos todas las actividades
            results = response.get("results", [])

            logger.debug(f"[DealStageTracker] Contacto {contact_id}: {len(results)} actividades encontradas")
            return results

        except Exception as e:
            logger.error(f"[DealStageTracker] Error obteniendo actividades de {contact_id}: {e}")
            return []

    def get_stage_name(self, stage_id: str) -> str:
        """
        Retorna el nombre legible de una etapa dado su ID.

        Args:
            stage_id: ID de la etapa

        Returns:
            Nombre de la etapa o el ID si no se encuentra
        """
        for name, sid in self.STAGE_IDS.items():
            if sid == stage_id:
                return name.replace("_", " ").title()
        return stage_id


# ═══════════════════════════════════════════════════════════════════════════
# FUNCIONES DE UTILIDAD
# ═══════════════════════════════════════════════════════════════════════════

async def auto_update_deal_stages_batch(
    tracker: DealStageTracker,
    deal_contact_pairs: List[tuple]
) -> Dict[str, int]:
    """
    Actualiza etapas de múltiples deals en batch.

    Args:
        tracker: Instancia de DealStageTracker
        deal_contact_pairs: Lista de tuplas (deal_id, contact_id)

    Returns:
        Diccionario con estadísticas de actualización
    """
    stats = {
        "checked": 0,
        "updated": 0,
        "errors": 0
    }

    for deal_id, contact_id in deal_contact_pairs:
        stats["checked"] += 1

        try:
            result = await tracker.check_and_update_stage(deal_id, contact_id)
            if result:
                stats["updated"] += 1
        except Exception:
            stats["errors"] += 1

    logger.info(
        f"[DealStageTracker] Batch completado: "
        f"{stats['checked']} verificados, {stats['updated']} actualizados, {stats['errors']} errores"
    )

    return stats