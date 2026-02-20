# integrations/hubspot/hubspot_client.py
"""
Cliente HTTP para HubSpot CRM API v3.
Maneja autenticación, retry logic y deduplicación de contactos.
"""

import os
import httpx
from typing import Optional, Dict, Any
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from logging_config import logger


class HubSpotClient:
    """
    Cliente asíncrono para interactuar con HubSpot CRM API v3.
    """

    def __init__(self):
        """
        Inicializa el cliente HubSpot.

        Variables de entorno requeridas:
        - HUBSPOT_API_KEY: Token de acceso privado
        - HUBSPOT_PIPELINE_ID: ID del pipeline de ventas
        - HUBSPOT_DEAL_STAGE: ID de la etapa inicial del deal
        """
        self.api_key = os.getenv("HUBSPOT_API_KEY")
        self.base_url = "https://api.hubapi.com"
        self.pipeline_id = os.getenv("HUBSPOT_PIPELINE_ID", "default")
        self.deal_stage = os.getenv("HUBSPOT_DEAL_STAGE", "appointmentscheduled")

        if not self.api_key:
            raise ValueError("HUBSPOT_API_KEY no está configurada en .env")

        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        logger.info("[HubSpotClient] Inicializado correctamente")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError))
    )
    async def _request(self, method: str, endpoint: str, json_data: Optional[dict] = None) -> Dict[str, Any]:
        """
        Wrapper interno para requests HTTP con retry logic.
        """
        async with httpx.AsyncClient(timeout=15.0) as client:
            url = f"{self.base_url}{endpoint}"
            try:
                response = await client.request(method, url, headers=self.headers, json=json_data)
                response.raise_for_status()

                # Si es 204 No Content, retornar vacío
                if response.status_code == 204:
                    return {}

                return response.json()

            except httpx.HTTPStatusError as e:
                # Rate limit (429): Convertir a NetworkError para forzar retry
                if e.response.status_code == 429:
                    logger.warning("[HubSpotClient] Rate limit alcanzado (429), reintentando...")
                    raise httpx.NetworkError("Rate Limit Exceeded", request=e.request)

                # Errores de cliente (4xx): NO reintentar
                if 400 <= e.response.status_code < 500:
                    logger.error(f"[HubSpotClient] Client Error {e.response.status_code}: {e.response.text}")
                    raise e  # Romper el retry

                # Errores de servidor (5xx): Reintentar
                logger.error(f"[HubSpotClient] Server Error {e.response.status_code}: {e.response.text}")
                raise e

    async def search_contact_by_phone(self, phone: str) -> Optional[str]:
        """
        Busca ID de contacto usando whatsapp_id como identificador único.
        """
        endpoint = "/crm/v3/objects/contacts/batch/read"
        payload = {
            "properties": ["id", "firstname"],
            "idProperty": "whatsapp_id",
            "inputs": [{"id": phone}]
        }

        try:
            response = await self._request("POST", endpoint, payload)
            results = response.get("results", [])

            if results:
                contact_id = results[0]["id"]
                logger.info(f"[HubSpotClient] Contacto encontrado: {contact_id} (whatsapp_id: {phone})")
                return contact_id

            logger.info(f"[HubSpotClient] No se encontró contacto con whatsapp_id: {phone}")
            return None

        except Exception as e:
            logger.error(f"[HubSpotClient] Error buscando contacto: {e}", exc_info=True)
            return None

    async def search_contacts_by_email(self, email: str) -> Dict[str, Any]:
        """
        Busca contactos por email usando la API de búsqueda.
        Retorna el resultado completo de la búsqueda.
        """
        endpoint = "/crm/v3/objects/contacts/search"
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "email",
                            "operator": "EQ",
                            "value": email
                        }
                    ]
                }
            ],
            "properties": ["id", "firstname", "lastname"]
        }

        try:
            response = await self._request("POST", endpoint, payload)
            logger.info(f"[HubSpotClient] Búsqueda por email '{email}' ejecutada correctamente")
            return response

        except Exception as e:
            logger.error(f"[HubSpotClient] Error buscando contactos por email: {e}", exc_info=True)
            raise

    async def create_contact(self, properties: Dict[str, Any]) -> str:
        """
        Crea un nuevo contacto en HubSpot.
        """
        endpoint = "/crm/v3/objects/contacts"
        response = await self._request("POST", endpoint, {"properties": properties})
        contact_id = response["id"]
        logger.info(f"[HubSpotClient] Contacto creado: {contact_id}")
        return contact_id

    async def update_contact(self, contact_id: str, properties: Dict[str, Any]) -> None:
        """
        Actualiza un contacto existente.
        """
        endpoint = f"/crm/v3/objects/contacts/{contact_id}"
        await self._request("PATCH", endpoint, {"properties": properties})
        logger.info(f"[HubSpotClient] Contacto actualizado: {contact_id}")

    async def create_deal(
        self,
        contact_id: str,
        properties: Dict[str, Any],
        pipeline_id: Optional[str] = None,
        dealstage: Optional[str] = None
    ) -> str:
        """
        Crea un Deal (oportunidad) y lo asocia automáticamente al contacto.
        """
        endpoint = "/crm/v3/objects/deals"

        # Agregar campos obligatorios del pipeline (usar parámetros o defaults)
        properties["pipeline"] = pipeline_id or self.pipeline_id
        properties["dealstage"] = dealstage or self.deal_stage

        payload = {
            "properties": properties,
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 3  # 3 = Deal asociado a Contact
                        }
                    ]
                }
            ]
        }

        response = await self._request("POST", endpoint, payload)
        deal_id = response["id"]
        logger.info(f"[HubSpotClient] Deal creado: {deal_id} (asociado a contacto {contact_id})")
        return deal_id

    async def create_note(
        self,
        contact_id: str,
        body: str,
        owner_id: Optional[str] = None,
        timestamp: Optional[str] = None
    ) -> str:
        """
        Crea una nota en HubSpot y la asocia a un contacto.
        """
        from datetime import datetime, timezone

        endpoint = "/crm/v3/objects/notes"

        # Preparar propiedades de la nota
        properties = {
            "hs_note_body": body,
            "hs_timestamp": timestamp or datetime.now(timezone.utc).isoformat()
        }

        # Agregar owner si se proporciona
        if owner_id:
            properties["hubspot_owner_id"] = owner_id

        payload = {
            "properties": properties,
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 202  # 202 = Note asociada a Contact
                        }
                    ]
                }
            ]
        }

        try:
            response = await self._request("POST", endpoint, payload)
            note_id = response.get("id")
            logger.info(f"[HubSpotClient] Nota creada: {note_id} (asociada a contacto {contact_id})")
            return note_id

        except Exception as e:
            logger.error(f"[HubSpotClient] Error creando nota para contacto {contact_id}: {e}")
            raise

    async def get_contact(self, contact_id: str, properties: Optional[list] = None) -> Dict[str, Any]:
        """
        Obtiene los datos de un contacto por su ID.
        """
        endpoint = f"/crm/v3/objects/contacts/{contact_id}"

        if properties:
            # Agregar propiedades como query params
            props_str = ",".join(properties)
            endpoint += f"?properties={props_str}"

        try:
            response = await self._request("GET", endpoint)
            logger.debug(f"[HubSpotClient] Contacto obtenido: {contact_id}")
            return response

        except Exception as e:
            logger.error(f"[HubSpotClient] Error obteniendo contacto {contact_id}: {e}")
            raise