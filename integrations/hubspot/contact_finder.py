# integrations/hubspot/contact_finder.py
"""
Módulo para búsqueda robusta de contactos en HubSpot por número de teléfono.

Características:
- Búsqueda en múltiples propiedades (phone, mobilephone, hs_whatsapp_phone_number, whatsapp_id)
- Caché en Redis para evitar llamadas repetidas (TTL 1 hora)
- Normalización de formatos de teléfono
- Fallback a creación de contacto si no existe
"""

import os
import json
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from datetime import datetime

import httpx
import redis
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from logging_config import logger


@dataclass
class ContactInfo:
    """Información del contacto encontrado en HubSpot."""
    vid: str  # ID único del contacto en HubSpot
    firstname: Optional[str] = None
    lastname: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    whatsapp_id: Optional[str] = None
    sofia_status: Optional[str] = None  # "activa" | "pausada"
    found_by: Optional[str] = None  # Propiedad donde se encontró el match


class ContactFinder:
    """
    Buscador de contactos en HubSpot con caché en Redis.
    """

    # Propiedades a buscar en orden de prioridad
    SEARCH_PROPERTIES = [
        "whatsapp_id",
        "hs_whatsapp_phone_number",
        "mobilephone",
        "phone"
    ]

    # Propiedades a retornar del contacto
    RETURN_PROPERTIES = [
        "firstname",
        "lastname",
        "email",
        "phone",
        "mobilephone",
        "whatsapp_id",
        "hs_whatsapp_phone_number",
        "sofia_status"  # Propiedad personalizada para estado de handoff
    ]

    # TTL del caché en segundos (1 hora)
    CACHE_TTL = 3600

    def __init__(self, redis_url: Optional[str] = None):
        """
        Inicializa el buscador de contactos.
        """
        self.api_key = os.getenv("HUBSPOT_API_KEY")
        self.base_url = "https://api.hubapi.com"

        if not self.api_key:
            raise ValueError("HUBSPOT_API_KEY no está configurada")

        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        # Configurar Redis para caché
        self.redis_url = redis_url or os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL")
        self._redis_client: Optional[redis.Redis] = None

    def _get_redis(self) -> Optional[redis.Redis]:
        """Obtiene el cliente Redis con lazy initialization."""
        if self._redis_client is None and self.redis_url:
            try:
                self._redis_client = redis.from_url(self.redis_url)
                self._redis_client.ping()
            except Exception as e:
                logger.warning(f"[ContactFinder] Redis no disponible para caché: {e}")
                self._redis_client = None
        return self._redis_client

    def _cache_key(self, phone: str) -> str:
        """Genera la clave de caché para un número de teléfono."""
        return f"hubspot:contact:phone:{phone}"

    def _get_from_cache(self, phone: str) -> Optional[ContactInfo]:
        """Intenta obtener el contacto del caché."""
        redis_client = self._get_redis()
        if not redis_client:
            return None

        try:
            cached = redis_client.get(self._cache_key(phone))
            if cached:
                data = json.loads(cached)
                logger.debug(f"[ContactFinder] Cache HIT para {phone}")
                return ContactInfo(**data)
        except Exception as e:
            logger.warning(f"[ContactFinder] Error leyendo caché: {e}")

        return None

    def _save_to_cache(self, phone: str, contact: ContactInfo) -> None:
        """Guarda el contacto en caché."""
        redis_client = self._get_redis()
        if not redis_client:
            return

        try:
            data = {
                "vid": contact.vid,
                "firstname": contact.firstname,
                "lastname": contact.lastname,
                "email": contact.email,
                "phone": contact.phone,
                "whatsapp_id": contact.whatsapp_id,
                "sofia_status": contact.sofia_status,
                "found_by": contact.found_by
            }
            redis_client.setex(
                self._cache_key(phone),
                self.CACHE_TTL,
                json.dumps(data)
            )
            logger.debug(f"[ContactFinder] Contacto guardado en caché: {phone} -> {contact.vid}")
        except Exception as e:
            logger.warning(f"[ContactFinder] Error guardando en caché: {e}")

    def _invalidate_cache(self, phone: str) -> None:
        """Invalida el caché para un número de teléfono."""
        redis_client = self._get_redis()
        if redis_client:
            try:
                redis_client.delete(self._cache_key(phone))
            except Exception:
                pass

    def _generate_phone_variants(self, phone_e164: str) -> List[str]:
        """
        Genera variantes del número para búsqueda.
        """
        variants = [phone_e164]

        # Sin el +
        if phone_e164.startswith("+"):
            without_plus = phone_e164[1:]
            variants.append(without_plus)

            # Sin código de país (asumiendo Colombia +57)
            if without_plus.startswith("57"):
                national = without_plus[2:]
                variants.append(national)
                variants.append(f"0{national}")  # Formato con 0 inicial

        return variants

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError))
    )
    async def _search_by_property(
        self,
        property_name: str,
        value: str
    ) -> Optional[Dict[str, Any]]:
        """
        Busca un contacto por una propiedad específica.
        """
        endpoint = f"{self.base_url}/crm/v3/objects/contacts/search"

        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": property_name,
                            "operator": "EQ",
                            "value": value
                        }
                    ]
                }
            ],
            "properties": self.RETURN_PROPERTIES,
            "limit": 1
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(endpoint, headers=self.headers, json=payload)

            if response.status_code == 200:
                data = response.json()
                results = data.get("results", [])

                if results:
                    return results[0]

            elif response.status_code == 429:
                logger.warning("[ContactFinder] Rate limit alcanzado")
                raise httpx.NetworkError("Rate Limit", request=response.request)

            elif response.status_code >= 400:
                logger.warning(
                    f"[ContactFinder] Error buscando por {property_name}: "
                    f"{response.status_code} - {response.text}"
                )

        return None

    async def find_by_phone(
        self,
        phone_e164: str,
        use_cache: bool = True
    ) -> Optional[ContactInfo]:
        """
        Busca un contacto por número de teléfono.
        """
        # 1. Intentar desde caché
        if use_cache:
            cached = self._get_from_cache(phone_e164)
            if cached:
                return cached

        # 2. Generar variantes del número
        phone_variants = self._generate_phone_variants(phone_e164)
        logger.info(f"[ContactFinder] Buscando contacto con variantes: {phone_variants}")

        # 3. Buscar en cada propiedad con cada variante
        for prop in self.SEARCH_PROPERTIES:
            for variant in phone_variants:
                try:
                    result = await self._search_by_property(prop, variant)

                    if result:
                        contact = self._parse_contact_result(result, found_by=prop)
                        logger.info(
                            f"[ContactFinder] Contacto encontrado: vid={contact.vid} "
                            f"(por {prop}={variant})"
                        )

                        # Guardar en caché
                        if use_cache:
                            self._save_to_cache(phone_e164, contact)

                        return contact

                except Exception as e:
                    logger.warning(f"[ContactFinder] Error buscando {prop}={variant}: {e}")
                    continue

        logger.info(f"[ContactFinder] No se encontró contacto para {phone_e164}")
        return None

    def _parse_contact_result(self, result: Dict[str, Any], found_by: str) -> ContactInfo:
        """Parsea el resultado de HubSpot a ContactInfo."""
        props = result.get("properties", {})

        return ContactInfo(
            vid=result.get("id"),
            firstname=props.get("firstname"),
            lastname=props.get("lastname"),
            email=props.get("email"),
            phone=props.get("phone") or props.get("mobilephone"),
            whatsapp_id=props.get("whatsapp_id") or props.get("hs_whatsapp_phone_number"),
            sofia_status=props.get("sofia_status", "activa"),
            found_by=found_by
        )

    async def find_or_create(
        self,
        phone_e164: str,
        default_properties: Optional[Dict[str, Any]] = None
    ) -> ContactInfo:
        """
        Busca un contacto o lo crea si no existe.
        """
        # Intentar encontrar primero
        contact = await self.find_by_phone(phone_e164)

        if contact:
            return contact

        # Crear nuevo contacto
        logger.info(f"[ContactFinder] Creando nuevo contacto para {phone_e164}")

        properties = {
            "phone": phone_e164,
            "mobilephone": phone_e164,
            "whatsapp_id": phone_e164,
            "hs_whatsapp_phone_number": phone_e164,
            "sofia_status": "activa",
            "lifecyclestage": "lead",
            **(default_properties or {})
        }

        endpoint = f"{self.base_url}/crm/v3/objects/contacts"

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                endpoint,
                headers=self.headers,
                json={"properties": properties}
            )

            if response.status_code == 201:
                data = response.json()
                contact = ContactInfo(
                    vid=data.get("id"),
                    phone=phone_e164,
                    whatsapp_id=phone_e164,
                    sofia_status="activa",
                    found_by="created"
                )

                # Guardar en caché
                self._save_to_cache(phone_e164, contact)

                logger.info(f"[ContactFinder] Contacto creado: vid={contact.vid}")
                return contact

            else:
                logger.error(
                    f"[ContactFinder] Error creando contacto: "
                    f"{response.status_code} - {response.text}"
                )
                raise Exception(f"Error creando contacto: {response.status_code}")

    async def update_sofia_status(
        self,
        vid: str,
        status: str,
        phone_e164: Optional[str] = None
    ) -> bool:
        """
        Actualiza el estado de Sofía en el contacto.
        """
        endpoint = f"{self.base_url}/crm/v3/objects/contacts/{vid}"

        payload = {
            "properties": {
                "sofia_status": status,
                "sofia_status_updated": datetime.utcnow().isoformat()
            }
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.patch(endpoint, headers=self.headers, json=payload)

            if response.status_code == 200:
                logger.info(f"[ContactFinder] Estado de Sofía actualizado: vid={vid}, status={status}")

                # Invalidar caché
                if phone_e164:
                    self._invalidate_cache(phone_e164)

                return True

            else:
                logger.error(
                    f"[ContactFinder] Error actualizando estado: "
                    f"{response.status_code} - {response.text}"
                )
                return False


# Instancia singleton para uso global
_contact_finder: Optional[ContactFinder] = None


def get_contact_finder() -> ContactFinder:
    """Obtiene la instancia singleton del ContactFinder."""
    global _contact_finder
    if _contact_finder is None:
        _contact_finder = ContactFinder()
    return _contact_finder