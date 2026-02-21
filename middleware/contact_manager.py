# middleware/contact_manager.py
"""
Gestor de Contactos para el Middleware.

Este módulo maneja la identificación y creación de contactos en HubSpot,
utilizando el número de teléfono normalizado como identificador único.
"""

from typing import Optional, Dict, Any
from dataclasses import dataclass
from datetime import datetime

from logging_config import logger
from .phone_normalizer import PhoneNormalizer, PhoneValidationResult
from integrations.hubspot.hubspot_client import HubSpotClient
from integrations.hubspot.lead_assigner import lead_assigner


@dataclass
class ContactInfo:
    """Información del contacto identificado o creado."""

    contact_id: str
    phone_normalized: str
    is_new: bool
    firstname: Optional[str] = None
    lastname: Optional[str] = None
    email: Optional[str] = None
    properties: Optional[Dict[str, Any]] = None


class ContactManager:
    """
    Gestor de contactos que unifica normalización y HubSpot.

    Responsabilidades:
    - Normalizar números telefónicos
    - Buscar contactos existentes por whatsapp_id
    - Crear leads básicos cuando no existen
    - Mantener consistencia de datos
    """

    def __init__(self, hubspot_client: Optional[HubSpotClient] = None):
        """
        Inicializa el gestor de contactos.

        """
        self.normalizer = PhoneNormalizer()
        self._hubspot_client = hubspot_client
        self._hubspot_initialized = False

    @property
    def hubspot(self) -> HubSpotClient:
        """Lazy initialization del cliente HubSpot."""
        if self._hubspot_client is None:
            self._hubspot_client = HubSpotClient()
            self._hubspot_initialized = True
        return self._hubspot_client

    async def identify_or_create_contact(
        self,
        phone_raw: str,
        source_channel: str = "whatsapp_directo"
    ) -> ContactInfo:
        """
        Identifica un contacto existente o crea uno nuevo.

        Este es el método principal del Paso A. Garantiza que:
        1. El número esté normalizado correctamente
        2. No se creen duplicados en HubSpot
        3. Siempre se retorne un contact_id válido
        """
        # Paso 1: Normalizar el número
        validation = self.normalizer.normalize(phone_raw)

        if not validation.is_valid:
            logger.error(
                f"[ContactManager] Número inválido: {phone_raw} - {validation.error_message}"
            )
            raise ValueError(f"Número telefónico inválido: {validation.error_message}")

        phone_normalized = validation.normalized
        logger.info(f"[ContactManager] Número normalizado: {phone_raw} → {phone_normalized}")

        # Paso 2: Buscar contacto existente en HubSpot
        contact_id = await self._search_contact(phone_normalized)

        if contact_id:
            # Contacto encontrado
            logger.info(f"[ContactManager] Contacto existente encontrado: {contact_id}")
            return ContactInfo(
                contact_id=contact_id,
                phone_normalized=phone_normalized,
                is_new=False
            )

        # Paso 3: Crear nuevo lead básico
        logger.info(f"[ContactManager] Creando nuevo lead para: {phone_normalized}")
        contact_id = await self._create_basic_lead(phone_normalized, source_channel)

        return ContactInfo(
            contact_id=contact_id,
            phone_normalized=phone_normalized,
            is_new=True
        )

    async def _search_contact(self, phone_normalized: str) -> Optional[str]:
        """
        Busca un contacto por whatsapp_id (número normalizado).

        Args:
            phone_normalized: Número en formato E.164

        Returns:
            contact_id si existe, None si no
        """
        try:
            contact_id = await self.hubspot.search_contact_by_phone(phone_normalized)
            return contact_id
        except Exception as e:
            logger.error(f"[ContactManager] Error buscando contacto: {e}")
            # En caso de error, asumimos que no existe para evitar duplicados
            # (mejor crear uno nuevo que perder el mensaje)
            return None

    async def _create_basic_lead(
        self,
        phone_normalized: str,
        source_channel: str
    ) -> str:
        """
        Crea un lead básico con la información mínima.
        Incluye asignación automática de owner basada en el canal de origen.
        """
        # Obtener owner basado en el canal de origen
        owner_id = lead_assigner.get_next_owner(source_channel)

        properties = {
            # Identificador único - CRÍTICO para evitar duplicados
            "whatsapp_id": phone_normalized,

            # Teléfono estándar de HubSpot
            "phone": phone_normalized,

            # Metadata del chatbot
            "canal_origen": source_channel,
            "chatbot_timestamp": str(int(datetime.now().timestamp() * 1000)),

            # Lifecycle stage inicial
            "lifecyclestage": "lead",
        }

        # Asignar owner si está disponible
        if owner_id:
            properties["hubspot_owner_id"] = owner_id
            logger.info(f"[ContactManager] Lead asignado a owner ID: {owner_id} (canal: {source_channel})")

        try:
            contact_id = await self.hubspot.create_contact(properties)
            logger.info(
                f"[ContactManager] Lead creado exitosamente: {contact_id} "
                f"(whatsapp_id: {phone_normalized})"
            )
            return contact_id

        except Exception as e:
            # Manejar caso de duplicado (409 Conflict)
            error_str = str(e).lower()
            if "conflict" in error_str or "already exists" in error_str:
                logger.warning(
                    f"[ContactManager] Contacto ya existe (race condition), buscando..."
                )
                # Reintentar búsqueda
                contact_id = await self._search_contact(phone_normalized)
                if contact_id:
                    return contact_id

            # Error no manejable
            logger.error(f"[ContactManager] Error creando lead: {e}")
            raise

    async def update_contact_info(
        self,
        contact_id: str,
        properties: Dict[str, Any]
    ) -> None:
        """
        Actualiza información adicional de un contacto.

        Usado cuando se obtiene más información durante la conversación
        (nombre, email, preferencias, etc.)

        Args:
            contact_id: ID del contacto en HubSpot
            properties: Propiedades a actualizar
        """
        try:
            await self.hubspot.update_contact(contact_id, properties)
            logger.info(f"[ContactManager] Contacto {contact_id} actualizado")
        except Exception as e:
            logger.error(f"[ContactManager] Error actualizando contacto: {e}")
            raise

    def normalize_phone(self, phone_raw: str) -> PhoneValidationResult:
        """
        Normaliza un número sin interactuar con HubSpot.

        Útil para validación previa o comparaciones.
        """
        return self.normalizer.normalize(phone_raw)