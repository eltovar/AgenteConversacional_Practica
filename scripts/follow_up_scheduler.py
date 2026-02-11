# scripts/follow_up_scheduler.py
"""
Revisa los Deals con etapa "visita_realizada" del día anterior
y envía mensajes de seguimiento por WhatsApp para calificar la experiencia.
Características:
- Verifica last_followup_date para evitar duplicados
- Respeta opt-out en comunicaciones_whatsapp
- Obtiene contactos asociados al Deal para extraer nombre y teléfono
- Puede ejecutarse manualmente o programarse con APScheduler/cron

Uso:
    python scripts/follow_up_scheduler.py
    python scripts/follow_up_scheduler.py --dry-run  # Solo muestra qué haría
    python scripts/follow_up_scheduler.py --days 2   # Deals de hace 2 días
"""

import os
import sys
import asyncio
import argparse
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

# Agregar directorio padre al path para imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import httpx
from logging_config import logger


# ============================================================================
# Configuración
# ============================================================================

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY")
BASE_URL = "https://api.hubapi.com"

# Etapa que indica visita realizada (configurar según tu pipeline)
VISIT_COMPLETED_STAGE = os.getenv("HUBSPOT_VISIT_STAGE", "visita_realizada")

# Mensaje de follow-up
FOLLOWUP_MESSAGE_TEMPLATE = """¡Hola {nombre}! 👋

Esperamos que la visita al inmueble haya sido de tu agrado.
¿Cómo calificarías tu experiencia del 1 al 5?

También nos gustaría saber:
- ¿El inmueble cumplió tus expectativas?
- ¿Te gustaría agendar otra visita a este u otro inmueble?

Estamos para ayudarte.
Sofía - Inmobiliaria Proteger"""


@dataclass
class FollowUpCandidate:
    """Candidato para recibir follow-up."""
    deal_id: str
    deal_name: str
    contact_id: str
    firstname: str
    phone: str
    last_followup_date: Optional[str]
    opt_out: bool
    skip_reason: Optional[str] = None


# ============================================================================
# Cliente HubSpot para Follow-up
# ============================================================================

class FollowUpClient:
    """Cliente para operaciones de follow-up."""

    def __init__(self):
        if not HUBSPOT_API_KEY:
            raise ValueError("HUBSPOT_API_KEY no configurada")

        self.headers = {
            "Authorization": f"Bearer {HUBSPOT_API_KEY}",
            "Content-Type": "application/json"
        }

    async def search_deals_for_followup(self, days_ago: int = 1) -> List[Dict[str, Any]]:
        """
        Busca Deals con visita realizada en los últimos N días.

        Args:
            days_ago: Días hacia atrás para buscar (default: 1 = ayer)

        Returns:
            Lista de deals que requieren follow-up
        """
        # Calcular rango de fechas
        now = datetime.now(timezone.utc)
        start_date = (now - timedelta(days=days_ago)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end_date = start_date + timedelta(days=1)

        endpoint = f"{BASE_URL}/crm/v3/objects/deals/search"

        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "dealstage",
                            "operator": "EQ",
                            "value": VISIT_COMPLETED_STAGE
                        },
                        {
                            "propertyName": "hs_lastmodifieddate",
                            "operator": "GTE",
                            "value": str(int(start_date.timestamp() * 1000))
                        },
                        {
                            "propertyName": "hs_lastmodifieddate",
                            "operator": "LT",
                            "value": str(int(end_date.timestamp() * 1000))
                        }
                    ]
                }
            ],
            "properties": [
                "dealname",
                "dealstage",
                "amount",
                "last_followup_date"
            ],
            "limit": 100
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(endpoint, headers=self.headers, json=payload)

            if response.status_code == 200:
                data = response.json()
                deals = data.get("results", [])
                logger.info(f"[FollowUp] Encontrados {len(deals)} deals con visita realizada")
                return deals
            else:
                logger.error(f"[FollowUp] Error buscando deals: {response.status_code} - {response.text}")
                return []

    async def get_deal_contacts(self, deal_id: str) -> List[Dict[str, Any]]:
        """
        Obtiene los contactos asociados a un Deal. Lista de contactos asociados
        """
        endpoint = f"{BASE_URL}/crm/v3/objects/deals/{deal_id}/associations/contacts"

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(endpoint, headers=self.headers)

            if response.status_code == 200:
                data = response.json()
                return data.get("results", [])
            else:
                logger.warning(f"[FollowUp] Error obteniendo contactos del deal {deal_id}: {response.status_code}")
                return []

    async def get_contact_details(self, contact_id: str) -> Optional[Dict[str, Any]]:
        """
        Obtiene detalles de un contacto.

        Args:
            contact_id: ID del contacto

        Returns:
            Datos del contacto o None
        """
        endpoint = f"{BASE_URL}/crm/v3/objects/contacts/{contact_id}"
        params = {
            "properties": "firstname,lastname,phone,whatsapp_id,comunicaciones_whatsapp,last_followup_date"
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(endpoint, headers=self.headers, params=params)

            if response.status_code == 200:
                return response.json()
            else:
                logger.warning(f"[FollowUp] Error obteniendo contacto {contact_id}: {response.status_code}")
                return None

    async def update_last_followup_date(self, contact_id: str) -> bool:
        """
        Actualiza la fecha del último follow-up en el contacto.

        Args:
            contact_id: ID del contacto

        Returns:
            True si se actualizó correctamente
        """
        endpoint = f"{BASE_URL}/crm/v3/objects/contacts/{contact_id}"

        payload = {
            "properties": {
                "last_followup_date": datetime.now(timezone.utc).strftime("%Y-%m-%d")
            }
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.patch(endpoint, headers=self.headers, json=payload)

            if response.status_code == 200:
                logger.info(f"[FollowUp] Actualizado last_followup_date para contacto {contact_id}")
                return True
            else:
                logger.error(f"[FollowUp] Error actualizando contacto {contact_id}: {response.status_code}")
                return False


# ============================================================================
# Lógica principal de Follow-up
# ============================================================================

class FollowUpScheduler:
    """Scheduler de follow-ups automáticos."""

    def __init__(self, dry_run: bool = False):
        """
        Inicializa el scheduler.

        Args:
            dry_run: Si True, solo muestra qué haría sin enviar mensajes
        """
        self.client = FollowUpClient()
        self.dry_run = dry_run

    async def process_followups(self, days_ago: int = 1) -> Dict[str, Any]:
        """
        Procesa todos los follow-ups pendientes.

        Args:
            days_ago: Días hacia atrás para buscar deals

        Returns:
            Resumen de la ejecución
        """
        results = {
            "processed": 0,
            "sent": 0,
            "skipped": 0,
            "errors": 0,
            "details": []
        }

        # 1. Buscar deals con visita realizada
        deals = await self.client.search_deals_for_followup(days_ago)

        if not deals:
            logger.info("[FollowUp] No hay deals para procesar")
            return results

        # 2. Para cada deal, obtener contactos y evaluar
        for deal in deals:
            deal_id = deal["id"]
            deal_name = deal.get("properties", {}).get("dealname", "Sin nombre")

            results["processed"] += 1

            # Obtener contactos asociados
            associations = await self.client.get_deal_contacts(deal_id)

            if not associations:
                logger.warning(f"[FollowUp] Deal {deal_id} sin contactos asociados")
                results["skipped"] += 1
                results["details"].append({
                    "deal_id": deal_id,
                    "deal_name": deal_name,
                    "status": "skipped",
                    "reason": "Sin contactos asociados"
                })
                continue

            # Procesar primer contacto asociado
            contact_assoc = associations[0]
            contact_id = contact_assoc.get("toObjectId") or contact_assoc.get("id")

            if not contact_id:
                continue

            # Obtener detalles del contacto
            contact = await self.client.get_contact_details(contact_id)

            if not contact:
                results["errors"] += 1
                continue

            props = contact.get("properties", {})

            # Crear candidato
            candidate = FollowUpCandidate(
                deal_id=deal_id,
                deal_name=deal_name,
                contact_id=contact_id,
                firstname=props.get("firstname", "Cliente"),
                phone=props.get("whatsapp_id") or props.get("phone", ""),
                last_followup_date=props.get("last_followup_date"),
                opt_out=props.get("comunicaciones_whatsapp", "true").lower() == "false"
            )

            # Evaluar si se debe enviar
            should_send, skip_reason = self._should_send_followup(candidate)

            if not should_send:
                candidate.skip_reason = skip_reason
                results["skipped"] += 1
                results["details"].append({
                    "deal_id": deal_id,
                    "deal_name": deal_name,
                    "contact_id": contact_id,
                    "status": "skipped",
                    "reason": skip_reason
                })
                logger.info(f"[FollowUp] Saltando {candidate.firstname}: {skip_reason}")
                continue

            # Enviar follow-up
            if self.dry_run:
                logger.info(f"[FollowUp] [DRY-RUN] Enviaría follow-up a {candidate.firstname} ({candidate.phone})")
                results["sent"] += 1
                results["details"].append({
                    "deal_id": deal_id,
                    "deal_name": deal_name,
                    "contact_id": contact_id,
                    "phone": candidate.phone,
                    "status": "would_send",
                    "message": "DRY-RUN"
                })
            else:
                success = await self._send_followup(candidate)

                if success:
                    results["sent"] += 1
                    results["details"].append({
                        "deal_id": deal_id,
                        "deal_name": deal_name,
                        "contact_id": contact_id,
                        "phone": candidate.phone,
                        "status": "sent"
                    })
                else:
                    results["errors"] += 1
                    results["details"].append({
                        "deal_id": deal_id,
                        "deal_name": deal_name,
                        "contact_id": contact_id,
                        "status": "error",
                        "reason": "Error enviando mensaje"
                    })

        return results

    def _should_send_followup(self, candidate: FollowUpCandidate) -> tuple[bool, Optional[str]]:
        """
        Evalúa si se debe enviar follow-up a un candidato.

        Args:
            candidate: Candidato a evaluar

        Returns:
            Tupla (should_send, skip_reason)
        """
        # Verificar opt-out
        if candidate.opt_out:
            return False, "Opt-out de comunicaciones WhatsApp"

        # Verificar teléfono
        if not candidate.phone:
            return False, "Sin número de teléfono"

        # Verificar last_followup_date (evitar duplicados)
        if candidate.last_followup_date:
            try:
                last_date = datetime.strptime(candidate.last_followup_date, "%Y-%m-%d").date()
                today = datetime.now(timezone.utc).date()

                # No enviar si ya se envió en los últimos 7 días
                if (today - last_date).days < 7:
                    return False, f"Follow-up enviado recientemente ({candidate.last_followup_date})"
            except ValueError:
                pass  # Formato inválido, ignorar

        return True, None

    async def _send_followup(self, candidate: FollowUpCandidate) -> bool:
        """
        Envía el mensaje de follow-up.

        Args:
            candidate: Candidato al que enviar

        Returns:
            True si se envió correctamente
        """
        try:
            # Importar cliente Twilio
            from utils.twilio_client import twilio_client

            if not twilio_client.is_available:
                logger.error("[FollowUp] Twilio no disponible")
                return False

            # Formatear mensaje
            message = FOLLOWUP_MESSAGE_TEMPLATE.format(nombre=candidate.firstname)

            # Enviar
            result = await twilio_client.send_whatsapp_message(
                to=candidate.phone,
                body=message
            )

            if result["status"] == "success":
                logger.info(f"[FollowUp] Follow-up enviado a {candidate.firstname} ({candidate.phone})")

                # Actualizar last_followup_date
                await self.client.update_last_followup_date(candidate.contact_id)

                return True
            else:
                logger.error(f"[FollowUp] Error enviando a {candidate.phone}: {result}")
                return False

        except Exception as e:
            logger.error(f"[FollowUp] Error en _send_followup: {e}")
            return False


# ============================================================================
# CLI
# ============================================================================

async def main():
    parser = argparse.ArgumentParser(
        description="Ejecuta follow-ups automáticos para visitas realizadas"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Solo muestra qué haría sin enviar mensajes"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="Días hacia atrás para buscar deals (default: 1 = ayer)"
    )

    args = parser.parse_args()

    print("=" * 60)
    print("Follow-Up Scheduler - Inmobiliaria Proteger")
    print("=" * 60)

    if args.dry_run:
        print("MODO: DRY-RUN (no se enviarán mensajes)")
    print(f"Buscando deals de hace {args.days} día(s)...")
    print()

    scheduler = FollowUpScheduler(dry_run=args.dry_run)
    results = await scheduler.process_followups(days_ago=args.days)

    print()
    print("=" * 60)
    print("RESUMEN")
    print("=" * 60)
    print(f"Deals procesados: {results['processed']}")
    print(f"Follow-ups enviados: {results['sent']}")
    print(f"Saltados: {results['skipped']}")
    print(f"Errores: {results['errors']}")

    if results["details"]:
        print()
        print("DETALLES:")
        for detail in results["details"]:
            status_icon = "✅" if detail["status"] == "sent" else "⏭️" if detail["status"] == "skipped" else "❌"
            print(f"  {status_icon} Deal {detail['deal_id']}: {detail.get('reason', detail['status'])}")


if __name__ == "__main__":
    asyncio.run(main())