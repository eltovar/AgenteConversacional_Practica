# middleware/outbound_panel.py
"""
Este módulo proporciona endpoints API y UI para que los asesores envíen
mensajes de WhatsApp directamente, sustituyendo el Inbox bloqueado de HubSpot.
"""

import os
import json
import re
import html
import asyncio
import httpx
from io import BytesIO
from typing import Optional, Dict, Any
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass

from fastapi import APIRouter, Form, Header, HTTPException, BackgroundTasks, Query, Request, Body, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from pathlib import Path
import redis.asyncio as redis

from logging_config import logger
from .phone_normalizer import PhoneNormalizer
from .conversation_state import ConversationStateManager, ConversationStatus
from .contact_manager import ContactManager
from .websocket_manager import ws_manager
from .templates.templates import DEFAULT_TEMPLATES  # Templates predefinidos
from utils.twilio_client import twilio_client
from integrations.hubspot import get_timeline_logger
from database.mongodb_client import get_mongo_manager
from utils.media_processor import media_processor


# Router de FastAPI para el panel de envío
router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])


# ============================================================================
# Modelos Pydantic para JSON requests
# ============================================================================

class SendMessageRequest(BaseModel):
    """Modelo para envío de mensajes via JSON (para testing E2E)."""
    phone: str = Field(..., description="Número de destino (+573001234567)")
    message: Optional[str] = Field(None, description="Contenido del mensaje de texto")
    body: Optional[str] = Field(None, description="Alias: contenido del mensaje (compatibilidad)")
    contact_id: Optional[str] = Field(None, description="ID del contacto en HubSpot")
    canal: Optional[str] = Field("whatsapp", description="Canal de origen para segregación")
    force_send: bool = Field(False, description="Forzar envío aunque ventana esté cerrada")
    
    class Config:
        json_schema_extra = {
            "example": {
                "phone": "+573001112235",
                "message": "Hola Carlos, un gusto. Soy el asesor asignado.",
                "canal": "whatsapp"
            }
        }

# Configuración de Jinja2 Templates
TEMPLATES_DIR = Path(__file__).parent / "PanelAsesores"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ============================================================================
# Configuración y constantes
# ============================================================================

# API Key para autenticación del panel
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

# API Key de HubSpot
HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")

# Ventana de 24 horas de WhatsApp (en segundos)
WHATSAPP_WINDOW_SECONDS = 24 * 60 * 60

# Prefijo en Redis para almacenar último mensaje del cliente
LAST_CLIENT_MESSAGE_PREFIX = "last_client_msg:"

# Prefijo en Redis para almacenar templates de WhatsApp
TEMPLATE_PREFIX = "whatsapp_template:"
DEFAULT_TEMPLATE_PREFIX = "whatsapp_template:default:"

# IDs del Pipeline Comercial de HubSpot (actualizado)
HUBSPOT_PIPELINE_ID = "854756009"
HUBSPOT_STAGE_NUEVO_LEAD = "1275156339"
HUBSPOT_STAGE_EN_CONVERSACION = "1275156340"

# Singleton de connection pool Redis (evita crear nueva conexión por request)
_redis_pool: Optional[redis.Redis] = None

# Singleton de cliente HTTP con connection pooling (evita crear conexión por request)
_httpx_client: Optional[httpx.AsyncClient] = None

# ============================================================================
# Caché de deal info en Redis (compartida entre workers Railway)
# ============================================================================
# Key: deal_cache:{contact_id}  Value: JSON {"deal_id": str, "current_stage": str}  TTL: 3600s
DEAL_CACHE_TTL_SECONDS = 3600  # TTL de 1 hora — deals no cambian frecuentemente; cubre propagación de HubSpot
DEAL_CACHE_KEY_PREFIX = "deal_cache:"

def get_httpx_client() -> httpx.AsyncClient:
    """
    Retorna cliente HTTP global con connection pooling.
    
    Reutilizar conexiones TCP reduce latencia y overhead.
    El cliente se cierra automáticamente al terminar el proceso.
    """
    global _httpx_client
    if _httpx_client is None:
        # Límites de conexiones para HubSpot API
        limits = httpx.Limits(
            max_keepalive_connections=10,
            max_connections=20,
            keepalive_expiry=30.0
        )
        _httpx_client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=limits,
            http2=False  # HTTP/2 puede causar problemas con algunos CDNs
        )
        logger.info("[Panel] Cliente HTTP global inicializado con connection pooling")
    return _httpx_client

# Flag para inicializar templates predefinidos solo una vez por proceso
_TEMPLATES_INITIALIZED: bool = False

# ============================================================================
# Etapas del Pipeline de HubSpot
# ============================================================================
PIPELINE_STAGES = {
    "1275156339": "Nuevo Lead",
    "1275156340": "En conversación",
    "1275156341": "Visita agendada",
    "1279054635": "Visita realizada",
    "1275312311": "Propuesta",
    "1279054636": "En estudio",
    "1275156342": "Cerrado ganado",
    "1279054637": "Cerrado vendido",
    "1323394565": "No responde",
    "1323394566": "Hasta 1.5M",
    "1323394567": "Hasta 2M",
    "1323394568": "Hasta 2.5M",
    "1323394569": "Mayor de 3M",
    "1323394570": "Local o Bodega",
    "1323393830": "Ana Contratos",
    "1323393831": "Propietarios",
    "1323393832": "Pagos y Servicios Publicos",
    "1323393833": "Ya encontro"
}

# Lista ordenada de etapas para el frontend
PIPELINE_STAGES_LIST = [
    {"id": "1275156339", "name": "Nuevo Lead"},
    {"id": "1275156340", "name": "En conversación"},
    {"id": "1275156341", "name": "Visita agendada"},
    {"id": "1279054635", "name": "Visita realizada"},
    {"id": "1275312311", "name": "Propuesta"},
    {"id": "1279054636", "name": "En estudio"},
    {"id": "1275156342", "name": "Cerrado ganado"},
    {"id": "1279054637", "name": "Cerrado vendido"},
    {"id": "1323394565", "name": "No responde"},
    {"id": "1323394566", "name": "Hasta 1.5M"},
    {"id": "1323394567", "name": "Hasta 2M"},
    {"id": "1323394568", "name": "Hasta 2.5M"},
    {"id": "1323394569", "name": "Mayor de 3M"},
    {"id": "1323394570", "name": "Local o Bodega"},
    {"id": "1323393830", "name": "Ana Contratos"},
    {"id": "1323393831", "name": "Propietarios"},
    {"id": "1323393832", "name": "Pagos y Servicios Publicos"},
    {"id": "1323393833", "name": "Ya encontro"}
]

@dataclass
class WindowStatus:
    """Estado de la ventana de 24 horas."""
    is_open: bool
    last_message_time: Optional[datetime]
    time_remaining_seconds: Optional[int]
    requires_template: bool
    message: str


# ============================================================================
# Funciones auxiliares
# ============================================================================

def _validate_api_key(api_key: Optional[str]) -> bool:
    """Valida la API key del admin."""
    if not api_key:
        logger.warning("[Panel] API Key no proporcionada en header")
        return False

    # Normalizar entrada (quitar espacios y comillas si existen)
    provided = api_key.strip().strip('"').strip("'")

    # Soporta dos nombres de variable de entorno por compatibilidad
    expected = os.getenv("ADMIN_API_KEY") or os.getenv("PANEL_API_KEY")

    # Si no hay configurada ninguna, usar el valor por defecto seguro para dev
    if not expected:
        expected = "protect_admin_2024_xK9mP3qR"

    # Normalizar valor esperado (quitar espacios y comillas si existen)
    expected = expected.strip().strip('"').strip("'")

    if provided == expected:
        logger.debug(f"[Panel] API Key validada correctamente")
        return True

    # Log para debugging (solo primeros 8 chars por seguridad)
    logger.warning(f"[Panel] API Key inválida. Recibido: '{provided[:8]}...' vs Esperado: '{expected[:8]}...'")
    return False


async def _get_contact_deal_info(contact_id: str, skip_cache: bool = False) -> Optional[Dict[str, Any]]:
    """
    Busca el deal asociado a un contacto y retorna su información.
    Usa caché en Redis (compartida entre workers Railway) para reducir llamadas a HubSpot.

    Args:
        contact_id: ID del contacto en HubSpot
        skip_cache: Si True, ignora el caché y consulta HubSpot directamente

    Returns:
        dict con deal_id y current_stage, o None si no hay deal
    """
    if not contact_id or not HUBSPOT_API_KEY:
        return None

    redis_client = await _get_redis_client()
    cache_key = f"{DEAL_CACHE_KEY_PREFIX}{contact_id}"

    # Verificar caché Redis
    if not skip_cache:
        try:
            cached_raw = await redis_client.get(cache_key)
            if cached_raw:
                cached = json.loads(cached_raw)
                logger.debug(f"[Panel] Deal info de Redis cache para {contact_id}")
                return {"deal_id": cached.get("deal_id"), "current_stage": cached.get("current_stage")}
        except Exception as cache_err:
            logger.debug(f"[Panel] Error leyendo deal cache Redis: {cache_err}")

    try:
        base_url = "https://api.hubapi.com"

        # Buscar deals asociados al contacto (usa cliente global)
        associations_url = f"{base_url}/crm/v3/objects/contacts/{contact_id}/associations/deals"
        response = await _hubspot_get(None, associations_url, HUBSPOT_API_KEY)

        if response.status_code == 429:
            logger.warning(f"[Panel] HubSpot 429 persistente al buscar deals de contacto {contact_id}")
            # Si hay rate limiting, intentar devolver del caché aunque esté expirado
            try:
                cached_raw = await redis_client.get(cache_key)
                if cached_raw:
                    cached = json.loads(cached_raw)
                    return {"deal_id": cached.get("deal_id"), "current_stage": cached.get("current_stage")}
            except Exception:
                pass
            return None
        if response.status_code != 200:
            logger.debug(
                f"[Panel] No se encontraron deals para contacto {contact_id} "
                f"(status={response.status_code})"
            )
            return None

        data = response.json()
        results = data.get("results", [])

        if not results:
            return None

        # Tomar el primer deal (más reciente)
        deal_id = results[0].get("id")

        # Obtener la etapa actual del deal (usa cliente global)
        deal_url = f"{base_url}/crm/v3/objects/deals/{deal_id}"
        deal_response = await _hubspot_get(
            None, deal_url, HUBSPOT_API_KEY, params={"properties": "dealstage"}
        )

        result = {"deal_id": deal_id, "current_stage": None}
        if deal_response.status_code == 200:
            deal_data = deal_response.json()
            result["current_stage"] = deal_data.get("properties", {}).get("dealstage")

        # Guardar en Redis con TTL
        try:
            cache_data = json.dumps({"deal_id": result["deal_id"], "current_stage": result["current_stage"]})
            await redis_client.setex(cache_key, DEAL_CACHE_TTL_SECONDS, cache_data)
        except Exception as write_err:
            logger.debug(f"[Panel] Error escribiendo deal cache Redis: {write_err}")

        return result

    except Exception as e:
        logger.debug(f"[Panel] Error obteniendo deal info para {contact_id}: {e}")
        return None


async def _invalidate_deal_cache(contact_id: str) -> None:
    """Invalida el caché de deal info en Redis para un contacto específico."""
    try:
        redis_client = await _get_redis_client()
        await redis_client.delete(f"{DEAL_CACHE_KEY_PREFIX}{contact_id}")
        logger.debug(f"[Panel] Caché de deal invalidado en Redis para {contact_id}")
    except Exception as e:
        logger.debug(f"[Panel] Error invalidando deal cache: {e}")


async def _get_redis_client() -> redis.Redis:
    """
    Obtiene cliente Redis con connection pool reutilizable.
    El pool se crea una sola vez por proceso — elimina el overhead de TCP
    handshake en cada llamada.
    """
    global _redis_pool
    if _redis_pool is None:
        is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
        redis_url = os.getenv("REDIS_URL") if is_railway else (
            os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
        )
        _redis_pool = redis.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=10,
        )
        logger.info("[Panel] Redis connection pool inicializado")
    return _redis_pool


async def _hubspot_post(client, url: str, payload: dict, api_key: str, max_retries: int = 3):
    """
    POST a HubSpot con retry automático en 429 (rate limit).
    Espera 12 segundos entre intentos — cubre la ventana de 10s de HubSpot.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    for attempt in range(1, max_retries + 1):
        response = await client.post(url, json=payload, headers=headers)
        if response.status_code == 429:
            wait = 12 * attempt
            logger.warning(
                f"[Panel] HubSpot 429 rate limit (intento {attempt}/{max_retries}), "
                f"esperando {wait}s..."
            )
            await asyncio.sleep(wait)
            continue
        return response
    # Último intento sin catch (dejará propagar el error)
    return await client.post(url, json=payload, headers=headers)


async def _hubspot_get(client, url: str, api_key: str, params: dict = None, max_retries: int = 2):
    """
    GET a HubSpot con retry automático en 429 (rate limit).
    Usa max_retries=2 para GET (vs 3 en POST) para no bloquear el panel demasiado tiempo.
    
    Args:
        client: Cliente httpx (si es None, usa el cliente global con pooling)
    """
    # Usar cliente global si no se provee uno
    if client is None:
        client = get_httpx_client()
    
    headers = {"Authorization": f"Bearer {api_key}"}
    for attempt in range(1, max_retries + 1):
        response = await client.get(url, headers=headers, params=params)
        if response.status_code == 429:
            wait = 12 * attempt
            logger.warning(
                f"[Panel] HubSpot 429 rate limit GET (intento {attempt}/{max_retries}), "
                f"esperando {wait}s..."
            )
            await asyncio.sleep(wait)
            continue
        return response
    return await client.get(url, headers=headers, params=params)


async def _hubspot_patch(url: str, payload: dict, api_key: str, max_retries: int = 3):
    """
    PATCH a HubSpot con retry automático en 429 (rate limit).
    Espera 12 segundos entre intentos — cubre la ventana de 10s de HubSpot.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    client = get_httpx_client()  # cliente global con pool persistente
    for attempt in range(1, max_retries + 1):
        response = await client.patch(url, headers=headers, json=payload)
        if response.status_code == 429:
            wait = 12 * attempt
            logger.warning(
                f"[Panel] HubSpot 429 rate limit PATCH (intento {attempt}/{max_retries}), "
                f"esperando {wait}s..."
            )
            await asyncio.sleep(wait)
            continue
        return response
    # Último intento (sin catch — deja propagar el error)
    return await client.patch(url, headers=headers, json=payload)


# ============================================================================
# BATCH REQUESTS - Optimización para reducir llamadas a HubSpot (429 fix)
# ============================================================================

async def _hubspot_batch_get_contacts(contact_ids: list[str]) -> Dict[str, Dict[str, Any]]:
    """
    Obtiene información de múltiples contactos en UNA sola llamada batch.
    
    Usa POST /crm/v3/objects/contacts/batch/read que permite hasta 100 IDs por llamada.
    Esto reduce drásticamente los 429 rate limits al cargar el panel.
    
    Args:
        contact_ids: Lista de IDs de contactos HubSpot (max 100)
        
    Returns:
        Dict[contact_id, properties] con firstname, lastname, email, phone
    """
    if not contact_ids or not HUBSPOT_API_KEY:
        return {}
    
    # HubSpot batch acepta máximo 100 IDs
    contact_ids = contact_ids[:100]
    
    url = "https://api.hubapi.com/crm/v3/objects/contacts/batch/read"
    payload = {
        "properties": ["firstname", "lastname", "email", "phone"],
        "inputs": [{"id": cid} for cid in contact_ids]
    }
    
    try:
        client = get_httpx_client()
        response = await _hubspot_post(client, url, payload, HUBSPOT_API_KEY, max_retries=2)
        
        if response.status_code in (200, 207):
            data = response.json()
            results = {}
            for contact in data.get("results", []):
                cid = contact.get("id")
                props = contact.get("properties", {})
                results[cid] = {
                    "firstname": props.get("firstname", ""),
                    "lastname": props.get("lastname", ""),
                    "email": props.get("email"),
                    "phone": props.get("phone")
                }
            if response.status_code == 207:
                logger.info(f"[Panel] Batch HubSpot 207 (parcial): {len(results)}/{len(contact_ids)} contactos válidos")
            else:
                logger.info(f"[Panel] Batch HubSpot: obtenidos {len(results)}/{len(contact_ids)} contactos")
            return results
        else:
            logger.warning(f"[Panel] Batch HubSpot falló: {response.status_code}")
            return {}
            
    except Exception as e:
        logger.error(f"[Panel] Error en batch HubSpot: {type(e).__name__}: {e}", exc_info=True)
        return {}


# Caché temporal para datos de contactos (se llena con batch, se consume en enriquecimiento)
_batch_contact_cache: Dict[str, Dict[str, Any]] = {}


async def _update_deal_to_en_conversacion(contact_id: str) -> None:
    """
    Busca el deal activo del contacto y actualiza su stage a 'En conversación'
    (1275156340) cuando la asesora inicia una conversación activa.
    Se ejecuta en background — no bloquea el envío del mensaje.
    """
    import httpx
    hubspot_api_key = os.getenv("HUBSPOT_API_KEY")
    if not hubspot_api_key or not contact_id:
        return
    try:
        # Buscar deals asociados al contacto
        search_url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}/associations/deals"
        headers = {"Authorization": f"Bearer {hubspot_api_key}"}

        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(search_url, headers=headers)
            if r.status_code != 200:
                logger.warning(f"[Panel] No se pudo obtener deals para contact {contact_id}: {r.status_code}")
                return

            results = r.json().get("results", [])
            if not results:
                logger.debug(f"[Panel] Sin deals asociados a contacto {contact_id}")
                return

            # Actualizar el primer deal encontrado
            deal_id = results[0].get("id")
            patch_url = f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}"

            # Solo avanzar si el deal está en "Nuevo Lead" — no sobreescribir etapas más avanzadas
            stage_r = await client.get(
                patch_url,
                headers=headers,
                params={"properties": "dealstage"}
            )
            if stage_r.status_code == 200:
                current_stage = stage_r.json().get("properties", {}).get("dealstage", "")
                if current_stage and current_stage != HUBSPOT_STAGE_NUEVO_LEAD:
                    logger.info(
                        f"[Panel] Deal {deal_id} ya en etapa '{current_stage}', "
                        f"no se sobreescribe con 'En conversación'"
                    )
                    return

            patch_payload = {
                "properties": {
                    "dealstage": HUBSPOT_STAGE_EN_CONVERSACION,  # 1275156340
                    "pipeline": HUBSPOT_PIPELINE_ID              # 854756009
                }
            }
            pr = await client.patch(
                patch_url,
                json=patch_payload,
                headers={**headers, "Content-Type": "application/json"}
            )
            if pr.status_code in [200, 201]:
                logger.info(f"[Panel] Deal {deal_id} actualizado a 'En conversación' (contact={contact_id})")
                # Invalidar caché de deal info para este contacto
                await _invalidate_deal_cache(contact_id)
            else:
                logger.warning(f"[Panel] Error actualizando deal stage: {pr.status_code} - {pr.text}")

    except Exception as e:
        logger.error(f"[Panel] Error en _update_deal_to_en_conversacion: {e}")


async def check_24h_window(phone_normalized: str) -> WindowStatus:
    """
    Verifica el estado de la ventana de 24 horas de WhatsApp.

    WhatsApp solo permite enviar mensajes de texto libre durante 24 horas
    después del último mensaje del cliente. Fuera de esa ventana,
    solo se pueden enviar Templates pre-aprobados.
    """
    try:
        r = await _get_redis_client()
        key = f"{LAST_CLIENT_MESSAGE_PREFIX}{phone_normalized}"

        last_msg_str = await r.get(key)

        if not last_msg_str:
            # No hay registro - asumir ventana cerrada por seguridad
            return WindowStatus(
                is_open=False,
                last_message_time=None,
                time_remaining_seconds=None,
                requires_template=True,
                message="No hay registro de mensaje reciente del cliente. Se requiere Template de WhatsApp."
            )

        # Normalizar formato antes de parsear (+00:00Z es inválido en Python < 3.11)
        _ts = last_msg_str.replace('+00:00Z', '+00:00')
        if 'Z' in _ts:
            _ts = _ts.replace('Z', '+00:00')
        last_msg_time = datetime.fromisoformat(_ts)
        now = datetime.now(timezone.utc)

        # Asegurar que last_msg_time tenga timezone
        if last_msg_time.tzinfo is None:
            last_msg_time = last_msg_time.replace(tzinfo=timezone.utc)

        elapsed = (now - last_msg_time).total_seconds()

        if elapsed < WHATSAPP_WINDOW_SECONDS:
            remaining = int(WHATSAPP_WINDOW_SECONDS - elapsed)
            hours = remaining // 3600
            minutes = (remaining % 3600) // 60

            return WindowStatus(
                is_open=True,
                last_message_time=last_msg_time,
                time_remaining_seconds=remaining,
                requires_template=False,
                message=f"Ventana abierta. Tiempo restante: {hours}h {minutes}m"
            )
        else:
            return WindowStatus(
                is_open=False,
                last_message_time=last_msg_time,
                time_remaining_seconds=0,
                requires_template=True,
                message="Ventana cerrada (>24h). Se requiere Template de WhatsApp."
            )

    except Exception as e:
        logger.error(f"[Panel] Error verificando ventana 24h: {e}")
        # En caso de error, asumir ventana abierta para no bloquear
        return WindowStatus(
            is_open=True,
            last_message_time=None,
            time_remaining_seconds=None,
            requires_template=False,
            message="No se pudo verificar la ventana. Intente enviar el mensaje."
        )


async def update_last_client_message(phone_normalized: str) -> None:
    """
    Actualiza el timestamp del último mensaje del cliente.

    Llamar desde webhook_handler cuando llega un mensaje del cliente.
    """
    try:
        r = await _get_redis_client()
        key = f"{LAST_CLIENT_MESSAGE_PREFIX}{phone_normalized}"

        # Guardar con TTL de 25 horas (un poco más que la ventana)
        await r.set(
            key,
            datetime.now(timezone.utc).isoformat() + "Z",
            ex=25 * 60 * 60
        )

        logger.debug(f"[Panel] Actualizado último mensaje del cliente: {phone_normalized}")

    except Exception as e:
        logger.error(f"[Panel] Error actualizando último mensaje: {e}")


# ============================================================================
# Funciones CRUD de Templates
# ============================================================================

async def _init_default_templates():
    """
    Sincroniza los templates predefinidos a Redis.
    Siempre sobreescribe los defaults para que cambios en templates.py
    (ej. nuevos content_sid aprobados) se propaguen sin necesidad de
    limpiar Redis manualmente.
    Además elimina claves de templates predefinidos que ya no existen en
    DEFAULT_TEMPLATES (por ejemplo, plantillas eliminadas del código).
    Los templates personalizados (por asesor) no se tocan.
    """
    global _TEMPLATES_INITIALIZED
    if _TEMPLATES_INITIALIZED:
        return
    try:
        r = await _get_redis_client()
        # Escribir/actualizar los templates actuales
        for template_id, template_data in DEFAULT_TEMPLATES.items():
            key = f"{DEFAULT_TEMPLATE_PREFIX}{template_id}"
            await r.set(key, json.dumps(template_data))
            logger.debug(f"[Templates] Template predefinido sincronizado: {template_id}")
        # Eliminar claves huérfanas (templates eliminados del código)
        existing_keys = await r.keys(f"{DEFAULT_TEMPLATE_PREFIX}*")
        for key in existing_keys:
            key_str = key if isinstance(key, str) else key.decode()
            template_id = key_str.replace(DEFAULT_TEMPLATE_PREFIX, "")
            if template_id not in DEFAULT_TEMPLATES:
                await r.delete(key_str)
                logger.info(f"[Templates] Template huérfano eliminado de Redis: {template_id}")
        _TEMPLATES_INITIALIZED = True
        logger.info("[Templates] Templates predefinidos sincronizados (%d)", len(DEFAULT_TEMPLATES))
    except Exception as e:
        logger.error(f"[Templates] Error inicializando templates: {e}")


# ============================================================================
# Caché de templates en memoria (evita SCAN costoso en cada request)
# ============================================================================
_TEMPLATES_CACHE: list = []
_TEMPLATES_CACHE_TS: float = 0
_TEMPLATES_CACHE_TTL: float = 60.0  # 60 segundos de caché


async def _get_all_templates() -> list:
    """
    Obtiene todos los templates de Redis con caché en memoria.
    
    Optimización: KEYS es más rápido que SCAN para <100 keys porque
    es un solo roundtrip vs múltiples iteraciones de SCAN.
    
    Caché: Los templates raramente cambian, así que cacheamos 60s.
    """
    import time
    global _TEMPLATES_CACHE, _TEMPLATES_CACHE_TS
    
    _t0 = time.monotonic()
    
    # Verificar caché primero
    if _TEMPLATES_CACHE and (time.monotonic() - _TEMPLATES_CACHE_TS) < _TEMPLATES_CACHE_TTL:
        logger.debug(f"[Templates][TIMING] Cache HIT: {len(_TEMPLATES_CACHE)} templates")
        return _TEMPLATES_CACHE
    
    # Cambiado: ahora requiere advisor_id como argumento
    raise NotImplementedError("Usar _get_all_templates_by_advisor(advisor_id)")

async def _get_all_templates_by_advisor(advisor_id: str) -> list:
    import time
    r = await _get_redis_client()
    # Personales
    personal_keys = await r.keys(f"{TEMPLATE_PREFIX}{advisor_id}:*")
    # Defaults
    default_keys = await r.keys(f"{DEFAULT_TEMPLATE_PREFIX}*")
    templates = []
    personal_ids = set()
    pipe = r.pipeline()
    for key in personal_keys:
        pipe.get(key)
    personal_values = await pipe.execute()
    for data in personal_values:
        if data:
            tpl = json.loads(data)
            tpl['is_default'] = False
            templates.append(tpl)
            personal_ids.add(tpl['id'])
    pipe = r.pipeline()
    for key in default_keys:
        pipe.get(key)
    default_values = await pipe.execute()
    for data in default_values:
        if data:
            tpl = json.loads(data)
            if tpl['id'] not in personal_ids:
                tpl['is_default'] = True
                templates.append(tpl)
    templates.sort(key=lambda x: (x.get("category", ""), x.get("name", "")))
    return templates


async def _get_template(template_id: str) -> Optional[dict]:
    """Obtiene un template específico de Redis."""
    # Cambiado: ahora requiere advisor_id como argumento
    raise NotImplementedError("Usar _get_template_by_advisor(advisor_id, template_id)")

async def _get_template_by_advisor(advisor_id: str, template_id: str) -> Optional[dict]:
    r = await _get_redis_client()
    key = f"{TEMPLATE_PREFIX}{advisor_id}:{template_id}"
    data = await r.get(key)
    if data:
        return json.loads(data)
    # Fallback a default
    key = f"{DEFAULT_TEMPLATE_PREFIX}{template_id}"
    data = await r.get(key)
    if data:
        tpl = json.loads(data)
        tpl['is_default'] = True
        return tpl
    return None


def _invalidate_templates_cache():
    """Invalida el caché de templates forzando recarga en próximo request."""
    global _TEMPLATES_CACHE_TS
    _TEMPLATES_CACHE_TS = 0
    logger.debug("[Templates] Caché invalidado")


async def _save_template(template_data: dict) -> bool:
    """Guarda o actualiza un template en Redis."""
    # Cambiado: ahora requiere advisor_id como argumento
    raise NotImplementedError("Usar _save_template_by_advisor(advisor_id, template_data)")

async def _save_template_by_advisor(advisor_id: str, template_data: dict) -> bool:
    r = await _get_redis_client()
    template_id = template_data.get("id")
    if not template_id:
        return False
    key = f"{TEMPLATE_PREFIX}{advisor_id}:{template_id}"
    await r.set(key, json.dumps(template_data))
    logger.info(f"[Templates] Template guardado: {template_id} para {advisor_id}")
    return True


async def _delete_template(template_id: str) -> bool:
    """Elimina un template de Redis."""
    # Cambiado: ahora requiere advisor_id como argumento
    raise NotImplementedError("Usar _delete_template_by_advisor(advisor_id, template_id)")

async def _delete_template_by_advisor(advisor_id: str, template_id: str) -> bool:
    r = await _get_redis_client()
    key = f"{TEMPLATE_PREFIX}{advisor_id}:{template_id}"
    data = await r.get(key)
    if data:
        template = json.loads(data)
        if template.get("is_default"):
            logger.warning(f"[Templates] No se puede eliminar template predefinido: {template_id}")
            return False
    result = await r.delete(key)
    if result > 0:
        logger.info(f"[Templates] Template eliminado: {template_id} para {advisor_id}")
        return True
    return False

# ============================================================================
# Endpoints de API
# ============================================================================

@router.post("/send-message")
async def send_message(
    background_tasks: BackgroundTasks,
    to: Optional[str] = Form(None, description="Número de destino (+573001234567)"),
    phone: Optional[str] = Form(None, description="Alias legacy: 'phone' (compatibilidad)"),
    body: Optional[str] = Form(None, description="Contenido del mensaje"),
    contact_id: Optional[str] = Form(None, description="ID del contacto en HubSpot"),
    canal: Optional[str] = Form(None, description="Canal de origen para segregación"),
    force_send: bool = Form(False, description="Forzar envío aunque ventana esté cerrada"),
    media_file: Optional[UploadFile] = File(None, description="Archivo multimedia (imagen/audio)"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Envía un mensaje de WhatsApp desde el panel de asesores.
    Soporta envío de texto, multimedia (imagen/audio), o ambos.
    """
    # Validar API Key
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida o no configurada")

    # Validar que haya contenido (texto o archivo)
    if not body and not media_file:
        raise HTTPException(status_code=400, detail="Debe enviar un mensaje de texto o un archivo multimedia")

    # Si solo hay texto, validar que no esté vacío
    if body and not body.strip() and not media_file:
        raise HTTPException(status_code=400, detail="El mensaje no puede estar vacío")

    # Normalizar número (aceptar legacy 'phone' form param)
    target_number = to or phone
    if not target_number:
        raise HTTPException(status_code=400, detail="Campo 'to' (o 'phone') es requerido")

    normalizer = PhoneNormalizer()
    validation = normalizer.normalize(target_number)

    if not validation.is_valid:
        raise HTTPException(
            status_code=400,
            detail=f"Número inválido: {validation.error_message}"
        )

    phone_normalized = validation.normalized

    # =========================================================================
    # ASIGNACIÓN DE CANAL POR DEFECTO
    # Si 'canal' es nulo, vacío, o literalmente "null" (que a veces manda JS)
    # =========================================================================
    if not canal or canal.strip() == "" or canal.lower() == "null":
        canal_final = "whatsapp"
    else:
        canal_final = canal.lower().strip()

    # Verificar ventana de 24 horas
    window_status = await check_24h_window(phone_normalized)

    if not window_status.is_open and not force_send:
        return JSONResponse(
            status_code=200,
            content={
                "status": "warning",
                "window_closed": True,
                "message": window_status.message,
                "requires_template": True,
                "hint": "Use force_send=true para enviar de todas formas (requiere Template)"
            }
        )

    # Verificar disponibilidad de Twilio
    if not twilio_client.is_available:
        raise HTTPException(
            status_code=503,
            detail="Twilio no está configurado correctamente"
        )

    # Obtener/crear contacto si no se proporcionó
    if not contact_id:
        try:
            contact_manager = ContactManager()
            contact_info = await contact_manager.identify_or_create_contact(
                phone_raw=phone_normalized,  # Usar número normalizado, no 'to' que puede ser None
                source_channel="panel_asesor"
            )
            contact_id = contact_info.contact_id
        except Exception as e:
            logger.warning(f"[Panel] No se pudo obtener contacto: {e}")
            # Continuar sin contact_id

    # Pausar Sofía y cambiar a IN_CONVERSATION (asesora está chateando activamente)
    # SEGREGACIÓN POR CANAL: Usar el canal proporcionado para operaciones de estado
    try:
        # Redis URL unificado (Railway interna, local pública)
        is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
        redis_url = os.getenv("REDIS_URL") if is_railway else (
            os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
        )
        state_manager = ConversationStateManager(redis_url)

        canal_info = f":{canal_final}"

        # Verificar estado actual (con canal)
        current_status = await state_manager.get_status(phone_normalized, canal_final)

        if current_status in [ConversationStatus.HUMAN_ACTIVE, ConversationStatus.PENDING_HANDOFF]:
            # Ya está en espera, cambiar a IN_CONVERSATION (asesora está atendiendo)
            await state_manager.set_status(
                phone_normalized,
                ConversationStatus.IN_CONVERSATION,
                ttl=state_manager.HANDOFF_TTL_SECONDS,
                canal=canal_final
            )
            logger.info(f"[Panel] Estado cambiado a IN_CONVERSATION para {phone_normalized}{canal_info}")
            # Sincronizar deal a "En conversación" en HubSpot (background)
            if contact_id:
                background_tasks.add_task(_update_deal_to_en_conversacion, contact_id)
        elif current_status == ConversationStatus.IN_CONVERSATION:
            # Ya está en conversación, solo refrescar TTL
            await state_manager.set_status(
                phone_normalized,
                ConversationStatus.IN_CONVERSATION,
                ttl=state_manager.HANDOFF_TTL_SECONDS,
                canal=canal_final
            )
            logger.info(f"[Panel] TTL refrescado para IN_CONVERSATION: {phone_normalized}{canal_info}")
        else:
            # Era BOT_ACTIVE o CLOSED, activar humano y cambiar a IN_CONVERSATION
            await state_manager.activate_human(phone_normalized, canal_origen=canal_final)
            await state_manager.set_status(
                phone_normalized,
                ConversationStatus.IN_CONVERSATION,
                ttl=state_manager.HANDOFF_TTL_SECONDS,
                canal=canal_final
            )
            logger.info(f"[Panel] Sofía pausada y estado IN_CONVERSATION para {phone_normalized}{canal_info}")
    except Exception as e:
        logger.warning(f"[Panel] Error manejando estado: {e}")

    # =========================================================================
    # Procesar archivo multimedia si se envió (Bunny.net Storage)
    # =========================================================================
    permanent_media_url = None
    media_type = None

    if media_file and media_file.filename:
        try:
            # Leer contenido del archivo
            file_bytes = await media_file.read()
            content_type = media_file.content_type or "application/octet-stream"

            logger.info(f"[Panel] 📁 Archivo recibido: {media_file.filename}, tipo={content_type}, tamaño={len(file_bytes)} bytes")

            # Subir a Bunny.net Storage (CDN)
            permanent_media_url = await media_processor.upload_outgoing_media(
                file_bytes=file_bytes,
                content_type=content_type,
                phone=phone_normalized
            )

            logger.info(f"[Panel] 📤 Bunny.net URL obtenida: {permanent_media_url}")

            # Determinar tipo de media (incluir webm como audio)
            if content_type.startswith("image/"):
                media_type = "image"
            elif content_type.startswith("audio/") or "webm" in content_type.lower():
                media_type = "audio"
            else:
                media_type = "file"

            logger.info(f"[Panel] ✅ Multimedia subido a Bunny.net: {media_type} -> {permanent_media_url}")

        except Exception as e:
            logger.error(f"[Panel] Error procesando multimedia: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Error procesando archivo multimedia: {str(e)}"
            )

    # Preparar body para envío
    message_body = body.strip() if body else ""

    # Enviar mensaje con multimedia si corresponde
    result = await twilio_client.send_whatsapp_message(
        to=phone_normalized,
        body=message_body or "📎",  # Twilio requiere body, usar emoji si solo hay media
        media_url=permanent_media_url
    )

    if result["status"] == "success":
        message_sid = result.get("message_sid")

        # =====================================================================
        # PASO 1: Guardar en MongoDB INMEDIATAMENTE (~5ms)
        # MongoDB es la fuente de verdad para el panel en tiempo real
        # =====================================================================
        mongo_message_id = None
        try:
            mongo_manager = get_mongo_manager()

            # Construir diccionario `media` compatible con MongoDBManager.save_message
            media_dict: Optional[Dict[str, Any]] = None
            if permanent_media_url:
                media_dict = {
                    "permanent_url": permanent_media_url,
                    "type": media_type,
                    "size_bytes": len(file_bytes) if 'file_bytes' in locals() and file_bytes is not None else None,
                    "format": (content_type.split('/')[-1] if content_type else None),
                    "duration_seconds": None,
                    "processed_at": datetime.utcnow(),
                    "uploaded_by": "advisor",
                }

            mongo_message_id = await mongo_manager.save_message(
                phone=phone_normalized,
                content=message_body or (f"[{media_type.upper()}]" if media_type else message_body),
                sender="advisor",
                channel=canal_final,
                hubspot_contact_id=contact_id,
                message_sid=message_sid,
                metadata={"source": "Manual via Panel"},
                media=media_dict
            )
            if mongo_message_id:
                logger.info(f"[Panel] Mensaje guardado en MongoDB: {mongo_message_id}, media_type={media_type}")
        except Exception as e:
            logger.error(f"[Panel] Error guardando en MongoDB: {e}")
            # No bloquear el flujo si MongoDB falla

        # =====================================================================
        # PASO 2: Registrar en HubSpot Timeline (BACKGROUND - no bloqueante)
        # HubSpot es archivo histórico, no afecta la experiencia del panel
        # =====================================================================
        if contact_id:
            # Construir contenido para HubSpot incluyendo link multimedia si existe
            hubspot_content = message_body
            if permanent_media_url:
                media_label = {"image": "📷 Imagen", "audio": "🎵 Audio", "file": "📎 Archivo"}.get(media_type, "📎 Archivo")
                hubspot_content = f"{message_body}\n\n{media_label}: {permanent_media_url}" if message_body else f"{media_label}: {permanent_media_url}"

            background_tasks.add_task(
                _log_advisor_message_to_hubspot,
                contact_id,
                hubspot_content,
                phone_normalized,
                "Manual via Panel",
                mongo_message_id  # Para marcar como sincronizado después
            )

        # Actualizar timestamp del asesor para TTL diferenciado
        background_tasks.add_task(
            _update_advisor_timestamp,
            phone_normalized,
            canal_final
        )

        # Mover contacto al top de la lista (actualizar score ZSET)
        try:
            _rc = await _get_redis_client()
            _now_ts = datetime.now(timezone.utc).timestamp()
            await _rc.zadd("active_conversations_sorted", {f"{phone_normalized}:{canal_final}": _now_ts})
            logger.info(f"[Panel] ZSET actualizado para {phone_normalized}:{canal_final}")
        except Exception as _ze:
            logger.warning(f"[Panel] No se pudo actualizar ZSET en send_message: {_ze}")

        # Notificar a todos los asesores via WS para que refresquen la lista
        try:
            await ws_manager.broadcast({
                "type": "contact_updated",
                "phone": phone_normalized,
                "action": "new_message",
                "canal": canal_final
            })
        except Exception as _we:
            logger.warning(f"[Panel] Error broadcast WS en send_message: {_we}")

        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message_sid": message_sid,
                "mongo_id": mongo_message_id,
                "to": phone_normalized,
                "contact_id": contact_id,
                "canal": canal_final,
                "window_status": {
                    "is_open": window_status.is_open,
                    "time_remaining": window_status.time_remaining_seconds
                },
                "sofia_paused": True,
                "message_source": "Manual via Panel",
                "media_url": permanent_media_url,
                "media_type": media_type
            }
        )
    else:
        raise HTTPException(
            status_code=500,
            detail=f"Error enviando mensaje: {result.get('message')}"
        )


@router.post("/send-message-json")
async def send_message_json(
    background_tasks: BackgroundTasks,
    request: SendMessageRequest,
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Envía un mensaje de WhatsApp desde el panel (formato JSON).
    
    Endpoint alternativo para testing E2E y clientes que prefieren JSON.
    Solo soporta texto (sin multimedia).
    
    Headers:
        X-API-Key: API key de autenticación
        Content-Type: application/json
    
    Body (JSON):
        {
            "phone": "+573001112235",
            "message": "Hola Carlos, soy el asesor asignado.",
            "canal": "whatsapp"
        }
    """
    # Validar API Key
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida o no configurada")

    # Obtener mensaje (soportar 'message' o 'body')
    body = request.message or request.body
    
    # Validar que haya contenido
    if not body or not body.strip():
        raise HTTPException(status_code=400, detail="El mensaje no puede estar vacío")

    # Normalizar número
    normalizer = PhoneNormalizer()
    validation = normalizer.normalize(request.phone)

    if not validation.is_valid:
        raise HTTPException(
            status_code=400,
            detail=f"Número inválido: {validation.error_message}"
        )

    phone_normalized = validation.normalized

    # Normalizar canal
    canal_final = request.canal.lower().strip() if request.canal else "whatsapp"
    if canal_final == "null" or not canal_final:
        canal_final = "whatsapp"

    # Verificar ventana de 24 horas
    window_status = await check_24h_window(phone_normalized)

    if not window_status.is_open and not request.force_send:
        return JSONResponse(
            status_code=200,
            content={
                "status": "warning",
                "window_closed": True,
                "message": window_status.message,
                "requires_template": True,
                "hint": "Use force_send=true para enviar de todas formas (requiere Template)"
            }
        )

    # Verificar disponibilidad de Twilio
    if not twilio_client.is_available:
        raise HTTPException(
            status_code=503,
            detail="Twilio no está configurado correctamente"
        )

    # Obtener/crear contacto si no se proporcionó
    contact_id = request.contact_id
    if not contact_id:
        try:
            contact_manager = ContactManager()
            contact_info = await contact_manager.identify_or_create_contact(
                phone_raw=phone_normalized,
                source_channel="panel_asesor"
            )
            contact_id = contact_info.contact_id
        except Exception as e:
            logger.warning(f"[Panel-JSON] No se pudo obtener contacto: {e}")

    # Pausar Sofía y cambiar estado
    try:
        is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
        redis_url = os.getenv("REDIS_URL") if is_railway else (
            os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
        )
        state_manager = ConversationStateManager(redis_url)

        current_status = await state_manager.get_status(phone_normalized, canal_final)

        if current_status in [ConversationStatus.HUMAN_ACTIVE, ConversationStatus.PENDING_HANDOFF, ConversationStatus.IN_CONVERSATION]:
            await state_manager.set_status(
                phone_normalized,
                ConversationStatus.IN_CONVERSATION,
                ttl=state_manager.HANDOFF_TTL_SECONDS,
                canal=canal_final
            )
            logger.info(f"[Panel-JSON] Estado: IN_CONVERSATION para {phone_normalized}:{canal_final}")
        else:
            # Crear nuevo estado para la conversación
            await state_manager.set_status(
                phone_normalized,
                ConversationStatus.IN_CONVERSATION,
                ttl=state_manager.HANDOFF_TTL_SECONDS,
                canal=canal_final
            )
            logger.info(f"[Panel-JSON] Nuevo estado IN_CONVERSATION para {phone_normalized}:{canal_final}")

    except Exception as e:
        logger.error(f"[Panel-JSON] Error actualizando estado: {e}")

    # Enviar mensaje vía Twilio
    result = await twilio_client.send_whatsapp_message(
        to=phone_normalized,
        body=body
    )

    if result["status"] == "success":
        message_sid = result.get("message_sid")
        logger.info(f"[Panel-JSON] ✅ Mensaje enviado: {message_sid} a {phone_normalized}")

        # Guardar en MongoDB
        mongo_message_id = None
        try:
            mongo_manager = get_mongo_manager()
            mongo_message_id = await mongo_manager.save_message(
                phone=phone_normalized,
                content=body,
                sender="advisor",
                channel=canal_final,
                hubspot_contact_id=contact_id,
                message_sid=message_sid,
                metadata={"source": "Panel JSON API"}
            )
        except Exception as e:
            logger.error(f"[Panel-JSON] Error guardando en MongoDB: {e}")

        # Registrar en HubSpot Timeline
        if contact_id:
            background_tasks.add_task(
                _log_advisor_message_to_hubspot,
                contact_id,
                body,
                phone_normalized,
                "Panel JSON API",
                mongo_message_id
            )

        # Mover contacto al top de la lista (actualizar score ZSET)
        try:
            _rc = await _get_redis_client()
            _now_ts = datetime.now(timezone.utc).timestamp()
            await _rc.zadd("active_conversations_sorted", {f"{phone_normalized}:{canal_final}": _now_ts})
            logger.info(f"[Panel-JSON] ZSET actualizado para {phone_normalized}:{canal_final}")
        except Exception as _ze:
            logger.warning(f"[Panel-JSON] No se pudo actualizar ZSET: {_ze}")

        # Notificar a todos los asesores via WS
        try:
            await ws_manager.broadcast({
                "type": "contact_updated",
                "phone": phone_normalized,
                "action": "new_message",
                "canal": canal_final
            })
        except Exception as _we:
            logger.warning(f"[Panel-JSON] Error broadcast WS: {_we}")

        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message_sid": message_sid,
                "mongo_id": mongo_message_id,
                "to": phone_normalized,
                "contact_id": contact_id,
                "canal": canal_final,
                "window_status": {
                    "is_open": window_status.is_open,
                    "time_remaining": window_status.time_remaining_seconds
                },
                "sofia_paused": True,
                "message_source": "Panel JSON API"
            }
        )
    else:
        raise HTTPException(
            status_code=500,
            detail=f"Error enviando mensaje: {result.get('message')}"
        )


@router.post("/send-template")
async def send_template_message(
    background_tasks: BackgroundTasks,
    to: str = Form(..., description="Número de destino (+573001234567)"),
    template_id: str = Form("reactivacion_general", description="ID del template a usar"),
    variables: str = Form("{}", description="JSON con variables para el template"),
    contact_id: Optional[str] = Form(None, description="ID del contacto en HubSpot"),
    canal: Optional[str] = Form(None, description="Canal de origen para segregación"),
    advisor_id: Optional[str] = Form(None, description="ID del asesor que envía el template"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Envía un mensaje de Template (plantilla) de WhatsApp para reactivar conversación.

    Este endpoint se usa cuando la ventana de 24 horas está cerrada.
    Los templates son mensajes pre-aprobados por Meta que pueden enviarse
    fuera de la ventana de 24 horas.
    """
    # Validar API Key
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # Normalizar número
    normalizer = PhoneNormalizer()
    validation = normalizer.normalize(to)

    if not validation.is_valid:
        raise HTTPException(
            status_code=400,
            detail=f"Número inválido: {validation.error_message}"
        )

    phone_normalized = validation.normalized

    # =========================================================================
    # ASIGNACIÓN DE CANAL POR DEFECTO
    # Si 'canal' es nulo, vacío, o literalmente "null" (que a veces manda JS)
    # =========================================================================
    if not canal or canal.strip() == "" or canal.lower() == "null":
        canal_final = "whatsapp"
    else:
        canal_final = canal.lower().strip()

    # Verificar disponibilidad de Twilio
    if not twilio_client.is_available:
        raise HTTPException(
            status_code=503,
            detail="Twilio no está configurado correctamente"
        )

    import time
    _t0 = time.monotonic()

    # Inicializar templates predefinidos si es necesario
    await _init_default_templates()
    logger.info(f"[Panel][TIMING] _init_default_templates: {(time.monotonic()-_t0)*1000:.1f}ms")

    # Obtener template de Redis
    _t1 = time.monotonic()
    template = await _get_template_by_advisor(advisor_id or "default", template_id)
    logger.info(f"[Panel][TIMING] _get_template_by_advisor: {(time.monotonic()-_t1)*1000:.1f}ms")
    if not template:
        raise HTTPException(
            status_code=404,
            detail=f"Template '{template_id}' no encontrado"
        )

    # Parsear variables
    try:
        vars_dict = json.loads(variables) if variables else {}
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400,
            detail="Variables JSON inválidas"
        )

    # Reemplazar variables en el body del template
    template_body = template.get("body", "")
    try:
        # Usar format_map para manejar variables faltantes graciosamente
        class SafeDict(dict):
            def __missing__(self, key):
                return f"{{{key}}}"  # Mantiene {variable} si no se proporciona

        template_message = template_body.format_map(SafeDict(vars_dict))
    except Exception as e:
        logger.warning(f"[Panel] Error formateando template: {e}")
        template_message = template_body  # Usar el body sin formato si hay error

    logger.info(f"[Panel] Enviando template '{template_id}' a {phone_normalized}")

    # Construir content_variables numeradas para Twilio Content API
    # (solo se usa cuando el template tiene content_sid aprobado por Meta)
    content_sid = template.get("content_sid")
    variables_map = template.get("content_variables_map", [])
    content_variables = None
    if content_sid and variables_map:
        content_variables = {
            str(i + 1): vars_dict.get(var_name, "")
            for i, var_name in enumerate(variables_map)
        }
        logger.info(
            f"[Panel] Usando ContentSid={content_sid} con variables={content_variables}"
        )
    elif not content_sid:
        logger.warning(
            f"[Panel] Template '{template_id}' sin content_sid — "
            f"se enviará como texto plano (fallará con ventana cerrada)"
        )

    # Enviar mensaje via Twilio
    _t2 = time.monotonic()
    result = await twilio_client.send_whatsapp_message(
        to=phone_normalized,
        body=template_message,
        content_sid=content_sid,
        content_variables=content_variables,
    )
    logger.info(
        f"[Panel][TIMING] twilio.send_whatsapp_message: {(time.monotonic()-_t2)*1000:.1f}ms | "
        f"total_hasta_aqui: {(time.monotonic()-_t0)*1000:.1f}ms | "
        f"status: {result.get('status')}"
    )

    if result["status"] == "success":
        message_sid = result.get("message_sid")
        template_content = f"[TEMPLATE: {template.get('name', template_id)}] {template_message}"

        # =====================================================================
        # PASO 1: Guardar en MongoDB INMEDIATAMENTE
        # =====================================================================
        mongo_message_id = None
        try:
            mongo_manager = get_mongo_manager()
            mongo_message_id = await mongo_manager.save_message(
                phone=phone_normalized,
                content=template_content,
                sender="advisor",
                channel=canal_final,
                hubspot_contact_id=contact_id,
                message_sid=message_sid,
                metadata={"source": "Template via Panel", "template_id": template_id}
            )
            if mongo_message_id:
                logger.info(f"[Panel] Template guardado en MongoDB: {mongo_message_id}")
        except Exception as e:
            logger.error(f"[Panel] Error guardando template en MongoDB: {e}")

        # =====================================================================
        # PASO 2: Registrar en HubSpot Timeline (BACKGROUND)
        # =====================================================================
        if contact_id:
            background_tasks.add_task(
                _log_advisor_message_to_hubspot,
                contact_id,
                template_content,
                phone_normalized,
                "Template via Panel",
                mongo_message_id
            )

        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message_sid": message_sid,
                "mongo_id": mongo_message_id,
                "to": phone_normalized,
                "contact_id": contact_id,
                "canal": canal_final,
                "template_id": template_id,
                "template_name": template.get("name"),
                "template_sent": True,
                "message": "Template enviado. La conversación se reabrirá cuando el cliente responda."
            }
        )
    else:
        raise HTTPException(
            status_code=500,
            detail=f"Error enviando template: {result.get('message')}"
        )


# ============================================================================
# Endpoints CRUD de Templates
# ============================================================================

@router.get("/templates")
async def list_templates(
    advisor_id: str = Query(...),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Lista todos los templates disponibles para un asesor."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    await _init_default_templates()
    templates = await _get_all_templates_by_advisor(advisor_id)
    categories = {}
    for t in templates:
        cat = t.get("category", "otros")
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(t)
    return {
        "templates": templates,
        "by_category": categories,
        "total": len(templates)
    }


@router.get("/templates/{template_id}")
async def get_template_by_id(
    template_id: str,
    advisor_id: str = Query(...),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Obtiene un template específico para un asesor."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    template = await _get_template_by_advisor(advisor_id, template_id)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' no encontrado")
    return template


@router.post("/templates")
async def create_template(
    advisor_id: str = Query(...),
    name: str = Form(..., description="Nombre del template"),
    category: str = Form(..., description="Categoría: reactivacion, cita, seguimiento, recordatorio, promocion"),
    body: str = Form(..., description="Cuerpo del mensaje con variables {nombre}, {fecha}, etc."),
    variables: str = Form("[]", description="JSON array de nombres de variables"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Crea un nuevo template para un asesor."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    # Permitir nombres flexibles, solo bloquear si el nombre ya existe (case-insensitive)
    template_id = name.strip().lower()
    existing_templates = await _get_template_by_advisor(advisor_id, template_id)
    if existing_templates:
        raise HTTPException(
            status_code=409,
            detail=f"Ya existe un template con ese nombre. Elige otro nombre."
        )
    # Variables opcionales y siempre JSON válido
    try:
        vars_list = json.loads(variables) if variables else []
        if not isinstance(vars_list, list):
            vars_list = []
    except Exception:
        vars_list = []
    template_data = {
        "id": template_id,
        "name": name.strip(),
        "category": category.strip(),
        "body": body.strip(),
        "variables": vars_list,
        "is_default": False,
        "created_at": datetime.now(timezone.utc).isoformat() + "Z"
    }
    success = await _save_template_by_advisor(advisor_id, template_data)
    if not success:
        raise HTTPException(status_code=500, detail="Error guardando template")
    return JSONResponse(
        status_code=201,
        content={
            "status": "success",
            "template": template_data,
            "message": f"Template '{name}' creado exitosamente"
        }
    )


@router.put("/templates/{template_id}")
async def update_template(
    template_id: str,
    advisor_id: str = Query(...),
    name: str = Form(None, description="Nombre del template"),
    category: str = Form(None, description="Categoría"),
    body: str = Form(None, description="Cuerpo del mensaje"),
    variables: str = Form(None, description="JSON array de variables"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Actualiza un template existente para un asesor."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    template = await _get_template_by_advisor(advisor_id, template_id)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' no encontrado")
    if name is not None:
        template["name"] = name.strip()
    if category is not None:
        template["category"] = category.strip()
    if body is not None:
        template["body"] = body.strip()
    if variables is not None:
        try:
            template["variables"] = json.loads(variables)
        except json.JSONDecodeError:
            pass
    template["updated_at"] = datetime.now(timezone.utc).isoformat() + "Z"
    success = await _save_template_by_advisor(advisor_id, template)
    if not success:
        raise HTTPException(status_code=500, detail="Error actualizando template")
    return {
        "status": "success",
        "template": template,
        "message": f"Template '{template_id}' actualizado"
    }


@router.delete("/templates/{template_id}")
async def delete_template(
    template_id: str,
    advisor_id: str = Query(...),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Elimina un template de un asesor (no se pueden eliminar templates predefinidos)."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    template = await _get_template_by_advisor(advisor_id, template_id)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' no encontrado")
    if template.get("is_default"):
        raise HTTPException(
            status_code=403,
            detail="No se pueden eliminar templates predefinidos"
        )
    success = await _delete_template_by_advisor(advisor_id, template_id)
    if not success:
        raise HTTPException(status_code=500, detail="Error eliminando template")
    return {
        "status": "success",
        "message": f"Template '{template_id}' eliminado"
    }


# ============================================================================
# Endpoint para CREAR contacto manualmente
# ============================================================================

@router.post("/contacts/create")
async def create_manual_contact(
    firstname: str = Form(..., description="Nombre del contacto"),
    phone: str = Form(..., description="Teléfono del contacto"),
    lastname: str = Form("", description="Apellido (opcional)"),
    property_type: Optional[str] = Form(None, description="Tipo de inmueble"),
    operation_type: Optional[str] = Form(None, description="Tipo de operación (compra/arriendo)"),
    budget: Optional[str] = Form(None, description="Presupuesto"),
    characteristics: Optional[str] = Form(None, description="Características adicionales"),
    canal: str = Form("whatsapp_directo", description="Canal de origen para asignación"),
    advisor_id: Optional[str] = Form(None, description="ID del asesor que crea el contacto (tiene prioridad sobre round-robin)"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Crea un contacto manualmente desde el panel de asesores.

    Flujo:
    1. Normalizar teléfono
    2. Verificar si existe (deduplicación por whatsapp_id)
    3. Si existe → Retornar error con opción de tomar control
    4. Si no existe → Crear contacto + deal en HubSpot
    5. Activar HUMAN_ACTIVE para que aparezca en el panel
    """
    logger.info(f"[Panel] POST /contacts/create - phone={phone}, firstname={firstname}, canal={canal}, advisor_id={advisor_id}")

    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # === 1. Normalizar teléfono ===
    normalizer = PhoneNormalizer()
    validation = normalizer.normalize(phone)
    if not validation.is_valid:
        raise HTTPException(
            status_code=400,
            detail=f"Número de teléfono inválido: {phone}"
        )
    phone_normalized = validation.normalized

    logger.info(f"[Panel] Teléfono normalizado: {phone_normalized}")

    # === 2. Verificar si el contacto ya existe (deduplicación) ===
    import httpx
    hubspot_api_key = os.getenv("HUBSPOT_API_KEY")
    if not hubspot_api_key:
        raise HTTPException(status_code=500, detail="HUBSPOT_API_KEY no configurada")

    # Buscar por whatsapp_id (identificador único)
    search_url = "https://api.hubapi.com/crm/v3/objects/contacts/batch/read"
    search_payload = {
        "properties": ["id", "firstname", "lastname", "phone", "hubspot_owner_id"],
        "idProperty": "whatsapp_id",
        "inputs": [{"id": phone_normalized}]
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await _hubspot_post(client, search_url, search_payload, hubspot_api_key)

            if response.status_code == 200:
                results = response.json().get("results", [])
                if results:
                    # Contacto ya existe — enriquecer respuesta con owner, historial y estado Redis
                    existing = results[0]
                    existing_id = existing.get("id")
                    existing_props = existing.get("properties", {})
                    _fn = existing_props.get('firstname') or ''
                    _ln = existing_props.get('lastname') or ''
                    existing_name = f"{_fn} {_ln}".strip()
                    existing_owner_id = existing_props.get("hubspot_owner_id") or ""

                    logger.warning(f"[Panel] Contacto ya existe: {existing_id} ({existing_name})")

                    # Resolver nombre del asesor desde OWNERS_CONFIG (lookup local, sin IO)
                    from integrations.hubspot.lead_assigner import LeadAssigner
                    existing_owner_name = "Sin asignar"
                    if existing_owner_id:
                        for _team_members in LeadAssigner.OWNERS_CONFIG.values():
                            for _m in _team_members:
                                if str(_m.get("id")) == existing_owner_id:
                                    existing_owner_name = _m.get("name", "Sin asignar")
                                    break

                    # Contar mensajes en MongoDB (sin bloquear si falla)
                    try:
                        message_count = await get_mongo_manager().get_message_count(phone_normalized)
                    except Exception:
                        message_count = 0

                    # Detectar canal activo en Redis (si el contacto está en el panel)
                    redis_canal = None
                    try:
                        _rc = await _get_redis_client()
                        _meta_keys = await _rc.keys(f"conv_meta:{phone_normalized}:*")
                        if _meta_keys:
                            redis_canal = _meta_keys[0].split(":")[-1]
                    except Exception:
                        pass

                    # "Tomar Control" directo si el contacto NO está activo en ningún panel (redis_canal=None).
                    # Solo se pide permiso ("Solicitar Transferencia") si alguien lo tiene activo en el panel.
                    can_take_control = not (existing_owner_id and message_count > 0 and redis_canal)

                    return JSONResponse(
                        status_code=409,
                        content={
                            "status": "exists",
                            "contact_id": existing_id,
                            "phone": phone_normalized,
                            "display_name": existing_name or "Sin nombre",
                            "owner_id": existing_owner_id,
                            "owner_name": existing_owner_name,
                            "message_count": message_count,
                            "redis_canal": redis_canal,
                            "can_take_control": can_take_control,
                        }
                    )
    except Exception as e:
        logger.error(f"[Panel] Error buscando contacto existente: {e}")
        # Continuar con la creación si falla la búsqueda

    # === 3. Determinar owner_id ===
    # Si el asesor crea el contacto manualmente desde SU panel, usarlo directamente.
    # Solo se usa round-robin (LeadAssigner) cuando la creación es automática/API.
    if advisor_id:
        owner_id = advisor_id
        logger.info(f"[Panel] Contacto asignado directamente al asesor creador: {advisor_id}")
    else:
        from integrations.hubspot.lead_assigner import lead_assigner
        owner_id = lead_assigner.get_next_owner(canal)
        if not owner_id:
            logger.warning(f"[Panel] No se pudo asignar owner para canal: {canal}")
        else:
            logger.info(f"[Panel] Contacto asignado por round-robin (canal={canal}): {owner_id}")

    # === 4. Crear contacto en HubSpot ===
    from datetime import timezone as tz
    midnight_utc = datetime.now(tz.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Solo propiedades estándar de HubSpot (siempre existen)
    contact_properties = {
        "whatsapp_id": phone_normalized,
        "phone": phone_normalized,
        "firstname": firstname.strip(),
        "lastname": lastname.strip() if lastname else "",
        "canal_origen": canal,
        "chatbot_timestamp": str(int(midnight_utc.timestamp() * 1000)),
        "lifecyclestage": "lead",
    }

    # Agregar owner si está disponible
    if owner_id:
        contact_properties["hubspot_owner_id"] = owner_id

    # NOTA: tipo_inmueble, tipo_operacion, presupuesto, caracteristicas
    # se guardan en el Deal (description) en lugar del contacto
    # porque son propiedades custom que pueden no existir en HubSpot

    create_url = "https://api.hubapi.com/crm/v3/objects/contacts"

    try:
        async with httpx.AsyncClient(timeout=40.0) as client:
            response = await _hubspot_post(
                client, create_url, {"properties": contact_properties}, hubspot_api_key
            )

            if response.status_code in [200, 201]:
                contact_data = response.json()
                contact_id = contact_data.get("id")
                logger.info(f"[Panel] Contacto creado exitosamente: {contact_id}")
            elif response.status_code == 409:
                # Conflicto - contacto ya existe (race condition)
                logger.warning(f"[Panel] Conflicto 409 al crear contacto: {response.text}")
                raise HTTPException(
                    status_code=409,
                    detail="El contacto ya existe. Por favor busca en el panel."
                )
            else:
                logger.error(f"[Panel] Error creando contacto: {response.status_code} - {response.text}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Error de HubSpot: {response.text}"
                )

    except httpx.HTTPError as e:
        logger.error(f"[Panel] Error HTTP creando contacto: {e}")
        raise HTTPException(status_code=500, detail=f"Error de conexión: {str(e)}")

    # === 5. Crear Deal asociado ===
    deal_id = None
    try:
        # Construir descripción con las características del inmueble
        description_parts = []
        if property_type:
            description_parts.append(f"Tipo inmueble: {property_type}")
        if operation_type:
            description_parts.append(f"Operación: {operation_type}")
        if budget:
            description_parts.append(f"Presupuesto: {budget}")
        if characteristics:
            description_parts.append(f"Características: {characteristics}")

        deal_properties = {
            "dealname": f"Lead: {firstname} {lastname}".strip(),
            "pipeline": HUBSPOT_PIPELINE_ID,          # 854756009
            "dealstage": HUBSPOT_STAGE_NUEVO_LEAD,    # 1275156339
            "canal_origen": canal,
        }

        # Agregar descripción si hay características
        if description_parts:
            deal_properties["description"] = "\n".join(description_parts)

        if owner_id:
            deal_properties["hubspot_owner_id"] = owner_id

        deal_url = "https://api.hubapi.com/crm/v3/objects/deals"
        deal_payload = {
            "properties": deal_properties,
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 3  # Deal to Contact
                        }
                    ]
                }
            ]
        }

        async with httpx.AsyncClient(timeout=40.0) as client:
            response = await _hubspot_post(client, deal_url, deal_payload, hubspot_api_key)

            if response.status_code in [200, 201]:
                deal_data = response.json()
                deal_id = deal_data.get("id")
                logger.info(f"[Panel] Deal creado: {deal_id} para contacto {contact_id}")
            else:
                logger.warning(f"[Panel] No se pudo crear deal: {response.status_code} - {response.text}")

    except Exception as e:
        logger.warning(f"[Panel] Error creando deal (no crítico): {e}")

    # === 5.5. Escribir url_chat en Contacto y Deal ===
    try:
        panel_base_url = os.getenv("PANEL_BASE_URL", "").rstrip("/")
        admin_api_key = os.getenv("ADMIN_API_KEY", "")
        if panel_base_url and owner_id and admin_api_key:
            from urllib.parse import quote
            phone_encoded = quote(phone_normalized, safe='')
            url_chat = f"{panel_base_url}/whatsapp/panel/?key={admin_api_key}&advisor={owner_id}&phone={phone_encoded}"
            
            # Escribir en contacto
            async with httpx.AsyncClient(timeout=15.0) as client:
                contact_patch_url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
                await client.patch(
                    contact_patch_url,
                    headers={"Authorization": f"Bearer {hubspot_api_key}", "Content-Type": "application/json"},
                    json={"properties": {"url_chat": url_chat}}
                )
                logger.info(f"[Panel] url_chat escrito en contacto {contact_id}")
            
            # Escribir en deal si existe
            if deal_id:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    deal_patch_url = f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}"
                    await client.patch(
                        deal_patch_url,
                        headers={"Authorization": f"Bearer {hubspot_api_key}", "Content-Type": "application/json"},
                        json={"properties": {"url_chat": url_chat}}
                    )
                    logger.info(f"[Panel] url_chat escrito en deal {deal_id}")
    except Exception as e:
        logger.warning(f"[Panel] Error escribiendo url_chat (no crítico): {e}")

    # === 6. Activar HUMAN_ACTIVE en Redis ===
    try:
        is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
        redis_url = os.getenv("REDIS_URL") if is_railway else (
            os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
        )
        state_manager = ConversationStateManager(redis_url)

        display_name = f"{firstname} {lastname}".strip()

        await state_manager.activate_human(
            phone_normalized=phone_normalized,
            canal_origen=canal,
            owner_id=owner_id,
            reason="Creado manualmente desde panel",
            display_name=display_name,
            contact_id=contact_id
        )

        logger.info(f"[Panel] HUMAN_ACTIVE activado para {phone_normalized}")

        # Escribir clave inversa phone_cache:{contact_id} → phone para que
        # update_contact_name pueda resolver el teléfono desde el contact_id
        try:
            _rc = await _get_redis_client()
            await _rc.set(f"phone_cache:{contact_id}", phone_normalized, ex=86400)
        except Exception:
            pass

    except Exception as e:
        logger.error(f"[Panel] Error activando HUMAN_ACTIVE: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=(
                f"Contacto creado en HubSpot (ID: {contact_id}) pero no se pudo registrar "
                f"en el panel. Usa restore-panel o vuelve a intentarlo. Error: {e}"
            )
        )

    return {
        "status": "success",
        "message": f"Contacto '{firstname}' creado exitosamente",
        "contact_id": contact_id,
        "deal_id": deal_id,
        "phone": phone_normalized,
        "display_name": f"{firstname} {lastname}".strip(),
        "owner_id": owner_id
    }


# ============================================================================
# Endpoint para TRANSFERIR contacto a otra asesora
# ============================================================================

@router.post("/contacts/{phone}/transfer")
async def transfer_contact(
    phone: str,
    to_owner_id: str = Form(..., description="ID del asesor destino"),
    mode: str = Form("exclusive", description="Modo: exclusive o collaborative"),
    reason: str = Form("", description="Motivo de la transferencia"),
    contact_id: Optional[str] = Form(None, description="ID del contacto en HubSpot"),
    canal: str = Form("whatsapp", description="Canal de la conversación"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Transfiere un contacto a otro asesor.

    Modos:
    - exclusive: El contacto pasa completamente al nuevo asesor
    - collaborative: Ambos asesores pueden ver y atender el contacto

    Actualiza:
    - Redis: Metadata de la conversación (assigned_owner_id)
    - HubSpot: hubspot_owner_id del contacto (solo en modo exclusive)
    """
    logger.info(f"[Panel] POST /contacts/{phone}/transfer -> {to_owner_id} (modo: {mode})")

    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # Normalizar teléfono
    normalizer = PhoneNormalizer()
    validation = normalizer.normalize(phone)
    if not validation.is_valid:
        raise HTTPException(status_code=400, detail=f"Teléfono inválido: {phone}")
    phone_normalized = validation.normalized

    # Validar modo
    if mode not in ["exclusive", "collaborative"]:
        raise HTTPException(status_code=400, detail="Modo debe ser 'exclusive' o 'collaborative'")

    # === 1. Transferir en Redis ===
    is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
    redis_url = os.getenv("REDIS_URL") if is_railway else (
        os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
    )
    state_manager = ConversationStateManager(redis_url)

    result = await state_manager.transfer_contact(
        phone=phone_normalized,
        to_owner_id=to_owner_id,
        canal=canal,
        mode=mode,
        reason=reason
    )

    if result.get("status") != "success":
        raise HTTPException(status_code=400, detail=result.get("message", "Error en transferencia"))

    from_owner = result.get("from_owner")

    # === 2. Actualizar HubSpot (solo en modo exclusive) ===
    hubspot_updated = False
    if mode == "exclusive" and contact_id:
        import httpx
        hubspot_api_key = os.getenv("HUBSPOT_API_KEY")

        if hubspot_api_key:
            try:
                url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.patch(
                        url,
                        json={"properties": {"hubspot_owner_id": to_owner_id}},
                        headers={
                            "Authorization": f"Bearer {hubspot_api_key}",
                            "Content-Type": "application/json"
                        }
                    )

                    if response.status_code == 200:
                        hubspot_updated = True
                        logger.info(f"[Panel] HubSpot owner actualizado: {contact_id} -> {to_owner_id}")
                    else:
                        logger.warning(f"[Panel] Error actualizando HubSpot: {response.status_code}")

            except Exception as e:
                logger.warning(f"[Panel] Error actualizando HubSpot (no crítico): {e}")

    # === 3. Notificar vía WebSocket ===
    try:
        # Obtener nombre del contacto
        meta = await state_manager.get_meta(phone_normalized, canal)
        contact_name = meta.display_name if meta else phone_normalized

        await ws_manager.notify_contact_transferred(
            phone=phone_normalized,
            from_advisor=from_owner or "unknown",
            to_advisor=to_owner_id,
            contact_name=contact_name,
            mode=mode
        )
    except Exception as e:
        logger.warning(f"[Panel] Error notificando WebSocket: {e}")

    return {
        "status": "success",
        "message": f"Contacto transferido a {to_owner_id}",
        "phone": phone_normalized,
        "from_owner": from_owner,
        "to_owner": to_owner_id,
        "mode": mode,
        "hubspot_updated": hubspot_updated,
        "transfer_history": result.get("transfer_history", [])
    }


# ============================================================================
# Helper para resolver nombre de asesor desde OWNERS_CONFIG
# ============================================================================

def _get_advisor_name(advisor_id: str) -> str:
    """Busca el nombre de un asesor en OWNERS_CONFIG por su ID."""
    if not advisor_id:
        return "Asesor"
    try:
        from integrations.hubspot.lead_assigner import LeadAssigner
        for _team_members in LeadAssigner.OWNERS_CONFIG.values():
            for _m in _team_members:
                if str(_m.get("id")) == str(advisor_id):
                    return _m.get("name", "Asesor")
    except Exception:
        pass
    return "Asesor"


async def _reassign_hubspot_owner(contact_id: str, to_owner_id: str) -> bool:
    """PATCH hubspot_owner_id de un contacto en HubSpot."""
    try:
        import httpx
        hubspot_api_key = os.getenv("HUBSPOT_API_KEY")
        if not hubspot_api_key or not contact_id:
            return False
        url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.patch(
                url,
                json={"properties": {"hubspot_owner_id": to_owner_id}},
                headers={
                    "Authorization": f"Bearer {hubspot_api_key}",
                    "Content-Type": "application/json"
                }
            )
        if response.status_code == 200:
            logger.info(f"[Panel] HubSpot owner reasignado: {contact_id} -> {to_owner_id}")
            return True
        logger.warning(f"[Panel] HubSpot reasignación falló: {response.status_code}")
        return False
    except Exception as e:
        logger.warning(f"[Panel] Error reasignando HubSpot owner: {e}")
        return False


# ============================================================================
# Endpoints de Solicitud de Transferencia
# ============================================================================

@router.post("/contacts/{contact_id}/transfer-request")
async def request_transfer(
    contact_id: str,
    phone: str = Query(..., description="Teléfono normalizado del contacto"),
    requesting_advisor_id: str = Query(..., description="ID del asesor que solicita"),
    owner_advisor_id: str = Query(..., description="ID del asesor propietario actual"),
    contact_name: str = Query("", description="Nombre del contacto para mostrar"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Solicita la transferencia de un contacto al asesor propietario.
    Guarda la solicitud en Redis y envía notificación WS al dueño.
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    _rc = await _get_redis_client()
    req_key = f"transfer_req:{contact_id}"
    req_data = {
        "requester_id": requesting_advisor_id,
        "owner_id": owner_advisor_id,
        "phone": phone,
        "contact_id": contact_id,
        "contact_name": contact_name or phone,
        "created_at": datetime.now().isoformat()
    }
    await _rc.set(req_key, json.dumps(req_data), ex=1800)  # TTL 30 min

    requester_name = _get_advisor_name(requesting_advisor_id)

    sent = await ws_manager.send_to_advisor(owner_advisor_id, {
        "type": "transfer_request",
        "contact_id": contact_id,
        "phone": phone,
        "contact_name": contact_name or phone,
        "requester_id": requesting_advisor_id,
        "requester_name": requester_name,
        "message": f"{requester_name} quiere atender este contacto"
    })

    logger.info(f"[Panel] Transfer request: {requesting_advisor_id} -> {owner_advisor_id} para {contact_id} (notified={sent > 0})")
    return {"status": "pending", "notified": sent > 0}


@router.post("/contacts/{contact_id}/transfer-accept")
async def accept_transfer(
    contact_id: str,
    by_advisor_id: str = Query(..., description="ID del asesor que acepta (propietario actual)"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    El asesor propietario acepta la solicitud de transferencia.
    Reasigna en HubSpot + Redis y notifica al solicitante.
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    _rc = await _get_redis_client()
    raw = await _rc.get(f"transfer_req:{contact_id}")
    if not raw:
        raise HTTPException(status_code=400, detail="Solicitud expirada o no encontrada")

    req_data = json.loads(raw)
    requester_id = req_data["requester_id"]
    phone = req_data["phone"]
    contact_name = req_data.get("contact_name", phone)

    # 1. Reasignar owner en HubSpot
    await _reassign_hubspot_owner(contact_id, to_owner_id=requester_id)

    # 2. Actualizar assigned_owner_id en Redis conv_meta si el contacto está activo
    try:
        _meta_keys = await _rc.keys(f"conv_meta:{phone}:*")
        for mk in _meta_keys:
            raw_meta = await _rc.get(mk)
            if raw_meta:
                meta = json.loads(raw_meta)
                meta["assigned_owner_id"] = requester_id
                ttl = await _rc.ttl(mk)
                await _rc.set(mk, json.dumps(meta), ex=max(ttl, 1))
    except Exception as e:
        logger.warning(f"[Panel] Error actualizando conv_meta en transfer-accept: {e}")

    # 3. Limpiar solicitud pendiente
    await _rc.delete(f"transfer_req:{contact_id}")

    # 4. Notificar al solicitante
    await ws_manager.send_to_advisor(requester_id, {
        "type": "transfer_accepted",
        "contact_id": contact_id,
        "phone": phone,
        "contact_name": contact_name,
        "message": "Transferencia aceptada — el contacto es tuyo"
    })

    # 5. Broadcast para refrescar el panel de todos
    await ws_manager.broadcast({
        "type": "contact_updated",
        "phone": phone,
        "action": "transfer_completed"
    })

    logger.info(f"[Panel] Transfer accept: {by_advisor_id} -> {requester_id} para {contact_id}")
    return {"status": "accepted"}


@router.post("/contacts/{contact_id}/transfer-reject")
async def reject_transfer(
    contact_id: str,
    by_advisor_id: str = Query(..., description="ID del asesor que rechaza"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    El asesor propietario rechaza la solicitud de transferencia.
    Notifica al solicitante y limpia Redis.
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    _rc = await _get_redis_client()
    raw = await _rc.get(f"transfer_req:{contact_id}")
    req_data = json.loads(raw) if raw else {}
    requester_id = req_data.get("requester_id", "")
    phone = req_data.get("phone", "")
    contact_name = req_data.get("contact_name", phone)

    await _rc.delete(f"transfer_req:{contact_id}")

    if requester_id:
        await ws_manager.send_to_advisor(requester_id, {
            "type": "transfer_rejected",
            "contact_id": contact_id,
            "phone": phone,
            "contact_name": contact_name,
            "message": "El asesor rechazó la transferencia"
        })

    logger.info(f"[Panel] Transfer reject: {by_advisor_id} para {contact_id}")
    return {"status": "rejected"}


# ============================================================================
# Endpoint para editar nombre de contacto
# ============================================================================

@router.patch("/contacts/{contact_id}/name")
async def update_contact_name(
    contact_id: str,
    firstname: str = Form(..., description="Nombre del contacto"),
    lastname: str = Form("", description="Apellido del contacto (opcional)"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Actualiza el nombre del contacto en HubSpot.

    Permite a los asesores corregir nombres de contactos directamente
    desde el panel sin ir a HubSpot.
    """
    logger.info(f"[Panel] PATCH nombre - contact_id={contact_id}, firstname={firstname}, lastname={lastname}")

    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # Validar contact_id
    if not contact_id or contact_id == "null" or contact_id == "undefined":
        logger.error(f"[Panel] contact_id inválido: {contact_id}")
        raise HTTPException(status_code=400, detail="ID de contacto inválido")

    # Validar que sea numérico (IDs de HubSpot son numéricos)
    try:
        int(contact_id)
    except ValueError:
        logger.error(f"[Panel] contact_id no es numérico: {contact_id}")
        raise HTTPException(status_code=400, detail="ID de contacto debe ser numérico")

    hubspot_api_key = os.getenv("HUBSPOT_API_KEY")
    if not hubspot_api_key:
        logger.error("[Panel] HUBSPOT_API_KEY no configurada")
        raise HTTPException(status_code=500, detail="HUBSPOT_API_KEY no configurada")

    url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
    payload = {
        "properties": {
            "firstname": firstname.strip(),
            "lastname": lastname.strip()
        }
    }

    logger.debug(f"[Panel] Enviando PATCH a HubSpot: {url}")

    try:
        response = await _hubspot_patch(url, payload, hubspot_api_key)

        logger.info(f"[Panel] Respuesta HubSpot: {response.status_code}")

        if response.status_code == 200:
            logger.info(f"[Panel] Nombre actualizado para contacto {contact_id}: {firstname} {lastname}")
            # Sincronizar display_name en Redis para todos los canales del contacto
            try:
                _rc = await _get_redis_client()
                _phone = await _rc.get(f"phone_cache:{contact_id}")
                if _phone:
                    _display = f"{firstname} {lastname}".strip()
                    _meta_keys = await _rc.keys(f"conv_meta:{_phone}:*")
                    for _meta_key in _meta_keys:
                        _raw = await _rc.get(_meta_key)
                        if _raw:
                            try:
                                _meta = json.loads(_raw)
                                _meta["display_name"] = _display
                                _ttl = await _rc.ttl(_meta_key)
                                if _ttl > 0:
                                    await _rc.setex(_meta_key, _ttl, json.dumps(_meta))
                                else:
                                    await _rc.set(_meta_key, json.dumps(_meta))
                            except (json.JSONDecodeError, Exception):
                                pass
                    logger.info(f"[Panel] display_name '{_display}' sincronizado en Redis para {_phone}")
            except Exception as redis_err:
                logger.warning(f"[Panel] No se pudo actualizar display_name en Redis: {redis_err}")
            # Notificar a todos los paneles para que actualicen el nombre sin esperar poll
            try:
                _phone_ws = await _get_redis_client()
                _phone_ws = await _phone_ws.get(f"phone_cache:{contact_id}")
                await ws_manager.broadcast({
                    "type": "contact_updated",
                    "phone": str(_phone_ws or ""),
                    "action": "name_updated",
                    "display_name": f"{firstname} {lastname}".strip()
                })
            except Exception:
                pass
            return {
                "status": "success",
                "message": "Nombre actualizado correctamente",
                "contact_id": contact_id,
                "firstname": firstname,
                "lastname": lastname,
                "display_name": f"{firstname} {lastname}".strip()
            }
        elif response.status_code == 404:
            logger.warning(f"[Panel] Contacto no encontrado en HubSpot: {contact_id}")
            raise HTTPException(
                status_code=404,
                detail="Contacto no encontrado en HubSpot"
            )
        else:
            logger.error(f"[Panel] Error actualizando nombre: {response.status_code} - {response.text}")
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Error de HubSpot: {response.text[:200]}"
            )

    except httpx.TimeoutException:
        logger.error(f"[Panel] Timeout actualizando nombre para {contact_id}")
        raise HTTPException(status_code=504, detail="Timeout conectando con HubSpot")
    except HTTPException:
        raise  # Re-raise HTTPExceptions sin modificar
    except Exception as e:
        logger.error(f"[Panel] Error inesperado actualizando nombre: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")


# ============================================================================
# Endpoint para cerrar conversación (transicionar a BOT_ACTIVE)
# ============================================================================

@router.delete("/contacts/{phone}/close")
async def close_conversation(
    phone: str,
    canal: Optional[str] = None,
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Cierra una conversación transicionando a BOT_ACTIVE.

    Esto hace que:
    1. El contacto desaparezca del panel de "activos" (HUMAN_ACTIVE/IN_CONVERSATION)
    2. Sofía retome la conversación automáticamente cuando el cliente escriba
    3. Se preserve el contexto de la conversación

    SEGREGACIÓN POR CANAL:
    Si se proporciona el parámetro canal, solo se cierra la conversación
    de ese canal específico.
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # Normalizar teléfono si no está normalizado
    normalizer = PhoneNormalizer()
    validation = normalizer.normalize(phone)

    phone_normalized = validation.normalized if validation.is_valid else phone

    try:
        # Redis URL unificado (Railway interna, local pública)
        is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
        redis_url = os.getenv("REDIS_URL") if is_railway else (
            os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
        )
        state_manager = ConversationStateManager(redis_url)

        # Transicionar a BOT_ACTIVE en lugar de eliminar
        # Esto permite que Sofía retome la conversación con contexto
        await state_manager.activate_bot(phone_normalized, canal=canal)

        # También intentar con el teléfono original si es diferente
        if phone != phone_normalized:
            await state_manager.activate_bot(phone, canal=canal)

        canal_info = f":{canal}" if canal else ""
        logger.info(
            f"[Panel] Conversación cerrada y transicionada a BOT_ACTIVE: "
            f"{phone_normalized}{canal_info}"
        )

        return {
            "status": "success",
            "message": "Conversación cerrada - Sofía retomará automáticamente",
            "phone": phone_normalized,
            "canal": canal,
            "new_status": "BOT_ACTIVE"
        }

    except Exception as e:
        logger.error(f"[Panel] Error cerrando conversación: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Endpoint para actualizar etapa del deal en HubSpot
# ============================================================================

@router.patch("/contacts/{contact_id}/stage")
async def update_deal_stage(
    contact_id: str,
    stage_id: str = Body(..., embed=True),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Actualiza la etapa del deal en HubSpot desde el panel de asesores.

    Este endpoint:
    1. Busca el deal asociado al contacto
    2. Actualiza la propiedad 'dealstage' del deal
    3. Retorna confirmación del cambio

    Args:
        contact_id: ID del contacto en HubSpot
        stage_id: ID de la nueva etapa del pipeline

    Returns:
        Confirmación del cambio con nombre de etapa
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # Validar que el stage_id sea válido
    if stage_id not in PIPELINE_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"stage_id inválido. Valores permitidos: {list(PIPELINE_STAGES.keys())}"
        )

    hubspot_token = os.getenv("HUBSPOT_API_KEY")
    if not hubspot_token:
        raise HTTPException(status_code=500, detail="HUBSPOT_API_KEY no configurada")

    try:
        import httpx

        headers = {
            "Authorization": f"Bearer {hubspot_token}",
            "Content-Type": "application/json"
        }

        base_url = "https://api.hubapi.com"

        async with httpx.AsyncClient(timeout=15.0) as client:
            # 1. Buscar deals asociados al contacto
            associations_url = f"{base_url}/crm/v3/objects/contacts/{contact_id}/associations/deals"

            response = await client.get(associations_url, headers=headers)

            if response.status_code != 200:
                logger.error(f"[Panel] Error buscando deals: {response.status_code} - {response.text}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Error buscando deals asociados: {response.text[:200]}"
                )

            associations = response.json()
            results = associations.get("results", [])

            if not results:
                raise HTTPException(
                    status_code=404,
                    detail="No se encontraron deals asociados a este contacto"
                )

            # 2. Tomar el primer deal (más reciente)
            deal_id = results[0].get("id")

            # 3. Actualizar la etapa del deal
            update_url = f"{base_url}/crm/v3/objects/deals/{deal_id}"
            payload = {
                "properties": {
                    "dealstage": stage_id
                }
            }

            response = await client.patch(update_url, headers=headers, json=payload)

            if response.status_code == 200:
                stage_name = PIPELINE_STAGES.get(stage_id, stage_id)
                logger.info(
                    f"[Panel] Deal {deal_id} actualizado a etapa '{stage_name}' "
                    f"(contact_id: {contact_id})"
                )
                
                # Invalidar caché de deal info para este contacto
                await _invalidate_deal_cache(contact_id)

                return {
                    "status": "success",
                    "message": f"Etapa actualizada a '{stage_name}'",
                    "deal_id": deal_id,
                    "contact_id": contact_id,
                    "stage_id": stage_id,
                    "stage_name": stage_name
                }
            else:
                logger.error(f"[Panel] Error actualizando deal: {response.status_code} - {response.text}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Error actualizando deal: {response.text[:200]}"
                )

    except httpx.TimeoutException:
        logger.error(f"[Panel] Timeout actualizando etapa para contact_id={contact_id}")
        raise HTTPException(status_code=504, detail="Timeout conectando con HubSpot")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Panel] Error inesperado actualizando etapa: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")


@router.get("/stages")
async def get_pipeline_stages(
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Retorna la lista de etapas del pipeline para el dropdown del frontend.
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    return {
        "stages": PIPELINE_STAGES_LIST,
        "count": len(PIPELINE_STAGES_LIST)
    }


@router.post("/reset-bot/{phone}")
async def reset_bot_state(
    phone: str,
    canal: Optional[str] = Query(None, description="Canal específico a resetear"),
    force: bool = Query(False, description="Forzar reset incluso si ya está en BOT_ACTIVE"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    ENDPOINT DE EMERGENCIA: Fuerza el regreso del estado a BOT_ACTIVE.

    Útil cuando un contacto se queda "trabado" en HUMAN_ACTIVE/IN_CONVERSATION
    y no aparece en el panel para cerrarlo manualmente.

    Este endpoint:
    1. Busca el contacto en Redis por teléfono (todos los canales si no se especifica)
    2. Transiciona a BOT_ACTIVE
    3. Retorna información del estado anterior

    Args:
        phone: Número de teléfono (E.164 o sin normalizar)
        canal: Canal específico (opcional, si no se especifica resetea todos)
        force: Si es True, resetea incluso si ya está en BOT_ACTIVE

    Returns:
        Estado anterior y confirmación del reset
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # Normalizar teléfono
    normalizer = PhoneNormalizer()
    validation = normalizer.normalize(phone)
    phone_normalized = validation.normalized if validation.is_valid else phone

    try:
        # Redis URL unificado (Railway interna, local pública)
        is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
        redis_url = os.getenv("REDIS_URL") if is_railway else (
            os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
        )
        state_manager = ConversationStateManager(redis_url)

        results = []

        if canal:
            # Resetear solo el canal especificado
            canales_to_reset = [canal]
        else:
            # Buscar todos los canales donde existe este teléfono
            # Patrones comunes de canales
            canales_to_check = [
                "whatsapp_directo", "instagram", "facebook",
                "finca_raiz", "metrocuadrado", "pagina_web", "default"
            ]
            canales_to_reset = []

            for c in canales_to_check:
                state = await state_manager.get_conversation_state(phone_normalized, c)
                if state:
                    canales_to_reset.append(c)

            # También verificar con teléfono original
            if phone != phone_normalized:
                for c in canales_to_check:
                    state = await state_manager.get_conversation_state(phone, c)
                    if state and c not in canales_to_reset:
                        canales_to_reset.append(c)

        if not canales_to_reset:
            logger.warning(f"[Panel] Reset: No se encontró estado para {phone_normalized}")
            return {
                "status": "warning",
                "message": f"No se encontró conversación activa para {phone_normalized}",
                "phone": phone_normalized,
                "results": []
            }

        # Resetear cada canal encontrado
        for c in canales_to_reset:
            try:
                # Obtener estado actual antes de resetear
                current_state = await state_manager.get_conversation_state(phone_normalized, c)
                previous_status = current_state.status.value if current_state else "UNKNOWN"

                # Si ya está en BOT_ACTIVE y no forzamos, skip
                if previous_status == "BOT_ACTIVE" and not force:
                    results.append({
                        "canal": c,
                        "previous_status": previous_status,
                        "new_status": "BOT_ACTIVE",
                        "action": "skipped (already BOT_ACTIVE)"
                    })
                    continue

                # Ejecutar reset
                await state_manager.activate_bot(phone_normalized, canal=c)

                results.append({
                    "canal": c,
                    "previous_status": previous_status,
                    "new_status": "BOT_ACTIVE",
                    "action": "reset successful"
                })

                logger.info(
                    f"[Panel] Reset exitoso: {phone_normalized}:{c} "
                    f"({previous_status} -> BOT_ACTIVE)"
                )

            except Exception as canal_error:
                results.append({
                    "canal": c,
                    "error": str(canal_error),
                    "action": "failed"
                })
                logger.error(f"[Panel] Error reseteando {phone_normalized}:{c}: {canal_error}")

        success_count = len([r for r in results if r.get("action") == "reset successful"])

        return {
            "status": "success" if success_count > 0 else "warning",
            "message": f"Sofía ha retomado el control de {phone_normalized} en {success_count} canal(es)",
            "phone": phone_normalized,
            "results": results
        }

    except Exception as e:
        logger.error(f"[Panel] Error en reset-bot: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/window-status/{phone}")
async def get_window_status(
    phone: str,
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Consulta el estado de la ventana de 24 horas para un número.
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    normalizer = PhoneNormalizer()
    validation = normalizer.normalize(phone)

    if not validation.is_valid:
        raise HTTPException(status_code=400, detail=f"Número inválido: {validation.error_message}")

    window_status = await check_24h_window(validation.normalized)

    return {
        "phone": validation.normalized,
        "window_open": window_status.is_open,
        "last_message_time": window_status.last_message_time.isoformat() if window_status.last_message_time else None,
        "time_remaining_seconds": window_status.time_remaining_seconds,
        "requires_template": window_status.requires_template,
        "message": window_status.message
    }


@router.get("/contacts/{phone}/detail")
async def get_contact_detail(
    phone: str,
    contact_id: Optional[str] = Query(None, description="ID del contacto en HubSpot"),
    canal: Optional[str] = Query(None, description="Canal de origen para filtrar mensajes"),
    limit: int = Query(50, ge=1, le=100),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Endpoint combinado: devuelve en un solo round-trip los datos necesarios para
    abrir el chat de un contacto — historial de mensajes + estado de ventana 24h.

    Reemplaza las 2 llamadas paralelas GET /history/{id} + GET /window-status/{phone}
    que se hacen en selectContact().
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    normalizer = PhoneNormalizer()
    validation = normalizer.normalize(phone)
    if not validation.is_valid:
        raise HTTPException(status_code=400, detail=f"Número inválido: {validation.error_message}")

    phone_normalized = validation.normalized
    mongo_manager = get_mongo_manager()

    async def _get_messages():
        messages = []
        source = "none"
        try:
            # Paso 1: MongoDB por teléfono (más eficiente)
            messages = await mongo_manager.get_history(
                phone=phone_normalized,
                limit=limit,
                channel=canal
            )
            if messages:
                return messages, "mongodb"

            # Paso 2: MongoDB por contact_id si hay
            if contact_id and contact_id.isdigit():
                messages = await mongo_manager.get_history_by_contact_id(
                    hubspot_contact_id=contact_id,
                    limit=limit
                )
                if messages:
                    return messages, "mongodb"

            # Paso 3: Fallback HubSpot
            if contact_id and contact_id.isdigit():
                timeline_logger = get_timeline_logger()
                messages = await timeline_logger.get_notes_for_contact(
                    contact_id=contact_id,
                    limit=limit
                )
                if messages:
                    return messages, "hubspot"
        except Exception as e:
            logger.error(f"[Panel] Error obteniendo mensajes en detail: {e}")
        return messages or [], source

    # Ejecutar historial + window-status en paralelo (1 round-trip combinado)
    (messages, source), window_status = await asyncio.gather(
        _get_messages(),
        check_24h_window(phone_normalized),
    )

    return {
        "phone": phone_normalized,
        "contact_id": contact_id,
        "canal": canal,
        # Historial
        "messages": messages,
        "message_count": len(messages),
        "message_source": source,
        # Ventana 24h
        "window_open": window_status.is_open,
        "last_message_time": window_status.last_message_time.isoformat() if window_status.last_message_time else None,
        "time_remaining_seconds": window_status.time_remaining_seconds,
        "requires_template": window_status.requires_template,
        "window_message": window_status.message,
    }


@router.get("/conversations/{phone}")
async def get_conversation_history(
    phone: str,
    limit: int = Query(50, ge=1, le=100),
    canal: Optional[str] = Query(None, description="Canal para filtrar mensajes"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Obtiene el historial de conversación de un contacto por teléfono.

    ARQUITECTURA v2.0:
    1. Consultar MongoDB primero (fuente de verdad en tiempo real, ~5ms)
    2. Si MongoDB vacío o no disponible → Fallback a HubSpot (migración)
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    normalizer = PhoneNormalizer()
    validation = normalizer.normalize(phone)

    if not validation.is_valid:
        raise HTTPException(status_code=400, detail=f"Número inválido: {validation.error_message}")

    phone_normalized = validation.normalized
    messages = []
    source = "none"

    try:
        # =====================================================================
        # PASO 1: MongoDB - Fuente de verdad en tiempo real
        # =====================================================================
        mongo_manager = get_mongo_manager()
        messages = await mongo_manager.get_history(
            phone=phone_normalized,
            limit=limit,
            channel=canal
        )

        if messages:
            source = "mongodb"
            logger.debug(f"[Panel] Historial desde MongoDB: {len(messages)} mensajes")

        # =====================================================================
        # PASO 2: Si MongoDB vacío → Fallback a HubSpot (datos históricos)
        # =====================================================================
        if not messages:
            contact_manager = ContactManager()
            contact_id = await contact_manager._search_contact(phone_normalized)

            if contact_id:
                timeline_logger = get_timeline_logger()
                messages = await timeline_logger.get_notes_for_contact(
                    contact_id=contact_id,
                    limit=limit
                )
                source = "hubspot"
                logger.debug(f"[Panel] Historial desde HubSpot (fallback): {len(messages)} mensajes")

        return {
            "phone": phone_normalized,
            "messages": messages,
            "count": len(messages),
            "source": source,
            "canal": canal
        }

    except Exception as e:
        logger.error(f"[Panel] Error obteniendo historial: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history/{contact_id}")
async def get_history_by_contact_id(
    contact_id: str,
    limit: int = Query(50, ge=1, le=100),
    canal: Optional[str] = Query(None, description="Canal de origen para filtrar mensajes"),
    phone: Optional[str] = Query(None, description="Teléfono para buscar historial"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Obtiene el historial de conversación por contact_id.

    ARQUITECTURA v2.0:
    1. Si hay phone → Consultar MongoDB primero (fuente de verdad, ~5ms)
    2. Si MongoDB vacío o sin phone → Consultar MongoDB por contact_id
    3. Si aún vacío → Fallback a HubSpot (datos históricos de migración)
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # Validar que contact_id sea numérico (ID de HubSpot)
    if not contact_id or not contact_id.isdigit():
        logger.warning(f"[Panel] contact_id inválido recibido: '{contact_id}'")
        return JSONResponse(
            status_code=200,
            content={
                "contact_id": contact_id,
                "messages": [],
                "count": 0,
                "canal": canal,
                "error": "ID de contacto inválido (debe ser numérico)"
            }
        )

    messages = []
    source = "none"

    try:
        mongo_manager = get_mongo_manager()

        # =====================================================================
        # PASO 1: MongoDB por teléfono (preferido - más eficiente)
        # =====================================================================
        if phone:
            normalizer = PhoneNormalizer()
            validation = normalizer.normalize(phone)

            if validation.is_valid:
                messages = await mongo_manager.get_history(
                    phone=validation.normalized,
                    limit=limit,
                    channel=canal
                )
                if messages:
                    source = "mongodb"
                    logger.debug(f"[Panel] Historial desde MongoDB (phone): {len(messages)} msgs")

        # =====================================================================
        # PASO 2: MongoDB por contact_id (si no hay phone o no hay resultados)
        # =====================================================================
        if not messages:
            messages = await mongo_manager.get_history_by_contact_id(
                hubspot_contact_id=contact_id,
                limit=limit
            )
            if messages:
                source = "mongodb"
                logger.debug(f"[Panel] Historial desde MongoDB (contact_id): {len(messages)} msgs")

        # =====================================================================
        # PASO 3: Fallback a HubSpot (datos históricos de migración)
        # =====================================================================
        if not messages:
            timeline_logger = get_timeline_logger()
            messages = await timeline_logger.get_notes_for_contact(
                contact_id=contact_id,
                limit=limit
            )
            if messages:
                source = "hubspot"
                logger.debug(f"[Panel] Historial desde HubSpot (fallback): {len(messages)} msgs")

        # Asegurar que messages sea una lista válida
        if messages is None:
            messages = []

        canal_info = f", canal={canal}" if canal else ""
        logger.info(f"[Panel] Historial cargado: {len(messages)} msgs para contact_id={contact_id}{canal_info} (source={source})")

        return {
            "contact_id": contact_id,
            "messages": messages,
            "count": len(messages),
            "canal": canal,
            "phone": phone,
            "source": source
        }

    except Exception as e:
        # Log del error pero retornar 200 con lista vacía para evitar 502
        logger.error(f"[Panel] Error obteniendo historial para {contact_id}: {e}", exc_info=True)
        return JSONResponse(
            status_code=200,
            content={
                "contact_id": contact_id,
                "messages": [],
                "count": 0,
                "canal": canal,
                "source": "error",
                "error": f"Error interno: {str(e)}"
            }
        )


@router.post("/contacts/{phone}/take-control")
async def take_control_of_conversation(
    phone: str,
    canal: Optional[str] = Query(None, description="Canal de origen"),
    contact_id: Optional[str] = Query(None, description="ID del contacto en HubSpot"),
    advisor_id: Optional[str] = Query(None, description="ID de la asesora que toma control"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Activa HUMAN_ACTIVE cuando la asesora hace click en un contacto.

    Este endpoint debe llamarse ANTES de cargar el historial para asegurar
    que Sofía no responda mientras la asesora está revisando la conversación.

    IMPORTANTE: Resuelve el bug donde Sofía seguía respondiendo porque
    HUMAN_ACTIVE solo se activaba al ENVIAR un mensaje, no al seleccionar.
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # Normalizar teléfono
    normalizer = PhoneNormalizer()
    validation = normalizer.normalize(phone)
    phone_normalized = validation.normalized if validation.is_valid else phone

    try:
        # Redis URL unificado
        is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
        redis_url = os.getenv("REDIS_URL") if is_railway else (
            os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
        )
        state_manager = ConversationStateManager(redis_url)

        # Verificar estado actual
        current_status = await state_manager.get_status(phone_normalized, canal or "whatsapp")

        # Si ya está en HUMAN_ACTIVE o IN_CONVERSATION, solo refrescar TTL
        if current_status in [ConversationStatus.HUMAN_ACTIVE, ConversationStatus.IN_CONVERSATION]:
            # Refrescar TTL sin cambiar estado
            await state_manager.set_status(
                phone_normalized,
                current_status,
                canal=canal or "whatsapp",
                ttl=state_manager.HANDOFF_TTL_SECONDS
            )
            logger.info(
                f"[Panel] Take Control: TTL refrescado para {phone_normalized}:{canal} "
                f"(estado: {current_status.value})"
            )
            return {
                "status": "success",
                "action": "ttl_refreshed",
                "phone": phone_normalized,
                "canal": canal,
                "current_status": current_status.value,
                "message": "TTL de sesión refrescado"
            }

        # Si está en PENDING_HANDOFF, cambiar a HUMAN_ACTIVE
        if current_status == ConversationStatus.PENDING_HANDOFF:
            await state_manager.activate_human(
                phone_normalized=phone_normalized,
                canal_origen=canal or "whatsapp",
                owner_id=advisor_id,
                contact_id=contact_id,
                reason="Asesora tomó control desde panel"
            )
            logger.info(
                f"[Panel] Take Control: PENDING_HANDOFF -> HUMAN_ACTIVE para {phone_normalized}:{canal}"
            )
            return {
                "status": "success",
                "action": "human_activated",
                "phone": phone_normalized,
                "canal": canal,
                "previous_status": "PENDING_HANDOFF",
                "new_status": "HUMAN_ACTIVE",
                "message": "Control tomado - Sofía pausada"
            }

        # Si era BOT_ACTIVE o no existía, activar HUMAN_ACTIVE
        await state_manager.activate_human(
            phone_normalized=phone_normalized,
            canal_origen=canal or "whatsapp",
            owner_id=advisor_id,
            contact_id=contact_id,
            reason="Asesora seleccionó contacto en panel"
        )

        previous_status = current_status.value if current_status else "BOT_ACTIVE"
        logger.info(
            f"[Panel] Take Control: {previous_status} -> HUMAN_ACTIVE para {phone_normalized}:{canal}"
        )

        return {
            "status": "success",
            "action": "human_activated",
            "phone": phone_normalized,
            "canal": canal,
            "advisor_id": advisor_id,
            "previous_status": previous_status,
            "new_status": "HUMAN_ACTIVE",
            "message": "Control tomado - Sofía pausada"
        }

    except Exception as e:
        logger.error(f"[Panel] Error en take-control: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/debug/redis")
async def debug_redis(
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Endpoint de diagnóstico para verificar conexión Redis y datos.
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        # Redis URL unificado (Railway interna, local pública)
        is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
        redis_url = os.getenv("REDIS_URL") if is_railway else (
            os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
        )

        r = await _get_redis_client()

        # Test connection
        pong = await r.ping()

        # Get all conversation state keys
        state_keys = []
        async for key in r.scan_iter(match="conv_state:*"):
            value = await r.get(key)
            ttl = await r.ttl(key)
            state_keys.append({
                "key": key,
                "value": value,
                "ttl": ttl
            })

        # Get all meta keys
        meta_keys = []
        async for key in r.scan_iter(match="conv_meta:*"):
            value = await r.get(key)
            ttl = await r.ttl(key)
            meta_keys.append({
                "key": key,
                "value": value[:100] + "..." if len(value or "") > 100 else value,
                "ttl": ttl
            })

        return {
            "redis_url": redis_url,
            "connection_ok": pong,
            "state_keys_count": len(state_keys),
            "state_keys": state_keys,
            "meta_keys_count": len(meta_keys),
            "meta_keys": meta_keys,
            "env_vars": {
                "REDIS_PUBLIC_URL": os.getenv("REDIS_PUBLIC_URL", "(not set)"),
                "REDIS_URL": os.getenv("REDIS_URL", "(not set)")
            }
        }

    except Exception as e:
        logger.error(f"[Panel] Error en debug Redis: {e}")
        return {
            "error": str(e),
            "redis_url": os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
        }


@router.get("/contacts/search")
async def search_contacts_by_keyword(
    q: str = Query(..., min_length=2, max_length=100, description="Palabra clave a buscar"),
    limit: int = Query(20, ge=1, le=50),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Busca contactos por palabra clave en el historial de mensajes.
    
    Usa búsqueda fulltext de MongoDB para encontrar mensajes que contengan
    la palabra buscada y retorna los contactos asociados.
    
    Args:
        q: Palabra clave a buscar (mínimo 2 caracteres)
        limit: Máximo de contactos a retornar
    
    Returns:
        Lista de teléfonos que tienen mensajes con la palabra buscada
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        from database.mongodb_client import MongoDBManager
        
        logger.info(f"[Panel] Búsqueda por palabra clave: '{q}'")
        
        mongo_manager = MongoDBManager()
        matching_phones = await mongo_manager.search_messages_fulltext(q, limit=limit)
        
        logger.info(f"[Panel] Búsqueda '{q}': {len(matching_phones)} contactos encontrados")
        
        return {
            "query": q,
            "count": len(matching_phones),
            "phones": matching_phones
        }
        
    except Exception as e:
        logger.error(f"[Panel] Error en búsqueda por palabra clave: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def _get_contacts_by_worker_filter(
    worker_id: str,
    advisor: Optional[str],
    limit: int,
) -> dict:
    """
    Branch del pipeline de contactos activado cuando se filtra por worker_id.

    Lógica:
    1. MongoDB: citas futuras activas del worker → contact_ids + phones
    2. HubSpot batch: obtener nombre/email de los contactos (1 llamada)
    3. Por cada contacto: obtener deal info (stage) desde _get_contact_deal_info (usa caché Redis)
    4. Filtrar: solo etapa "1275156341" (Visita agendada)
    5. Redis: añadir estado de conversación si está activo
    6. Retornar ordenado por appointment_dt ASC
    """
    STAGE_CITA_AGENDADA = "1275156341"

    mongo_mgr = get_mongo_manager()
    appointment_records = await mongo_mgr.get_contacts_by_worker(worker_id)

    if not appointment_records:
        return {
            "contacts": [],
            "filter": "worker",
            "worker_id": worker_id,
            "advisor": advisor,
            "active_count": 0,
            "historical_count": 0,
            "total_count": 0,
            "page": 1,
            "limit": limit,
        }

    # Deduplicar por contact_id (un contacto puede tener varias citas con el mismo worker)
    seen_ids: set = set()
    unique_records = []
    for rec in appointment_records:
        cid = rec.get("contact_id", "")
        if cid and cid not in seen_ids:
            seen_ids.add(cid)
            unique_records.append(rec)

    contact_ids = [r["contact_id"] for r in unique_records if r.get("contact_id")]
    appt_by_contact = {r["contact_id"]: r for r in unique_records}

    # Batch: nombres y emails desde HubSpot (1 llamada)
    batch_names = {}
    if contact_ids:
        batch_names = await _hubspot_batch_get_contacts(contact_ids)

    # Redis: estado de conversación
    is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
    redis_url = os.getenv("REDIS_URL") if is_railway else (
        os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
    )
    state_manager = ConversationStateManager(redis_url)

    contacts_out = []
    for rec in unique_records[:limit]:
        cid = rec.get("contact_id", "")
        if not cid:
            continue

        # Deal info con caché Redis (evita rate limit)
        try:
            deal_info = await _get_contact_deal_info(cid)
        except Exception:
            deal_info = None

        current_stage = deal_info.get("current_stage") if deal_info else None

        # Filtrar: solo etapa "Visita agendada"
        if current_stage != STAGE_CITA_AGENDADA:
            continue

        deal_id = deal_info.get("deal_id") if deal_info else None

        # Nombre desde HubSpot batch
        hs_info = batch_names.get(cid, {})
        display_name = f"{hs_info.get('firstname', '')} {hs_info.get('lastname', '')}".strip() or "Sin nombre"
        phone = hs_info.get("phone") or rec.get("phone", "")
        email = hs_info.get("email")

        # Filtro opcional por advisor (owner)
        owner_id = deal_info.get("owner_id") if deal_info else None
        if advisor and owner_id != advisor:
            continue

        # Estado de conversación desde Redis
        redis_meta = None
        try:
            if phone:
                redis_meta = await state_manager.get_meta(phone)
        except Exception:
            pass

        conversation_status = redis_meta.status if redis_meta else "historical"
        last_activity = redis_meta.last_activity.isoformat() if (redis_meta and redis_meta.last_activity) else None

        contacts_out.append({
            "contact_id": cid,
            "phone": phone,
            "display_name": display_name,
            "email": email,
            "owner_id": owner_id,
            "deal_id": deal_id,
            "current_stage": current_stage,
            "has_appointment": True,
            "appointment_dt": rec.get("appointment_dt"),
            "worker_name": rec.get("worker_name", ""),
            "conversation_status": conversation_status,
            "last_activity": last_activity,
            "is_active": conversation_status in ("HUMAN_ACTIVE", "IN_CONVERSATION", "BOT_ACTIVE"),
            "canal_origen": rec.get("canal", "whatsapp"),
            "time_ago": "",
            "ttl_display": "",
        })

    active_count = sum(1 for c in contacts_out if c.get("is_active"))
    return {
        "contacts": contacts_out,
        "filter": "worker",
        "worker_id": worker_id,
        "advisor": advisor,
        "active_count": active_count,
        "historical_count": len(contacts_out) - active_count,
        "total_count": len(contacts_out),
        "page": 1,
        "limit": limit,
    }


@router.get("/contacts")
async def get_active_contacts(
    filter_time: str = Query("24h", description="Filtro de tiempo: 24h, 48h, 1week, custom"),
    date_from: Optional[str] = Query(None, description="Fecha desde (ISO) para filtro custom"),
    date_to: Optional[str] = Query(None, description="Fecha hasta (ISO) para filtro custom"),
    advisor: Optional[str] = Query(None, description="Owner ID para filtrar contactos por asesora"),
    limit: int = Query(30, ge=1, le=100),
    page: int = Query(1, ge=1, description="Página (1-based) para paginación del ZSET"),
    worker_id: Optional[str] = Query(None, description="Worker ID para filtrar contactos por encargado de cita"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Retorna lista de contactos combinando dos fuentes:
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        from zoneinfo import ZoneInfo
        TIMEZONE = ZoneInfo("America/Bogota")

        now = datetime.now(TIMEZONE)

        # === BRANCH: Filtro por worker_id (citas) ===
        # Cuando worker_id está activo, el pipeline normal se omite.
        # Se buscan contactos desde MongoDB appointments y se filtran por etapa HubSpot.
        if worker_id:
            return await _get_contacts_by_worker_filter(
                worker_id=worker_id,
                advisor=advisor,
                limit=limit,
            )

        # === PASO 1: Obtener contactos ACTIVOS de Redis ===
        # Redis URL unificado (Railway interna, local pública)
        is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
        redis_url = os.getenv("REDIS_URL") if is_railway else (
            os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
        )
        logger.info(f"[Panel] Usando Redis URL: {redis_url}")
        state_manager = ConversationStateManager(redis_url)

        if advisor:
            # Cuando hay filtro de advisor: escanear todo el ZSET para no perder contactos
            # del advisor en páginas posteriores (problema cuando todos tienen el mismo score,
            # e.g. tras restore-panel — Redis ordena lex inverso y los del advisor quedan en p2+).
            _all = await state_manager.get_all_human_active_contacts(limit=1000, offset=0)
            advisor_contacts = [
                c for c in _all
                if c.get("owner_id") == advisor
                or advisor in (c.get("assigned_owner_ids") or [])
            ]
            logger.info(f"[Panel] Pre-filtrado por advisor {advisor}: {len(advisor_contacts)} contactos")
            zset_offset = (page - 1) * limit
            active_contacts = advisor_contacts[zset_offset:zset_offset + limit]
            total_for_advisor = len(advisor_contacts)
        else:
            zset_offset = (page - 1) * limit
            active_contacts = await state_manager.get_all_human_active_contacts(limit=limit, offset=zset_offset)
            total_for_advisor = None
            logger.info(f"[Panel] Encontrados {len(active_contacts)} contactos activos en Redis")

        # ── Pre-limitar ANTES del enriquecimiento con HubSpot.
        # Los contactos de prioridad (ZSET) siempre van; del resto solo los más recientes.
        # Sin esto, 534 contactos × waits de 429 (36s c/u) = timeout de Railway.
        priority_contacts = [c for c in active_contacts if c.get("in_priority_zset", True)]
        bot_contacts = [c for c in active_contacts if not c.get("in_priority_zset", True)]

        # Ordenar bot_contacts por última actividad (más reciente primero)
        def _sort_key(c):
            ts = c.get("last_activity") or ""
            return ts

        bot_contacts.sort(key=_sort_key, reverse=True)

        # Solo enriquecer los slots restantes hasta `limit`
        remaining_slots = max(0, limit - len(priority_contacts))
        active_contacts = priority_contacts + bot_contacts[:remaining_slots]
        logger.info(
            f"[Panel] Pre-limitado a {len(active_contacts)} contactos para enriquecimiento "
            f"({len(priority_contacts)} prioridad + {len(bot_contacts[:remaining_slots])} bot)"
        )

        # === PASO 2: Enriquecer contactos activos con HubSpot (OPTIMIZADO CON BATCH) ===
        contact_manager = ContactManager()
        
        # ── PASO 2.1: Batch request para obtener nombres de TODOS los contactos en 1 llamada ──
        # Esto reduce drásticamente los 429 (de N llamadas a 1)
        contact_ids_to_fetch = [
            c.get("contact_id") for c in active_contacts
            if c.get("contact_id")
        ]
        
        batch_contact_data = {}
        if contact_ids_to_fetch:
            batch_contact_data = await _hubspot_batch_get_contacts(contact_ids_to_fetch)
            logger.info(f"[Panel] Batch pre-fetch: {len(batch_contact_data)} contactos obtenidos")

        async def _enrich_single_contact(contact: dict) -> dict:
            """Enriquece un contacto individual con datos de HubSpot."""
            phone = contact.get("phone", "")

            # Buscar contact_id si no lo tenemos
            if phone and not contact.get("contact_id"):
                try:
                    contact_id = await contact_manager._search_contact(phone)
                    if contact_id:
                        contact["contact_id"] = contact_id
                except Exception:
                    pass

            # Si tenemos contact_id, obtener nombre de HubSpot y deal info
            if contact.get("contact_id"):
                cid = contact["contact_id"]

                # ✅ HubSpot es source of truth para nombres cuando está disponible en batch
                if cid in batch_contact_data:
                    hs_info = batch_contact_data[cid]
                    hs_name = f"{hs_info.get('firstname', '')} {hs_info.get('lastname', '')}".strip()
                    if hs_name:
                        contact["display_name"] = hs_name
                    elif not contact.get("display_name") or contact.get("display_name") in ("Cliente Nuevo", "Sin nombre"):
                        contact["display_name"] = "Sin nombre"
                    contact["email"] = hs_info.get("email")
                # Si no estaba en batch, conservar display_name de Redis

                # Buscar deal asociado para el dropdown de pipeline.
                # Si Redis meta ya tiene deal_id (contactos migrados via patch_redis_deal_ids.py),
                # usarlo directamente sin llamar HubSpot → elimina los 429 en carga del panel.
                if contact.get("deal_id"):
                    contact.setdefault("current_stage", contact.get("deal_stage", "1275156339"))
                else:
                    try:
                        deal_info = await _get_contact_deal_info(cid)
                        if deal_info:
                            contact["deal_id"] = deal_info.get("deal_id")
                            contact["current_stage"] = deal_info.get("current_stage")
                        else:
                            # AUTO-CREATE DEAL: Si el contacto no tiene deal, crear uno
                            # Evitar reintentos para contactos cuya creación de deal ya falló
                            _deal_fail_key = f"deal_failed:{cid}"
                            try:
                                _rc = await _get_redis_client()
                                _already_failed = await _rc.exists(_deal_fail_key)
                            except Exception:
                                _rc = None
                                _already_failed = False
                            if _already_failed:
                                logger.debug(f"[Panel] Skipping deal auto-create para {cid} (marcado como fallido)")
                            else:
                                try:
                                    created_deal = await contact_manager._create_deal_for_new_lead(
                                        contact_id=cid,
                                        phone_normalized=phone,
                                        source_channel="panel_auto"
                                    )
                                    if created_deal:
                                        contact["deal_id"] = created_deal.get("deal_id")
                                        contact["current_stage"] = created_deal.get("current_stage")
                                        try:
                                            if _rc is None:
                                                _rc = await _get_redis_client()
                                            _cache_data = json.dumps({
                                                "deal_id": created_deal.get("deal_id"),
                                                "current_stage": created_deal.get("current_stage")
                                            })
                                            await _rc.setex(f"{DEAL_CACHE_KEY_PREFIX}{cid}", DEAL_CACHE_TTL_SECONDS, _cache_data)
                                        except Exception:
                                            pass
                                        logger.info(f"[Panel] Deal auto-creado para contacto {cid}: {created_deal.get('deal_id')}")
                                    else:
                                        # Falló (None) → marcar para no reintentar durante 1h
                                        try:
                                            if _rc is None:
                                                _rc = await _get_redis_client()
                                            await _rc.setex(_deal_fail_key, 3600, "1")
                                        except Exception:
                                            pass
                                        logger.warning(f"[Panel] Deal auto-create fallido para {cid}, marcado 1h")
                                except Exception as create_err:
                                    try:
                                        if _rc is None:
                                            _rc = await _get_redis_client()
                                        await _rc.setex(_deal_fail_key, 3600, "1")
                                    except Exception:
                                        pass
                                    logger.warning(f"[Panel] No se pudo auto-crear deal: {create_err}")
                    except Exception as e:
                        logger.debug(f"[Panel] No se pudo obtener deal info: {e}")

            # Si aún no tenemos nombre, usar teléfono
            if not contact.get("display_name"):
                contact["display_name"] = phone or "Sin nombre"

            # Formatear TTL para mostrar
            ttl = contact.get("ttl_remaining")
            if ttl and ttl > 0:
                hours = ttl // 3600
                minutes = (ttl % 3600) // 60
                contact["ttl_display"] = f"Expira en {hours}h {minutes}m"

            return contact

        # ✅ FIX: Semáforo REDUCIDO para limitar llamadas concurrentes (deal lookups)
        hubspot_semaphore = asyncio.Semaphore(2)  # Reducido de 3 a 2 para evitar 429
        
        async def _enrich_with_rate_limit(contact: dict) -> dict:
            async with hubspot_semaphore:
                return await _enrich_single_contact(contact)

        # Ejecutar enriquecimiento en paralelo (limitado por semáforo)
        if active_contacts:
            enriched_contacts = await asyncio.gather(
                *[_enrich_with_rate_limit(contact) for contact in active_contacts],
                return_exceptions=True
            )
            # Filtrar excepciones y mantener contactos válidos
            active_contacts = [
                c for c in enriched_contacts
                if isinstance(c, dict)
            ]
            logger.info(f"[Panel] Enriquecimiento paralelo completado: {len(active_contacts)} contactos")

            # Batch write phone_cache inverso en 1 pipeline (en lugar de 30 writes concurrentes)
            try:
                _rc_batch = await _get_redis_client()
                _pipe = _rc_batch.pipeline(transaction=False)
                for _c in active_contacts:
                    _cid = _c.get("contact_id")
                    _ph  = _c.get("phone")
                    if _cid and _ph:
                        _pipe.set(f"phone_cache:{_cid}", _ph, ex=86400, nx=True)
                await _pipe.execute()
            except Exception:
                pass

        # === PASO 3: Calcular rango de tiempo para historial ===
        if filter_time == "24h":
            since = now - timedelta(hours=24)
            until = now
        elif filter_time == "48h":
            since = now - timedelta(hours=48)
            until = now
        elif filter_time == "1week":
            since = now - timedelta(weeks=1)
            until = now
        elif filter_time == "custom" and date_from:
            since = datetime.fromisoformat(date_from)
            until = datetime.fromisoformat(date_to) if date_to else now
        else:
            since = now - timedelta(hours=24)
            until = now

        # === PASO 3.5: Filtrar contactos por fecha, PERO siempre incluir los activos ===
        # REGLA IMPORTANTE: Contactos en HUMAN_ACTIVE o IN_CONVERSATION SIEMPRE se muestran
        # porque están esperando atención. El filtro de tiempo solo aplica a históricos.
        if filter_time != "all":
            filtered_active = []
            for contact in active_contacts:
                # PRIORIDAD 1: Si está activamente esperando atención, SIEMPRE incluir
                is_waiting = contact.get("is_active", False)
                status = contact.get("conversation_status") or contact.get("status") or ""
                is_human_active = status in ["HUMAN_ACTIVE", "PENDING_HANDOFF", "IN_CONVERSATION"]

                if is_waiting or is_human_active:
                    # Calcular time_ago para mostrar, pero NO filtrar
                    # Usar last_activity (último mensaje) como referencia de tiempo
                    ref_time = contact.get("last_activity") or contact.get("activated_at")
                    if ref_time:
                        try:
                            if isinstance(ref_time, str):
                                ref_dt = datetime.fromisoformat(ref_time.replace('+00:00Z', '+00:00').replace("Z", "+00:00"))
                            else:
                                ref_dt = ref_time
                            if ref_dt.tzinfo is None:
                                ref_dt = ref_dt.replace(tzinfo=TIMEZONE)
                            time_ago = now - ref_dt.astimezone(TIMEZONE)
                            if time_ago.total_seconds() < 3600:
                                contact["time_ago"] = f"hace {int(time_ago.total_seconds() // 60)} min"
                            elif time_ago.total_seconds() < 86400:
                                contact["time_ago"] = f"hace {int(time_ago.total_seconds() // 3600)} h"
                            else:
                                contact["time_ago"] = f"hace {int(time_ago.days)} días"
                        except (ValueError, TypeError):
                            contact["time_ago"] = "en espera"
                    else:
                        contact["time_ago"] = "en espera"

                    filtered_active.append(contact)
                    logger.debug(f"[Panel] Contacto {contact.get('phone')} incluido (activo/en espera)")
                    continue

                # PRIORIDAD 2: Para contactos no activos, filtrar por last_activity
                # Usar last_activity como referencia principal; fallback a activated_at
                ref_time = contact.get("last_activity") or contact.get("activated_at")
                if ref_time:
                    try:
                        if isinstance(ref_time, str):
                            ref_dt = datetime.fromisoformat(ref_time.replace("Z", "+00:00"))
                        else:
                            ref_dt = ref_time

                        if ref_dt.tzinfo is None:
                            ref_dt = ref_dt.replace(tzinfo=TIMEZONE)

                        if since <= ref_dt <= until:
                            time_ago = now - ref_dt.astimezone(TIMEZONE)
                            if time_ago.total_seconds() < 3600:
                                contact["time_ago"] = f"hace {int(time_ago.total_seconds() // 60)} min"
                            elif time_ago.total_seconds() < 86400:
                                contact["time_ago"] = f"hace {int(time_ago.total_seconds() // 3600)} h"
                            else:
                                contact["time_ago"] = f"hace {int(time_ago.days)} días"
                            filtered_active.append(contact)
                        else:
                            logger.debug(
                                f"[Panel] Contacto histórico {contact.get('phone')} excluido por filtro de tiempo"
                            )
                    except (ValueError, TypeError) as e:
                        logger.debug(f"[Panel] Error parseando fecha: {e}")
                        filtered_active.append(contact)
                else:
                    contact["time_ago"] = "reciente"
                    filtered_active.append(contact)

            logger.info(
                f"[Panel] Contactos después de filtro de tiempo: "
                f"{len(filtered_active)}/{len(active_contacts)} (activos siempre incluidos)"
            )
            active_contacts = filtered_active

        # === PASO 4: Obtener historial de HubSpot (si hay espacio) ===
        remaining_slots = limit - len(active_contacts)
        historical_contacts = []

        if remaining_slots > 0:
            try:
                timeline_logger = get_timeline_logger()
                result = await timeline_logger.get_contacts_with_advisor_activity(
                    since=since,
                    until=until,
                    limit=remaining_slots
                )

                # Extraer contactos del resultado (nuevo formato con paginación)
                historical_contacts = result.get("contacts", []) if isinstance(result, dict) else result

                # Marcar como no activos y enriquecer
                for contact in historical_contacts:
                    contact["is_active"] = False
                    contact["conversation_status"] = "historical"

                    # Formatear nombre
                    firstname = contact.get("firstname", "")
                    lastname = contact.get("lastname", "")
                    contact["display_name"] = f"{firstname} {lastname}".strip() or "Sin nombre"

            except Exception as e:
                logger.warning(f"[Panel] Error obteniendo historial de HubSpot: {e}")

        # === PASO 5: Combinar y deduplicar ===
        seen_phones = {c.get("phone") for c in active_contacts if c.get("phone")}
        seen_contact_ids = {c.get("contact_id") for c in active_contacts if c.get("contact_id")}

        for contact in historical_contacts:
            phone = contact.get("phone")
            contact_id = contact.get("id") or contact.get("contact_id")

            # Evitar duplicados
            if phone and phone in seen_phones:
                continue
            if contact_id and contact_id in seen_contact_ids:
                continue

            active_contacts.append(contact)
            if phone:
                seen_phones.add(phone)
            if contact_id:
                seen_contact_ids.add(contact_id)

        # === PASO 6: SEGREGACIÓN ESTRICTA por equipo/portal ===
        # REGLA DE ORO: Una asesora SOLO ve contactos cuyo canal_origen pertenece a su equipo.
        # NO hay filtraciones de leads entre portales.
        if advisor:
            # Modo admin: mostrar todos sin filtrar (bypass de segregación)
            if advisor.lower() == "admin":
                logger.info(f"[Panel] Modo ADMIN: mostrando todos los {len(active_contacts)} contactos (sin segregación)")
            else:
                from integrations.hubspot.lead_assigner import LeadAssigner

                advisor_str = str(advisor)

                # ═══════════════════════════════════════════════════════════════
                # PASO 6.1: Identificar a qué EQUIPO pertenece este advisor
                # ═══════════════════════════════════════════════════════════════
                advisor_team = None
                advisor_name = "Desconocido"

                for team_name, team_members in LeadAssigner.OWNERS_CONFIG.items():
                    for member in team_members:
                        if str(member.get("id")) == advisor_str:
                            advisor_team = team_name
                            advisor_name = member.get("name", "Desconocido")
                            break
                    if advisor_team:
                        break

                if not advisor_team:
                    logger.warning(
                        f"[Panel] Advisor ID {advisor} no encontrado en OWNERS_CONFIG. "
                        f"Usando equipo 'default' (acceso restringido)"
                    )
                    advisor_team = "default"

                # ═══════════════════════════════════════════════════════════════
                # PASO 6.2: Obtener los canales PERMITIDOS para este equipo
                # ═══════════════════════════════════════════════════════════════
                allowed_channels = set()

                for canal, team in LeadAssigner.CHANNEL_TO_TEAM.items():
                    if team == advisor_team:
                        allowed_channels.add(canal)

                # Si es equipo "default", SOLO permitir canales que mapean a "default"
                # (NO dar acceso a todos los canales)
                if advisor_team == "default":
                    for canal, team in LeadAssigner.CHANNEL_TO_TEAM.items():
                        if team == "default":
                            allowed_channels.add(canal)

                logger.info(
                    f"[Panel] SEGREGACIÓN: Advisor '{advisor_name}' (ID: {advisor}) "
                    f"pertenece a '{advisor_team}'. "
                    f"Canales permitidos: {sorted(allowed_channels)}"
                )

                # ═══════════════════════════════════════════════════════════════
                # PASO 6.3: Obtener lista de TODOS los advisors conocidos
                # (para detectar transferencias a otros advisors)
                # ═══════════════════════════════════════════════════════════════
                all_advisor_ids = set()
                for team_members in LeadAssigner.OWNERS_CONFIG.values():
                    for member in team_members:
                        member_id = member.get("id")
                        if member_id:
                            all_advisor_ids.add(str(member_id))
                
                other_advisor_ids = all_advisor_ids - {advisor_str}

                # ═══════════════════════════════════════════════════════════════
                # PASO 6.4: Filtrar contactos ESTRICTAMENTE
                # PRIORIDAD: owner_id > canal_origen
                # ═══════════════════════════════════════════════════════════════
                filtered_contacts = []
                excluded_count = 0

                for contact in active_contacts:
                    canal_origen = (contact.get("canal_origen") or "").lower().strip()
                    phone = contact.get("phone", "N/A")
                    
                    # Obtener owner_id del contacto (puede venir de transferencia)
                    contact_owner = contact.get("owner_id") or contact.get("assigned_owner_id") or contact.get("hubspot_owner_id")
                    contact_owner_str = str(contact_owner) if contact_owner else ""
                    
                    # Obtener lista de colaboradores (modo collaborative)
                    assigned_owner_ids = contact.get("assigned_owner_ids") or []
                    assigned_owner_ids_str = [str(x) for x in assigned_owner_ids if x]
                    
                    # REGLA 1: TRANSFERENCIA tiene PRIORIDAD sobre segregación por canal
                    # Incluir si:
                    #   a) owner_id coincide con el advisor (transferencia exclusiva), O
                    #   b) advisor está en assigned_owner_ids (modo colaborativo)
                    is_owner = contact_owner_str == advisor_str
                    is_collaborator = advisor_str in assigned_owner_ids_str
                    
                    if is_owner or is_collaborator:
                        filtered_contacts.append(contact)
                        if is_collaborator and not is_owner:
                            logger.info(
                                f"[Panel] ✓ Contacto {phone} INCLUIDO por COLABORACIÓN "
                                f"(advisor {advisor_str} está en assigned_owner_ids={assigned_owner_ids_str})"
                            )
                        elif canal_origen and canal_origen not in allowed_channels:
                            logger.info(
                                f"[Panel] ✓ Contacto {phone} INCLUIDO por TRANSFERENCIA "
                                f"(owner_id={contact_owner_str} coincide, canal '{canal_origen}' es de otro equipo)"
                            )
                        else:
                            logger.debug(
                                f"[Panel] ✓ Contacto {phone} INCLUIDO "
                                f"(owner_id coincide directamente)"
                            )
                        continue

                    # REGLA 2: Si tiene canal_origen, verificar si pertenece al equipo
                    # PERO primero verificar si fue TRANSFERIDO a otro advisor
                    if canal_origen:
                        # REGLA 2a: Si fue transferido a OTRO advisor conocido, EXCLUIR
                        if contact_owner_str and contact_owner_str in other_advisor_ids:
                            excluded_count += 1
                            logger.info(
                                f"[Panel] ✗ Contacto {phone} EXCLUIDO por TRANSFERENCIA "
                                f"(owner_id={contact_owner_str} es otro advisor, "
                                f"aunque canal '{canal_origen}' pertenece a '{advisor_team}')"
                            )
                            continue
                        
                        # REGLA 2b: Canal pertenece al equipo y no fue transferido a otro
                        if canal_origen in allowed_channels:
                            filtered_contacts.append(contact)
                            logger.debug(
                                f"[Panel] ✓ Contacto {phone} INCLUIDO "
                                f"(canal '{canal_origen}' pertenece a '{advisor_team}')"
                            )
                        else:
                            # Canal NO pertenece al equipo Y no fue transferido a este advisor
                            excluded_count += 1
                            logger.debug(
                                f"[Panel] ✗ Contacto {phone} EXCLUIDO "
                                f"(canal '{canal_origen}' NO pertenece a '{advisor_team}', "
                                f"owner={contact_owner_str} != {advisor_str})"
                            )
                        continue

                    # REGLA 3: Sin canal_origen y owner no coincide -> EXCLUIR
                    excluded_count += 1
                    logger.debug(
                        f"[Panel] ✗ Contacto {phone} EXCLUIDO "
                        f"(sin canal_origen y owner_id {contact_owner_str} no coincide con {advisor_str})"
                    )

                active_contacts = filtered_contacts

                logger.info(
                    f"[Panel] Segregación estricta completada para '{advisor_name}' ({advisor_team}): "
                    f"{len(active_contacts)} contactos visibles, {excluded_count} excluidos"
                )

        # === PASO 7: El orden ya viene correcto del ZSET (por last_activity descendente) ===
        # NO reordenar por activated_at porque destruye el orden de "actividad reciente primero"
        contacts_sorted = active_contacts  # Mantener orden del backend

        # === PASO 7.5: Marcar contactos con citas activas (una sola query MongoDB) ===
        try:
            mongo_mgr = get_mongo_manager()
            page_contact_ids = [
                c.get("contact_id", "") for c in contacts_sorted[:limit]
                if c.get("contact_id")
            ]
            contacts_with_appts = await mongo_mgr.get_contacts_with_appointments(page_contact_ids)
            for c in contacts_sorted:
                c["has_appointment"] = c.get("contact_id", "") in contacts_with_appts
        except Exception as appt_err:
            logger.warning(f"[Panel] Error verificando citas activas: {appt_err}")
            for c in contacts_sorted:
                c["has_appointment"] = False

        # active_count = solo contactos ESPERANDO respuesta (HUMAN_ACTIVE / PENDING_HANDOFF)
        # IN_CONVERSATION no cuenta: ya están siendo atendidos
        waiting_statuses = {"HUMAN_ACTIVE", "PENDING_HANDOFF"}
        active_count = len([
            c for c in contacts_sorted
            if c.get("conversation_status") in waiting_statuses
        ])

        # Log para diagnóstico
        logger.info(
            f"[Panel] Retornando {len(contacts_sorted[:limit])} contactos "
            f"(activos: {active_count}, advisor: {advisor})"
        )
        for c in contacts_sorted[:limit]:
            logger.debug(
                f"[Panel] -> {c.get('phone', 'N/A')} | "
                f"active={c.get('is_active')} | "
                f"canal={c.get('canal_origen', 'N/A')} | "
                f"owner={c.get('owner_id', 'N/A')} | "
                f"deal_id={c.get('deal_id', 'NONE')} | "
                f"stage={c.get('current_stage', 'N/A')}"
            )

        return {
            "contacts": contacts_sorted[:limit],
            "filter": filter_time,
            "advisor": advisor,
            "active_count": active_count,
            "historical_count": len(contacts_sorted) - active_count,
            "total_count": total_for_advisor if total_for_advisor is not None else len(contacts_sorted),
            "page": page,
            "limit": limit,
            "since": since.isoformat(),
            "until": until.isoformat()
        }

    except Exception as e:
        logger.error(f"[Panel] Error obteniendo contactos: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def _get_hubspot_contact_info(contact_id: str) -> Optional[dict]:
    """
    Obtiene información básica de un contacto de HubSpot.
    """
    import httpx

    hubspot_api_key = os.getenv("HUBSPOT_API_KEY")
    if not hubspot_api_key:
        return None

    try:
        url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await _hubspot_get(
                client, url, hubspot_api_key,
                params={"properties": "firstname,lastname,email,phone"}
            )

            if response.status_code == 200:
                data = response.json()
                return data.get("properties", {})
            elif response.status_code == 429:
                logger.warning(f"[Panel] HubSpot 429 persistente al obtener contacto {contact_id}")

    except Exception as e:
        logger.debug(f"[Panel] Error obteniendo info de HubSpot: {e}")

    return None


# ============================================================================
# ADVISORS (Asesores del panel - nombres editables)
# ============================================================================

@router.get("/advisors")
async def list_advisors(x_api_key: str = Header(None, alias="X-API-Key")):
    """Lista todos los asesores con sus nombres editables."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    advisors = await mongo_mgr.get_advisors()
    return {"advisors": advisors}


class AdvisorUpdateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


@router.patch("/advisors/{advisor_id}")
async def update_advisor(
    advisor_id: str,
    body: AdvisorUpdateRequest,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Actualiza el nombre de un asesor."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    ok = await mongo_mgr.update_advisor(advisor_id, body.name)
    if not ok:
        raise HTTPException(status_code=404, detail="Asesor no encontrado")
    logger.info(f"[Panel] Advisor {advisor_id} renombrado a: {body.name}")
    return {"ok": True, "id": advisor_id, "name": body.name}


# ============================================================================
# WORKERS (Equipo de campo para citas)
# ============================================================================

@router.get("/workers")
async def list_workers(x_api_key: str = Header(None, alias="X-API-Key")):
    """Lista todos los workers activos del equipo de campo."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    workers = await mongo_mgr.get_workers()
    return {"workers": workers}


class WorkerCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class WorkerUpdateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


@router.post("/workers", status_code=201)
async def create_worker(
    body: WorkerCreateRequest,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Crea un nuevo worker (encargado de mostrar inmuebles)."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    worker_id = await mongo_mgr.create_worker(body.name)
    if not worker_id:
        raise HTTPException(status_code=409, detail="Ya existe un worker con ese nombre")
    logger.info(f"[Panel] Worker creado: {body.name} ({worker_id})")
    return {"worker_id": worker_id, "name": body.name}


@router.patch("/workers/{worker_id}")
async def update_worker(
    worker_id: str,
    body: WorkerUpdateRequest,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Actualiza el nombre de un worker."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    ok = await mongo_mgr.update_worker(worker_id, body.name)
    if not ok:
        raise HTTPException(status_code=404, detail="Worker no encontrado")
    return {"ok": True, "name": body.name}


@router.delete("/workers/{worker_id}")
async def delete_worker(
    worker_id: str,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Elimina (soft-delete) un worker. Sus citas históricas se preservan."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    ok = await mongo_mgr.delete_worker(worker_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Worker no encontrado")
    return {"ok": True}


# ============================================================================
# APPOINTMENTS (Citas agendadas)
# ============================================================================

class AppointmentCreateRequest(BaseModel):
    worker_id: str = Field(..., description="ID del worker en MongoDB")
    worker_name: str = Field(..., description="Nombre del worker (desnormalizado para rapidez)")
    appointment_dt: str = Field(..., description="Fecha y hora en ISO 8601 (ej. 2026-03-10T10:00:00)")
    notes: str = Field("", description="Observaciones de la cita")
    advisor_id: Optional[str] = Field(None, description="HubSpot owner ID de la asesora")
    canal: str = Field("whatsapp", description="Canal de origen del contacto (whatsapp, instagram, etc.)")


@router.post("/contacts/{contact_id}/appointments", status_code=201)
async def create_appointment(
    contact_id: str,
    body: AppointmentCreateRequest,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """
    Agenda una cita para un contacto:
    1. Crea nota en HubSpot con formato estructurado
    2. Persiste la cita en MongoDB
    3. Retorna el appointment_id
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    from datetime import datetime as dt
    from zoneinfo import ZoneInfo
    BOGOTA_TZ = ZoneInfo("America/Bogota")

    # Parsear fecha y convertir a timezone Colombia
    try:
        appt_dt_utc = dt.fromisoformat(body.appointment_dt)
        if appt_dt_utc.tzinfo is None:
            appt_dt_utc = appt_dt_utc.replace(tzinfo=BOGOTA_TZ)
        appt_dt_bogota = appt_dt_utc.astimezone(BOGOTA_TZ)
    except ValueError:
        raise HTTPException(status_code=422, detail="Formato de fecha inválido. Use ISO 8601.")

    # Formatear fecha legible para la nota
    DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    MESES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
    fecha_str = (
        f"{DIAS[appt_dt_bogota.weekday()]}, "
        f"{appt_dt_bogota.day} {MESES[appt_dt_bogota.month - 1]} {appt_dt_bogota.year} | "
        f"{appt_dt_bogota.strftime('%I:%M %p')} (Hora Colombia)"
    )

    # Construir nota HubSpot
    notas_extra = f"\nNotas: {body.notes}" if body.notes.strip() else ""
    note_body = (
        f"📅 CITA PROGRAMADA\n"
        f"Encargado: {body.worker_name}\n"
        f"Fecha: {fecha_str}"
        f"{notas_extra}"
    )

    # Crear nota en HubSpot (en background para no bloquear)
    hubspot_note_id = None
    try:
        from integrations.hubspot.hubspot_client import HubSpotClient
        hs_client = HubSpotClient()
        hubspot_note_id = await hs_client.create_note(
            contact_id=contact_id,
            body=note_body,
            owner_id=body.advisor_id
        )
        logger.info(f"[Panel] Nota de cita creada en HubSpot: {hubspot_note_id}")
    except Exception as hs_err:
        logger.warning(f"[Panel] No se pudo crear nota en HubSpot para cita: {hs_err}")
        # No falla el endpoint — la cita se guarda en MongoDB de todas formas

    # Obtener phone del contacto para guardar en MongoDB
    phone = ""
    try:
        hs_info = await _get_hubspot_contact_info(contact_id)
        if hs_info:
            phone = hs_info.get("phone", "")
    except Exception:
        pass

    # Persistir en MongoDB
    mongo_mgr = get_mongo_manager()
    appointment_id = await mongo_mgr.create_appointment(
        contact_id=contact_id,
        phone=phone,
        advisor_id=body.advisor_id or "",
        worker_id=body.worker_id,
        worker_name=body.worker_name,
        appointment_dt=appt_dt_utc,
        notes=body.notes,
        hubspot_note_id=hubspot_note_id
    )

    if not appointment_id:
        raise HTTPException(status_code=500, detail="Error guardando la cita en base de datos")

    # Insertar nota de cita en el historial de la conversación (colección messages)
    if phone:
        try:
            await mongo_mgr.save_message(
                phone=phone,
                content=note_body,
                sender="system",
                channel=body.canal,
                hubspot_contact_id=contact_id,
                metadata={
                    "type": "appointment_created",
                    "appointment_id": appointment_id,
                    "worker_name": body.worker_name,
                    "fecha_display": fecha_str,
                }
            )
            logger.info(f"[Panel] Nota de cita guardada en historial: contact={contact_id}")
        except Exception as msg_err:
            logger.warning(f"[Panel] No se pudo guardar nota de cita en historial: {msg_err}")

    # Sincronizar cita a Redis para que el scheduler de recordatorios la detecte
    if phone:
        try:
            from middleware.appointment_manager import AppointmentManager
            redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
            normalizer = PhoneNormalizer()
            phone_norm_result = normalizer.normalize(phone)
            phone_normalized = phone_norm_result.normalized if phone_norm_result.is_valid else phone

            apt_manager = AppointmentManager(redis_url)
            await apt_manager.create_appointment(
                phone_normalized=phone_normalized,
                canal=body.canal,
                scheduled_datetime=appt_dt_bogota,
                contact_name=None,  # Se enriquece si se obtiene del contacto
                contact_id=contact_id,
                notes=body.notes or None,
            )
            await apt_manager.close()
            logger.info(
                f"[Panel] Cita sincronizada a Redis: phone={phone_normalized}, canal={body.canal}, "
                f"dt={appt_dt_bogota.isoformat()}"
            )
        except Exception as redis_err:
            # No falla el endpoint — MongoDB ya tiene la cita
            logger.warning(f"[Panel] No se pudo sincronizar cita a Redis (recordatorios pueden no funcionar): {redis_err}")

    logger.info(
        f"[Panel] Cita agendada: contact={contact_id}, worker={body.worker_name}, "
        f"fecha={fecha_str}, appt_id={appointment_id}"
    )
    return {
        "appointment_id": appointment_id,
        "hubspot_note_id": hubspot_note_id,
        "worker_name": body.worker_name,
        "appointment_dt": appt_dt_bogota.isoformat(),
        "fecha_display": fecha_str,
        "note_body": note_body
    }


@router.get("/contacts/{contact_id}/appointments")
async def get_appointments(
    contact_id: str,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Lista las citas de un contacto."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    appts = await mongo_mgr.get_appointments(contact_id)
    return {"appointments": appts, "total": len(appts)}


@router.patch("/appointments/{appointment_id}/cancel")
async def cancel_appointment(
    appointment_id: str,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Cancela una cita existente."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    ok = await mongo_mgr.cancel_appointment(appointment_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Cita no encontrada")
    return {"ok": True}


class AppointmentUpdateBody(BaseModel):
    worker_id: str = None
    worker_name: str = None
    appointment_dt: str = None
    notes: str = None


@router.patch("/appointments/{appointment_id}")
async def update_appointment(
    appointment_id: str,
    body: AppointmentUpdateBody,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Actualiza una cita existente."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    from datetime import datetime as dt
    from zoneinfo import ZoneInfo
    BOGOTA_TZ = ZoneInfo("America/Bogota")

    appt_dt = None
    if body.appointment_dt:
        try:
            appt_dt = dt.fromisoformat(body.appointment_dt)
            if appt_dt.tzinfo is None:
                appt_dt = appt_dt.replace(tzinfo=BOGOTA_TZ)
        except ValueError:
            raise HTTPException(status_code=422, detail="Formato de fecha inválido")

    mongo_mgr = get_mongo_manager()
    ok = await mongo_mgr.update_appointment(
        appointment_id=appointment_id,
        worker_id=body.worker_id,
        worker_name=body.worker_name,
        appointment_dt=appt_dt,
        notes=body.notes
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Cita no encontrada")
    return {"ok": True}


@router.delete("/appointments/{appointment_id}")
async def delete_appointment(
    appointment_id: str,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Elimina una cita permanentemente."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    ok = await mongo_mgr.delete_appointment(appointment_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Cita no encontrada")
    return {"ok": True}


# ============================================================================
# UI del Panel
# ============================================================================

@router.get("/", response_class=HTMLResponse)
async def panel_ui(request: Request, x_api_key: str = Query(None, alias="key")):
    """
    Interfaz web del panel de envio para asesores - WhatsApp Web Style.

    Acceso: /whatsapp/panel/?key=TU_API_KEY
    """
    # Validar API Key via query param para acceso web
    if not _validate_api_key(x_api_key):
        return HTMLResponse(
            content="""
            <!DOCTYPE html>
            <html>
            <head><title>Acceso Denegado</title></head>
            <body style="font-family: Arial; padding: 50px; text-align: center;">
                <h1>Acceso Denegado</h1>
                <p>Se requiere API Key valida.</p>
                <p>Uso: /whatsapp/panel/?key=TU_API_KEY</p>
            </body>
            </html>
            """,
            status_code=401
        )

    # Cargar nombres de asesores desde MongoDB (editables)
    mongo_mgr = get_mongo_manager()
    advisors_list = await mongo_mgr.get_advisors()
    
    # Convertir lista a diccionario {id: name} para el template
    advisor_names = {a["id"]: a["name"] for a in advisors_list}

    return templates.TemplateResponse("index.html", {
        "request": request,
        "api_key": x_api_key,
        "base_url": "/whatsapp/panel",
        "advisor_names": advisor_names
    })

# ============================================================================
# Funciones de background
# ============================================================================

async def _log_advisor_message_to_hubspot(
    contact_id: str,
    message: str,
    phone: str,
    message_source: str,
    mongo_message_id: Optional[str] = None
) -> None:
    """
    Registra un mensaje del asesor en HubSpot Timeline.

    Esta función corre en background y NO bloquea la respuesta al panel.
    El mensaje ya está disponible en MongoDB para el usuario.
    """
    try:
        timeline_logger = get_timeline_logger()

        # Agregar source al mensaje para el registro
        content_with_source = f"{message}\n\n[Fuente: {message_source}]"

        await timeline_logger.log_advisor_message(
            contact_id=contact_id,
            content=content_with_source,
            session_id=phone
        )

        logger.info(f"[Panel] Mensaje del asesor registrado en Timeline: {contact_id}")

        # Marcar mensaje como sincronizado en MongoDB
        if mongo_message_id:
            try:
                mongo_manager = get_mongo_manager()
                await mongo_manager.mark_as_synced_to_hubspot(mongo_message_id)
                logger.debug(f"[Panel] MongoDB mensaje {mongo_message_id} marcado como sincronizado")
            except Exception as sync_error:
                logger.warning(f"[Panel] Error marcando sincronización: {sync_error}")

    except Exception as e:
        logger.error(f"[Panel] Error registrando en HubSpot: {e}")


async def _update_advisor_timestamp(phone_normalized: str, canal: Optional[str] = None) -> None:
    """
    Actualiza timestamp del mensaje del asesor en ConversationMeta.
    Usado para calcular TTL de 72h si asesor deja de responder.
    """
    try:
        # Redis URL unificado (Railway interna, local pública)
        is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
        redis_url = os.getenv("REDIS_URL") if is_railway else (
            os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
        )
        state_manager = ConversationStateManager(redis_url)
        await state_manager.update_advisor_message_timestamp(phone_normalized, canal)
        logger.info(f"[Panel] ✓ Timestamp asesor actualizado: {phone_normalized}:{canal or 'default'}")
    except Exception as e:
        logger.error(f"[Panel] Error actualizando timestamp asesor: {e}")


# ============================================================================
# Dashboard de Métricas para Analista de Redes Sociales (READ-ONLY)
# ============================================================================

# Canales de redes sociales para métricas
SOCIAL_MEDIA_CHANNELS = ["facebook", "instagram", "linkedin", "youtube", "tiktok"]


# ============================================================================
# Funciones de sanitización para exportación Excel
# ============================================================================

# Patrón para eliminar emojis (compilado una vez para eficiencia)
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # símbolos & pictogramas
    "\U0001F680-\U0001F6FF"  # transporte & mapa
    "\U0001F1E0-\U0001F1FF"  # banderas
    "\U00002702-\U000027B0"  # dingbats
    "\U0001F900-\U0001F9FF"  # suplementarios
    "\U00002600-\U000026FF"  # misc symbols
    "\U0001FA00-\U0001FA6F"  # chess symbols
    "\U0001FA70-\U0001FAFF"  # symbols extended
    "\U00002300-\U000023FF"  # misc technical
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0001F000-\U0001F02F"  # mahjong
    "]+",
    flags=re.UNICODE
)

# Tags de HubSpot y otros sistemas CRM
HUBSPOT_TAGS_PATTERN = re.compile(
    r'(\{\{[^}]+\}\})|'           # {{contact.property}}
    r'(\{%[^%]+%\})|'             # {% if condition %}
    r'(\[\[[^\]]+\]\])|'          # [[merge_field]]
    r'(hs-[a-zA-Z0-9_-]+)|'       # hs-cta-wrapper, hs-menu, etc.
    r'(hubspot[_\-]?[a-zA-Z0-9]*)|'  # hubspot_*, hubspot-*
    r'(__hs[a-zA-Z0-9_]+)|'       # __hsFormSelectors
    r'(data-hs[a-zA-Z0-9\-_="\']+)|'  # data-hs-*
    r'(mkt-[a-zA-Z0-9_-]+)',      # mkt-*
    flags=re.IGNORECASE
)


def sanitize_text(text: str) -> str:
    """
    Sanitización profunda: elimina HTML, emojis, tags de HubSpot y caracteres especiales.
    """
    if not text or not isinstance(text, str):
        return ""

    # Convertir a string si no lo es
    text = str(text)

    # 1. Decodificar HTML entities (&amp; → &, &nbsp; → espacio)
    text = html.unescape(text)

    # 2. Eliminar etiquetas HTML completas
    text = re.sub(r'<[^>]+>', ' ', text)

    # 3. Eliminar tags de HubSpot y CRM
    text = HUBSPOT_TAGS_PATTERN.sub('', text)

    # 4. Eliminar URLs
    text = re.sub(r'https?://[^\s<>"{}|\\^`\[\]]+', '', text)
    text = re.sub(r'www\.[^\s<>"{}|\\^`\[\]]+', '', text)

    # 5. Eliminar emojis
    text = EMOJI_PATTERN.sub('', text)

    # 6. Eliminar caracteres de control y no imprimibles
    text = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', text)

    # 7. Normalizar guiones y caracteres especiales
    text = re.sub(r'[–—]', '-', text)  # Guiones largos a normal
    text = re.sub(r'[""''„‚]', '"', text)  # Comillas tipográficas
    text = re.sub(r'[•●○◦▪▫]', '-', text)  # Bullets a guión

    # 8. Limpiar espacios múltiples y trim
    text = re.sub(r'\s+', ' ', text).strip()

    return text


def sanitize_name(firstname: str, lastname: str) -> str:
    """
    Sanitiza y combina nombre y apellido.
    Elimina prefijos de HubSpot, emojis y caracteres extraños.
    """
    first = sanitize_text(firstname or "")
    last = sanitize_text(lastname or "")

    # Combinar y limpiar
    full_name = f"{first} {last}".strip()

    # Si está vacío o solo tiene caracteres especiales
    if not full_name or len(full_name) < 2:
        return "Sin nombre"

    # Capitalizar cada palabra
    return ' '.join(word.capitalize() for word in full_name.split())


def format_phone_excel(phone: str) -> str:
    """
    Normaliza número de teléfono para Excel.
    Mantiene solo dígitos y el símbolo +.
    """
    if not phone:
        return "Sin teléfono"

    # Convertir a string y limpiar
    phone_str = str(phone).strip()

    # Eliminar todo excepto números y +
    cleaned = re.sub(r'[^\d+]', '', phone_str)

    # Validar que tenga al menos 7 dígitos
    digits_only = re.sub(r'\D', '', cleaned)
    if len(digits_only) < 7:
        return "Sin teléfono"

    return cleaned


def format_date_excel(iso_date: str) -> str:
    """
    Convierte fecha ISO a DD/MM/YYYY HH:mm.
    Maneja múltiples formatos de entrada.
    """
    if not iso_date:
        return ""

    date_str = str(iso_date).strip()

    # Intentar parsear como ISO
    try:
        # Manejar formato con Z o sin timezone
        if 'Z' in date_str:
            date_str = date_str.replace("Z", "+00:00")

        dt = datetime.fromisoformat(date_str)
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        pass

    # Intentar extraer solo la fecha si falla
    try:
        # Buscar patrón YYYY-MM-DD
        match = re.search(r'(\d{4}-\d{2}-\d{2})', date_str)
        if match:
            dt = datetime.strptime(match.group(1), "%Y-%m-%d")
            return dt.strftime("%d/%m/%Y")
    except Exception:
        pass

    # Retornar los primeros 10 caracteres como fallback
    return date_str[:10] if len(date_str) >= 10 else date_str


def format_status_excel(status: str) -> str:
    """
    Formatea el status/lifecyclestage de HubSpot a texto legible.
    """
    if not status:
        return "Lead"

    status_clean = sanitize_text(str(status).lower())

    # Mapeo de status de HubSpot a español
    status_map = {
        'subscriber': 'Suscriptor',
        'lead': 'Lead',
        'marketingqualifiedlead': 'MQL',
        'salesqualifiedlead': 'SQL',
        'opportunity': 'Oportunidad',
        'customer': 'Cliente',
        'evangelist': 'Evangelista',
        'other': 'Otro',
        'new': 'Nuevo',
        'open': 'Abierto',
        'in_progress': 'En Proceso',
        'closed': 'Cerrado',
    }

    return status_map.get(status_clean, status_clean.capitalize())


@router.get("/metrics")
async def get_social_media_metrics(
    days: int = Query(7, ge=1, le=30, description="Días a analizar"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Retorna métricas de leads de redes sociales.

    Este endpoint es para el analista de redes sociales que solo necesita
    ver estadísticas, no enviar mensajes ni ver conversaciones detalladas.
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        import httpx
        from collections import defaultdict

        hubspot_api_key = os.getenv("HUBSPOT_API_KEY")
        if not hubspot_api_key:
            raise HTTPException(status_code=500, detail="HUBSPOT_API_KEY no configurada")

        # Calcular rango de fechas
        from zoneinfo import ZoneInfo
        TIMEZONE = ZoneInfo("America/Bogota")
        now = datetime.now(TIMEZONE)
        since = now - timedelta(days=days)
        since_ms = int(since.timestamp() * 1000)

        # Buscar contactos de redes sociales en HubSpot
        url = "https://api.hubapi.com/crm/v3/objects/contacts/search"
        payload = {
            "filterGroups": [{
                "filters": [
                    {
                        "propertyName": "canal_origen",
                        "operator": "IN",
                        "values": SOCIAL_MEDIA_CHANNELS
                    },
                    {
                        "propertyName": "createdate",
                        "operator": "GTE",
                        "value": since_ms
                    }
                ]
            }],
            "properties": [
                "createdate",
                "canal_origen",
                "firstname",
                "lastname",
                "phone",
                "chatbot_score",
                "lifecyclestage",
                "hs_lead_status",       # Motivo/status del lead
                "message",              # Mensaje inicial (si existe)
                "notes_last_updated",   # Notas recientes
            ],
            "limit": 100,
            "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}]
        }

        contacts = []
        after = None
        async with httpx.AsyncClient() as client:
            while True:
                page_payload = dict(payload)
                if after:
                    page_payload["after"] = after

                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {hubspot_api_key}"},
                    json=page_payload,
                    timeout=15.0
                )

                if response.status_code != 200:
                    logger.error(f"[Metrics] HubSpot error: {response.status_code} - {response.text}")
                    raise HTTPException(
                        status_code=503,
                        detail=f"Error consultando HubSpot: {response.status_code}. Intenta de nuevo en unos minutos."
                    )

                data = response.json()
                contacts.extend(data.get("results", []))

                after = data.get("paging", {}).get("next", {}).get("after")
                if not after:
                    break

        logger.info(f"[Metrics] Total contactos obtenidos de HubSpot: {len(contacts)}")

        # Procesar métricas
        leads_by_channel = defaultdict(int)
        leads_by_day = defaultdict(int)
        contacts_by_channel = defaultdict(list)  # Lista de contactos por canal
        total_leads = len(contacts)

        for contact in contacts:
            props = contact.get("properties", {})

            # Por canal - sanitizado
            canal_raw = props.get("canal_origen", "desconocido")
            canal = sanitize_text(canal_raw).lower() or "desconocido"
            leads_by_channel[canal] += 1

            # Extraer y sanitizar nombre
            firstname = props.get("firstname", "")
            lastname = props.get("lastname", "")
            nombre_completo = sanitize_name(firstname, lastname)

            # Extraer y formatear teléfono
            phone = format_phone_excel(props.get("phone", ""))

            # Extraer fecha y formatear
            fecha_raw = props.get("createdate", "")

            # Extraer motivo (combinar hs_lead_status con message si existe)
            motivo_parts = []
            hs_lead_status = props.get("hs_lead_status", "")
            if hs_lead_status:
                motivo_parts.append(sanitize_text(hs_lead_status))
            message = props.get("message", "")
            if message:
                # Truncar mensaje a 100 caracteres
                msg_clean = sanitize_text(message)[:100]
                if msg_clean:
                    motivo_parts.append(msg_clean)

            motivo = " - ".join(motivo_parts) if motivo_parts else "Consulta general"

            # Extraer status y formatear
            status = format_status_excel(props.get("lifecyclestage", "lead"))

            # Score
            score_raw = props.get("chatbot_score", "")
            score = sanitize_text(str(score_raw)) if score_raw else "-"

            # Agregar a la lista de contactos por canal
            # LLAVES CONSISTENTES para toda la cadena
            contacts_by_channel[canal].append({
                "fecha": fecha_raw,                 # Se formatea en Excel
                "canal": canal.capitalize(),        # Canal ya sanitizado
                "nombre": nombre_completo,          # Ya sanitizado
                "telefono": phone,                  # Ya formateado
                "motivo": motivo,                   # Nuevo campo
                "status": status,                   # Ya formateado
                "score": score,                     # Sanitizado
            })

            # Por día
            createdate = props.get("createdate")
            if createdate:
                try:
                    dt = datetime.fromisoformat(createdate.replace("Z", "+00:00"))
                    day_key = dt.strftime("%Y-%m-%d")
                    leads_by_day[day_key] += 1
                except Exception:
                    pass

        # Ordenar leads por día
        leads_by_day_sorted = dict(sorted(leads_by_day.items()))

        # Log para debug - verificar datos extraídos
        logger.info(f"[Metrics] Total leads encontrados: {total_leads}")
        logger.info(f"[Metrics] Leads por canal: {dict(leads_by_channel)}")
        for canal, contactos in contacts_by_channel.items():
            logger.info(f"[Metrics] Canal '{canal}': {len(contactos)} contactos")
            if contactos:
                # Mostrar primer contacto como ejemplo
                ejemplo = contactos[0]
                logger.info(f"[Metrics] Ejemplo contacto: nombre='{ejemplo.get('nombre')}', tel='{ejemplo.get('telefono')}'")

        return {
            "period_days": days,
            "since": since.isoformat(),
            "until": now.isoformat(),
            "total_leads": total_leads,
            "leads_by_channel": dict(leads_by_channel),
            "leads_by_day": leads_by_day_sorted,
            "contacts_by_channel": dict(contacts_by_channel),  # Lista de contactos
            "channels_tracked": SOCIAL_MEDIA_CHANNELS
        }

    except Exception as e:
        logger.error(f"[Metrics] Error obteniendo métricas: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/metrics/export")
async def export_metrics_csv(
    days: int = Query(7, ge=1, le=30, description="Días a analizar"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Exporta métricas de redes sociales a formato CSV.

    Genera un archivo CSV descargable con:
    - Resumen por canal de origen
    - Leads por día
    """
    from fastapi.responses import Response
    from io import StringIO
    import csv

    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # Obtener datos de métricas
    metrics_data = await get_social_media_metrics(days=days, x_api_key=x_api_key)

    # Crear CSV en memoria
    output = StringIO()
    writer = csv.writer(output)

    # Sección: Resumen
    writer.writerow(["=== MÉTRICAS DE REDES SOCIALES ==="])
    writer.writerow([f"Periodo: últimos {days} días"])
    writer.writerow([f"Desde: {metrics_data['since'][:10]}"])
    writer.writerow([f"Hasta: {metrics_data['until'][:10]}"])
    writer.writerow([f"Total leads: {metrics_data['total_leads']}"])
    writer.writerow([])

    # Sección: Por canal
    writer.writerow(["=== LEADS POR CANAL ==="])
    writer.writerow(["Canal", "Cantidad", "Porcentaje"])

    total = metrics_data["total_leads"]
    for canal, count in sorted(metrics_data["leads_by_channel"].items(), key=lambda x: -x[1]):
        pct = (count / total * 100) if total > 0 else 0
        writer.writerow([canal, count, f"{pct:.1f}%"])

    writer.writerow([])

    # Sección: Por día
    writer.writerow(["=== LEADS POR DÍA ==="])
    writer.writerow(["Fecha", "Cantidad"])

    for day, count in metrics_data["leads_by_day"].items():
        writer.writerow([day, count])

    writer.writerow([])

    # Sección: Contactos por canal (con todas las columnas)
    writer.writerow(["=== DETALLE DE CONTACTOS POR CANAL ==="])
    contacts_by_channel = metrics_data.get("contacts_by_channel", {})

    for canal in sorted(contacts_by_channel.keys()):
        contactos = contacts_by_channel[canal]
        writer.writerow([])
        writer.writerow([f"--- {canal.upper()} ({len(contactos)} leads) ---"])
        writer.writerow(["Fecha", "Canal", "Nombre", "Teléfono", "Motivo", "Status"])

        for contacto in contactos:
            writer.writerow([
                format_date_excel(contacto.get("fecha", "")),
                contacto.get("canal", canal.capitalize()),
                contacto.get("nombre", "Sin nombre"),
                contacto.get("telefono", "Sin teléfono"),
                contacto.get("motivo", "Consulta general"),
                contacto.get("status", "Lead"),
            ])

    # Generar nombre de archivo
    from datetime import datetime
    filename = f"metricas_redes_{days}d_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@router.get("/metrics/export-excel")
async def export_metrics_excel(
    days: int = Query(7, ge=1, le=30, description="Días a analizar"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Exporta métricas de redes sociales a formato Excel profesional.

    Genera un archivo .xlsx con:
    - Hoja "Resumen": Métricas agregadas por canal
    - Hoja "Contactos": Detalle de todos los leads con formato profesional
    - Hojas por canal: Si hay >5 contactos por canal
    """
    from fastapi.responses import Response
    import pandas as pd

    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        # Obtener datos de métricas
        metrics_data = await get_social_media_metrics(days=days, x_api_key=x_api_key)

        # Crear buffer en memoria para el archivo Excel
        output = BytesIO()

        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            workbook = writer.book

            # ========== FORMATOS ==========
            header_format = workbook.add_format({
                'bold': True,
                'font_color': 'white',
                'bg_color': '#1F4E79',  # Azul marino
                'border': 1,
                'align': 'center',
                'valign': 'vcenter',
                'text_wrap': True
            })

            title_format = workbook.add_format({
                'bold': True,
                'font_size': 14,
                'font_color': 'white',
                'bg_color': '#1F4E79',
                'align': 'center',
                'valign': 'vcenter',
            })

            # ========== HOJA 1: RESUMEN GENERAL ==========
            summary_data = {
                'Métrica': [
                    'Período Analizado',
                    'Total Leads',
                    'Instagram',
                    'Facebook',
                    'TikTok',
                    'LinkedIn',
                    'YouTube'
                ],
                'Valor': [
                    f"Últimos {days} días",
                    metrics_data['total_leads'],
                    metrics_data['leads_by_channel'].get('instagram', 0),
                    metrics_data['leads_by_channel'].get('facebook', 0),
                    metrics_data['leads_by_channel'].get('tiktok', 0),
                    metrics_data['leads_by_channel'].get('linkedin', 0),
                    metrics_data['leads_by_channel'].get('youtube', 0),
                ]
            }
            df_summary = pd.DataFrame(summary_data)
            df_summary.to_excel(writer, sheet_name='Resumen', index=False, startrow=1)

            ws_summary = writer.sheets['Resumen']
            ws_summary.merge_range('A1:B1', 'RESUMEN DE MÉTRICAS - REDES SOCIALES', title_format)
            ws_summary.set_column('A:A', 25)
            ws_summary.set_column('B:B', 20)
            ws_summary.freeze_panes(2, 0)

            # Aplicar formato a encabezados de resumen
            for col_num, col_name in enumerate(df_summary.columns):
                ws_summary.write(1, col_num, col_name, header_format)

            # ========== HOJA 2: DETALLE DE CONTACTOS ==========
            all_contacts = []
            contacts_by_channel = metrics_data.get('contacts_by_channel', {})

            for canal, contactos in contacts_by_channel.items():
                for c in contactos:
                    # Los datos ya vienen sanitizados desde get_social_media_metrics
                    all_contacts.append({
                        'Fecha Registro': format_date_excel(c.get('fecha', '')),
                        'Canal': c.get('canal', canal.capitalize()),
                        'Nombre': c.get('nombre', 'Sin nombre'),
                        'Teléfono': c.get('telefono', 'Sin teléfono'),
                        'Motivo': c.get('motivo', 'Consulta general'),
                        'Status': c.get('status', 'Lead'),
                        'Score': c.get('score', '-'),
                    })

            if all_contacts:
                df_contacts = pd.DataFrame(all_contacts)

                # Ordenar columnas según requerimiento
                cols_order = ['Fecha Registro', 'Canal', 'Nombre', 'Teléfono', 'Motivo', 'Status', 'Score']
                df_contacts = df_contacts.reindex(columns=cols_order)

                df_contacts.to_excel(writer, sheet_name='Contactos', index=False, startrow=0)

                ws_contacts = writer.sheets['Contactos']

                # Aplicar formato a encabezados
                for col_num, col_name in enumerate(df_contacts.columns):
                    ws_contacts.write(0, col_num, col_name, header_format)

                # Auto-ajustar columnas
                for col_num, col_name in enumerate(df_contacts.columns):
                    try:
                        max_len = max(
                            df_contacts[col_name].astype(str).map(len).max(),
                            len(col_name)
                        ) + 2
                        ws_contacts.set_column(col_num, col_num, min(max_len, 40))
                    except Exception:
                        ws_contacts.set_column(col_num, col_num, 15)

                # Freeze pane y auto-filter
                ws_contacts.freeze_panes(1, 0)
                ws_contacts.autofilter(0, 0, len(df_contacts), len(df_contacts.columns) - 1)

            # ========== HOJAS POR CANAL (si >5 contactos) ==========
            for canal, contactos in contacts_by_channel.items():
                if len(contactos) > 5:
                    canal_data = []
                    for c in contactos:
                        # Datos ya sanitizados desde get_social_media_metrics
                        canal_data.append({
                            'Fecha': format_date_excel(c.get('fecha', '')),
                            'Nombre': c.get('nombre', 'Sin nombre'),
                            'Teléfono': c.get('telefono', 'Sin teléfono'),
                            'Motivo': c.get('motivo', 'Consulta general'),
                            'Status': c.get('status', 'Lead'),
                        })

                    df_canal = pd.DataFrame(canal_data)
                    sheet_name = canal.capitalize()[:31]  # Excel limita a 31 chars
                    df_canal.to_excel(writer, sheet_name=sheet_name, index=False)

                    ws_canal = writer.sheets[sheet_name]
                    for col_num, col_name in enumerate(df_canal.columns):
                        ws_canal.write(0, col_num, col_name, header_format)
                        ws_canal.set_column(col_num, col_num, 20)
                    ws_canal.freeze_panes(1, 0)
                    ws_canal.autofilter(0, 0, len(df_canal), len(df_canal.columns) - 1)

        # Preparar respuesta
        output.seek(0)
        filename = f"metricas_redes_{days}d_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"

        return Response(
            content=output.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Metrics] Error exportando Excel: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error generando Excel: {str(e)}")


@router.get("/metrics/", response_class=HTMLResponse)
async def metrics_dashboard_ui(request: Request, x_api_key: str = Query(None, alias="key")):
    """
    Dashboard de metricas para analista de redes sociales.

    Acceso: /whatsapp/panel/metrics/?key=TU_API_KEY
    """
    # Validar API Key via query param para acceso web
    if not _validate_api_key(x_api_key):
        return HTMLResponse(
            content="""
            <!DOCTYPE html>`x
            <html>
            <head><title>Acceso Denegado</title></head>
            <body style="font-family: Arial; padding: 50px; text-align: center;">
                <h1>Acceso Denegado</h1>
                <p>Se requiere API Key valida.</p>
                <p>Uso: /whatsapp/panel/metrics/?key=TU_API_KEY</p>
            </body>
            </html>
            """,
            status_code=401
        )

    return templates.TemplateResponse("metrics.html", {
        "request": request,
        "api_key": x_api_key,
        "base_url": "/whatsapp/panel"
    })


# ============================================================================
# WebSocket para notificaciones en tiempo real
# ============================================================================

@router.websocket("/ws/{advisor_id}")
async def websocket_endpoint(websocket: WebSocket, advisor_id: str):
    """
    Endpoint WebSocket para notificaciones en tiempo real.

    Conexión:
        ws://host/whatsapp/panel/ws/{advisor_id}
    """
    await ws_manager.connect(websocket, advisor_id)

    # Enviar transfer_requests pendientes donde este asesor es el propietario
    try:
        _rc_ws = await _get_redis_client()
        pending_keys = await _rc_ws.keys("transfer_req:*")
        for pk in pending_keys:
            raw_req = await _rc_ws.get(pk)
            if raw_req:
                req = json.loads(raw_req)
                if req.get("owner_id") == advisor_id:
                    requester_name = _get_advisor_name(req.get("requester_id", ""))
                    await websocket.send_json({
                        "type": "transfer_request",
                        "contact_id": req.get("contact_id"),
                        "phone": req.get("phone"),
                        "contact_name": req.get("contact_name", req.get("phone")),
                        "requester_id": req.get("requester_id"),
                        "requester_name": requester_name,
                        "message": f"{requester_name} quiere atender este contacto (solicitud pendiente)"
                    })
    except Exception:
        pass

    # Keepalive: si no llega ningún mensaje del cliente en 20s el servidor envía
    # un ping para que Railway no cierre la conexión TCP por inactividad.
    WS_KEEPALIVE_INTERVAL = 20

    try:
        while True:
            try:
                data = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=WS_KEEPALIVE_INTERVAL
                )
                message = json.loads(data) if data else {}

                # Responder a pings
                if message.get("type") == "ping":
                    await websocket.send_json({
                        "type": "pong",
                        "timestamp": datetime.now().isoformat()
                    })

                # Comando para registrar teléfono activo
                elif message.get("type") == "watching":
                    phone = message.get("phone")
                    if phone:
                        ws_manager.register_phone_owner(phone, advisor_id)
                        logger.debug(f"[WebSocket] Asesor {advisor_id} observando {phone}")

            except asyncio.TimeoutError:
                # Sin mensajes del cliente → ping proactivo para mantener viva la conexión
                await websocket.send_json({
                    "type": "ping",
                    "timestamp": datetime.now().isoformat()
                })

            except json.JSONDecodeError:
                pass

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, advisor_id)
        logger.info(f"[WebSocket] Asesor {advisor_id} desconectado")

    except Exception as e:
        logger.error(f"[WebSocket] Error en conexión de {advisor_id}: {e}")
        ws_manager.disconnect(websocket, advisor_id)


@router.get("/ws/stats")
async def websocket_stats(x_api_key: str = Header(None, alias="X-API-Key")):
    """
    Retorna estadísticas de conexiones WebSocket activas.
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    return ws_manager.get_stats()


# ============================================================================
# ENDPOINT RECOVERY: Restaurar contactos desaparecidos desde HubSpot
# ============================================================================

@router.post("/admin/restore-panel")
async def restore_panel_from_hubspot(
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Recupera contactos cuyo meta Redis expiró.
    Busca contactos en HubSpot que tengan owner asignado + conversación de chatbot.
    Ejecutar una vez para restaurar contactos desaparecidos del panel.
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida o no configurada")

    try:
        # 1. Buscar contactos asignados que tuvieron interacción con el chatbot
        search_url = "https://api.hubapi.com/crm/v3/objects/contacts/search"
        payload = {
            "filterGroups": [{
                "filters": [
                    {"propertyName": "hubspot_owner_id", "operator": "HAS_PROPERTY"}
                ]
            }],
            "properties": [
                "phone", "firstname", "hubspot_owner_id",
                "canal_origen", "whatsapp_id"
            ],
            "sorts": [{"propertyName": "lastmodifieddate", "direction": "DESCENDING"}],
            "limit": 100
        }
        client = get_httpx_client()
        # Paginar hasta obtener todos los contactos (HubSpot devuelve máx 100 por página)
        contacts = []
        after_cursor = None
        MAX_CONTACTS = 500  # límite de seguridad para evitar loops infinitos
        while len(contacts) < MAX_CONTACTS:
            page_payload = {**payload}
            if after_cursor:
                page_payload["after"] = after_cursor
            response = await _hubspot_post(client, search_url, page_payload, HUBSPOT_API_KEY, max_retries=2)
            if response.status_code not in (200, 207):
                raise HTTPException(status_code=502,
                    detail=f"HubSpot devolvió {response.status_code}")
            data = response.json()
            contacts.extend(data.get("results", []))
            after_cursor = data.get("paging", {}).get("next", {}).get("after")
            if not after_cursor:
                break
        logger.info(f"[RestorePanel] HubSpot devolvió {len(contacts)} contactos candidatos (paginado)")

        if not contacts:
            return {
                "restored": 0, "already_active": 0,
                "message": "No se encontraron contactos asignados en HubSpot"
            }

        # 2. Restaurar metas Redis
        is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
        redis_url = os.getenv("REDIS_URL") if is_railway else (
            os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
        )
        state_manager = ConversationStateManager(redis_url)

        restored = 0
        already_active = 0
        skipped_no_phone = 0
        errors = []
        now_ts = datetime.now(timezone.utc).timestamp()
        now_iso = datetime.now(timezone.utc).isoformat() + "Z"

        for contact in contacts:
            try:
                props = contact.get("properties", {})
                # whatsapp_id es el identificador canónico; fallback a phone
                raw_phone = props.get("whatsapp_id") or props.get("phone", "")
                if not raw_phone:
                    skipped_no_phone += 1
                    continue
                validation = PhoneNormalizer().normalize(raw_phone)
                if not validation.is_valid:
                    skipped_no_phone += 1
                    continue
                phone_norm = validation.normalized

                canal_raw = props.get("canal_origen") or "whatsapp_directo"
                canal_safe = canal_raw.lower().replace(" ", "_").strip()
                meta_key = f"{state_manager.META_PREFIX}{phone_norm}:{canal_safe}"

                # Si ya existe meta activo: no sobreescribir meta, pero sí refrescar
                # el ZSET score a now_ts para que no quede enterrado más allá del
                # límite 100 de get_active_contacts() (zrevrange devuelve los más recientes).
                existing = await state_manager.redis.get(meta_key)
                if existing:
                    zset_member = f"{phone_norm}:{canal_safe}"
                    state_key = f"conv_state:{phone_norm}:{canal_safe}"
                    pipe = state_manager.redis.pipeline(transaction=False)
                    # Refrescar ZSET score
                    pipe.zadd(state_manager.ACTIVE_CONTACTS_ZSET, {zset_member: now_ts})
                    # Recrear conv_state con TTL 365d (puede haber expirado si fue
                    # creado antes del fix de TTL permanente)
                    pipe.set(state_key, ConversationStatus.HUMAN_ACTIVE.value,
                             ex=state_manager.PANEL_TTL_SECONDS)
                    # Refrescar TTL del meta también
                    pipe.expire(meta_key, state_manager.PANEL_TTL_SECONDS)
                    await pipe.execute()
                    already_active += 1
                    continue

                # Crear meta restaurado con TTL permanente
                meta = {
                    "phone_normalized": phone_norm,
                    "contact_id": contact.get("id"),
                    "status": ConversationStatus.HUMAN_ACTIVE.value,
                    "last_activity": now_iso,
                    "canal_origen": canal_safe,
                    "display_name": props.get("firstname", ""),
                    "assigned_owner_id": props.get("hubspot_owner_id"),
                    "in_panel": True,
                    "restored": True,
                }
                # state_key es CRÍTICO: get_active_contacts() lo lee primero para el status.
                # Sin él → fuerza BOT_ACTIVE → contacto queda excluido del panel.
                state_key = f"conv_state:{phone_norm}:{canal_safe}"
                pipe = state_manager.redis.pipeline(transaction=False)
                pipe.set(meta_key, json.dumps(meta), ex=state_manager.PANEL_TTL_SECONDS)
                pipe.set(state_key, ConversationStatus.HUMAN_ACTIVE.value, ex=state_manager.PANEL_TTL_SECONDS)
                pipe.zadd(state_manager.ACTIVE_CONTACTS_ZSET, {f"{phone_norm}:{canal_safe}": now_ts})
                await pipe.execute()
                restored += 1
                logger.info(f"[RestorePanel] Contacto restaurado: {phone_norm} ({canal_safe})")

            except Exception as ce:
                errors.append(str(ce))

        return {
            "restored": restored,
            "already_active": already_active,
            "skipped_no_phone": skipped_no_phone,
            "total_from_hubspot": len(contacts),
            "errors": errors[:10]
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[RestorePanel] Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
