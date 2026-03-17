# middleware/contact_manager.py
"""
Gestor de Contactos para el Middleware.

Este módulo maneja la identificación y creación de contactos en HubSpot,
utilizando el número de teléfono normalizado como identificador único.

OPTIMIZACIONES v2.0:
- Lock distribuido de Redis para evitar creación de duplicados (Race Condition)
- Manejo robusto de Rate Limits (429) sin detener el hilo principal
- Retry con backoff exponencial para errores transitorios
"""

import os
import asyncio

from typing import Optional, Dict, Any
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import redis.asyncio as redis
from logging_config import logger
from .phone_normalizer import PhoneNormalizer, PhoneValidationResult
from integrations.hubspot.hubspot_client import HubSpotClient
from integrations.hubspot.lead_assigner import lead_assigner


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTES
# ═══════════════════════════════════════════════════════════════════════════════

TIMEZONE_BOGOTA = ZoneInfo("America/Bogota")

# Prefijo para locks de creación de leads
LEAD_CREATION_LOCK_PREFIX = "lead_creation_lock:"

# TTL del lock en segundos (2 segundos como especificado)
LEAD_CREATION_LOCK_TTL = 2

# Máximo de reintentos para rate limits
MAX_RATE_LIMIT_RETRIES = 3


# ═══════════════════════════════════════════════════════════════════════════════
# EXCEPCIONES PERSONALIZADAS
# ═══════════════════════════════════════════════════════════════════════════════

class HubSpotRateLimitError(Exception):
    """Error específico para rate limits de HubSpot (429)."""

    def __init__(self, retry_after: int = 10):
        self.retry_after = retry_after
        super().__init__(f"HubSpot rate limit. Retry after {retry_after}s")


class LeadCreationLockError(Exception):
    """Error cuando no se puede adquirir el lock para crear lead."""

    def __init__(self, phone: str):
        self.phone = phone
        super().__init__(f"No se pudo adquirir lock para crear lead: {phone}")


# ═══════════════════════════════════════════════════════════════════════════════
# DATACLASS PARA INFORMACIÓN DE CONTACTO
# ═══════════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════════
# CONTACT MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class ContactManager:
    """
    Gestor de contactos que unifica normalización y HubSpot.

    OPTIMIZACIONES v2.0:
    - Lock distribuido de Redis (2s) para evitar duplicados en webhooks concurrentes
    - Manejo de 429 Rate Limit sin detener procesamiento
    - Método seguro que nunca lanza excepciones

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
        self._redis: Optional[redis.Redis] = None

    @property
    def hubspot(self) -> HubSpotClient:
        """Lazy initialization del cliente HubSpot."""
        if self._hubspot_client is None:
            self._hubspot_client = HubSpotClient()
            self._hubspot_initialized = True
        return self._hubspot_client

    async def _get_redis(self) -> redis.Redis:
        """Lazy initialization de conexión Redis."""
        if self._redis is None:
            redis_url = os.getenv(
                "REDIS_PUBLIC_URL",
                os.getenv("REDIS_URL", "redis://localhost:6379")
            )
            self._redis = redis.from_url(
                redis_url,
                encoding="utf-8",
                decode_responses=True
            )
        return self._redis

    async def close(self):
        """Cierra conexiones."""
        if self._redis:
            await self._redis.close()
            self._redis = None

    # ═══════════════════════════════════════════════════════════════════════════
    # MÉTODOS PÚBLICOS
    # ═══════════════════════════════════════════════════════════════════════════

    async def identify_or_create_contact(
        self,
        phone_raw: str,
        source_channel: str = "whatsapp_directo",
        create_deal: bool = True
    ) -> ContactInfo:
        """
        Identifica un contacto existente o crea uno nuevo.

        Este es el método principal. Garantiza que:
        1. El número esté normalizado correctamente
        2. No se creen duplicados en HubSpot (usa lock de Redis)
        3. Siempre se retorne un contact_id válido

        Incluye timeout de 7s en las llamadas a HubSpot para evitar que
        el proxy de Railway (límite ~15s) mate la conexión antes de que
        el webhook responda con el TwiML de Sofía.

        Args:
            phone_raw: Número de teléfono sin normalizar
            source_channel: Canal de origen del contacto

        Returns:
            ContactInfo con los datos del contacto

        Raises:
            ValueError: Si el número es inválido
            asyncio.TimeoutError: Si HubSpot no responde en 7s (webhook_handler
                                  lo captura como contact_info=None)
            HubSpotRateLimitError: Si HubSpot devuelve 429 (después de reintentos)
        """
        import time as _time

        # Paso 1: Normalizar el número (rápido, sin red)
        validation = self.normalizer.normalize(phone_raw)

        if not validation.is_valid:
            logger.error(
                "[ContactManager] Número inválido: %s - %s",
                phone_raw, validation.error_message
            )
            raise ValueError(f"Número telefónico inválido: {validation.error_message}")

        phone_normalized = validation.normalized
        logger.info(
            "[ContactManager] Número normalizado: %s → %s",
            phone_raw, phone_normalized
        )

        _deadline = 3.0  # segundos totales para llamadas HubSpot (reducido para responder más rápido)
        _t0 = _time.monotonic()

        # Paso 2: Buscar contacto existente CON PROPIEDADES (con timeout global)
        try:
            contact_result = await asyncio.wait_for(
                self._search_contact_with_properties(phone_normalized),
                timeout=_deadline
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[ContactManager] Timeout (%.1fs) buscando contacto %s. "
                "Sofía responderá sin CRM; contacto se asegura en background.",
                _deadline, phone_normalized
            )
            asyncio.create_task(self._background_ensure_contact(phone_raw, source_channel))
            raise  # webhook_handler lo captura → contact_info = None

        if contact_result:
            contact_id = contact_result["contact_id"]
            properties = contact_result.get("properties") or {}
            existing_owner = properties.get("hubspot_owner_id")
            
            logger.info(
                "[ContactManager] Contacto existente encontrado: %s (nombre: %s, owner: %s)",
                contact_id,
                contact_result.get("firstname", "N/A"),
                existing_owner or "SIN ASIGNAR"
            )
            
            # ═══════════════════════════════════════════════════════════════════
            # FIX: Asignar owner a contactos existentes que no tienen uno
            # ═══════════════════════════════════════════════════════════════════
            if not existing_owner:
                logger.info(
                    "[ContactManager] Contacto %s sin owner. Asignando por canal: %s",
                    contact_id, source_channel
                )
                # Asignar owner en background para no bloquear respuesta de Sofia
                asyncio.create_task(
                    self._assign_owner_to_existing_contact(
                        contact_id, phone_normalized, source_channel
                    )
                )
            
            return ContactInfo(
                contact_id=contact_id,
                phone_normalized=phone_normalized,
                is_new=False,
                firstname=contact_result.get("firstname"),
                lastname=contact_result.get("lastname"),
                email=contact_result.get("email"),
                properties=properties
            )

        # Paso 3: Crear nuevo lead básico (con tiempo restante del timeout)
        logger.info(
            "[ContactManager] Creando nuevo lead para: %s",
            phone_normalized
        )
        remaining = max(1.0, _deadline - (_time.monotonic() - _t0))
        try:
            contact_id, created_owner_id = await asyncio.wait_for(
                self._create_basic_lead_with_lock(phone_normalized, source_channel, create_deal=create_deal),
                timeout=remaining
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[ContactManager] Timeout (%.1fs restante) creando contacto %s. "
                "Sofía responderá sin CRM; creación continúa en background.",
                remaining, phone_normalized
            )
            asyncio.create_task(self._background_ensure_contact(phone_raw, source_channel))
            raise

        return ContactInfo(
            contact_id=contact_id,
            phone_normalized=phone_normalized,
            is_new=True,
            properties={"hubspot_owner_id": created_owner_id} if created_owner_id else None
        )

    async def identify_or_create_contact_safe(
        self,
        phone_raw: str,
        source_channel: str = "whatsapp_directo"
    ) -> Optional[ContactInfo]:
        """
        Versión segura que NUNCA detiene el procesamiento del mensaje.

        Retorna None si falla en lugar de propagar excepción.
        Incluye timeout de 10s para evitar que Railway proxy (límite ~15s)
        mate la conexión y Twilio reintente con 502/422.

        Args:
            phone_raw: Número de teléfono sin normalizar
            source_channel: Canal de origen del contacto

        Returns:
            ContactInfo o None si hay error
        """
        try:
            # identify_or_create_contact ya tiene timeout interno de 7s.
            # Si lanza TimeoutError, lo capturamos aquí sin re-agendar background
            # (ya fue agendado dentro de identify_or_create_contact).
            return await self.identify_or_create_contact(phone_raw, source_channel)

        except asyncio.TimeoutError:
            # El timeout interno (7s) ya agendó _background_ensure_contact.
            logger.warning(
                "[ContactManager] Timeout HubSpot para %s. "
                "Sofía responderá sin CRM.",
                phone_raw
            )
            return None

        except HubSpotRateLimitError as e:
            logger.warning(
                "[ContactManager] Rate limit alcanzado para %s. "
                "Mensaje se procesará sin CRM. Retry en %ds",
                phone_raw, e.retry_after
            )
            return None

        except LeadCreationLockError as e:
            logger.warning(
                "[ContactManager] Lock no disponible para %s. "
                "Posible webhook duplicado, ignorando.",
                e.phone
            )
            return None

        except ValueError as e:
            logger.error(
                "[ContactManager] Número inválido %s: %s",
                phone_raw, str(e)
            )
            return None

        except Exception as e:
            logger.error(
                "[ContactManager] Error no recuperable para %s: %s. "
                "Mensaje se procesará sin CRM.",
                phone_raw, str(e)
            )
            return None

    async def _background_ensure_contact(self, phone_raw: str, source_channel: str) -> None:
        """
        Asegura que el contacto exista en HubSpot y quede en cache Redis.
        Se llama como background task tras un timeout en identify_or_create_contact_safe.
        Espera 2s antes de buscar/crear para darle tiempo a cualquier llamada en vuelo.
        """
        await asyncio.sleep(2.0)
        try:
            result = await self.identify_or_create_contact(phone_raw, source_channel)
            logger.info(
                "[ContactManager] Background: contacto asegurado %s → %s",
                phone_raw, result.contact_id if result else None
            )
        except Exception as e:
            logger.error(
                "[ContactManager] Background: falló asegurar contacto %s: %s",
                phone_raw, e
            )

    # ═══════════════════════════════════════════════════════════════════════════
    # MÉTODOS PRIVADOS - BÚSQUEDA
    # ═══════════════════════════════════════════════════════════════════════════

    async def _search_contact(self, phone_normalized: str) -> Optional[str]:
        """
        Busca un contacto por whatsapp_id (número normalizado).
        Verifica Redis cache antes de consultar HubSpot.

        Args:
            phone_normalized: Número en formato E.164

        Returns:
            contact_id si existe, None si no
        """
        # --- Fast path: Redis cache ---
        cache_key = f"phone_cache:{phone_normalized}"
        try:
            r = await self._get_redis()
            cached_id = await r.get(cache_key)
            if cached_id:
                logger.info(
                    "[ContactManager] Cache hit: %s → %s", phone_normalized, cached_id
                )
                return cached_id
        except Exception:
            pass  # Cache unavailable, fall through to HubSpot

        # --- Slow path: HubSpot API ---
        try:
            contact_id = await self.hubspot.search_contact_by_phone(phone_normalized)
            # Populate cache for next time
            if contact_id:
                try:
                    r = await self._get_redis()
                    await r.set(cache_key, contact_id, ex=86400)  # 24h TTL
                except Exception:
                    pass
            return contact_id
        except Exception as e:
            logger.error("[ContactManager] Error buscando contacto: %s", e)
            # En caso de error, asumimos que no existe para evitar duplicados
            return None

    async def _search_contact_with_properties(
        self, 
        phone_normalized: str
    ) -> Optional[Dict[str, Any]]:
        """
        Busca un contacto por whatsapp_id y retorna ID + propiedades chatbot.
        
        Usa cache de Redis para el contact_id, pero siempre obtiene
        propiedades frescas de HubSpot para contexto de Sofia.

        Args:
            phone_normalized: Número en formato E.164

        Returns:
            Dict con 'contact_id', 'firstname', 'properties' o None
        """
        # --- Fast path: Redis cache para contact_id ---
        cache_key = f"phone_cache:{phone_normalized}"
        cached_id = None
        try:
            r = await self._get_redis()
            cached_id = await r.get(cache_key)
        except Exception:
            pass

        # --- Obtener propiedades de HubSpot ---
        try:
            result = await self.hubspot.search_contact_by_phone_with_properties(phone_normalized)
            
            if result:
                contact_id = result["id"]
                properties = result.get("properties", {})
                
                # Actualizar cache si no estaba
                if not cached_id:
                    try:
                        r = await self._get_redis()
                        await r.set(cache_key, contact_id, ex=86400)
                    except Exception:
                        pass
                
                logger.info(
                    "[ContactManager] Contacto encontrado con propiedades: %s "
                    "(nombre: %s, zona: %s)",
                    contact_id,
                    properties.get("firstname", "N/A"),
                    properties.get("chatbot_location", "N/A")
                )
                
                return {
                    "contact_id": contact_id,
                    "firstname": properties.get("firstname"),
                    "lastname": properties.get("lastname"),
                    "email": properties.get("email"),
                    "properties": properties
                }
            
            return None
            
        except Exception as e:
            logger.error("[ContactManager] Error buscando contacto con propiedades: %s", e)
            return None

    # ═══════════════════════════════════════════════════════════════════════════
    # MÉTODOS PRIVADOS - CREACIÓN CON LOCK
    # ═══════════════════════════════════════════════════════════════════════════

    async def _create_basic_lead_with_lock(
        self,
        phone_normalized: str,
        source_channel: str,
        create_deal: bool = True
    ) -> tuple[str, str | None]:
        """
        Crea un lead básico con lock distribuido de Redis.

        RACE CONDITION PREVENTION:
        Usa un lock de 2 segundos para evitar que webhooks concurrentes
        creen leads duplicados para el mismo teléfono.

        Args:
            phone_normalized: Teléfono en formato E.164
            source_channel: Canal de origen

        Returns:
            contact_id del lead creado

        Raises:
            LeadCreationLockError: Si no se puede adquirir el lock
            HubSpotRateLimitError: Si HubSpot devuelve 429
        """
        r = await self._get_redis()
        lock_key = f"{LEAD_CREATION_LOCK_PREFIX}{phone_normalized}"

        # Intentar adquirir lock (SET NX = solo si no existe)
        lock_acquired = await r.set(
            lock_key,
            "creating",
            nx=True,
            ex=LEAD_CREATION_LOCK_TTL
        )

        if not lock_acquired:
            # Otro proceso está creando este lead
            logger.warning(
                "[ContactManager] Lock existente para %s. "
                "Esperando resultado del otro proceso...",
                phone_normalized
            )

            # Esperar brevemente y buscar el contacto que el otro proceso debería crear
            await asyncio.sleep(0.5)

            # Buscar si ya se creó
            contact_id = await self._search_contact(phone_normalized)
            if contact_id:
                logger.info(
                    "[ContactManager] Contacto encontrado después de esperar lock: %s",
                    contact_id
                )
                return contact_id, None

            # Si aún no existe, esperar un poco más
            await asyncio.sleep(1.0)
            contact_id = await self._search_contact(phone_normalized)
            if contact_id:
                return contact_id, None

            # Si después de esperar no existe, lanzar error
            raise LeadCreationLockError(phone_normalized)

        try:
            # Lock adquirido, crear el lead
            return await self._create_basic_lead(phone_normalized, source_channel, create_deal=create_deal)
        finally:
            # Siempre liberar el lock
            await r.delete(lock_key)

    async def _create_basic_lead(
        self,
        phone_normalized: str,
        source_channel: str,
        create_deal: bool = True
    ) -> tuple[str, str | None]:
        """
        Crea un lead básico con la información mínima.

        Incluye:
        - Asignación automática de owner basada en el canal
        - Manejo de rate limits con retry
        - Detección de duplicados (409 Conflict)

        Args:
            phone_normalized: Teléfono en formato E.164
            source_channel: Canal de origen

        Returns:
            contact_id del lead creado
        """
        # Obtener owner basado en el canal de origen
        owner_id = lead_assigner.get_next_owner(source_channel)

        # HubSpot no acepta "whatsapp" como valor de canal_origen; mapear a "whatsapp_directo"
        hs_canal = "whatsapp_directo" if source_channel == "whatsapp" else source_channel

        # HubSpot requiere que chatbot_timestamp sea medianoche UTC (no hora exacta)
        from datetime import timezone
        midnight_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        properties = {
            # Identificador único - CRÍTICO para evitar duplicados
            "whatsapp_id": phone_normalized,

            # Teléfono estándar de HubSpot
            "phone": phone_normalized,

            # Metadata del chatbot
            "canal_origen": hs_canal,
            "chatbot_timestamp": str(int(midnight_utc.timestamp() * 1000)),

            # Lifecycle stage inicial
            "lifecyclestage": "lead",
        }

        # Asignar owner si está disponible
        if owner_id:
            properties["hubspot_owner_id"] = owner_id
            logger.info(
                "[ContactManager] Lead asignado a owner ID: %s (canal: %s)",
                owner_id, source_channel
            )

        # Intentar crear con manejo de rate limits
        contact_id = await self._create_contact_with_retry(
            properties, phone_normalized
        )

        # Guardar en cache Redis para evitar búsquedas futuras lentas a HubSpot
        try:
            r = await self._get_redis()
            await r.set(f"phone_cache:{phone_normalized}", contact_id, ex=86400)
            logger.info("[ContactManager] phone_cache guardado para %s", phone_normalized)
        except Exception:
            pass

        # Construir url_chat para deep link CRM → Panel (requiere PANEL_BASE_URL en env)
        panel_base_url = os.getenv("PANEL_BASE_URL", "").rstrip("/")
        admin_api_key = os.getenv("ADMIN_API_KEY", "")
        # URL encode del teléfono para que el + se preserve como %2B
        from urllib.parse import quote
        phone_encoded = quote(phone_normalized, safe='')
        url_chat = (
            f"{panel_base_url}/whatsapp/panel/?key={admin_api_key}&advisor={owner_id}&phone={phone_encoded}"
            if panel_base_url and owner_id and admin_api_key else ""
        )

        # Crear deal asociado en background solo si corresponde (contacto con intención comercial)
        if create_deal:
            asyncio.create_task(
                self._create_deal_for_new_lead(contact_id, phone_normalized, source_channel, url_chat, owner_id)
            )

        # Escribir url_chat en el contacto (en background, independiente del deal)
        if url_chat:
            asyncio.create_task(self._write_panel_url(contact_id, url_chat))

        return contact_id, owner_id

    async def _create_deal_for_new_lead(
        self,
        contact_id: str,
        phone_normalized: str,
        source_channel: str,
        url_chat: str = "",
        owner_id: str | None = None
    ) -> dict | None:
        """
        Crea un Deal en HubSpot y lo asocia al contacto recién creado.

        Se ejecuta en background (asyncio.create_task) para no bloquear
        el procesamiento del webhook. Usa el pipeline y etapa correctos.

        Pipeline: 854756009
        Etapa inicial: 1275156339 (Nuevo Lead)

        Si se provee url_chat, también actualiza la propiedad url_chat del negocio
        para que el deep link esté disponible tanto en el Contacto como en el Negocio.

        Si se provee owner_id, asigna el deal a ese asesor.

        Returns:
            dict con deal_id y current_stage si se creó exitosamente, None si falló
        """
        # IDs del pipeline definidos como constantes
        PIPELINE_ID = "854756009"
        STAGE_NUEVO_LEAD = "1275156339"

        try:
            deal_name = f"Lead WA {phone_normalized}"
            # Misma conversión que en _create_basic_lead: "whatsapp" → "whatsapp_directo"
            hs_canal = "whatsapp_directo" if source_channel == "whatsapp" else source_channel
            deal_properties = {
                "dealname": deal_name,
                "canal_origen": hs_canal,
            }
            if owner_id:
                deal_properties["hubspot_owner_id"] = owner_id
            deal_id = await self.hubspot.create_deal(
                contact_id=contact_id,
                properties=deal_properties,
                pipeline_id=PIPELINE_ID,
                dealstage=STAGE_NUEVO_LEAD
            )
            logger.info(
                "[ContactManager] Deal creado: %s para contacto %s (canal=%s)",
                deal_id, contact_id, source_channel
            )

            # Cachear deal_id en Redis para que el CRM Agent pueda hacer sync parcial
            # sin necesidad de buscar en HubSpot. TTL 72h (mismo ciclo que conv_meta).
            try:
                r = await self._get_redis()
                await r.setex(f"deal_id_cache:{phone_normalized}", 72 * 3600, deal_id)
                logger.info(
                    "[ContactManager] deal_id_cache guardado: deal=%s, phone=%s",
                    deal_id, phone_normalized
                )
            except Exception as redis_err:
                logger.warning(
                    "[ContactManager] No se pudo cachear deal_id en Redis: %s", redis_err
                )

            # Escribir url_chat en el negocio si está disponible
            if url_chat and deal_id:
                try:
                    await self.hubspot.update_deal(deal_id, {"url_chat": url_chat})
                    logger.info("[ContactManager] url_chat escrito en negocio %s", deal_id)
                except Exception as deal_url_err:
                    logger.warning(
                        "[ContactManager] No se pudo escribir url_chat en negocio %s: %s",
                        deal_id, deal_url_err
                    )

            return {"deal_id": deal_id, "current_stage": STAGE_NUEVO_LEAD}
        except Exception as e:
            # El fallo de creación de deal NO debe afectar la conversación
            logger.warning(
                "[ContactManager] No se pudo crear deal para contacto %s: %s",
                contact_id, e
            )
            return None

    async def _write_panel_url(self, contact_id: str, url_chat: str) -> None:
        """
        Escribe la URL de deep link al panel en la propiedad url_chat de HubSpot.
        Se ejecuta en background — un fallo no afecta el flujo principal.
        """
        try:
            await self.hubspot.update_contact(contact_id, {"url_chat": url_chat})
            logger.info("[ContactManager] url_chat registrado para contacto %s", contact_id)
        except Exception as e:
            logger.warning("[ContactManager] No se pudo escribir url_chat para %s: %s", contact_id, e)

    async def _assign_owner_to_existing_contact(
        self,
        contact_id: str,
        phone_normalized: str,
        source_channel: str
    ) -> None:
        """
        Asigna owner a un contacto existente que no tiene uno.
        
        Se ejecuta en background para no bloquear la respuesta de Sofía.
        También crea deal si no existe y actualiza url_chat.
        
        Args:
            contact_id: ID del contacto en HubSpot
            phone_normalized: Teléfono en formato E.164
            source_channel: Canal de origen para determinar el equipo
        """
        try:
            # 1. Obtener owner basado en el canal
            owner_id = lead_assigner.get_next_owner(source_channel)
            
            if not owner_id:
                logger.warning(
                    "[ContactManager] No se pudo obtener owner para canal %s. "
                    "Contacto %s no será asignado.",
                    source_channel, contact_id
                )
                return
            
            logger.info(
                "[ContactManager] Asignando owner %s a contacto existente %s (canal: %s)",
                owner_id, contact_id, source_channel
            )
            
            # 2. Actualizar contacto con owner
            await self.hubspot.update_contact(contact_id, {
                "hubspot_owner_id": owner_id
            })
            logger.info(
                "[ContactManager] ✅ Owner %s asignado a contacto %s",
                owner_id, contact_id
            )
            
            # 3. Construir url_chat para deep link
            panel_base_url = os.getenv("PANEL_BASE_URL", "").rstrip("/")
            admin_api_key = os.getenv("ADMIN_API_KEY", "")
            from urllib.parse import quote
            phone_encoded = quote(phone_normalized, safe='')
            url_chat = (
                f"{panel_base_url}/whatsapp/panel/?key={admin_api_key}&advisor={owner_id}&phone={phone_encoded}"
                if panel_base_url and owner_id and admin_api_key else ""
            )
            
            # 4. Escribir url_chat en contacto
            if url_chat:
                try:
                    await self.hubspot.update_contact(contact_id, {"url_chat": url_chat})
                    logger.info("[ContactManager] url_chat actualizado para contacto %s", contact_id)
                except Exception as url_err:
                    logger.warning(
                        "[ContactManager] No se pudo actualizar url_chat: %s", url_err
                    )
            
            # 5. Verificar si ya tiene deal y crear uno si no
            try:
                has_deal = await self._check_contact_has_deal(contact_id)
                if not has_deal:
                    logger.info(
                        "[ContactManager] Contacto %s sin deal. Creando...",
                        contact_id
                    )
                    await self._create_deal_for_new_lead(
                        contact_id, phone_normalized, source_channel, url_chat, owner_id
                    )
                else:
                    logger.info(
                        "[ContactManager] Contacto %s ya tiene deal. No se crea duplicado.",
                        contact_id
                    )
            except Exception as deal_err:
                logger.warning(
                    "[ContactManager] Error verificando/creando deal para %s: %s",
                    contact_id, deal_err
                )
                
        except Exception as e:
            logger.warning(
                "[ContactManager] Error asignando owner a contacto %s: %s",
                contact_id, e
            )

    async def _check_contact_has_deal(self, contact_id: str) -> bool:
        """
        Verifica si un contacto ya tiene al menos un deal asociado.
        
        Args:
            contact_id: ID del contacto en HubSpot
            
        Returns:
            True si tiene al menos un deal, False si no
        """
        try:
            deals = await self.hubspot.get_contact_deals(contact_id)
            return bool(deals and len(deals) > 0)
        except Exception as e:
            logger.warning(
                "[ContactManager] Error verificando deals para contacto %s: %s",
                contact_id, e
            )
            # En caso de error, asumir que no tiene deal para crear uno
            return False

    async def _create_contact_with_retry(
        self,
        properties: Dict[str, Any],
        phone_normalized: str,
        attempt: int = 1
    ) -> str:
        """
        Crea contacto con retry para rate limits.

        Args:
            properties: Propiedades del contacto
            phone_normalized: Teléfono (para logs y búsqueda de duplicados)
            attempt: Número de intento actual

        Returns:
            contact_id del contacto creado
        """
        try:
            contact_id = await self.hubspot.create_contact(properties)
            logger.info(
                "[ContactManager] Lead creado exitosamente: %s "
                "(whatsapp_id: %s)",
                contact_id, phone_normalized
            )
            return contact_id

        except Exception as e:
            error_str = str(e).lower()

            # Detectar Rate Limit (429)
            if "429" in str(e) or "rate limit" in error_str:
                if attempt >= MAX_RATE_LIMIT_RETRIES:
                    logger.error(
                        "[ContactManager] Rate limit: máximo de reintentos alcanzado "
                        "para %s", phone_normalized
                    )
                    raise HubSpotRateLimitError(retry_after=10)

                # Calcular backoff exponencial: 2^attempt segundos
                wait_time = 2 ** attempt
                logger.warning(
                    "[ContactManager] Rate limit (429). Reintento %d/%d en %ds",
                    attempt, MAX_RATE_LIMIT_RETRIES, wait_time
                )

                await asyncio.sleep(wait_time)

                return await self._create_contact_with_retry(
                    properties, phone_normalized, attempt + 1
                )

            # Detectar Duplicado (409 Conflict)
            if "conflict" in error_str or "already exists" in error_str:
                logger.warning(
                    "[ContactManager] Contacto ya existe (race condition), buscando..."
                )
                contact_id = await self._search_contact(phone_normalized)
                if contact_id:
                    return contact_id

                # Si no lo encontramos después de 409, es un error real
                logger.error(
                    "[ContactManager] 409 pero contacto no encontrado: %s",
                    phone_normalized
                )

            # Error no manejable
            logger.error("[ContactManager] Error creando lead: %s", e)
            raise

    # ═══════════════════════════════════════════════════════════════════════════
    # MÉTODOS DE ACTUALIZACIÓN
    # ═══════════════════════════════════════════════════════════════════════════

    async def update_contact_info(
        self,
        contact_id: str,
        properties: Dict[str, Any],
        attempt: int = 0
    ) -> bool:
        """
        Actualiza información adicional de un contacto.

        Usado cuando se obtiene más información durante la conversación
        (nombre, email, preferencias, etc.)

        Args:
            contact_id: ID del contacto en HubSpot
            properties: Propiedades a actualizar

        Returns:
            True si se actualizó correctamente, False si falló
        
        Raises:
            Exception: Re-lanza excepciones de propiedades no existentes
                      para que el caller pueda manejarlas como opcionales
        """
        try:
            await self.hubspot.update_contact(contact_id, properties)
            logger.info("[ContactManager] Contacto %s actualizado", contact_id)
            return True

        except Exception as e:
            error_str = str(e).lower()

            # Re-lanzar si es propiedad faltante (para manejo en webhook_handler)
            if "PROPERTY_DOESNT_EXIST" in str(e):
                logger.debug(
                    "[ContactManager] Propiedad faltante al actualizar %s. "
                    "Re-lanzando para manejo de opcional.",
                    contact_id
                )
                raise

            # Si es rate limit, reintentar con backoff exponencial
            if "429" in str(e) or "rate limit" in error_str:
                if attempt >= MAX_RATE_LIMIT_RETRIES:
                    logger.error(
                        "[ContactManager] Rate limit: máximo de reintentos al actualizar %s",
                        contact_id
                    )
                    return False
                wait_time = 2 ** (attempt + 1)  # 2s, 4s, 8s
                logger.warning(
                    "[ContactManager] Rate limit (429) al actualizar %s. "
                    "Reintento %d/%d en %ds",
                    contact_id, attempt + 1, MAX_RATE_LIMIT_RETRIES, wait_time
                )
                await asyncio.sleep(wait_time)
                return await self.update_contact_info(contact_id, properties, attempt + 1)

            logger.error(
                "[ContactManager] Error actualizando contacto %s: %s",
                contact_id, e
            )
            return False

    # ═══════════════════════════════════════════════════════════════════════════
    # UTILIDADES
    # ═══════════════════════════════════════════════════════════════════════════

    def normalize_phone(self, phone_raw: str) -> PhoneValidationResult:
        """
        Normaliza un número sin interactuar con HubSpot.

        Útil para validación previa o comparaciones.
        """
        return self.normalizer.normalize(phone_raw)