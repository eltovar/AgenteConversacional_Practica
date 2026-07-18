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
import hashlib
import httpx
from io import BytesIO
from typing import Optional, Dict, Any, Tuple, List
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
from .conversation_state import ConversationStateManager, ConversationStatus, get_bogota_now, get_bogota_now_iso
from .contact_manager import ContactManager
from .websocket_manager import ws_manager
from .templates.templates import DEFAULT_TEMPLATES  # Templates predefinidos
from utils.twilio_client import twilio_client
from integrations.hubspot import get_timeline_logger, hubspot_client as _hs_singleton
from slowapi import Limiter
from slowapi.util import get_remote_address
limiter = Limiter(key_func=get_remote_address)
from database.mongodb_client import get_mongo_manager
from utils.media_processor import media_processor, DOCUMENT_MIME_TYPES, MAX_DOCUMENT_SIZE_BYTES, MAX_VIDEO_SIZE_BYTES
from utils.reply_quote_formatter import inject_quote, reply_audio_intro


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
    reply_to_id: Optional[str] = Field(None, description="ID del mensaje citado")
    reply_to_preview: Optional[dict] = Field(None, description="Preview del mensaje citado")

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
HUBSPOT_STAGE_EN_CONVERSACION = "1326623075"  # Stage unificado (entrada de todos los leads)
HUBSPOT_STAGE_VISITA_AGENDADA = "marketingqualifiedlead"
HUBSPOT_STAGE_VISITA_REALIZADA = "salesqualifiedlead"
# Stages comerciales que NO se deben sobreescribir (progreso manual de asesora)
PROTECTED_STAGES_POST_VISITA = {
    "salesqualifiedlead", "opportunity", "customer", "evangelist"
}

# ============================================================================
# Bulk Campaigns (mensajes masivos) — constantes
# ============================================================================
BULK_EXCLUDED_STAGES = {
    "1326623075",  # En Conversación (stage unificado)
    "evangelist",  # Cerrado perdido
    "1326632628",  # Ana Contratos
    "1326632209",  # Pagos y Servicios Publicos
}
BULK_PROCESSOR_LOCK_KEY = "bulk_processor_lock"
BULK_PROCESSOR_LOCK_TTL = 30
BULK_BATCH_SIZE = 5
BULK_SEND_SPACING_SEC = 0.5
BULK_MAX_CONCURRENT = 2
BULK_MESSAGE_DEDUP_TTL = 86400 * 7
HUBSPOT_BULK_SEARCH_CACHE_TTL = 1800
BULK_NO_RESPONDE_FLAG_PREFIX = "bulk_no_responde_pending:"
BULK_NO_RESPONDE_FLAG_TTL = 86400 * 30
BULK_SEND_DEDUP_PREFIX = "bulk_send_dedup:"
HUBSPOT_BULK_SEARCH_CACHE_PREFIX = "hubspot_bulk_search:"

# Plantillas permitidas para envío masivo. Mapeo SID → metadata.
# La variable key="1" (nombre) SIEMPRE se auto-fill desde HubSpot firstname per-contacto.
# Las demás variables se reciben del frontend como texto literal (aplicado a todos).
BULK_ALLOWED_TEMPLATES = {
    "HX550a2475d09a5fb3b5410e6d36eadf3f": {
        "name": "aun_en_busqueda",
        "label": "¿Aún estás interesado?",
        "vars": [{"key": "1", "label": "Nombre del contacto", "auto_fill": "firstname"}],
    },
    "HX287a1c005459a6ceaec90a35108330c3": {
        "name": "seguimiento_personalizado",
        "label": "Seguimiento Personalizado",
        "vars": [
            {"key": "1", "label": "Nombre del contacto", "auto_fill": "firstname"},
            {"key": "2", "label": "Mensaje personalizado"},
        ],
    },
}

# Singleton de connection pool Redis (evita crear nueva conexión por request)
_redis_pool: Optional[redis.Redis] = None

# Singleton de cliente HTTP con connection pooling (evita crear conexión por request)
_httpx_client: Optional[httpx.AsyncClient] = None

# Singleton de ContactManager (evita crear Redis pool + HubSpotClient por request)
_contact_manager_singleton: Optional[ContactManager] = None

def _get_contact_manager() -> ContactManager:
    """Singleton de ContactManager — 1 pool Redis + 1 HubSpotClient para todo el proceso."""
    global _contact_manager_singleton
    if _contact_manager_singleton is None:
        _contact_manager_singleton = ContactManager(hubspot_client=_hs_singleton)
        logger.info("[Panel] ContactManager singleton inicializado")
    return _contact_manager_singleton

# ============================================================================
# Caché de lifecyclestage del Contact en Redis (compartida entre workers Railway)
# ============================================================================
CONTACT_STAGE_CACHE_TTL = 3600  # 1 hora
CONTACT_NAME_CACHE_TTL = 14400   # 4 horas — invalidación explícita al editar nombre (ver PATCH /contacts/{id})
CONTACT_NAME_CACHE_PREFIX = "contact_name:"

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
    "1326623075": "En conversación",
    "marketingqualifiedlead": "Visita agendada",
    "salesqualifiedlead": "Visita realizada",
    "opportunity": "En estudio",
    "customer": "Cerrado ganado",
    "evangelist": "Cerrado perdido",
    "other": "No responde",
    "1326623067": "Hasta 1.5M",
    "1326631573": "Hasta 2M",
    "1326632625": "Hasta 2.5M",
    "1326631574": "De 3M en adelante",
    "1326623539": "Local o Bodega",
    "1326632628": "Ana Contratos",
    "1326623069": "Propietarios",
    "1326632209": "Pagos y Servicios Publicos",
    "1326623541": "Ya encontro",
    "subscriber": "Reubicados",
    "lead": "Aprobado",
    "1353539189": "Venta"
}

# Lista ordenada de etapas para el frontend
PIPELINE_STAGES_LIST = [
    {"id": "1326623075", "name": "En conversación"},
    {"id": "marketingqualifiedlead", "name": "Visita agendada"},
    {"id": "salesqualifiedlead", "name": "Visita realizada"},
    {"id": "opportunity", "name": "En estudio"},
    {"id": "customer", "name": "Cerrado ganado"},
    {"id": "evangelist", "name": "Cerrado perdido"},
    {"id": "other", "name": "No responde"},
    {"id": "1326623067", "name": "Hasta 1.5M"},
    {"id": "1326631573", "name": "Hasta 2M"},
    {"id": "1326632625", "name": "Hasta 2.5M"},
    {"id": "1326631574", "name": "De 3M en adelante"},
    {"id": "1326623539", "name": "Local o Bodega"},
    {"id": "1326632628", "name": "Ana Contratos"},
    {"id": "1326623069", "name": "Propietarios"},
    {"id": "1326632209", "name": "Pagos y Servicios Publicos"},
    {"id": "1326623541", "name": "Ya encontro"},
    {"id": "subscriber", "name": "Reubicados"},
    {"id": "lead", "name": "Aprobado"},
    {"id": "1353539189", "name": "Venta"}
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


async def _get_contact_lifecyclestage(contact_id: str) -> str:
    """
    Lee la propiedad lifecyclestage del Contact en HubSpot.
    Usa caché en Redis (1h) para reducir llamadas a la API.
    """
    if not contact_id or not HUBSPOT_API_KEY:
        return HUBSPOT_STAGE_EN_CONVERSACION

    cache_key = f"contact_stage:{contact_id}"
    try:
        redis_client = await _get_redis_client()
        cached = await redis_client.get(cache_key)
        if cached:
            return cached.decode()
    except Exception:
        pass

    try:
        url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}?properties=lifecyclestage"
        response = await _hubspot_get(None, url, HUBSPOT_API_KEY)
        if response.status_code == 200:
            stage = response.json().get("properties", {}).get("lifecyclestage") or HUBSPOT_STAGE_EN_CONVERSACION
            try:
                redis_client = await _get_redis_client()
                await redis_client.setex(cache_key, CONTACT_STAGE_CACHE_TTL, stage)
            except Exception:
                pass
            return stage
    except Exception as e:
        logger.debug(f"[Panel] Error leyendo lifecyclestage de contacto {contact_id}: {e}")

    return HUBSPOT_STAGE_EN_CONVERSACION


async def _invalidate_contact_stage_cache(contact_id: str) -> None:
    """Invalida el caché de lifecyclestage en Redis para un contacto específico."""
    try:
        redis_client = await _get_redis_client()
        await redis_client.delete(f"contact_stage:{contact_id}")
        logger.debug(f"[Panel] Caché de stage invalidado para {contact_id}")
    except Exception as e:
        logger.debug(f"[Panel] Error invalidando contact stage cache: {e}")


async def _cache_contact_stage(contact_id: str, stage: str) -> None:
    """Guarda lifecyclestage en Redis cache en background (sin bloquear el request)."""
    try:
        redis_client = await _get_redis_client()
        await redis_client.setex(f"contact_stage:{contact_id}", CONTACT_STAGE_CACHE_TTL, stage)
    except Exception:
        pass


async def _cache_contact_name(contact_id: str, firstname: str, lastname: str) -> None:
    """Guarda nombre del contacto en Redis cache (4h TTL). No bloquea el request."""
    if not contact_id:
        return
    try:
        r = await _get_redis_client()
        await r.setex(
            f"{CONTACT_NAME_CACHE_PREFIX}{contact_id}",
            CONTACT_NAME_CACHE_TTL,
            json.dumps({"firstname": firstname or "", "lastname": lastname or ""})
        )
    except Exception:
        pass


async def _get_cached_contact_name(contact_id: str) -> Optional[Dict[str, Any]]:
    """Lee nombre del contacto desde Redis cache. Retorna None si no está cacheado."""
    if not contact_id:
        return None
    try:
        r = await _get_redis_client()
        raw = await r.get(f"{CONTACT_NAME_CACHE_PREFIX}{contact_id}")
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return None


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
            max_connections=20,
        )
        logger.info("[Panel] Redis connection pool inicializado (max=20)")
    return _redis_pool


def _get_redis_url_str() -> str:
    """Retorna la URL de Redis según entorno (Railway vs local). Helper centralizado para ConversationStateManager."""
    is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
    return os.getenv("REDIS_URL") if is_railway else (
        os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
    )


# Singleton de ConversationStateManager (evita crear pool Redis por request)
_state_manager_singleton: Optional[ConversationStateManager] = None


def _get_state_manager() -> ConversationStateManager:
    """Singleton de ConversationStateManager — 1 pool Redis para todo el proceso."""
    global _state_manager_singleton
    if _state_manager_singleton is None:
        _state_manager_singleton = ConversationStateManager(_get_redis_url_str())
        logger.info("[Panel] ConversationStateManager singleton inicializado")
    return _state_manager_singleton


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

HUBSPOT_BATCH_CACHE_TTL = 600  # 10 minutos — batch key incluye md5 de IDs, reutilizable entre workers


async def _hubspot_batch_get_contacts(contact_ids: list[str]) -> Dict[str, Dict[str, Any]]:
    """
    Obtiene información de múltiples contactos en UNA sola llamada batch. lastname, email, phone
    """
    if not contact_ids or not HUBSPOT_API_KEY:
        return {}

    # HubSpot batch acepta máximo 100 IDs
    contact_ids = contact_ids[:100]

    # Cache key basada en los IDs ordenados (invariante al orden de la lista)
    cache_key = "hs_batch:" + hashlib.md5(",".join(sorted(contact_ids)).encode()).hexdigest()
    try:
        r = await _get_redis_client()
        cached_raw = await r.get(cache_key)
        if cached_raw:
            logger.debug(f"[Panel] Batch HubSpot cache HIT ({len(contact_ids)} contactos)")
            return json.loads(cached_raw)
    except Exception:
        pass

    url = "https://api.hubapi.com/crm/v3/objects/contacts/batch/read"
    payload = {
        "properties": ["firstname", "lastname", "email", "phone", "lifecyclestage", "hubspot_owner_id"],
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
                    "phone": props.get("phone"),
                    "lifecyclestage": props.get("lifecyclestage") or "",
                    "hubspot_owner_id": props.get("hubspot_owner_id") or "",
                }
            if response.status_code == 207:
                logger.info(f"[Panel] Batch HubSpot 207 (parcial): {len(results)}/{len(contact_ids)} contactos válidos")
            else:
                logger.info(f"[Panel] Batch HubSpot: obtenidos {len(results)}/{len(contact_ids)} contactos")
            # Guardar en Redis — compartido entre los 4 workers de Gunicorn
            try:
                r = await _get_redis_client()
                await r.setex(cache_key, HUBSPOT_BATCH_CACHE_TTL, json.dumps(results))
            except Exception:
                pass
            return results
        else:
            logger.warning(f"[Panel] Batch HubSpot falló: {response.status_code}")
            return {}

    except Exception as e:
        logger.error(f"[Panel] Error en batch HubSpot: {type(e).__name__}: {e}", exc_info=True)
        return {}


# ============================================================================
# Contact Hydration Service — pipeline unificada Redis → HubSpot → Mongo
# ============================================================================

async def _hydrate_contact(
    phone: str,
    canal_hint: Optional[str] = None,
    add_to_zset: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Materializa un contacto completo desde Redis + HubSpot + MongoDB.
    """
    # ── Paso A: Normalizar phone ────────────────────────────────────────────
    try:
        validation = PhoneNormalizer().normalize(phone)
    except Exception as e:
        logger.warning(f"[Hydrate] Error normalizando phone '{phone}': {e}")
        return None
    if not validation.is_valid:
        logger.debug(f"[Hydrate] Phone inválido '{phone}': {validation.error_message}")
        return None
    phone_norm = validation.normalized

    redis_client = await _get_redis_client()

    STATUS_PRIORITY = {
        ConversationStatus.IN_CONVERSATION.value: 4,
        ConversationStatus.HUMAN_ACTIVE.value: 3,
        ConversationStatus.PENDING_HANDOFF.value: 2,
        ConversationStatus.BOT_ACTIVE.value: 1,
    }

    # Flag para saber si Redis quedó disponible — gobierna el fallback HubSpot
    redis_ok = True

    # ── Paso B: ZSCAN para detectar canales en el panel ─────────────────────
    score_by_canal: Dict[str, float] = {}
    try:
        cursor = 0
        while True:
            cursor, results = await redis_client.zscan(
                ConversationStateManager.ACTIVE_CONTACTS_ZSET,
                cursor,
                match=f"{phone_norm}:*",
                count=20,
            )
            for raw_member, score in results:
                m = raw_member if isinstance(raw_member, str) else raw_member.decode()
                if ":" not in m:
                    continue
                _p, canal_part = m.split(":", 1)
                if _p != phone_norm:
                    continue  # defensive: zscan match debería filtrar, pero por si acaso
                score_by_canal[canal_part.lower()] = float(score) if score is not None else 0.0
            if cursor == 0:
                break
    except Exception as e:
        redis_ok = False
        logger.warning(f"[Hydrate] Error zscan ZSET para {phone_norm}: {e}")

    # Canales candidatos: los del ZSET + canal_hint + whatsapp (siempre probamos)
    probe_canals: list = []
    if canal_hint:
        ch = canal_hint.lower()
        if ch not in probe_canals:
            probe_canals.append(ch)
    for c in score_by_canal.keys():
        if c not in probe_canals:
            probe_canals.append(c)
    if "whatsapp" not in probe_canals:
        probe_canals.append("whatsapp")

    state_raws: Dict[str, Optional[str]] = {}
    meta_raws: Dict[str, Optional[str]] = {}
    markers: Dict[str, bool] = {}
    try:
        pipe = redis_client.pipeline(transaction=False)
        for c in probe_canals:
            pipe.get(f"{ConversationStateManager.STATE_PREFIX}{phone_norm}:{c}")
            pipe.get(f"{ConversationStateManager.META_PREFIX}{phone_norm}:{c}")
            pipe.exists(f"conv_was_panel:{phone_norm}:{c}")
        _results = await pipe.execute()
        for i, c in enumerate(probe_canals):
            state_raws[c] = _results[i * 3]
            meta_raws[c] = _results[i * 3 + 1]
            markers[c] = bool(_results[i * 3 + 2])
    except Exception as e:
        redis_ok = False
        logger.warning(f"[Hydrate] Error pipeline lectura state/meta para {phone_norm}: {e}")

    # Seleccionar el mejor canal: prioridad de status > score > bonus canal_hint
    best_canal: Optional[str] = None
    best_prio = -1
    best_score_eff: float = -1.0
    best_meta_dict: dict = {}
    best_status: Optional[str] = None

    for c in probe_canals:
        state_raw = state_raws.get(c)
        meta_raw = meta_raws.get(c)
        has_marker = markers.get(c, False)
        if not (state_raw or meta_raw or has_marker or c in score_by_canal):
            continue
        m_dict: dict = {}
        if meta_raw:
            try:
                m_dict = json.loads(meta_raw)
                if 'advisor' in m_dict and not m_dict.get('assigned_owner_id'):
                    m_dict['assigned_owner_id'] = m_dict['advisor']
                if 'owner_id' in m_dict and not m_dict.get('assigned_owner_id'):
                    m_dict['assigned_owner_id'] = m_dict['owner_id']
                if 'name' in m_dict and not m_dict.get('display_name'):
                    m_dict['display_name'] = m_dict['name']
            except Exception:
                m_dict = {}
        effective_status = state_raw or m_dict.get("status") or ConversationStatus.BOT_ACTIVE.value
        prio = STATUS_PRIORITY.get(effective_status, 0)
        score_here = score_by_canal.get(c, 0.0)
        bonus = 0.5 if (canal_hint and c == canal_hint.lower()) else 0.0
        score_eff = score_here + bonus
        if (prio > best_prio) or (prio == best_prio and score_eff > best_score_eff):
            best_canal = c
            best_prio = prio
            best_score_eff = score_eff
            best_meta_dict = m_dict
            best_status = effective_status

    if best_canal is None:
        # Sin traza en Redis. Seguiremos intentando HubSpot antes de retornar.
        best_canal = (canal_hint.lower() if canal_hint else "whatsapp")
        best_status = ConversationStatus.BOT_ACTIVE.value
        best_meta_dict = {}

    # ── Paso C: Resolver contact_id ─────────────────────────────────────────
    contact_id = best_meta_dict.get("contact_id")
    # Si Redis está saturado, evitar _search_contact (usa Redis cache internamente)
    # y saltar directo a HubSpot — reduce presión sobre el pool.
    if not contact_id and redis_ok:
        try:
            cm = _get_contact_manager()
            contact_id = await cm._search_contact(phone_norm)
        except Exception as e:
            logger.debug(f"[Hydrate] _search_contact falló para {phone_norm}: {e}")

    # ── Paso D: Enriquecer con HubSpot ──────────────────────────────────────
    display_name = (best_meta_dict.get("display_name") or "").strip()
    hs_owner_id: Optional[str] = None
    lifecyclestage_cached: Optional[str] = None

    # Si NO hay contact_id aún (Redis down o _search_contact falló),
    # consultar HubSpot directamente ANTES del batch — la API directa
    # no depende de nuestro Redis pool.
    if not contact_id and HUBSPOT_API_KEY:
        try:
            _hc = _hs_singleton
            hs = await _hc.search_contact_by_phone_with_properties(phone_norm)
            if hs:
                if hs.get("id"):
                    contact_id = hs.get("id")
                props = hs.get("properties") or {}
                fn = (props.get("firstname") or "").strip()
                ln = (props.get("lastname") or "").strip()
                full = (fn + " " + ln).strip()
                if full and not display_name:
                    display_name = full
                if not hs_owner_id and props.get("hubspot_owner_id"):
                    hs_owner_id = str(props.get("hubspot_owner_id"))
                if props.get("lifecyclestage"):
                    lifecyclestage_cached = props.get("lifecyclestage")
        except Exception as e:
            logger.debug(f"[Hydrate] HubSpot direct lookup falló para {phone_norm}: {e}")

    if contact_id:
        try:
            cached = await _get_cached_contact_name(contact_id)
            if cached:
                fn = (cached.get("firstname") or "").strip()
                ln = (cached.get("lastname") or "").strip()
                full = (fn + " " + ln).strip()
                if full and not display_name:
                    display_name = full
        except Exception:
            pass

        try:
            batch = await _hubspot_batch_get_contacts([contact_id])
            b = batch.get(contact_id) or {}
            fn = (b.get("firstname") or "").strip()
            ln = (b.get("lastname") or "").strip()
            full = (fn + " " + ln).strip()
            if full:
                display_name = full  # HubSpot es la autoridad del nombre
                try:
                    await _cache_contact_name(contact_id, fn, ln)
                except Exception:
                    pass
            if b.get("hubspot_owner_id"):
                hs_owner_id = str(b.get("hubspot_owner_id"))
            if b.get("lifecyclestage"):
                lifecyclestage_cached = b.get("lifecyclestage")
        except Exception as e:
            logger.debug(f"[Hydrate] batch HubSpot falló para {contact_id}: {e}")

    # Último recurso: si aún no hay nombre, intentar search_contact_by_phone_with_properties
    if not display_name and HUBSPOT_API_KEY:
        try:
            _hc = _hs_singleton
            hs = await _hc.search_contact_by_phone_with_properties(phone_norm)
            if hs:
                props = hs.get("properties") or {}
                fn = (props.get("firstname") or "").strip()
                ln = (props.get("lastname") or "").strip()
                full = (fn + " " + ln).strip()
                if full:
                    display_name = full
                if not contact_id and hs.get("id"):
                    contact_id = hs.get("id")
                if not hs_owner_id and props.get("hubspot_owner_id"):
                    hs_owner_id = str(props.get("hubspot_owner_id"))
        except Exception as e:
            logger.debug(f"[Hydrate] search_contact_by_phone_with_properties falló: {e}")

    # Fallback final: phone como display (NUNCA None ni vacío)
    if not display_name:
        display_name = phone_norm

    # ── Paso E: current_stage ───────────────────────────────────────────────
    current_stage = lifecyclestage_cached
    if contact_id and not current_stage:
        try:
            current_stage = await _get_contact_lifecyclestage(contact_id)
        except Exception:
            current_stage = None

    # ── Paso F: owner_id / owner_name ───────────────────────────────────────
    owner_id = best_meta_dict.get("assigned_owner_id") or hs_owner_id
    assigned_owner_ids = best_meta_dict.get("assigned_owner_ids") or []
    owner_name: Optional[str] = None
    if owner_id:
        try:
            owner_name = _get_advisor_name(str(owner_id))
        except Exception:
            owner_name = None

    # ── Paso G: has_appointment ─────────────────────────────────────────────
    has_appointment = False
    if contact_id:
        try:
            mongo_mgr = get_mongo_manager()
            appt_set = await mongo_mgr.get_contacts_with_appointments([contact_id])
            has_appointment = contact_id in appt_set
        except Exception:
            pass

    # ── Paso H: conversation_status ─────────────────────────────────────────
    conversation_status = best_status or ConversationStatus.BOT_ACTIVE.value

    # ── Paso I: canal / canal_origen ────────────────────────────────────────
    canal_final = best_canal or (canal_hint.lower() if canal_hint else "whatsapp")
    canal_origen = best_meta_dict.get("canal_origen") or canal_final

    last_activity = best_meta_dict.get("last_activity") or get_bogota_now_iso()
    created_at = best_meta_dict.get("created_at") or last_activity

    # Fuente efectiva del dato
    if best_meta_dict:
        source = "redis"
    elif contact_id:
        source = "hubspot"
    else:
        source = "phone"

    # Si no hay NINGUNA señal (sin meta en Redis, sin contact_id, sin display real)
    # aun así retornamos el contacto con fallback phone — el caller decide qué hacer.
    result: Dict[str, Any] = {
        "phone": phone_norm,
        "contact_id": contact_id,
        "display_name": display_name,
        "canal": canal_final,
        "canal_origen": canal_origen,
        "owner_id": owner_id,
        "owner_name": owner_name,
        "assigned_owner_ids": assigned_owner_ids,
        "conversation_status": conversation_status,
        "status": conversation_status,
        "last_activity": last_activity,
        "activated_at": created_at,
        "current_stage": current_stage,
        "has_appointment": has_appointment,
        "is_active": True,
        "handoff_reason": best_meta_dict.get("handoff_reason"),
        "deal_id": best_meta_dict.get("deal_id"),
        "deal_stage": best_meta_dict.get("deal_stage"),
        "last_advisor_message": best_meta_dict.get("last_advisor_message"),
        "source": source,
    }

    # Si no hay rastro alguno (ni Redis ni HubSpot), retornar None — contacto inexistente
    if source == "phone" and not best_meta_dict:
        logger.info(f"[Hydrate] {phone_norm} sin rastro en Redis/HubSpot → None")
        return None

    # ── Paso J: Persistencia opcional ───────────────────────────────────────
    if add_to_zset:
        try:
            _sm = _get_state_manager()
            await _sm.ensure_meta_with_channel(
                phone=phone_norm,
                canal=canal_final,
                canal_origen=canal_origen,
                contact_id=contact_id,
                display_name=display_name,
                owner_id=owner_id,
                add_to_zset=True,
            )
            await _sm.update_activity(phone_norm, canal=canal_final)
            logger.info(
                f"[Hydrate] {phone_norm}:{canal_final} re-inyectado al panel "
                f"(owner={owner_id}, name='{display_name}')"
            )
        except Exception as e:
            logger.warning(f"[Hydrate] Error persistiendo {phone_norm} al panel: {e}")

    logger.debug(
        f"[Hydrate] {phone_norm} → canal={canal_final}, status={conversation_status}, "
        f"contact_id={contact_id}, owner={owner_id}, source={source}"
    )
    return result


async def _hydrate_contact_and_ensure_panel(
    phone: str,
    canal_hint: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Thin wrapper: hidrata el contacto y lo persiste al panel (add_to_zset=True)."""
    return await _hydrate_contact(phone, canal_hint=canal_hint, add_to_zset=True)


async def _update_contact_to_visita_realizada(contact_id: str) -> None:
    """
    Avanza lifecyclestage 'marketingqualifiedlead' (Visita agendada)
    → 'salesqualifiedlead' (Visita realizada) cuando se completa la visita
    (scheduler post-cita 1h30min). Background — no bloquea envío WhatsApp.

    """
    import httpx
    if not HUBSPOT_API_KEY or not contact_id:
        return
    try:
        current_stage = await _get_contact_lifecyclestage(contact_id)

        # Guard 1: stage protegido (ya avanzó manualmente) → no revertir
        if current_stage in PROTECTED_STAGES_POST_VISITA:
            logger.info(
                f"[Lifecycle] Contacto {contact_id} ya en '{current_stage}' — no se sobreescribe"
            )
            return

        # Guard 2: solo avanzar desde 'Visita agendada' (nunca saltar stages)
        if current_stage != HUBSPOT_STAGE_VISITA_AGENDADA:
            logger.info(
                f"[Lifecycle] Contacto {contact_id} en '{current_stage}' — no es 'Visita agendada', skip"
            )
            return

        url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
        headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}", "Content-Type": "application/json"}
        client = get_httpx_client()
        r = await client.patch(
            url, headers=headers,
            json={"properties": {"lifecyclestage": HUBSPOT_STAGE_VISITA_REALIZADA}},
        )
        if r.status_code == 200:
            logger.info(
                f"[Lifecycle] Contacto {contact_id}: marketingqualifiedlead → salesqualifiedlead"
            )
            await _invalidate_contact_stage_cache(contact_id)
        else:
            logger.warning(
                f"[Lifecycle] Error actualizando lifecyclestage: {r.status_code} - {r.text}"
            )
    except Exception as e:
        logger.error(f"[Lifecycle] Error en _update_contact_to_visita_realizada: {e}")


async def _promote_no_responde_to_en_conversacion(
    contact_id: str,
    phone_normalized: str,
) -> bool:
    """
    Auto-promueve un contacto de 'No responde' (other) a 'En conversación' (1326623075)
    cuando responde a un mensaje masivo. Llamado desde webhook_handler.
    """
    if not HUBSPOT_API_KEY or not contact_id or not phone_normalized:
        return False
    try:
        r = await _get_redis_client()
        flag_key = f"{BULK_NO_RESPONDE_FLAG_PREFIX}{phone_normalized}"
        flag_present = await r.get(flag_key)
        if not flag_present:
            return False  # caso normal — no había bulk No Responde pendiente

        current_stage = await _get_contact_lifecyclestage(contact_id)
        if current_stage != "other":
            logger.info(
                f"[BulkNoResponde] Contacto {contact_id} en '{current_stage}' — "
                f"no se promueve (guard). Limpiando flag."
            )
            await r.delete(flag_key)
            return False

        url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
        headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}", "Content-Type": "application/json"}
        client = get_httpx_client()
        resp = await client.patch(
            url, headers=headers,
            json={"properties": {"lifecyclestage": HUBSPOT_STAGE_EN_CONVERSACION}},
        )
        if resp.status_code == 200:
            logger.info(
                f"[BulkNoResponde] Contacto {contact_id}: other → 1326623075 (En conversación) "
                f"por respuesta a mensaje masivo"
            )
            await _invalidate_contact_stage_cache(contact_id)
            await r.delete(flag_key)
            return True
        logger.warning(
            f"[BulkNoResponde] Error PATCH HubSpot {contact_id}: "
            f"{resp.status_code} - {resp.text[:200]}"
        )
        return False
    except Exception as e:
        logger.error(f"[BulkNoResponde] Error en promoción {contact_id}: {e}")
        return False


async def check_24h_window(phone_normalized: str) -> WindowStatus:
    """
    Verifica el estado de la ventana de 24 horas de WhatsApp.
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


async def _save_template_by_advisor(advisor_id: str, template_data: dict) -> bool:
    r = await _get_redis_client()
    template_id = template_data.get("id")
    if not template_id:
        return False
    key = f"{TEMPLATE_PREFIX}{advisor_id}:{template_id}"
    await r.set(key, json.dumps(template_data))
    logger.info(f"[Templates] Template guardado: {template_id} para {advisor_id}")
    return True


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
@limiter.limit("20/minute")
async def send_message(
    request: Request,
    background_tasks: BackgroundTasks,
    to: Optional[str] = Form(None, description="Número de destino (+573001234567)"),
    phone: Optional[str] = Form(None, description="Alias legacy: 'phone' (compatibilidad)"),
    body: Optional[str] = Form(None, description="Contenido del mensaje"),
    contact_id: Optional[str] = Form(None, description="ID del contacto en HubSpot"),
    canal: Optional[str] = Form(None, description="Canal de origen para segregación"),
    force_send: bool = Form(False, description="Forzar envío aunque ventana esté cerrada"),
    media_file: Optional[UploadFile] = File(None, description="Archivo multimedia (imagen/audio)"),
    reply_to_id: Optional[str] = Form(None, description="ID del mensaje citado"),
    reply_to_preview: Optional[str] = Form(None, description="Preview JSON del mensaje citado"),
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
    # =========================================================================
    if not canal or canal.strip() == "" or canal.lower() == "null":
        canal_final = "whatsapp"
    else:
        canal_final = canal.lower().strip()

    # Parsear reply_to_preview (Form data no soporta objetos anidados)
    parsed_reply_preview = None
    if reply_to_id and reply_to_preview:
        try:
            parsed_reply_preview = json.loads(reply_to_preview)
        except (json.JSONDecodeError, TypeError):
            parsed_reply_preview = None

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
            contact_manager = _get_contact_manager()
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
        state_manager = _get_state_manager()

        canal_info = f":{canal_final}"

        # Verificar estado actual (con canal)
        current_status = await state_manager.get_status(phone_normalized, canal_final)

        if current_status in [ConversationStatus.HUMAN_ACTIVE, ConversationStatus.PENDING_HANDOFF]:
            # Ya está en espera, cambiar a IN_CONVERSATION (asesora está atendiendo)
            await state_manager.set_status(
                phone_normalized,
                ConversationStatus.IN_CONVERSATION,
                ttl=state_manager.HUMAN_PANEL_STATE_TTL,
                canal=canal_final
            )
            logger.info(f"[Panel] Estado cambiado a IN_CONVERSATION para {phone_normalized}{canal_info}")
        elif current_status == ConversationStatus.IN_CONVERSATION:
            # Ya está en conversación, solo refrescar TTL
            await state_manager.set_status(
                phone_normalized,
                ConversationStatus.IN_CONVERSATION,
                ttl=state_manager.HUMAN_PANEL_STATE_TTL,
                canal=canal_final
            )
            logger.info(f"[Panel] TTL refrescado para IN_CONVERSATION: {phone_normalized}{canal_info}")
        else:
            # Era BOT_ACTIVE o CLOSED, activar humano y cambiar a IN_CONVERSATION
            await state_manager.activate_human(phone_normalized, canal_origen=canal_final)
            await state_manager.set_status(
                phone_normalized,
                ConversationStatus.IN_CONVERSATION,
                ttl=state_manager.HUMAN_PANEL_STATE_TTL,
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

            # Validar tamaño máximo para documentos y videos
            if content_type in DOCUMENT_MIME_TYPES and len(file_bytes) > MAX_DOCUMENT_SIZE_BYTES:
                raise HTTPException(status_code=413, detail="El archivo excede el límite de 10MB")
            if content_type.startswith("video/") and len(file_bytes) > MAX_VIDEO_SIZE_BYTES:
                raise HTTPException(status_code=413, detail="El video excede el límite de 16MB")

            # Subir a Bunny.net Storage (CDN)
            permanent_media_url = await media_processor.upload_outgoing_media(
                file_bytes=file_bytes,
                content_type=content_type,
                phone=phone_normalized
            )

            logger.info(f"[Panel] 📤 Bunny.net URL obtenida: {permanent_media_url}")

            # Determinar tipo de media (incluir webm como audio)
            if content_type in DOCUMENT_MIME_TYPES:
                media_type = "document"
            elif content_type.startswith("video/"):
                media_type = "video"
            elif content_type.startswith("image/"):
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

    # ── Citación inline (compensa que Twilio Conversations API no soporta ReplyTo) ──
    # Para audio: WhatsApp NO acepta caption; enviamos primero un texto-cita y
    body_for_twilio = message_body
    audio_intro_sid = None
    if parsed_reply_preview:
        if media_type == "audio":
            intro_text = reply_audio_intro(parsed_reply_preview)
            if intro_text:
                intro_result = await twilio_client.send_whatsapp_message(
                    to=phone_normalized,
                    body=intro_text,
                )
                if intro_result.get("status") == "success":
                    audio_intro_sid = intro_result.get("message_sid")
                    logger.info(f"[Panel][QuoteInject] Audio intro enviado: {audio_intro_sid}")
                else:
                    logger.warning(
                        f"[Panel][QuoteInject] Falló intro de audio: {intro_result.get('message')}"
                    )
        else:
            is_caption = bool(permanent_media_url)
            body_for_twilio = inject_quote(message_body, parsed_reply_preview, is_caption=is_caption)
            logger.info(f"[Panel][QuoteInject] Quote inyectada (is_caption={is_caption})")

    # Enviar mensaje con multimedia si corresponde
    result = await twilio_client.send_whatsapp_message(
        to=phone_normalized,
        body=body_for_twilio or "📎",  # Twilio requiere body, usar emoji si solo hay media
        media_url=permanent_media_url
    )

    if result["status"] == "success":
        message_sid = result.get("message_sid")
        sent_via = result.get("via")
        conv_sid_from_send = result.get("conversation_sid")
        im_sid_from_send = result.get("conversations_message_sid")
        chat_svc_sid_from_send = result.get("chat_service_sid")

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
                doc_fmt, doc_icon = DOCUMENT_MIME_TYPES.get(content_type, (None, None)) if media_type == "document" else (None, None)
                media_dict = {
                    "permanent_url": permanent_media_url,
                    "type": media_type,
                    "size_bytes": len(file_bytes) if 'file_bytes' in locals() and file_bytes is not None else None,
                    "format": (content_type.split('/')[-1] if content_type else None),
                    "duration_seconds": None,
                    "processed_at": datetime.utcnow(),
                    "uploaded_by": "advisor",
                    "doc_format": doc_fmt,
                    "doc_icon": doc_icon,
                    "original_filename": media_file.filename if media_file else None,
                }

            mongo_message_id = await mongo_manager.save_message(
                phone=phone_normalized,
                content=message_body or (f"[{media_type.upper()}]" if media_type else message_body),
                sender="advisor",
                channel=canal_final,
                hubspot_contact_id=contact_id,
                message_sid=message_sid,
                metadata={"source": "Manual via Panel", "send_via": sent_via or "legacy"},
                media=media_dict,
                reply_to_id=reply_to_id,
                reply_to_preview=parsed_reply_preview,
                conversation_sid=conv_sid_from_send,
                conversations_message_sid=im_sid_from_send,
                chat_service_sid=chat_svc_sid_from_send,
            )
            if mongo_message_id:
                logger.info(f"[Panel] Mensaje guardado en MongoDB: {mongo_message_id}, media_type={media_type}")
        except Exception as e:
            logger.error(f"[Panel] Error guardando en MongoDB: {e}")
            # No bloquear el flujo si MongoDB falla

        # Backfill solo si NO enviamos vía Conversations (sin IM SID en mano)
        if mongo_message_id and not im_sid_from_send:
            background_tasks.add_task(
                _backfill_im_sid_after_send,
                phone_normalized,
                message_body or "📎",
                str(mongo_message_id),
                None,
            )

        # =====================================================================
        # PASO 2: Registrar en HubSpot Timeline (BACKGROUND - no bloqueante)
        # HubSpot es archivo histórico, no afecta la experiencia del panel
        # =====================================================================
        if contact_id:
            # Construir contenido para HubSpot incluyendo link multimedia si existe
            hubspot_content = message_body
            if permanent_media_url:
                media_label = {"image": "📷 Imagen", "audio": "🎵 Audio", "video": "🎬 Video", "file": "📎 Archivo", "document": "📄 Documento"}.get(media_type, "📎 Archivo")
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

        # Mover contacto al top de la lista (actualizar score ZSET + last_activity en meta)
        _rc = None  # P1-B fix: inicializar antes del try para evitar UnboundLocalError si Redis cae
        try:
            _rc = await _get_redis_client()
            _now_ts = datetime.now(timezone.utc).timestamp()
            _now_iso = get_bogota_now().isoformat()

            # FIX: Actualizar last_activity síncronamente ANTES del WS para que loadContacts()
            # reciba el contacto en la posición correcta. El sort de GET /contacts usa last_activity
            # del meta; sin esto, el reorder en memoria del frontend se pierde al llamar loadContacts().
            _meta_key_la = f"conv_meta:{phone_normalized}:{canal_final}"
            _meta_raw_la = await _rc.get(_meta_key_la)
            if _meta_raw_la:
                _meta_obj_la = json.loads(_meta_raw_la)
                _meta_obj_la["last_activity"] = _now_iso
                _meta_ttl_la = await _rc.ttl(_meta_key_la)
                _ex_la = _meta_ttl_la if _meta_ttl_la and _meta_ttl_la > 0 else 7 * 86400
                await _rc.set(_meta_key_la, json.dumps(_meta_obj_la), ex=_ex_la)
                logger.debug(f"[Panel] last_activity actualizado síncronamente para {phone_normalized}:{canal_final}")

            await _rc.zadd("active_conversations_sorted", {f"{phone_normalized}:{canal_final}": _now_ts})
            logger.info(f"[Panel] ZSET actualizado para {phone_normalized}:{canal_final}")
        except Exception as _ze:
            logger.warning(f"[Panel] No se pudo actualizar ZSET en send_message: {_ze}")

        # Notificar a todos los asesores via WS para que refresquen la lista
        try:
            if _rc:  # P1-B fix: solo publicar si _rc fue asignado exitosamente
                await ws_manager.publish_broadcast(_rc, {
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
                "media_type": media_type,
                "reply_to_id": reply_to_id,
                "reply_to_preview": parsed_reply_preview
            }
        )
    else:
        raise HTTPException(
            status_code=500,
            detail=f"Error enviando mensaje: {result.get('message')}"
        )


# =============================================================================
# Edición y eliminación de mensajes propios del asesor (Conversations API)
# =============================================================================
EDIT_WINDOW_SECONDS = 15 * 60
DELETE_WINDOW_SECONDS = 60 * 60


async def _resolve_conversation_sid_for_phone(
    phone: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Busca el conversation_sid activo (< 23h) para este phone en MongoDB.
    """
    try:
        from datetime import timezone
        cutoff = datetime.utcnow() - timedelta(hours=23)
        mm = get_mongo_manager()
        if not await mm.connect():
            return None, None
        doc = await mm.db.messages.find_one(
            {
                "phone": phone,
                "conversation_sid": {"$nin": [None, ""]},
                "timestamp_utc": {"$gte": cutoff},
            },
            sort=[("timestamp_utc", -1)],
            projection={"conversation_sid": 1, "chat_service_sid": 1},
        )
        if not doc:
            return None, None
        return doc.get("conversation_sid"), doc.get("chat_service_sid")
    except Exception as e:
        logger.warning(f"[Panel][Backfill] resolve conv_sid fallo: {e}")
        return None, None


async def _backfill_im_sid_after_send(
    phone: str,
    body: str,
    mongo_message_id: Optional[str],
    conversation_sid: Optional[str] = None,
) -> None:
    """
    Backfill activo del IM SID tras un envío del panel.
    """
    if not mongo_message_id:
        return
    try:
        await asyncio.sleep(1.5)
        chat_service_sid: Optional[str] = None
        if not conversation_sid:
            conversation_sid, chat_service_sid = await _resolve_conversation_sid_for_phone(phone)
        if not conversation_sid:
            logger.info(
                f"[Panel][Backfill] Sin conversation_sid conocido para phone={phone} "
                f"(probablemente primera interacción saliente — esperar webhook)"
            )
            return
        result = await twilio_client.list_conversation_messages(
            conversation_sid, limit=5, chat_service_sid=chat_service_sid
        )
        if result.get("status") != "success":
            logger.warning(
                f"[Panel][Backfill] No se pudo listar mensajes conv={conversation_sid}: "
                f"{result.get('message')}"
            )
            return

        messages = result.get("messages", [])
        target_body = (body or "").strip()
        match = None
        for m in messages:
            m_body = (m.get("body") or "").strip()
            m_sid = m.get("sid") or ""
            if not m_sid.startswith("IM"):
                continue
            if m_body == target_body:
                match = m
                break
        if not match and messages:
            for m in messages:
                m_sid = m.get("sid") or ""
                if m_sid.startswith("IM"):
                    match = m
                    break

        if not match:
            logger.warning(
                f"[Panel][Backfill] Sin match IM en conv={conversation_sid} "
                f"para body={target_body[:40]!r}"
            )
            return

        im_sid = match.get("sid")
        mongo_manager = get_mongo_manager()
        ok = await mongo_manager.update_message_fields(
            mongo_message_id,
            {"conversations_message_sid": im_sid, "conversation_sid": conversation_sid},
        )
        if ok:
            logger.info(
                f"[Panel][Backfill] IM SID {im_sid} asignado a mongo_id={mongo_message_id}"
            )
        else:
            logger.warning(
                f"[Panel][Backfill] No se pudo persistir IM SID para mongo_id={mongo_message_id}"
            )
    except Exception as e:
        logger.error(f"[Panel][Backfill] Excepción: {e}")


def _msg_age_seconds(msg_doc: Dict[str, Any]) -> float:
    ts = msg_doc.get("timestamp_utc") or msg_doc.get("timestamp")
    if not ts:
        return 0.0
    try:
        if ts.tzinfo is None:
            return (datetime.utcnow() - ts).total_seconds()
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return 0.0


@router.patch("/messages/{mongo_id}")
@limiter.limit("20/minute")
async def edit_advisor_message(
    request: Request,
    mongo_id: str,
    advisor_id: str = Query(..., description="ID del asesor que solicita editar"),
    new_content: str = Form(..., description="Nuevo contenido del mensaje"),
    notify_client: bool = Form(False, description="Si True, envía mensaje '✏️ Corrección' al cliente"),
):
    """Edita un mensaje del asesor.
    """
    new_content = (new_content or "").strip()
    if not new_content:
        raise HTTPException(status_code=400, detail="Contenido vacío")

    mongo_manager = get_mongo_manager()
    msg = await mongo_manager.get_message_by_id(mongo_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Mensaje no encontrado")

    if msg.get("sender") != "advisor":
        raise HTTPException(status_code=403, detail="Solo se pueden editar mensajes propios del asesor")

    if msg.get("deleted"):
        raise HTTPException(status_code=410, detail="Mensaje ya eliminado")

    age = _msg_age_seconds(msg)
    conv_sid = msg.get("conversation_sid")
    im_sid   = msg.get("conversations_message_sid")
    chat_svc_sid = msg.get("chat_service_sid")

    # Best-effort: sincronizar Twilio Conversations Store (solo dentro de 15min)
    twilio_store_synced = False
    if conv_sid and im_sid and age <= EDIT_WINDOW_SECONDS:
        tw = await twilio_client.update_conversation_message(conv_sid, im_sid, new_content, chat_service_sid=chat_svc_sid)
        twilio_store_synced = tw.get("status") == "success"
        if not twilio_store_synced:
            logger.warning(
                f"[Panel][Edit] Twilio store update fallo conv={conv_sid} im={im_sid}: "
                f"{tw.get('message')} (continúa con edición local)"
            )

    # Persistir edición local (siempre, fuente de verdad del panel)
    updated = await mongo_manager.update_message_content(mongo_id, new_content)
    if not updated:
        raise HTTPException(status_code=500, detail="No se pudo persistir la edición")

    # Modo D: notificar al cliente con mensaje nuevo "✏️ Corrección: ..."
    correction_sid = None
    correction_mongo_id = None
    if notify_client:
        phone = msg.get("phone")
        if not phone:
            logger.warning(f"[Panel][Edit-Notify] msg sin phone, no se puede notificar")
        else:
            correction_body = f"✏️ Corrección: {new_content}"
            snd = await twilio_client.send_whatsapp_message(
                to=phone,
                body=correction_body,
                conversation_sid=conv_sid,
                chat_service_sid=chat_svc_sid,
            )
            if snd.get("status") == "success":
                correction_sid = snd.get("message_sid")
                try:
                    correction_mongo_id = await mongo_manager.save_message(
                        phone=phone,
                        content=correction_body,
                        sender="advisor",
                        channel=msg.get("channel", "whatsapp"),
                        hubspot_contact_id=msg.get("hubspot_contact_id"),
                        message_sid=correction_sid,
                        metadata={
                            "source": "Edit Notify",
                            "ref_message_id": str(mongo_id),
                            "send_via": snd.get("via") or "legacy",
                        },
                        conversation_sid=snd.get("conversation_sid"),
                        conversations_message_sid=snd.get("conversations_message_sid"),
                    )
                except Exception as _se:
                    logger.error(f"[Panel][Edit-Notify] save_message fallo: {_se}")
            else:
                logger.warning(
                    f"[Panel][Edit-Notify] envío fallo a {phone}: {snd.get('message')}"
                )

    # Broadcast a otras pestañas / asesores
    try:
        rc = await _get_redis_client()
        await ws_manager.publish_broadcast(rc, {
            "type": "message_edited",
            "message_id": str(updated["_id"]),
            "phone": updated.get("phone", ""),
            "new_content": new_content,
            "notified": bool(notify_client and correction_sid),
            "correction_message_id": correction_mongo_id,
        })
    except Exception as _we:
        logger.warning(f"[Panel] WS broadcast edit fallo: {_we}")

    return JSONResponse(content={
        "status": "success",
        "message_id": str(updated["_id"]),
        "new_content": new_content,
        "twilio_store_synced": twilio_store_synced,
        "notified_client": bool(notify_client and correction_sid),
        "correction_message_id": correction_mongo_id,
    })


@router.delete("/messages/{mongo_id}")
@limiter.limit("20/minute")
async def delete_advisor_message(
    request: Request,
    mongo_id: str,
    advisor_id: str = Query(..., description="ID del asesor que solicita eliminar"),
    notify_client: bool = Query(False, description="Si True, envía '🚫 Mensaje anterior anulado' al cliente"),
):
    """Elimina un mensaje del asesor.
    """
    mongo_manager = get_mongo_manager()
    msg = await mongo_manager.get_message_by_id(mongo_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Mensaje no encontrado")

    if msg.get("sender") != "advisor":
        raise HTTPException(status_code=403, detail="Solo se pueden eliminar mensajes propios del asesor")

    if msg.get("deleted"):
        return JSONResponse(content={"status": "success", "already_deleted": True})

    if _msg_age_seconds(msg) > DELETE_WINDOW_SECONDS:
        raise HTTPException(status_code=422, detail="Ventana de eliminación vencida (>60 min)")

    conv_sid = msg.get("conversation_sid")
    im_sid   = msg.get("conversations_message_sid")
    chat_svc_sid = msg.get("chat_service_sid")

    # Best-effort: borrar del Twilio Conversations Store (no afecta WhatsApp del cliente)
    twilio_store_deleted = False
    if conv_sid and im_sid:
        tw = await twilio_client.delete_conversation_message(conv_sid, im_sid, chat_service_sid=chat_svc_sid)
        twilio_store_deleted = (tw.get("status") == "success")
        if not twilio_store_deleted:
            logger.warning(
                f"[Panel][Delete] Twilio store delete fallo conv={conv_sid} im={im_sid}: "
                f"{tw.get('message')} (continúa con soft-delete local)"
            )

    # Soft-delete local (siempre)
    deleted = await mongo_manager.soft_delete_message(mongo_id)
    if not deleted:
        raise HTTPException(status_code=500, detail="No se pudo soft-delete en MongoDB")

    # Modo D: notificar al cliente con mensaje nuevo "🚫 Mensaje anulado"
    cancel_sid = None
    cancel_mongo_id = None
    if notify_client:
        phone = msg.get("phone")
        if not phone:
            logger.warning(f"[Panel][Delete-Notify] msg sin phone, no se puede notificar")
        else:
            cancel_body = "🚫 Mensaje anterior anulado"
            snd = await twilio_client.send_whatsapp_message(
                to=phone,
                body=cancel_body,
                conversation_sid=conv_sid,
                chat_service_sid=chat_svc_sid,
            )
            if snd.get("status") == "success":
                cancel_sid = snd.get("message_sid")
                try:
                    cancel_mongo_id = await mongo_manager.save_message(
                        phone=phone,
                        content=cancel_body,
                        sender="advisor",
                        channel=msg.get("channel", "whatsapp"),
                        hubspot_contact_id=msg.get("hubspot_contact_id"),
                        message_sid=cancel_sid,
                        metadata={
                            "source": "Delete Notify",
                            "ref_message_id": str(mongo_id),
                            "send_via": snd.get("via") or "legacy",
                        },
                        conversation_sid=snd.get("conversation_sid"),
                        conversations_message_sid=snd.get("conversations_message_sid"),
                    )
                except Exception as _se:
                    logger.error(f"[Panel][Delete-Notify] save_message fallo: {_se}")
            else:
                logger.warning(
                    f"[Panel][Delete-Notify] envío fallo a {phone}: {snd.get('message')}"
                )

    try:
        rc = await _get_redis_client()
        await ws_manager.publish_broadcast(rc, {
            "type": "message_deleted",
            "message_id": str(deleted["_id"]),
            "phone": deleted.get("phone", ""),
            "notified": bool(notify_client and cancel_sid),
            "cancel_message_id": cancel_mongo_id,
        })
    except Exception as _we:
        logger.warning(f"[Panel] WS broadcast delete fallo: {_we}")

    return JSONResponse(content={
        "status": "success",
        "message_id": str(deleted["_id"]),
        "twilio_store_deleted": twilio_store_deleted,
        "notified_client": bool(notify_client and cancel_sid),
        "cancel_message_id": cancel_mongo_id,
    })


@router.post("/send-message-json")
@limiter.limit("20/minute")
async def send_message_json(
    request: Request,
    background_tasks: BackgroundTasks,
    msg_request: SendMessageRequest = Body(...),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Envía un mensaje de WhatsApp desde el panel (formato JSON).
    """
    # Validar API Key
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida o no configurada")

    # Obtener mensaje (soportar 'message' o 'body')
    body = msg_request.message or msg_request.body
    
    # Validar que haya contenido
    if not body or not body.strip():
        raise HTTPException(status_code=400, detail="El mensaje no puede estar vacío")

    # Normalizar número
    normalizer = PhoneNormalizer()
    validation = normalizer.normalize(msg_request.phone)

    if not validation.is_valid:
        raise HTTPException(
            status_code=400,
            detail=f"Número inválido: {validation.error_message}"
        )

    phone_normalized = validation.normalized

    # Normalizar canal
    canal_final = msg_request.canal.lower().strip() if msg_request.canal else "whatsapp"
    if canal_final == "null" or not canal_final:
        canal_final = "whatsapp"

    # Verificar ventana de 24 horas
    window_status = await check_24h_window(phone_normalized)

    if not window_status.is_open and not msg_request.force_send:
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
    contact_id = msg_request.contact_id
    if not contact_id:
        try:
            contact_manager = _get_contact_manager()
            contact_info = await contact_manager.identify_or_create_contact(
                phone_raw=phone_normalized,
                source_channel="panel_asesor"
            )
            contact_id = contact_info.contact_id
        except Exception as e:
            logger.warning(f"[Panel-JSON] No se pudo obtener contacto: {e}")

    # Pausar Sofía y cambiar estado
    try:
        state_manager = _get_state_manager()

        current_status = await state_manager.get_status(phone_normalized, canal_final)

        if current_status in [ConversationStatus.HUMAN_ACTIVE, ConversationStatus.PENDING_HANDOFF, ConversationStatus.IN_CONVERSATION]:
            await state_manager.set_status(
                phone_normalized,
                ConversationStatus.IN_CONVERSATION,
                ttl=state_manager.HUMAN_PANEL_STATE_TTL,
                canal=canal_final
            )
            logger.info(f"[Panel-JSON] Estado: IN_CONVERSATION para {phone_normalized}:{canal_final}")
        else:
            # Crear nuevo estado para la conversación
            await state_manager.set_status(
                phone_normalized,
                ConversationStatus.IN_CONVERSATION,
                ttl=state_manager.HUMAN_PANEL_STATE_TTL,
                canal=canal_final
            )
            logger.info(f"[Panel-JSON] Nuevo estado IN_CONVERSATION para {phone_normalized}:{canal_final}")

    except Exception as e:
        logger.error(f"[Panel-JSON] Error actualizando estado: {e}")

    # ── Citación inline: prepend quote SOLO para Twilio. Mongo guarda body original. ──
    body_to_send = body
    if msg_request.reply_to_id and msg_request.reply_to_preview:
        body_to_send = inject_quote(body, msg_request.reply_to_preview, is_caption=False)
        logger.info(f"[Panel-JSON][QuoteInject] Quote inyectada en body (solo Twilio)")

    # Enviar mensaje vía Twilio
    result = await twilio_client.send_whatsapp_message(
        to=phone_normalized,
        body=body_to_send
    )

    if result["status"] == "success":
        message_sid = result.get("message_sid")
        sent_via = result.get("via")
        conv_sid_from_send = result.get("conversation_sid")
        im_sid_from_send = result.get("conversations_message_sid")
        chat_svc_sid_from_send = result.get("chat_service_sid")
        logger.info(f"[Panel-JSON] ✅ Mensaje enviado: {message_sid} a {phone_normalized} (via={sent_via or 'legacy'})")

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
                metadata={"source": "Panel JSON API", "send_via": sent_via or "legacy"},
                reply_to_id=msg_request.reply_to_id,
                reply_to_preview=msg_request.reply_to_preview,
                conversation_sid=conv_sid_from_send,
                conversations_message_sid=im_sid_from_send,
                chat_service_sid=chat_svc_sid_from_send,
            )
        except Exception as e:
            logger.error(f"[Panel-JSON] Error guardando en MongoDB: {e}")

        if mongo_message_id and not im_sid_from_send:
            background_tasks.add_task(
                _backfill_im_sid_after_send,
                phone_normalized,
                body,
                str(mongo_message_id),
                None,
            )

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

        # Mover contacto al top de la lista (actualizar score ZSET + last_activity en meta)
        _rc = None  # P1-B fix: inicializar antes del try para evitar UnboundLocalError si Redis cae
        try:
            _rc = await _get_redis_client()
            _now_ts = datetime.now(timezone.utc).timestamp()
            _now_iso = get_bogota_now().isoformat()

            # FIX: Actualizar last_activity síncronamente ANTES del WS para que loadContacts()
            # reciba el contacto en la posición correcta. El sort de GET /contacts usa last_activity
            # del meta; sin esto, el reorder en memoria del frontend se pierde al llamar loadContacts().
            _meta_key_la = f"conv_meta:{phone_normalized}:{canal_final}"
            _meta_raw_la = await _rc.get(_meta_key_la)
            if _meta_raw_la:
                _meta_obj_la = json.loads(_meta_raw_la)
                _meta_obj_la["last_activity"] = _now_iso
                _meta_ttl_la = await _rc.ttl(_meta_key_la)
                _ex_la = _meta_ttl_la if _meta_ttl_la and _meta_ttl_la > 0 else 7 * 86400
                await _rc.set(_meta_key_la, json.dumps(_meta_obj_la), ex=_ex_la)
                logger.debug(f"[Panel-JSON] last_activity actualizado síncronamente para {phone_normalized}:{canal_final}")

            await _rc.zadd("active_conversations_sorted", {f"{phone_normalized}:{canal_final}": _now_ts})
            logger.info(f"[Panel-JSON] ZSET actualizado para {phone_normalized}:{canal_final}")
        except Exception as _ze:
            logger.warning(f"[Panel-JSON] No se pudo actualizar ZSET: {_ze}")

        # Notificar a todos los asesores via WS
        try:
            if _rc:  # P1-B fix: solo publicar si _rc fue asignado exitosamente
                await ws_manager.publish_broadcast(_rc, {
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

    # Safety net: rechazar si template requiere variables pero no se proporcionaron.
    # Previene envío de placeholders crudos ({fecha}, {hora}) por fallo de extracción.
    template_vars = template.get("variables", [])
    if template_vars and not vars_dict:
        logger.warning(
            f"[Panel] Template '{template_id}' requiere {len(template_vars)} variables "
            f"pero vars_dict está vacío — rechazando para evitar placeholders crudos"
        )
        raise HTTPException(
            status_code=400,
            detail=f"Template '{template_id}' requiere variables ({', '.join(template_vars)}) pero no se proporcionaron"
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
    if content_sid:
        if isinstance(variables_map, dict):
            # Numeración explícita: {"2": "link_inmueble"} → {"2": valor}
            # Usado cuando el template Meta no empieza en {{1}}
            content_variables = {
                num: vars_dict.get(var_name, "")
                for num, var_name in variables_map.items()
            }
        else:
            # Numeración secuencial: ["nombre", "tema"] → {"1": nombre, "2": tema}
            content_variables = {
                str(i + 1): vars_dict.get(var_name, "")
                for i, var_name in enumerate(variables_map)
            }
        # Validar que ninguna variable esté vacía — evita error 21656 de Twilio
        empty_vars = [k for k, v in content_variables.items() if not v or not str(v).strip()]
        if empty_vars:
            logger.error(
                f"[Panel] content_variables vacíos: keys={empty_vars} "
                f"vars_dict={vars_dict} map={variables_map}"
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Las variables {', '.join(empty_vars)} están vacías. "
                    f"Selecciona la plantilla de nuevo con / y solo reemplaza "
                    f"los campos {{variable}} sin modificar el texto fijo."
                )
            )
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
        sent_via = result.get("via")
        conv_sid_from_send = result.get("conversation_sid")
        im_sid_from_send = result.get("conversations_message_sid")
        chat_svc_sid_from_send = result.get("chat_service_sid")
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
                metadata={"source": "Template via Panel", "template_id": template_id, "send_via": sent_via or "legacy"},
                conversation_sid=conv_sid_from_send,
                conversations_message_sid=im_sid_from_send,
                chat_service_sid=chat_svc_sid_from_send,
            )
            if mongo_message_id:
                logger.info(f"[Panel] Template guardado en MongoDB: {mongo_message_id}")
        except Exception as e:
            logger.error(f"[Panel] Error guardando template en MongoDB: {e}")

        if mongo_message_id and not im_sid_from_send:
            background_tasks.add_task(
                _backfill_im_sid_after_send,
                phone_normalized,
                template_message,
                str(mongo_message_id),
                None,
            )

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
        client = get_httpx_client()
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

    # Mapeo de canales internos → valores permitidos por HubSpot (enum fijo)
    _HUBSPOT_CANAL_MAP = {
        "charly": "pagina_web",  # Chatling se registra como pagina_web en HubSpot
    }
    canal_hs = _HUBSPOT_CANAL_MAP.get(canal, canal)

    # Solo propiedades estándar de HubSpot (siempre existen)
    contact_properties = {
        "whatsapp_id": phone_normalized,
        "phone": phone_normalized,
        "firstname": firstname.strip(),
        "lastname": lastname.strip() if lastname else "",
        "canal_origen": canal_hs,
        "chatbot_timestamp": str(int(midnight_utc.timestamp() * 1000)),
        "lifecyclestage": HUBSPOT_STAGE_EN_CONVERSACION,
    }

    # Agregar owner si está disponible
    if owner_id:
        contact_properties["hubspot_owner_id"] = owner_id

    # NOTA: tipo_inmueble, tipo_operacion, presupuesto, caracteristicas
    # se guardan en el Deal (description) en lugar del contacto
    # porque son propiedades custom que pueden no existir en HubSpot

    create_url = "https://api.hubapi.com/crm/v3/objects/contacts"

    try:
        client = get_httpx_client()
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

    # === 5.5. Escribir url_chat en Contacto ===
    try:
        panel_base_url = os.getenv("PANEL_BASE_URL", "").rstrip("/")
        admin_api_key = os.getenv("ADMIN_API_KEY", "")
        if panel_base_url and owner_id and admin_api_key:
            from urllib.parse import quote
            phone_encoded = quote(phone_normalized, safe='')
            url_chat = f"{panel_base_url}/whatsapp/panel/?key={admin_api_key}&advisor={owner_id}&phone={phone_encoded}"
            
            # Escribir en contacto
            client = get_httpx_client()
            contact_patch_url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
            await client.patch(
                contact_patch_url,
                headers={"Authorization": f"Bearer {hubspot_api_key}", "Content-Type": "application/json"},
                json={"properties": {"url_chat": url_chat}}
            )
            logger.info(f"[Panel] url_chat escrito en contacto {contact_id}")
    except Exception as e:
        logger.warning(f"[Panel] Error escribiendo url_chat (no crítico): {e}")

    # === 6. Activar HUMAN_ACTIVE en Redis ===
    try:
        state_manager = _get_state_manager()

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
    state_manager = _get_state_manager()

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

    # === 2. Actualizar HubSpot en background (no bloquear la respuesta) ===
    hubspot_updated = "queued"
    if mode == "exclusive" and contact_id:
        import httpx
        hubspot_api_key = os.getenv("HUBSPOT_API_KEY")

        if hubspot_api_key:
            _cid = contact_id
            _oid = to_owner_id
            _key = hubspot_api_key

            async def _update_hubspot_owner():
                try:
                    from urllib.parse import quote as _quote
                    _props = {"hubspot_owner_id": _oid}
                    _panel_base = os.getenv("PANEL_BASE_URL", "").rstrip("/")
                    _admin_key = os.getenv("ADMIN_API_KEY", "")
                    if _panel_base and _admin_key and phone_normalized:
                        _props["url_chat"] = (
                            f"{_panel_base}/whatsapp/panel/"
                            f"?key={_admin_key}&advisor={_oid}&phone={_quote(phone_normalized, safe='')}"
                        )
                    url = f"https://api.hubapi.com/crm/v3/objects/contacts/{_cid}"
                    client = get_httpx_client()
                    response = await client.patch(
                        url,
                        json={"properties": _props},
                        headers={
                            "Authorization": f"Bearer {_key}",
                            "Content-Type": "application/json"
                        }
                    )
                    if response.status_code == 200:
                        logger.info(f"[Panel] HubSpot owner actualizado: {_cid} -> {_oid}"
                                    + (" + url_chat actualizado" if "url_chat" in _props else ""))
                    else:
                        logger.warning(f"[Panel] Error actualizando HubSpot: {response.status_code}")
                except Exception as e:
                    logger.warning(f"[Panel] Error actualizando HubSpot (no crítico): {e}")

            asyncio.create_task(_update_hubspot_owner())

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
            mode=mode,
            redis_client=state_manager.redis
        )
    except Exception as e:
        logger.warning(f"[Panel] Error notificando WebSocket: {e}")

    # Inbox: agregar al inbox del receptor, remover del emisor
    try:
        await state_manager.add_to_advisor_inbox(to_owner_id, phone_normalized, canal or "whatsapp")
        if from_owner:
            await state_manager.remove_from_advisor_inbox(from_owner, phone_normalized, canal or "whatsapp")
    except Exception as _tr_inbox_err:
        logger.warning(f"[Panel][Inbox] Error en inbox transfer (non-fatal): {_tr_inbox_err}")

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


async def _reassign_hubspot_owner(contact_id: str, to_owner_id: str, phone: str = None) -> bool:
    """PATCH hubspot_owner_id (y url_chat si se proporciona phone) en HubSpot."""
    try:
        import httpx
        from urllib.parse import quote
        hubspot_api_key = os.getenv("HUBSPOT_API_KEY")
        if not hubspot_api_key or not contact_id:
            return False

        properties = {"hubspot_owner_id": to_owner_id}

        # Actualizar url_chat si tenemos phone y las env vars necesarias
        panel_base_url = os.getenv("PANEL_BASE_URL", "").rstrip("/")
        admin_api_key = os.getenv("ADMIN_API_KEY", "")
        if phone and panel_base_url and admin_api_key:
            phone_encoded = quote(phone, safe='')
            properties["url_chat"] = (
                f"{panel_base_url}/whatsapp/panel/"
                f"?key={admin_api_key}&advisor={to_owner_id}&phone={phone_encoded}"
            )

        url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
        client = get_httpx_client()
        response = await client.patch(
            url,
            json={"properties": properties},
            headers={
                "Authorization": f"Bearer {hubspot_api_key}",
                "Content-Type": "application/json"
            }
        )
        if response.status_code == 200:
            logger.info(f"[Panel] HubSpot owner reasignado: {contact_id} -> {to_owner_id}"
                        + (" + url_chat actualizado" if "url_chat" in properties else ""))
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
    canal: str = Query("whatsapp_directo", description="Canal del contacto"),
    contact_name: str = Query("", description="Nombre del contacto para mostrar"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Transfiere un contacto al asesor solicitante de forma inmediata.
    El contacto aparece en el panel del solicitante al instante.
    El ex-propietario recibe una notificación informativa (no de aprobación).
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    requester_name = _get_advisor_name(requesting_advisor_id)
    display = contact_name or phone

    # 1. Activar contacto en el panel del solicitante inmediatamente
    state_manager = _get_state_manager()
    try:
        await state_manager.activate_human(
            phone_normalized=phone,
            canal_origen=canal,
            owner_id=requesting_advisor_id,
            contact_id=contact_id,
            display_name=display,
            reason=f"Transferencia directa desde modal de creación"
        )
    except Exception as e:
        logger.warning(f"[Panel] activate_human en transfer-request falló: {e}")

    # 2. Reasignar owner en HubSpot en background (no bloquear respuesta)
    asyncio.create_task(_reassign_hubspot_owner(contact_id, to_owner_id=requesting_advisor_id, phone=phone))

    # 3-5. Un cliente redis para phone_cache + notificaciones + broadcast
    _rc_transfer = await _get_redis_client()
    try:
        await _rc_transfer.set(f"phone_cache:{contact_id}", phone, ex=86400, nx=True)
    except Exception:
        pass

    # 4. Notificar al ex-propietario (solo informativo) — cross-worker safe
    await ws_manager.notify_contact_transferred(
        phone=phone,
        from_advisor=owner_advisor_id,
        to_advisor=requesting_advisor_id,
        contact_name=display,
        mode="exclusive",
        redis_client=_rc_transfer
    )

    # 5. Broadcast para que todos los paneles refresquen
    await ws_manager.publish_broadcast(_rc_transfer, {
        "type": "contact_updated",
        "phone": phone,
        "action": "transfer_completed"
    })

    # Inbox: el receptor recibe el contacto como no-leído; el emisor lo pierde
    try:
        _sm_tr = _get_state_manager()
        await _sm_tr.add_to_advisor_inbox(requesting_advisor_id, phone, canal or "whatsapp")
        await _sm_tr.remove_from_advisor_inbox(owner_advisor_id, phone, canal or "whatsapp")
    except Exception as _tr_req_inbox_err:
        logger.warning(f"[Panel][Inbox] Error inbox transfer-request (non-fatal): {_tr_req_inbox_err}")

    logger.info(f"[Panel] Transfer directo: {owner_advisor_id} -> {requesting_advisor_id} para {contact_id} ({canal})")
    return {"status": "transferred", "phone": phone, "canal": canal}


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
    await _reassign_hubspot_owner(contact_id, to_owner_id=requester_id, phone=phone)

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
    await ws_manager.publish_to_advisor(_rc, requester_id, {
        "type": "transfer_accepted",
        "contact_id": contact_id,
        "phone": phone,
        "contact_name": contact_name,
        "message": "Transferencia aceptada — el contacto es tuyo"
    })

    # 5. Broadcast para refrescar el panel de todos
    await ws_manager.publish_broadcast(_rc, {
        "type": "contact_updated",
        "phone": phone,
        "action": "transfer_completed"
    })

    # Inbox: el solicitante recibe el contacto como no-leído; el propietario anterior lo pierde
    try:
        _sm_ta = _get_state_manager()
        await _sm_ta.add_to_advisor_inbox(requester_id, phone, None)
        await _sm_ta.remove_from_advisor_inbox(by_advisor_id, phone, None)
    except Exception as _ta_inbox_err:
        logger.warning(f"[Panel][Inbox] Error inbox transfer-accept (non-fatal): {_ta_inbox_err}")

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
        await ws_manager.publish_to_advisor(_rc, requester_id, {
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
            # Invalidar cache de nombre para que el próximo poll traiga el dato fresco de HubSpot
            try:
                _rc_inv = await _get_redis_client()
                await _rc_inv.delete(f"{CONTACT_NAME_CACHE_PREFIX}{contact_id}")
            except Exception:
                pass
            # Notificar a todos los paneles para que actualicen el nombre sin esperar poll
            try:
                _rc_ws = await _get_redis_client()
                _phone_ws = await _rc_ws.get(f"phone_cache:{contact_id}")
                await ws_manager.publish_broadcast(_rc_ws, {
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

async def _close_conversation_internal(
    phone: str,
    canal: Optional[str] = None,
) -> dict:
    """
    Lógica reutilizable de cierre de conversación: BOT_ACTIVE + retirar del ZSET
    y bot_controlled_conversations + in_panel=False + retirar del inbox del asesor.

    Idempotente. Llamada por el endpoint DELETE /contacts/{phone}/close y por el
    auto-cierre del embudo "No responde" en update_contact_stage.

    Devuelve {"phone": normalizado, "canal": canal, "new_status": "BOT_ACTIVE"}.
    """
    normalizer = PhoneNormalizer()
    validation = normalizer.normalize(phone)
    phone_normalized = validation.normalized if validation.is_valid else phone

    state_manager = _get_state_manager()

    await state_manager.activate_bot(phone_normalized, canal=canal)
    if phone != phone_normalized:
        await state_manager.activate_bot(phone, canal=canal)

    try:
        r = await _get_redis_client()
        canal_safe = (canal or "whatsapp").lower()
        member = f"{phone_normalized}:{canal_safe}"
        await r.zrem("active_conversations_sorted", member)
        await r.srem("bot_controlled_conversations", member)
        if phone != phone_normalized:
            member_orig = f"{phone}:{canal_safe}"
            await r.zrem("active_conversations_sorted", member_orig)
            await r.srem("bot_controlled_conversations", member_orig)

        meta_key_close = f"conv_meta:{phone_normalized}:{canal_safe}"
        raw_close = await r.get(meta_key_close)
        if raw_close:
            try:
                meta_close = json.loads(raw_close)
                meta_close["in_panel"] = False
                ttl_close = await r.ttl(meta_key_close)
                ex_close = ttl_close if ttl_close and ttl_close > 0 else state_manager.PANEL_TTL_SECONDS
                await r.set(meta_key_close, json.dumps(meta_close), ex=ex_close)
            except Exception as e_meta:
                logger.warning(f"[Panel] No se pudo setear in_panel=False al cerrar {phone_normalized}: {e_meta}")
    except Exception as e:
        logger.warning(f"[Panel] No se pudo remover del ZSET al cerrar {phone_normalized}: {e}")

    try:
        _close_meta = await state_manager.get_meta(phone_normalized, canal or "whatsapp")
        if _close_meta and _close_meta.assigned_owner_id:
            await state_manager.remove_from_advisor_inbox(
                _close_meta.assigned_owner_id, phone_normalized, canal or "whatsapp"
            )
    except Exception as _close_inbox_err:
        logger.warning(f"[Panel] Error removiendo inbox al cerrar (non-fatal): {_close_inbox_err}")

    canal_info = f":{canal}" if canal else ""
    logger.info(
        f"[Panel] Conversación cerrada — BOT_ACTIVE + removido del panel: "
        f"{phone_normalized}{canal_info}"
    )

    return {
        "phone": phone_normalized,
        "canal": canal,
        "new_status": "BOT_ACTIVE",
    }


@router.delete("/contacts/{phone}/close")
async def close_conversation(
    phone: str,
    canal: Optional[str] = None,
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Cierra una conversación transicionando a BOT_ACTIVE.
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        result = await _close_conversation_internal(phone, canal)
        return {
            "status": "success",
            "message": "Conversación cerrada - Sofía retomará automáticamente",
            **result,
        }
    except Exception as e:
        logger.error(f"[Panel] Error cerrando conversación: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Endpoint: Marcar contacto como leído (borra del advisor_inbox en Redis)
# ============================================================================

@router.post("/contacts/{phone}/mark-read")
async def mark_contact_read(
    phone: str,
    advisor_id: Optional[str] = Query(None, description="ID del asesor que leyó el contacto (se infiere del meta si no se provee)"),
    canal: Optional[str] = Query(None, description="Canal del contacto (opcional)"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Registra que el asesor abrió un contacto, removiéndolo de su advisor_inbox en Redis.
    Llamado desde el frontend al hacer click en un contacto (fire-and-forget).
    Si advisor_id no se provee, se infiere de ConversationMeta.assigned_owner_id.
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    try:
        normalizer = PhoneNormalizer()
        validation = normalizer.normalize(phone)
        phone_norm = validation.normalized if validation.is_valid else phone
        sm = _get_state_manager()
        # Si no se recibe advisor_id, inferirlo del meta del contacto
        if not advisor_id:
            _meta = await sm.get_meta(phone_norm, canal or "whatsapp")
            if _meta:
                advisor_id = _meta.assigned_owner_id
        if advisor_id:
            await sm.remove_from_advisor_inbox(advisor_id, phone_norm, canal)
        return {"status": "ok", "phone": phone_norm}
    except Exception as e:
        logger.warning(f"[Panel][Inbox] mark_contact_read error (non-fatal): {e}")
        return {"status": "error"}


# ============================================================================
# Endpoint para actualizar lifecyclestage del Contact en HubSpot
# ============================================================================

@router.patch("/contacts/{contact_id}/stage")
async def update_contact_stage(
    contact_id: str,
    stage_id: str = Body(..., embed=True),
    phone: Optional[str] = Body(None, embed=True),
    canal: Optional[str] = Body(None, embed=True),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Actualiza la etapa (lifecyclestage) del Contact en HubSpot desde el panel de asesores.
    Arquitectura Contact-Centric: no depende de objetos Deal.
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    if stage_id not in PIPELINE_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"stage_id inválido. Valores permitidos: {list(PIPELINE_STAGES.keys())}"
        )

    hubspot_token = os.getenv("HUBSPOT_API_KEY")
    if not hubspot_token:
        raise HTTPException(status_code=500, detail="HUBSPOT_API_KEY no configurada")

    try:
        url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"

        # Two-step update: HubSpot lifecyclestage es unidireccional — solo permite avanzar.
        # Limpiar primero elimina esa restricción y permite mover el stage en cualquier dirección.
        # Ambos steps usan _hubspot_patch() que reintenta automáticamente en 429.
        # Step 1: Clear
        await _hubspot_patch(url, {"properties": {"lifecyclestage": ""}}, hubspot_token)
        # Step 2: Set
        response = await _hubspot_patch(url, {"properties": {"lifecyclestage": stage_id}}, hubspot_token)

        if response.status_code == 200:
            stage_name = PIPELINE_STAGES.get(stage_id, stage_id)
            logger.info(f"[Panel] Contacto {contact_id} actualizado a etapa '{stage_name}'")
            await _invalidate_contact_stage_cache(contact_id)

            payload = {
                "status": "success",
                "message": f"Etapa actualizada a '{stage_name}'",
                "contact_id": contact_id,
                "stage_id": stage_id,
                "stage_name": stage_name,
                "closed": False,
            }

            if stage_id == "other" and phone:
                try:
                    close_result = await _close_conversation_internal(phone, canal)
                    payload["closed"] = True
                    payload["close_result"] = close_result
                    logger.info(
                        f"[Panel] Auto-cierre por 'No responde' — contact={contact_id} phone={phone}"
                    )
                except Exception as e_close:
                    logger.error(
                        f"[Panel] Auto-cierre falló para contact={contact_id} phone={phone}: {e_close}"
                    )
                    payload["close_error"] = str(e_close)

            return payload
        else:
            logger.error(f"[Panel] Error actualizando lifecyclestage: {response.status_code} - {response.text}")
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Error actualizando etapa: {response.text[:200]}"
            )

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Timeout conectando con HubSpot")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Panel] Error inesperado actualizando etapa: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")


# ─── Canales válidos para edición manual de canal_display ────────────────────
_CANALES_DISPLAY_VALIDOS = [
    "whatsapp", "whatsapp_directo",
    "instagram", "facebook", "linkedin", "youtube", "tiktok",
    "finca_raiz", "metrocuadrado", "mercado_libre", "ciencuadras",
    "pagina_web", "desconocido",
]


@router.patch("/contacts/{phone}/canal")
async def update_contact_canal_display(
    phone: str,
    canal_display: str = Body(..., embed=True),
    canal: Optional[str] = Body(None, embed=True),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Actualiza el canal visible (canal_display) de un contacto.
    canal_display es solo para UI y métricas — NO cambia canal_origen ni el routing.
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    canal_display_clean = canal_display.lower().strip()
    if canal_display_clean not in _CANALES_DISPLAY_VALIDOS:
        raise HTTPException(
            status_code=400,
            detail=f"canal_display inválido. Valores permitidos: {_CANALES_DISPLAY_VALIDOS}"
        )

    try:
        normalizer = PhoneNormalizer()
        validation = normalizer.normalize(phone)
        if not validation.is_valid:
            raise HTTPException(status_code=400, detail=f"Número inválido: {validation.error_message}")
        phone_normalized = validation.normalized

        state_manager = _get_state_manager()
        canal_meta = (canal or "whatsapp").lower().strip()
        meta_key = f"{state_manager.META_PREFIX}{phone_normalized}:{canal_meta}"

        raw = await state_manager.redis.get(meta_key)
        if not raw:
            raise HTTPException(status_code=404, detail="Contacto no encontrado en Redis")

        meta_dict = json.loads(raw)
        meta_dict["canal_display"] = canal_display_clean
        ttl = await state_manager.redis.ttl(meta_key)
        await state_manager.redis.set(meta_key, json.dumps(meta_dict), ex=max(ttl, 86400) if ttl > 0 else state_manager.PANEL_TTL_SECONDS)

        logger.info(f"[Panel] canal_display actualizado: {phone_normalized}:{canal_meta} → {canal_display_clean}")
        return {"status": "success", "phone": phone_normalized, "canal_display": canal_display_clean}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Panel] Error actualizando canal_display: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")


# ─── Notificaciones del asesor ────────────────────────────────────────────────

@router.get("/notifications")
@limiter.limit("60/minute")
async def get_advisor_notifications(
    request: Request,
    advisor: str = Query(..., description="ID del asesor"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Retorna las notificaciones activas del asesor (inactividad 24h + recordatorios diarios).
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        state_manager = _get_state_manager()
        notifications = await state_manager.get_advisor_notifications(advisor)
        return {
            "notifications": notifications,
            "unread_count": len(notifications),
        }
    except Exception as e:
        logger.error(f"[Panel] Error obteniendo notificaciones advisor={advisor}: {e}")
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")


@router.post("/notifications/{notif_id}/read")
async def mark_notification_read(
    notif_id: str,
    advisor: str = Body(..., embed=True),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Marca una notificación como leída (la elimina del ZSET)."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        state_manager = _get_state_manager()
        removed = await state_manager.remove_advisor_notification(advisor, notif_id)
        return {"status": "success", "removed": removed}
    except Exception as e:
        logger.error(f"[Panel] Error marcando notificación leída: {e}")
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")


@router.post("/notifications/read-all")
async def mark_all_notifications_read(
    advisor: str = Body(..., embed=True),
    notif_type: Optional[str] = Body(None, embed=True),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Marca todas las notificaciones del asesor como leídas.
    Si notif_type se especifica (ej: 'inactivity_24h'), solo borra ese tipo.
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        state_manager = _get_state_manager()
        notifications = await state_manager.get_advisor_notifications(advisor)
        removed = 0
        for notif in notifications:
            if notif_type is None or notif.get("type") == notif_type:
                ok = await state_manager.remove_advisor_notification(advisor, notif["id"])
                if ok:
                    removed += 1
        return {"status": "success", "removed": removed}
    except Exception as e:
        logger.error(f"[Panel] Error marcando todas las notificaciones: {e}")
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
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # Normalizar teléfono
    normalizer = PhoneNormalizer()
    validation = normalizer.normalize(phone)
    phone_normalized = validation.normalized if validation.is_valid else phone

    try:
        state_manager = _get_state_manager()

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
    # ⚠️ 2026-06-05: cursor para que el panel pueda paginar con scroll infinito.
    # Si llega, NO se consulta HubSpot (mismo guard que /history/{cid}).
    before_ts: Optional[str] = Query(
        None,
        description="Cursor ISO 8601 — retorna mensajes con timestamp < before_ts"
    ),
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

    # ⚠️ 2026-06-05: Parse del cursor before_ts (igual que /history/{cid})
    before_ts_dt = None
    if before_ts:
        try:
            before_ts_dt = datetime.fromisoformat(before_ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail=f"before_ts inválido: {before_ts}")
    # En paginación nunca consultar HubSpot — solo Mongo (evita N requests externos)
    hs_fallback_allowed = (before_ts_dt is None)

    async def _get_messages():
        try:
            # Paso 1: MongoDB por teléfono (sin filtro de canal).
            # Antes filtraba por canal, pero el canal con que el panel abre el chat
            # (canal_origen de HubSpot) puede diferir del canal con que se guardan los
            # mensajes (historical_channel de Redis), causando mensajes invisibles.
            mongo_msgs = await mongo_manager.get_history(
                phone=phone_normalized,
                limit=limit,
                channel=None,
                before_ts=before_ts_dt,
            )

            # Si MongoDB ya tiene historial completo (>20) → retornar sin consultar HubSpot
            if len(mongo_msgs) > 20:
                return mongo_msgs, "mongodb"

            # Pasos 2 + 3 en PARALELO: MongoDB por contact_id y HubSpot simultáneamente.
            # SIEMPRE se ejecuta cuando mongo_msgs ≤ 20, incluso si Paso 1 devolvió algo,
            # para evitar mostrar historial parcial (ej: 1 mensaje outbound vs 15 en HubSpot).
            # ⚠️ Pero NO en paginación (hs_fallback_allowed=False) — solo Mongo.
            if contact_id and contact_id.isdigit() and hs_fallback_allowed:
                _tl = get_timeline_logger()
                mongo2_msgs, hs_messages = await asyncio.gather(
                    mongo_manager.get_history_by_contact_id(
                        hubspot_contact_id=contact_id,
                        limit=limit,
                        before_ts=before_ts_dt,
                    ),
                    _tl.get_notes_for_contact(
                        contact_id=contact_id,
                        limit=limit
                    ),
                    return_exceptions=True,
                )
                if isinstance(mongo2_msgs, Exception):
                    logger.warning(f"[Panel] MongoDB contact_id falló: {mongo2_msgs}")
                    mongo2_msgs = []
                if isinstance(hs_messages, Exception):
                    logger.warning(f"[Panel] HubSpot falló: {hs_messages}")
                    hs_messages = []

                # Mejor resultado de MongoDB entre Paso 1 y Paso 2
                best_mongo = mongo_msgs if len(mongo_msgs) >= len(mongo2_msgs) else mongo2_msgs

                if len(hs_messages) > len(best_mongo):
                    logger.info(
                        f"[Panel] HubSpot supera MongoDB ({len(hs_messages)} vs "
                        f"{len(best_mongo)} msgs) → usando HubSpot para {phone_normalized}"
                    )
                    return hs_messages, "hubspot"

                if best_mongo:
                    return best_mongo, "mongodb"

            elif mongo_msgs:
                return mongo_msgs, "mongodb"

        except Exception as e:
            logger.error(f"[Panel] Error obteniendo mensajes en detail: {e}")
        return [], "none"

    # Ejecutar historial + window-status en paralelo (1 round-trip combinado)
    (messages, source), window_status = await asyncio.gather(
        _get_messages(),
        check_24h_window(phone_normalized),
    )

    # ⚠️ 2026-06-05: Metadata para que el panel pueda paginar con scroll infinito.
    # has_more=True si la página llegó al límite (puede haber más antes).
    # oldest_ts = timestamp del primer mensaje (cronológicamente más antiguo).
    # Si source='hubspot', has_more=False — HubSpot ya retorna todo lo disponible
    # y la paginación cursor solo funciona contra MongoDB.
    has_more = len(messages) >= limit and source == "mongodb"
    oldest_ts = messages[0].get("timestamp") if messages else None

    return {
        "phone": phone_normalized,
        "contact_id": contact_id,
        "canal": canal,
        # Historial
        "messages": messages,
        "message_count": len(messages),
        "message_source": source,
        "has_more": has_more,
        "oldest_ts": oldest_ts,
        # Ventana 24h
        "window_open": window_status.is_open,
        "last_message_time": window_status.last_message_time.isoformat() if window_status.last_message_time else None,
        "time_remaining_seconds": window_status.time_remaining_seconds,
        "requires_template": window_status.requires_template,
        "window_message": window_status.message,
    }


@router.get("/contacts/{phone}/hydrate")
async def hydrate_contact_endpoint(
    phone: str,
    canal: Optional[str] = Query(None, description="Canal preferido (hint) si se conoce"),
    ensure_panel: bool = Query(False, description="Si True, re-inyecta el contacto al panel (ZSET)"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Retorna un contacto completamente hidratado desde Redis + HubSpot + MongoDB.

    Complementa /contacts/{phone}/detail (historial + ventana 24h) — este endpoint
    retorna SOLO metadatos completos del contacto (nombre, owner, stage, canal, etc.)
    y NO filtra por advisor (flujo cross-advisor correcto).

    Usado por el frontend en deep links de HubSpot y recuperación de estado ghost.
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    hydrated = await _hydrate_contact(
        phone=phone,
        canal_hint=canal,
        add_to_zset=bool(ensure_panel),
    )
    if hydrated is None:
        raise HTTPException(status_code=404, detail="contacto no encontrado")
    return hydrated


@router.get("/conversations/{phone}")
@limiter.limit("60/minute")
async def get_conversation_history(
    request: Request,
    phone: str,
    # ⚠️ 2026-06-02: default bajado de 500 → 100 (carga inicial rápida).
    # Frontend usa scroll infinito con before_ts para cargar páginas siguientes.
    limit: int = Query(100, ge=1, le=500),
    canal: Optional[str] = Query(None, description="Canal para filtrar mensajes"),
    before_ts: Optional[str] = Query(
        None,
        description="Cursor ISO 8601 — retorna mensajes con timestamp < before_ts (paginación)"
    ),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Obtiene el historial de conversación de un contacto por teléfono.

    ARQUITECTURA v2.0 + paginación cursor-based (2026-06-02):
    1. Consultar MongoDB con filtro before_ts si presente (paginación).
    2. Si MongoDB vacío Y NO es página paginada → Fallback a HubSpot.
       En páginas paginadas (before_ts presente) NUNCA llamar HubSpot
       para evitar 25+ requests externos en un scroll.
    3. Retornar `has_more` y `oldest_ts` para que frontend pagine.
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

    # ⚠️ Parse del cursor before_ts → datetime
    before_ts_dt = None
    if before_ts:
        try:
            before_ts_dt = datetime.fromisoformat(before_ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail=f"before_ts inválido: {before_ts}")

    # ⚠️ Guard HubSpot fallback: solo permitido en primera página (sin cursor).
    hs_fallback_allowed = (before_ts_dt is None)

    try:
        # =====================================================================
        # PASO 1: MongoDB - Fuente de verdad en tiempo real
        # =====================================================================
        mongo_manager = get_mongo_manager()
        messages = await mongo_manager.get_history(
            phone=phone_normalized,
            limit=limit,
            channel=canal,
            before_ts=before_ts_dt,
        )

        if messages:
            source = "mongodb"
            logger.debug(f"[Panel] Historial desde MongoDB: {len(messages)} mensajes")

        # Fallback sin canal: lead de portal (mercado_libre/ciencuadras) → mensajes en channel=whatsapp
        if not messages and canal and canal not in ("whatsapp", "instagram"):
            messages = await mongo_manager.get_history(
                phone=phone_normalized,
                limit=limit,
                channel=None,
                before_ts=before_ts_dt,
            )
            if messages:
                source = "mongodb"

        # =====================================================================
        # PASO 2: Si MongoDB vacío Y es primera página → Fallback HubSpot
        # ⚠️ 2026-06-02: hs_fallback_allowed bloquea fallback en paginación
        # =====================================================================
        if not messages and hs_fallback_allowed:
            contact_manager = _get_contact_manager()
            contact_id = await contact_manager._search_contact(phone_normalized)

            if contact_id:
                timeline_logger = get_timeline_logger()
                messages = await timeline_logger.get_notes_for_contact(
                    contact_id=contact_id,
                    limit=limit
                )
                source = "hubspot"
                logger.debug(f"[Panel] Historial desde HubSpot (fallback): {len(messages)} mensajes")

        # ⚠️ Metadata de paginación
        has_more = len(messages) == limit  # si recibimos exactamente `limit`, puede haber más
        oldest_ts = None
        if messages:
            # mensajes vienen ordenados cronológicamente (más antiguo primero)
            oldest_ts = messages[0].get("timestamp")

        return {
            "phone": phone_normalized,
            "messages": messages,
            "count": len(messages),
            "source": source,
            "canal": canal,
            "has_more": has_more,
            "oldest_ts": oldest_ts,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Panel] Error obteniendo historial: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history/{contact_id}")
@limiter.limit("60/minute")
async def get_history_by_contact_id(
    request: Request,
    contact_id: str,
    # ⚠️ 2026-06-02: default 100 (carga inicial rápida). Frontend pagina con before_ts.
    limit: int = Query(100, ge=1, le=500),
    canal: Optional[str] = Query(None, description="Canal de origen para filtrar mensajes"),
    phone: Optional[str] = Query(None, description="Teléfono para buscar historial"),
    before_ts: Optional[str] = Query(
        None,
        description="Cursor ISO 8601 — retorna mensajes con timestamp < before_ts (paginación)"
    ),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Obtiene el historial de conversación por contact_id.

    Paginación cursor-based (2026-06-02):
    - Sin before_ts: trae últimos N mensajes (página inicial). HubSpot fallback
      activo si MongoDB tiene ≤20 mensajes.
    - Con before_ts: trae N mensajes con timestamp < before_ts. HubSpot fallback
      BLOQUEADO para evitar requests externos en cada scroll.
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

    # ⚠️ 2026-06-02: Parse del cursor before_ts
    before_ts_dt = None
    if before_ts:
        try:
            before_ts_dt = datetime.fromisoformat(before_ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return JSONResponse(
                status_code=400,
                content={"error": f"before_ts inválido: {before_ts}"}
            )

    # Guard HubSpot fallback: solo permitido en primera página (sin cursor).
    # En páginas paginadas evita 25+ requests a HubSpot durante un scroll continuo.
    hs_fallback_allowed = (before_ts_dt is None)

    try:
        mongo_manager = get_mongo_manager()

        # ⚠️ 2026-06-02: Lanzar HubSpot en background SOLO en primera página.
        # En páginas paginadas no llamamos HubSpot, así que no creamos la task.
        _hs_task = None
        if hs_fallback_allowed:
            _tl = get_timeline_logger()
            _hs_task = asyncio.create_task(
                _tl.get_notes_for_contact(contact_id=contact_id, limit=limit)
            )

        # =====================================================================
        # PASO 1: MongoDB por teléfono (preferido - más eficiente)
        # =====================================================================
        if phone:
            normalizer = PhoneNormalizer()
            validation = normalizer.normalize(phone)

            if validation.is_valid:
                # Sin filtro de canal: el canal con que el panel abre el chat
                # (canal_origen de HubSpot) puede diferir del canal con que se
                # guardan los mensajes (historical_channel de Redis).
                messages = await mongo_manager.get_history(
                    phone=validation.normalized,
                    limit=limit,
                    channel=None,
                    before_ts=before_ts_dt,
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
                limit=limit,
                before_ts=before_ts_dt,
            )
            if messages:
                source = "mongodb"
                logger.debug(f"[Panel] Historial desde MongoDB (contact_id): {len(messages)} msgs")

        # =====================================================================
        # PASO 3: HubSpot fallback — SOLO si es primera página (hs_fallback_allowed).
        # ⚠️ 2026-06-02: en paginación (before_ts presente) nunca tocamos HubSpot
        # para evitar 25+ requests externos durante scroll continuo.
        # =====================================================================
        hs_timeout = False  # P2-B: flag para indicar al frontend que HubSpot no respondió a tiempo
        if hs_fallback_allowed and _hs_task is not None and len(messages) <= 20:
            try:
                hs_messages = await asyncio.wait_for(_hs_task, timeout=15.0)
            except asyncio.TimeoutError:
                logger.warning(f"[Panel] HubSpot timeout (>15s) para contact_id={contact_id} — historial puede ser parcial")
                hs_messages = []
                hs_timeout = True
            except Exception as hs_err:
                logger.warning(f"[Panel] HubSpot task falló: {hs_err}")
                hs_messages = []
            if len(hs_messages) > len(messages):
                logger.info(
                    f"[Panel] HubSpot supera MongoDB ({len(hs_messages)} vs "
                    f"{len(messages)} msgs) → usando HubSpot para contact_id={contact_id}"
                )
                messages = hs_messages
                source = "hubspot"
        elif _hs_task is not None:
            # MongoDB ya tiene suficientes o es paginación → cancelar HubSpot para ahorrar recursos
            _hs_task.cancel()

        # Asegurar que messages sea una lista válida
        if messages is None:
            messages = []

        # ⚠️ Metadata de paginación
        has_more = len(messages) == limit
        oldest_ts = None
        if messages:
            # messages viene ordenado cronológicamente (más antiguo primero) tras formatBubbles
            # → primer elemento es el más antiguo de la página actual.
            oldest_ts = messages[0].get("timestamp")

        canal_info = f", canal={canal}" if canal else ""
        page_info = f", before_ts={before_ts}" if before_ts else ""
        logger.info(
            f"[Panel] Historial cargado: {len(messages)} msgs para contact_id={contact_id}"
            f"{canal_info}{page_info} (source={source}, has_more={has_more})"
        )

        return {
            "contact_id": contact_id,
            "messages": messages,
            "count": len(messages),
            "canal": canal,
            "phone": phone,
            "source": source,
            "hs_timeout": hs_timeout,
            "has_more": has_more,
            "oldest_ts": oldest_ts,
        }

    except Exception as e:
        logger.error(f"[Panel] Error obteniendo historial para {contact_id}: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
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
        state_manager = _get_state_manager()

        # Verificar estado actual
        current_status = await state_manager.get_status(phone_normalized, canal or "whatsapp")

        # Si ya está en HUMAN_ACTIVE o IN_CONVERSATION, solo refrescar TTL
        if current_status in [ConversationStatus.HUMAN_ACTIVE, ConversationStatus.IN_CONVERSATION]:
            # Refrescar TTL sin cambiar estado
            await state_manager.set_status(
                phone_normalized,
                current_status,
                canal=canal or "whatsapp",
                ttl=state_manager.HUMAN_PANEL_STATE_TTL
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
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        from database.mongodb_client import MongoDBManager

        logger.info(f"[Panel] Búsqueda por palabra clave: '{q}'")

        mongo_manager = MongoDBManager()
        matching_phones = await mongo_manager.search_messages_fulltext(q, limit=limit)

        logger.info(f"[Panel] Búsqueda '{q}': {len(matching_phones)} contactos encontrados")

        # Enriquecimiento UNIFICADO via _hydrate_contact — garantiza que cada contacto
        # venga con owner_id, owner_name, canal, current_stage, etc. (mismos campos que
        # GET /contacts). Rate-limit con Semaphore(3) para no saturar HubSpot.
        enriched_contacts = []
        if matching_phones:
            _sem = asyncio.Semaphore(3)

            async def _h(_p: str):
                async with _sem:
                    try:
                        return await _hydrate_contact(_p, canal_hint=None, add_to_zset=False)
                    except Exception as _he:
                        logger.debug(f"[Panel][Search] Hydrate {_p} falló: {_he}")
                        return None

            hydrated_list = await asyncio.gather(
                *[_h(p) for p in matching_phones[:limit]],
                return_exceptions=False,
            )
            enriched_contacts = [c for c in hydrated_list if c]

        return {
            "query": q,
            "count": len(matching_phones),
            "phones": matching_phones,
            "contacts": enriched_contacts,
        }

    except Exception as e:
        logger.error(f"[Panel] Error en búsqueda por palabra clave: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def _get_contacts_by_worker_filter(
    worker_id: str,
    advisor: Optional[str],
    limit: int,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> dict:
    """
    Branch del pipeline de contactos activado cuando se filtra por worker_id.
    """
    STAGES_VISIBLES_WORKER = {
        "marketingqualifiedlead",  # Visita agendada
        "customer",                # Cerrado ganado
        "1326623075",              # En Conversación (stage unificado)
    }

    mongo_mgr = get_mongo_manager()
    appointment_records = await mongo_mgr.get_contacts_by_worker(
        worker_id, date_from=date_from, date_to=date_to
    )

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

        state_manager = _get_state_manager()

    contacts_out = []
    for rec in unique_records[:limit]:
        cid = rec.get("contact_id", "")
        if not cid:
            continue

        # Leer lifecyclestage del Contact (con caché Redis)
        try:
            current_stage = await _get_contact_lifecyclestage(cid)
        except Exception:
            current_stage = None

        # Filtrar: solo etapas visibles (Visita agendada + Cerrado ganado)
        if current_stage not in STAGES_VISIBLES_WORKER:
            continue

        # Nombre desde HubSpot batch
        hs_info = batch_names.get(cid, {})
        display_name = f"{hs_info.get('firstname', '')} {hs_info.get('lastname', '')}".strip() or "Sin nombre"
        phone = hs_info.get("phone") or rec.get("phone", "")
        email = hs_info.get("email")

        # Filtro opcional por advisor (owner)
        owner_id = hs_info.get("hubspot_owner_id")
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
        last_activity = redis_meta.last_activity if (redis_meta and redis_meta.last_activity) else None

        # Calcular time_ago usando last_activity o appointment_dt como fallback
        from zoneinfo import ZoneInfo
        _tz = ZoneInfo("America/Bogota")
        _now = datetime.now(_tz)
        time_ago_str = ""
        ref_time = last_activity or rec.get("appointment_dt")
        if ref_time:
            try:
                if isinstance(ref_time, str):
                    ref_dt = datetime.fromisoformat(ref_time.replace("Z", "+00:00"))
                else:
                    ref_dt = ref_time
                if ref_dt.tzinfo is None:
                    ref_dt = ref_dt.replace(tzinfo=_tz)
                delta = _now - ref_dt.astimezone(_tz)
                secs = delta.total_seconds()
                local = ref_dt.astimezone(_tz)
                h = local.hour % 12 or 12
                t = f"{h}:{local.strftime('%M')} {'a.m.' if local.hour < 12 else 'p.m.'}"
                if secs < 3600:
                    time_ago_str = f"hace {int(secs // 60)} min"
                elif secs < 86400:
                    time_ago_str = f"hoy {t}"
                elif secs < 172800:
                    time_ago_str = f"ayer {t}"
                elif delta.days < 7:
                    _dias = ['lun','mar','mié','jue','vie','sáb','dom']
                    time_ago_str = f"{_dias[local.weekday()]} {t}"
                else:
                    _meses = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic']
                    time_ago_str = f"{local.day} {_meses[local.month - 1]}"
            except (ValueError, TypeError):
                time_ago_str = "en espera"

        contacts_out.append({
            "contact_id": cid,
            "phone": phone,
            "display_name": display_name,
            "email": email,
            "owner_id": owner_id,
            "current_stage": current_stage,
            "has_appointment": True,
            "appointment_dt": rec.get("appointment_dt"),
            "worker_name": rec.get("worker_name", ""),
            "conversation_status": conversation_status,
            "last_activity": last_activity,
            "is_active": conversation_status in ("HUMAN_ACTIVE", "IN_CONVERSATION", "BOT_ACTIVE"),
            "canal_origen": rec.get("canal", "whatsapp"),
            "time_ago": time_ago_str,
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
@limiter.limit("30/minute")
async def get_active_contacts(
    request: Request,
    filter_time: str = Query("24h", description="Filtro de tiempo: 24h, 48h, 1week, custom"),
    date_from: Optional[str] = Query(None, description="Fecha desde (ISO) para filtro custom"),
    date_to: Optional[str] = Query(None, description="Fecha hasta (ISO) para filtro custom"),
    advisor: Optional[str] = Query(None, description="Owner ID para filtrar contactos por asesora"),
    limit: int = Query(30, ge=1, le=100),
    page: int = Query(1, ge=1, description="Página (1-based) para paginación del ZSET"),
    worker_id: Optional[str] = Query(None, description="Worker ID para filtrar contactos por encargado de cita"),
    include_phone: Optional[str] = Query(None, description="Teléfono a incluir aunque no pertenezca al advisor (deep link cross-advisor)"),
    date_field: str = Query("last_activity", description="Campo de fecha para filtro custom: last_activity | created_at"),
    stage: Optional[str] = Query(None, description="Filtro por lifecyclestage — trae TODOS los contactos del owner en esa etapa (cap 500). Ignora limit/page."),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Retorna lista de contactos combinando dos fuentes:
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # ── Branch: filtro por etapa → delega a módulo aislado stage_filter ──────
    if stage and advisor:
        try:
            from middleware.stage_filter import get_contacts_by_stage_full
            _sm = _get_state_manager()
            _stage_result = await get_contacts_by_stage_full(stage, advisor, _sm)
            if worker_id:
                logger.info(f"[StageFilter] worker_id={worker_id} ignorado al combinar con stage={stage}")
            return {
                "contacts": _stage_result["contacts"],
                "total": _stage_result["total"],
                "page": 1,
                "limit": _stage_result["total"],
                "filter_mode": "stage_full",
                "max_reached": _stage_result["max_reached"],
                "from_cache": _stage_result["from_cache"],
            }
        except Exception as _stage_err:
            logger.error(
                f"[StageFilter] Error en branch stage={stage}, fallback al flujo ZSET: {_stage_err}",
                exc_info=True,
            )
            # Cae al flujo normal abajo

    try:
        from zoneinfo import ZoneInfo
        TIMEZONE = ZoneInfo("America/Bogota")

        now = datetime.now(TIMEZONE)

        # Helpers para time_ago en español (locale-agnostic)
        _DIAS_CORTOS  = ['lun','mar','mié','jue','vie','sáb','dom']
        _MESES_CORTOS = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic']

        def _fmt_time(dt_local):
            """'4:00 p.m.' en español sin depender del locale del sistema."""
            h = dt_local.hour % 12 or 12
            return f"{h}:{dt_local.strftime('%M')} {'a.m.' if dt_local.hour < 12 else 'p.m.'}"

        def _fmt_time_ago(ref_dt, delta):
            """Retorna string time_ago en español preciso."""
            secs = delta.total_seconds()
            local = ref_dt.astimezone(TIMEZONE)
            if secs < 3600:
                return f"hace {int(secs // 60)} min"
            if secs < 86400:
                return f"hoy {_fmt_time(local)}"
            if secs < 172800:
                return f"ayer {_fmt_time(local)}"
            if delta.days < 7:
                return f"{_DIAS_CORTOS[local.weekday()]} {_fmt_time(local)}"
            return f"{local.day} {_MESES_CORTOS[local.month - 1]}"

        # === BRANCH: Filtro por worker_id (citas) ===
        # Cuando worker_id está activo, el pipeline normal se omite.
        # Se buscan contactos desde MongoDB appointments y se filtran por etapa HubSpot.
        if worker_id:
            _wf_date_from: Optional[datetime] = None
            _wf_date_to: Optional[datetime] = None
            if date_from:
                try:
                    _wf_date_from = datetime.fromisoformat(date_from)
                    if _wf_date_from.tzinfo is None:
                        _wf_date_from = _wf_date_from.replace(tzinfo=TIMEZONE)
                except ValueError:
                    pass
            if date_to:
                try:
                    _wf_date_to = datetime.fromisoformat(date_to)
                    if _wf_date_to.tzinfo is None:
                        _wf_date_to = _wf_date_to.replace(tzinfo=TIMEZONE)
                except ValueError:
                    pass
            return await _get_contacts_by_worker_filter(
                worker_id=worker_id,
                advisor=advisor,
                limit=limit,
                date_from=_wf_date_from,
                date_to=_wf_date_to,
            )

        # === PASO 1: Obtener contactos ACTIVOS de Redis ===
        state_manager = _get_state_manager()

        # === CACHE: Respuesta completa en Redis (TTL 5s) para colapsar concurrent requests ===
        _cache_params = f"{advisor or ''}:{filter_time}:{date_from or ''}:{date_to or ''}:{date_field}:{page}:{limit}"
        _contacts_cache_key = f"contacts_resp:{hashlib.md5(_cache_params.encode()).hexdigest()}"
        try:
            _cached = await state_manager.redis.get(_contacts_cache_key)
            if _cached:
                logger.debug(f"[Panel] GET /contacts cache HIT para advisor={advisor or 'all'}")
                return json.loads(_cached)
        except Exception:
            pass  # Cache miss o error Redis → continuar con lógica normal

        if advisor:
            # Cuando hay filtro de advisor: escanear todo el ZSET para no perder contactos
            # del advisor en páginas posteriores (problema cuando todos tienen el mismo score,
            # e.g. tras restore-panel — Redis ordena lex inverso y los del advisor quedan en p2+).
            _all = await state_manager.get_all_human_active_contacts(limit=300, offset=0)
            advisor_contacts = [
                c for c in _all
                if c.get("owner_id") == advisor
                or advisor in (c.get("assigned_owner_ids") or [])
            ]
            logger.info(f"[Panel] Pre-filtrado por advisor {advisor}: {len(advisor_contacts)} contactos")

            # ⚠️ 2026-05-30: Fallback MongoDB — recupera conversaciones del owner que
            # ya no estén en el ZSET Redis (TTL expirado, ghost cleanup histórico,
            # eviction de Redis, etc.). Garantiza que el panel muestre TODAS las
            # conversaciones del asesor aunque tengan semanas/meses.
            try:
                _zset_phones = {c.get("phone") for c in advisor_contacts if c.get("phone")}
                _mongo_extra = await state_manager.get_archived_conversations_from_mongo(
                    owner_id=advisor,
                    limit=200,  # cap para no inflar la página
                    exclude_phones=_zset_phones,
                )
                if _mongo_extra:
                    advisor_contacts = advisor_contacts + _mongo_extra
                    logger.info(
                        f"[Panel][MongoFallback] +{len(_mongo_extra)} conversaciones desde MongoDB "
                        f"(owner={advisor}, total ahora {len(advisor_contacts)})"
                    )
            except Exception as _fb_err:
                logger.warning(f"[Panel] Fallback MongoDB falló (non-fatal): {_fb_err}")

            # Límite dinámico: siempre incluir TODOS los contactos con unread + top `limit` del resto.
            # Garantiza que si hay 40 contactos con mensajes nuevos todos se muestran sin importar el límite.
            _unread_phones = await state_manager.get_all_inbox_phones(advisor)
            _unread_set = [c for c in advisor_contacts if c.get("phone") in _unread_phones]
            _other_set  = [c for c in advisor_contacts if c.get("phone") not in _unread_phones]
            _other_offset = (page - 1) * limit
            active_contacts = _unread_set + _other_set[_other_offset:_other_offset + limit]
            total_for_advisor = len(advisor_contacts)
            logger.info(
                f"[Panel] Límite dinámico: {len(_unread_set)} unread (sin límite) + "
                f"{len(active_contacts) - len(_unread_set)} otros = {len(active_contacts)} total"
            )
        else:
            zset_offset = (page - 1) * limit
            active_contacts = await state_manager.get_all_human_active_contacts(limit=limit, offset=zset_offset)
            total_for_advisor = None
            logger.info(f"[Panel] Encontrados {len(active_contacts)} contactos activos en Redis")

        # ── Pre-calcular has_unread ANTES del sort para usarlo en el ordenamiento.
        # get_inbox_unread_map() es O(N) con 1 round-trip Redis (ZRANGE) → <5ms.
        # Fail-safe: si falla, _unread_map={} → sort cae al comportamiento por status.
        _unread_map = {}
        if advisor and active_contacts:
            try:
                _inbox_pre = [
                    {"phone": c.get("phone", ""), "canal": c.get("canal") or "whatsapp"}
                    for c in active_contacts if c.get("phone")
                ]
                _unread_map = await state_manager.get_inbox_unread_map(advisor, _inbox_pre)
                for c in active_contacts:
                    c["has_unread"] = _unread_map.get(c.get("phone", ""), False)
            except Exception as _pre_unread_err:
                logger.warning(f"[Panel] Error pre-calculando has_unread (non-fatal): {_pre_unread_err}")

        # ── Pre-limitar ANTES del enriquecimiento con HubSpot.
        # Los contactos de prioridad (ZSET) siempre van; del resto solo los más recientes.
        # Sin esto, 534 contactos × waits de 429 (36s c/u) = timeout de Railway.
        priority_contacts = [c for c in active_contacts if c.get("in_priority_zset", True)]
        bot_contacts = [c for c in active_contacts if not c.get("in_priority_zset", True)]

        # Ordenar con 3 tiers (estilo WhatsApp):
        #   Tier 0: has_unread=True → prioridad máxima (necesitan atención)
        #   Tier 1: IN_CONVERSATION / HUMAN_ACTIVE / PENDING_HANDOFF sin unread
        #   Tier 2: BOT_ACTIVE sin unread
        # Dentro de cada tier: más reciente primero (ISO string lexicográfico-correcto)
        def _status_priority(c):
            if c.get("has_unread", False):
                return 0
            s = c.get("status") or c.get("conversation_status") or ""
            return 1 if s in ("IN_CONVERSATION", "HUMAN_ACTIVE", "PENDING_HANDOFF") else 2

        # Stable sort: primero por last_activity desc, luego por tier asc
        # (Python sort es estable → dentro del mismo tier queda orden por actividad)
        priority_contacts.sort(key=lambda c: c.get("last_activity") or "", reverse=True)
        priority_contacts.sort(key=_status_priority)

        # Ordenar bot_contacts por última actividad (más reciente primero)
        def _sort_key(c):
            ts = c.get("last_activity") or ""
            return ts

        bot_contacts.sort(key=_sort_key, reverse=True)

        # Solo enriquecer los slots restantes hasta `limit`.
        # Los contactos con has_unread NO cuentan contra el límite (dynamic limit).
        _priority_no_unread = sum(1 for c in priority_contacts if not c.get("has_unread", False))
        remaining_slots = max(0, (limit * 2) - _priority_no_unread)
        active_contacts = priority_contacts + bot_contacts[:remaining_slots]
        logger.info(
            f"[Panel] Pre-limitado a {len(active_contacts)} contactos "
            f"({len(priority_contacts)} prioridad [{_priority_no_unread} sin unread] + "
            f"{len(bot_contacts[:remaining_slots])} bot) — pre-segregación"
        )

        # === PASO 6 (MOVIDO ANTES de enriquecimiento): SEGREGACIÓN ESTRICTA por equipo/portal ===
        # Filtrar ANTES de llamar a HubSpot para reducir llamadas API y evitar 429.
        # Los campos necesarios (canal_origen, owner_id, assigned_owner_ids) ya están desde Redis/MongoDB.
        if advisor:
            if advisor.lower() == "admin":
                logger.info(f"[Panel] Modo ADMIN: mostrando todos los {len(active_contacts)} contactos (sin segregación)")
            else:
                from integrations.hubspot.lead_assigner import LeadAssigner

                advisor_str = str(advisor)

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

                allowed_channels = set()
                for canal, team in LeadAssigner.CHANNEL_TO_TEAM.items():
                    if team == advisor_team:
                        allowed_channels.add(canal)
                if advisor_team == "default":
                    for canal, team in LeadAssigner.CHANNEL_TO_TEAM.items():
                        if team == "default":
                            allowed_channels.add(canal)

                all_advisor_ids = set()
                for team_members in LeadAssigner.OWNERS_CONFIG.values():
                    for member in team_members:
                        member_id = member.get("id")
                        if member_id:
                            all_advisor_ids.add(str(member_id))
                other_advisor_ids = all_advisor_ids - {advisor_str}

                filtered_contacts = []
                excluded_count = 0
                for contact in active_contacts:
                    canal_origen = (contact.get("canal_origen") or "").lower().strip()
                    contact_owner = contact.get("owner_id") or contact.get("assigned_owner_id") or contact.get("hubspot_owner_id")
                    contact_owner_str = str(contact_owner) if contact_owner else ""
                    assigned_owner_ids = contact.get("assigned_owner_ids") or []
                    assigned_owner_ids_str = [str(x) for x in assigned_owner_ids if x]

                    is_owner = contact_owner_str == advisor_str
                    is_collaborator = advisor_str in assigned_owner_ids_str

                    if is_owner or is_collaborator:
                        filtered_contacts.append(contact)
                        continue

                    if canal_origen:
                        if contact_owner_str and contact_owner_str in other_advisor_ids:
                            excluded_count += 1
                            continue
                        if canal_origen in allowed_channels:
                            filtered_contacts.append(contact)
                        else:
                            excluded_count += 1
                        continue

                    excluded_count += 1

                active_contacts = filtered_contacts
                logger.info(
                    f"[Panel] Segregación pre-enriquecimiento '{advisor_name}' ({advisor_team}): "
                    f"{len(active_contacts)} visibles, {excluded_count} excluidos. "
                    f"Canales: {sorted(allowed_channels)}"
                )

        # === PASO 1.5: Batch MongoDB — inyectar preview del último mensaje ===
        _phones_need_preview = [
            c.get("phone") for c in active_contacts
            if c.get("phone") and not c.get("last_message_preview")
        ]
        if _phones_need_preview:
            try:
                from database.mongodb_client import get_mongo_manager
                _previews = await get_mongo_manager().get_message_previews_batch(_phones_need_preview)
                for c in active_contacts:
                    _p = _previews.get(c.get("phone"))
                    if _p:
                        c.setdefault("last_message_preview", _p.get("last_message_preview", ""))
                        c.setdefault("last_message_sender", _p.get("last_message_sender", ""))
            except Exception as _prev_err:
                logger.warning(f"[Panel] Batch previews falló (non-fatal): {_prev_err}")

        # === PASO 2: Enriquecer contactos activos con HubSpot (OPTIMIZADO CON BATCH) ===
        contact_manager = _get_contact_manager()
        
        # ── PASO 2.1: Batch request para obtener nombres de contactos ──
        # Pre-check Redis cache (4h TTL) antes de llamar HubSpot — reduce 429s aún más
        all_contact_ids = [
            c.get("contact_id") for c in active_contacts
            if c.get("contact_id")
        ]

        redis_name_cache: Dict[str, Dict[str, Any]] = {}
        ids_needing_fetch: list = []
        for cid in all_contact_ids:
            cached = await _get_cached_contact_name(cid)
            if cached:
                redis_name_cache[cid] = cached
            else:
                ids_needing_fetch.append(cid)

        batch_contact_data = {}
        if ids_needing_fetch:
            batch_contact_data = await _hubspot_batch_get_contacts(ids_needing_fetch)
            logger.info(
                f"[Panel] Batch pre-fetch: {len(batch_contact_data)} contactos de HubSpot "
                f"(cache hits: {len(redis_name_cache)})"
            )
        elif redis_name_cache:
            logger.debug(f"[Panel] Todos los nombres desde cache Redis ({len(redis_name_cache)} contactos)")

        # Merge cache en batch_contact_data para uso uniforme en _enrich_single_contact
        for cid, nd in redis_name_cache.items():
            if cid not in batch_contact_data:
                batch_contact_data[cid] = {
                    "firstname": nd.get("firstname", ""),
                    "lastname": nd.get("lastname", ""),
                    "email": None,
                    "phone": None,
                    "lifecyclestage": "",
                    "hubspot_owner_id": "",
                }

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
                        # Solo cachear si vino de HubSpot (no del cache propio)
                        if cid not in redis_name_cache:
                            asyncio.create_task(_cache_contact_name(
                                cid, hs_info.get("firstname", ""), hs_info.get("lastname", "")
                            ))
                    elif not contact.get("display_name") or contact.get("display_name") in ("Cliente Nuevo", "Sin nombre"):
                        contact["display_name"] = "Sin nombre"
                    contact["email"] = hs_info.get("email")
                # Si no estaba en batch, conservar display_name de Redis

                # Leer lifecyclestage — preferir batch (0 llamadas extra) → fallback individual
                if not contact.get("current_stage"):
                    batch_stage = batch_contact_data.get(cid, {}).get("lifecyclestage")
                    if batch_stage:
                        contact["current_stage"] = batch_stage
                        asyncio.create_task(_cache_contact_stage(cid, batch_stage))
                    else:
                        try:
                            contact["current_stage"] = await _get_contact_lifecyclestage(cid)
                        except Exception as e:
                            logger.debug(f"[Panel] No se pudo obtener lifecyclestage: {e}")
                            contact["current_stage"] = HUBSPOT_STAGE_EN_CONVERSACION

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
            if since.tzinfo is None:
                since = since.replace(tzinfo=TIMEZONE)
            until = datetime.fromisoformat(date_to) if date_to else now
            if until.tzinfo is None:
                until = until.replace(tzinfo=TIMEZONE)
        else:
            since = now - timedelta(hours=24)
            until = now

        # === PASO 3.5: Filtrar contactos por fecha, PERO siempre incluir los activos ===
        # REGLA: En modos de ventana deslizante (24h/48h/1week), los contactos en
        # HUMAN_ACTIVE/PENDING_HANDOFF/IN_CONVERSATION siempre se muestran (bypass PRIORIDAD 1).
        # En modo fecha específica (custom), TODOS los contactos se filtran por fecha — sin bypass.
        if filter_time != "all":
            filtered_active = []
            for contact in active_contacts:
                # PRIORIDAD 1: Bypass solo en modos de ventana deslizante (no "custom").
                # Nota: is_active=True está hardcoded para TODOS los contactos de Redis
                # (conversation_state.py:316,377) — no puede usarse como condición de bypass
                # porque haría que PRIORIDAD 2 (filtro por fecha) NUNCA se ejecute.
                status = contact.get("conversation_status") or contact.get("status") or ""
                is_human_active = status in ["HUMAN_ACTIVE", "PENDING_HANDOFF", "IN_CONVERSATION"]

                if is_human_active and filter_time != "custom":
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
                            contact["time_ago"] = _fmt_time_ago(ref_dt, time_ago)
                        except (ValueError, TypeError):
                            contact["time_ago"] = "en espera"
                    else:
                        contact["time_ago"] = "en espera"

                    filtered_active.append(contact)
                    logger.debug(f"[Panel] Contacto {contact.get('phone')} incluido (activo/en espera)")
                    continue

                # PRIORIDAD 2: Para contactos no activos, filtrar por el campo elegido
                # date_field=created_at → filtrar por fecha de llegada; default → last_activity
                if date_field == "created_at":
                    ref_time = contact.get("activated_at") or contact.get("last_activity")
                else:
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
                            contact["time_ago"] = _fmt_time_ago(ref_dt, time_ago)
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

        # === [Sync] Deep link cross-advisor: incluir contacto aunque no pertenezca al advisor ===
        # Hidratación completa via _hydrate_contact — garantiza que el frontend reciba
        # display_name, contact_id, canal, owner, stage reales (NO skeleton parcial).
        if include_phone:
            _ip_norm = include_phone.strip()
            if not any(c.get("phone") == _ip_norm for c in active_contacts):
                try:
                    hydrated = await _hydrate_contact(
                        _ip_norm, canal_hint=None, add_to_zset=False
                    )
                    if hydrated:
                        # Marcar como cross_advisor si el owner real no coincide con el advisor actual
                        hydrated["cross_advisor"] = bool(
                            advisor
                            and hydrated.get("owner_id")
                            and str(hydrated.get("owner_id")) != str(advisor)
                        )
                        active_contacts.insert(0, hydrated)
                        logger.info(
                            f"[Sync] include_phone={_ip_norm} hidratado "
                            f"(owner={hydrated.get('owner_id')}, "
                            f"name='{hydrated.get('display_name')}', "
                            f"cross_advisor={hydrated.get('cross_advisor')})"
                        )
                    else:
                        logger.warning(
                            f"[Sync] _hydrate_contact({_ip_norm}) retornó None "
                            f"— contacto no existe en Redis/HubSpot"
                        )
                except Exception as _he:
                    logger.warning(f"[Sync] Error hidratando include_phone={_ip_norm}: {_he}")

        # === PASO 7: El orden ya viene correcto del ZSET (por last_activity descendente) ===
        # NO reordenar por activated_at porque destruye el orden de "actividad reciente primero"
        contacts_sorted = active_contacts  # Mantener orden del backend

        # === PASO 7.5: Marcar contactos con citas activas (una sola query MongoDB) ===
        # FIX Bug5: se consultan TODOS los contact_ids, no solo los de la primera página.
        # Antes: contacts_sorted[:limit] → contactos en posición >limit nunca obtenían badge.
        try:
            mongo_mgr = get_mongo_manager()
            all_contact_ids = [
                c.get("contact_id", "") for c in contacts_sorted
                if c.get("contact_id")
            ]
            contacts_with_appts = await mongo_mgr.get_contacts_with_appointments(all_contact_ids)
            logger.debug(
                f"[Badge] Appointment query: {len(all_contact_ids)} contactos revisados, "
                f"{len(contacts_with_appts)} con cita activa"
            )
            for c in contacts_sorted:
                c["has_appointment"] = c.get("contact_id", "") in contacts_with_appts
        except Exception as appt_err:
            logger.warning(f"[Badge] Error verificando citas activas: {appt_err}")
            for c in contacts_sorted:
                c["has_appointment"] = False

        # === PASO 7.6: Calcular has_unread desde advisor_inbox ===
        # Reutiliza _unread_map pre-calculado antes del sort (evita 2do round-trip Redis).
        # Si _unread_map está vacío (error en pre-cálculo o branch sin advisor), recalcular.
        if advisor and contacts_sorted:
            try:
                if not _unread_map:
                    _inbox_contacts = [
                        {
                            "phone": c.get("phone", ""),
                            "canal": c.get("canal") or "whatsapp",
                        }
                        for c in contacts_sorted if c.get("phone")
                    ]
                    _unread_map = await state_manager.get_inbox_unread_map(advisor, _inbox_contacts)
                for c in contacts_sorted:
                    c["has_unread"] = _unread_map.get(c.get("phone", ""), False)
                logger.info(
                    f"[Panel] has_unread calculado para {advisor}: "
                    f"{sum(1 for c in contacts_sorted if c.get('has_unread'))} no leídos"
                )
            except Exception as _ue:
                logger.warning(f"[Panel] Error calculando has_unread (non-fatal): {_ue}")
                for c in contacts_sorted:
                    c["has_unread"] = False

        # active_count = solo contactos ESPERANDO respuesta (HUMAN_ACTIVE / PENDING_HANDOFF)
        # IN_CONVERSATION no cuenta: ya están siendo atendidos
        waiting_statuses = {"HUMAN_ACTIVE", "PENDING_HANDOFF"}
        active_count = len([
            c for c in contacts_sorted
            if c.get("conversation_status") in waiting_statuses
        ])

        _unread_in_final = [c for c in contacts_sorted if c.get("has_unread", False)]
        _read_in_final = [c for c in contacts_sorted if not c.get("has_unread", False)]
        _dynamic_result = _unread_in_final + _read_in_final[:limit]

        logger.info(
            f"[Panel] Retornando {len(_dynamic_result)} contactos "
            f"({len(_unread_in_final)} unread + {len(_read_in_final[:limit])} leidos, "
            f"activos: {active_count}, advisor: {advisor})"
        )

        _response_data = {
            "contacts": _dynamic_result,
            "filter": filter_time,
            "advisor": advisor,
            "active_count": active_count,
            "historical_count": len(_dynamic_result) - active_count,
            "total_count": total_for_advisor if total_for_advisor is not None else len(contacts_sorted),
            "page": page,
            "limit": limit,
            "since": since.isoformat(),
            "until": until.isoformat()
        }
        try:
            await state_manager.redis.set(
                _contacts_cache_key, json.dumps(_response_data, default=str), ex=5
            )
        except Exception:
            pass  # No bloquear la respuesta si el cache write falla
        return _response_data

    except Exception as e:
        logger.error(f"[Panel] Error obteniendo contactos: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/diagnose")
async def diagnose_system(x_api_key: str = Header(None, alias="X-API-Key")):
    """
    Endpoint de diagnóstico: estado de Redis + estimación de carga HubSpot.
    Solo para administradores. Llamar con:
      curl -H "X-API-Key: <ADMIN_API_KEY>" https://<app>/whatsapp/panel/diagnose
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    import json as _json
    from datetime import timezone as _tz

    result = {}
    try:
        redis_client = await _get_redis_client()

        # ── Contactos activos ──────────────────────────────────────
        total_zset = await redis_client.zcard("active_conversations_sorted")
        all_entries = await redis_client.zrange("active_conversations_sorted", 0, -1, withscores=True)

        status_counts: dict = {}
        with_contact_id = 0
        without_contact_id = 0
        for phone, score in all_entries:
            meta_raw = await redis_client.get(f"conv_meta:{phone}")
            if meta_raw:
                try:
                    meta = _json.loads(meta_raw)
                    st = meta.get("status", "UNKNOWN")
                    status_counts[st] = status_counts.get(st, 0) + 1
                    if meta.get("contact_id"):
                        with_contact_id += 1
                    else:
                        without_contact_id += 1
                except Exception:
                    status_counts["PARSE_ERROR"] = status_counts.get("PARSE_ERROR", 0) + 1
            else:
                status_counts["NO_META"] = status_counts.get("NO_META", 0) + 1

        result["contacts"] = {
            "total_zset": total_zset,
            "by_status": status_counts,
            "with_contact_id": with_contact_id,
            "without_contact_id": without_contact_id,
        }

        # ── Caché HubSpot en Redis ─────────────────────────────────
        stage_keys = len([k async for k in redis_client.scan_iter("contact_stage:*")])
        name_keys  = len([k async for k in redis_client.scan_iter("contact_name:*")])
        assoc_keys = len([k async for k in redis_client.scan_iter("hs_assoc:*")])
        idem_keys  = len([k async for k in redis_client.scan_iter("hs_note_processed:*")])

        result["hubspot_cache"] = {
            "contact_stage_keys": stage_keys,
            "contact_name_keys":  name_keys,
            "hs_assoc_keys":      assoc_keys,
            "note_idempotency_keys": idem_keys,
        }

        # ── Estimación de carga HubSpot ────────────────────────────
        contacts_needing_stage = max(0, with_contact_id - stage_keys)
        contacts_needing_assoc = max(0, total_zset - assoc_keys)
        worst_case_calls = 1 + contacts_needing_stage + contacts_needing_assoc * 2
        result["hubspot_load_estimate"] = {
            "contacts_needing_stage_fetch": contacts_needing_stage,
            "contacts_needing_assoc_fetch": contacts_needing_assoc,
            "worst_case_calls_per_panel_load": worst_case_calls,
            "hubspot_limit_per_10s": 50,
            "exceeds_limit": worst_case_calls > 50,
            "over_by": max(0, worst_case_calls - 50),
        }

        # ── Info Redis ─────────────────────────────────────────────
        info_clients = await redis_client.info("clients")
        info_stats   = await redis_client.info("stats")
        info_mem     = await redis_client.info("memory")
        result["redis"] = {
            "connected_clients":   info_clients.get("connected_clients"),
            "blocked_clients":     info_clients.get("blocked_clients"),
            "rejected_connections_ever": info_stats.get("rejected_connections"),
            "memory_used":         info_mem.get("used_memory_human"),
        }

        # ── Últimos 5 contactos activos ────────────────────────────
        recent = await redis_client.zrange("active_conversations_sorted", -5, -1, withscores=True)
        recent_out = []
        for phone, score in reversed(recent):
            meta_raw = await redis_client.get(f"conv_meta:{phone}")
            status, cid = "?", "?"
            if meta_raw:
                try:
                    m = _json.loads(meta_raw)
                    status = m.get("status", "?")
                    cid = m.get("contact_id", "sin_id")
                except Exception:
                    pass
            ts = datetime.fromtimestamp(score, tz=_tz.utc).strftime("%m/%d %H:%M")
            recent_out.append({"ts": ts, "phone_last10": phone[-10:], "status": status, "contact_id": cid})
        result["recent_contacts"] = recent_out

        result["timestamp"] = get_bogota_now().isoformat()
        return result

    except Exception as e:
        logger.error(f"[Panel][Diagnose] Error: {e}", exc_info=True)
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

        client = get_httpx_client()
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


class ScheduledMessageCreateRequest(BaseModel):
    template_sid: str = Field(..., description="Content SID de la plantilla Twilio (ej. HX...)")
    template_name: str = Field(..., description="Nombre legible de la plantilla")
    template_variables: dict = Field(..., description="Variables de la plantilla, ej. {'1': 'Carlos'}")
    scheduled_dt: str = Field(..., description="Fecha y hora de envío en ISO 8601 (naive = hora Colombia)")
    advisor_id: Optional[str] = Field(None, description="HubSpot owner ID de la asesora")
    canal: str = Field("whatsapp", description="Canal del contacto")
    notes: str = Field("", description="Nota interna opcional")


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
        hs_client = _hs_singleton
        hubspot_note_id = await hs_client.create_note(
            contact_id=contact_id,
            body=note_body,
            owner_id=body.advisor_id
        )
        logger.info(f"[Panel] Nota de cita creada en HubSpot: {hubspot_note_id}")
    except Exception as hs_err:
        logger.warning(f"[Panel] No se pudo crear nota en HubSpot para cita: {hs_err}")
        # No falla el endpoint — la cita se guarda en MongoDB de todas formas

    # Obtener phone y firstname del contacto para guardar en MongoDB y Redis
    phone = ""
    contact_firstname = ""
    try:
        hs_info = await _get_hubspot_contact_info(contact_id)
        if hs_info:
            phone = hs_info.get("phone", "")
            contact_firstname = (hs_info.get("firstname") or "").strip()
    except Exception:
        pass
    if contact_firstname:
        logger.info(f"[Naming] Nombre resuelto desde HubSpot: '{contact_firstname}' para contact_id={contact_id}")
    else:
        logger.info(f"[Naming] Sin firstname en HubSpot para contact_id={contact_id} — se usará fallback en scheduler")

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
        hubspot_note_id=hubspot_note_id,
        contact_name=contact_firstname or None,
        canal=body.canal,
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
            try:
                await apt_manager.create_appointment(
                    phone_normalized=phone_normalized,
                    canal=body.canal,
                    scheduled_datetime=appt_dt_bogota,
                    contact_name=contact_firstname or None,
                    contact_id=contact_id,
                    notes=body.notes or None,
                    advisor_id=body.advisor_id or None,
                )
                logger.info(
                    f"[Panel] Cita sincronizada a Redis: phone={phone_normalized}, canal={body.canal}, "
                    f"dt={appt_dt_bogota.isoformat()}"
                )
            finally:
                await apt_manager.close()
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
    """Cancela una cita existente y sincroniza Redis para que el scheduler no la envíe."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()

    # Leer antes de cancelar para obtener phone (canal no se guarda en MongoDB → default whatsapp)
    apt_doc = await mongo_mgr.get_appointment_by_id(appointment_id)

    ok = await mongo_mgr.cancel_appointment(appointment_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Cita no encontrada")

    # Sincronizar Redis — evita que el scheduler envíe recordatorio de cita cancelada
    if apt_doc and apt_doc.get("phone"):
        try:
            from middleware.appointment_manager import AppointmentManager
            redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
            apt_mgr = AppointmentManager(redis_url)
            try:
                await apt_mgr.cancel_appointment(apt_doc["phone"], apt_doc.get("canal", "whatsapp"))
                logger.info("[Panel] Cita cancelada en Redis: %s", apt_doc["phone"])
            finally:
                await apt_mgr.close()
        except Exception as redis_err:
            logger.error("[Panel] Error sincronizando cancelación en Redis: %s", redis_err)

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

    # Sync Redis si cambió appointment_dt — actualiza ZSET score y resetea flags
    # para que el scheduler use la nueva ventana de recordatorio/seguimiento
    if appt_dt:
        try:
            apt_doc = await mongo_mgr.get_appointment_by_id(appointment_id)
            if apt_doc and apt_doc.get("phone"):
                from middleware.appointment_manager import AppointmentManager
                redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
                apt_mgr = AppointmentManager(redis_url)
                try:
                    rescheduled = await apt_mgr.reschedule_appointment(
                        apt_doc["phone"],
                        apt_doc.get("canal", "whatsapp"),
                        appt_dt
                    )
                finally:
                    await apt_mgr.close()
                if rescheduled:
                    logger.info("[Panel] Cita reprogramada en Redis: %s → %s", apt_doc["phone"], appt_dt)
                else:
                    logger.warning("[Panel] Cita no encontrada en Redis para reprogramar: %s", apt_doc["phone"])
        except Exception as redis_err:
            logger.warning("[Panel] Error sync Redis en reprogramación (non-fatal): %s", redis_err)

    return {"ok": True}


@router.delete("/appointments/{appointment_id}")
async def delete_appointment(
    appointment_id: str,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Elimina una cita permanentemente y limpia Redis para que el scheduler no la envíe."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()

    # Leer antes de borrar para obtener phone (canal no se guarda en MongoDB → default whatsapp)
    apt_doc = await mongo_mgr.get_appointment_by_id(appointment_id)

    ok = await mongo_mgr.delete_appointment(appointment_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Cita no encontrada")

    # Limpiar Redis — evita que el scheduler envíe recordatorio de cita eliminada
    if apt_doc and apt_doc.get("phone"):
        try:
            from middleware.appointment_manager import AppointmentManager
            redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
            apt_mgr = AppointmentManager(redis_url)
            try:
                await apt_mgr.cancel_appointment(apt_doc["phone"], apt_doc.get("canal", "whatsapp"))
                logger.info("[Panel] Cita eliminada de Redis: %s", apt_doc["phone"])
            finally:
                await apt_mgr.close()
        except Exception as redis_err:
            logger.error("[Panel] Error limpiando cita eliminada en Redis: %s", redis_err)

    return {"ok": True}


# ============================================================================
# Notas Internas de Contacto
# ============================================================================

class NoteCreateBody(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)
    advisor_id: str = ""
    advisor_name: str = ""
    phone: str = ""

class NoteUpdateBody(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)


@router.get("/contacts/{contact_id}/notes")
async def get_contact_notes(
    contact_id: str,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Lista notas internas activas de un contacto."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    notes = await mongo_mgr.get_notes(contact_id)
    return {"notes": notes, "total": len(notes)}


@router.post("/contacts/{contact_id}/notes", status_code=201)
async def create_contact_note(
    contact_id: str,
    body: NoteCreateBody,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Crea una nota interna para un contacto."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    note_id = await mongo_mgr.create_note(
        contact_id=contact_id,
        content=body.content,
        advisor_id=body.advisor_id,
        advisor_name=body.advisor_name,
        phone=body.phone,
    )
    if not note_id:
        raise HTTPException(status_code=500, detail="Error al crear la nota")
    notes = await mongo_mgr.get_notes(contact_id)
    return {"ok": True, "note_id": note_id, "notes": notes}


@router.patch("/contacts/{contact_id}/notes/{note_id}")
async def update_contact_note(
    contact_id: str,
    note_id: str,
    body: NoteUpdateBody,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Actualiza el contenido de una nota interna."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    ok = await mongo_mgr.update_note(note_id, body.content)
    if not ok:
        raise HTTPException(status_code=404, detail="Nota no encontrada")
    notes = await mongo_mgr.get_notes(contact_id)
    return {"ok": True, "notes": notes}


@router.delete("/contacts/{contact_id}/notes/{note_id}")
async def delete_contact_note(
    contact_id: str,
    note_id: str,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Elimina (soft delete) una nota interna."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    ok = await mongo_mgr.soft_delete_note(note_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Nota no encontrada")
    notes = await mongo_mgr.get_notes(contact_id)
    return {"ok": True, "notes": notes}


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

    return templates.TemplateResponse(request, "index.html", {
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
    También remueve del inbox y limpia notificaciones de inactividad del contacto.
    """
    try:
        state_manager = _get_state_manager()
        await state_manager.update_advisor_message_timestamp(phone_normalized, canal)
        logger.info(f"[Panel] ✓ Timestamp asesor actualizado: {phone_normalized}:{canal or 'default'}")
        meta = await state_manager.get_meta(phone_normalized, canal or "whatsapp")
        if meta and meta.assigned_owner_id:
            # Inbox: el asesor está enviando → ya vio el contacto → remover del inbox
            await state_manager.remove_from_advisor_inbox(
                meta.assigned_owner_id, phone_normalized, canal or "whatsapp"
            )
            # Notificaciones: asesor respondió → limpiar alertas de inactividad 24h
            await state_manager.clear_inactivity_notifications_for_contact(
                meta.assigned_owner_id, phone_normalized
            )
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
        client = get_httpx_client()
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

            # Por día — convertir a zona Bogotá para que el gráfico coincida con
            # la percepción local (leads de 7pm-11pm no aparecen al día siguiente)
            createdate = props.get("createdate")
            if createdate:
                try:
                    dt_utc = datetime.fromisoformat(createdate.replace("Z", "+00:00"))
                    dt_bog = dt_utc.astimezone(TIMEZONE)
                    day_key = dt_bog.strftime("%Y-%m-%d")
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
            "_contacts_by_channel": dict(contacts_by_channel),  # Interno: solo usado por export-excel
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
    contacts_by_channel = metrics_data.get("_contacts_by_channel", {})

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
            contacts_by_channel = metrics_data.get('_contacts_by_channel', {})

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
                        canal_data.append({
                            'Fecha': format_date_excel(c.get('fecha', '')),
                            'Nombre': c.get('nombre', 'Sin nombre'),
                            'Teléfono': c.get('telefono', 'Sin teléfono'),
                            'Motivo': c.get('motivo', 'Consulta general'),
                            'Status': c.get('status', 'Lead'),
                            'Score': c.get('score', '-'),
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
        from urllib.parse import quote as _url_quote
        output.seek(0)
        filename = f"metricas_redes_{days}d_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        content_disposition = (
            f'attachment; filename="{filename}"; '
            f"filename*=UTF-8''{_url_quote(filename)}"
        )

        return Response(
            content=output.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": content_disposition}
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Metrics] Error exportando Excel: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error generando Excel: {str(e)}")


# ============================================================================
# MÉTRICAS DE CITAS REALIZADAS (dashboard mercadeo)
# ============================================================================

@router.get("/metrics/appointments")
async def get_appointments_metrics(
    date_from: str = Query(..., description="YYYY-MM-DD (Bogotá)"),
    date_to: str = Query(..., description="YYYY-MM-DD (Bogotá)"),
    worker_id: Optional[str] = Query(None, description="ID del trabajador de campo"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Métricas de citas completadas — para dashboard mercadeo.
    Filtra por status='completed' y visit_completed_at en rango.
    Segmenta por worker_id (trabajador de campo que realizó la visita).
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        from zoneinfo import ZoneInfo
        from collections import defaultdict
        TZ = ZoneInfo("America/Bogota")

        try:
            from_dt = datetime.fromisoformat(date_from).replace(tzinfo=TZ)
            to_dt = (datetime.fromisoformat(date_to).replace(tzinfo=TZ)
                     + timedelta(days=1))
        except ValueError:
            raise HTTPException(status_code=400, detail="Formato de fecha inválido. Usa YYYY-MM-DD")

        if from_dt >= to_dt:
            raise HTTPException(status_code=400, detail="date_from debe ser < date_to")

        period_days = (to_dt - from_dt).days
        if period_days > 90:
            raise HTTPException(status_code=400, detail="Rango máximo: 90 días")

        mongo = get_mongo_manager()
        appointments = await mongo.get_completed_appointments(
            from_dt, to_dt, worker_id
        )

        total = len(appointments)
        by_day: Dict[str, int] = defaultdict(int)
        by_worker: Dict[str, int] = defaultdict(int)
        details = []
        now_bog = datetime.now(TZ)
        week_threshold = now_bog - timedelta(days=7)
        week_count = 0

        for apt in appointments:
            completed_at = apt.get("visit_completed_at")
            if completed_at is None:
                continue
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=ZoneInfo("UTC"))
            dt_bog = completed_at.astimezone(TZ)
            day_key = dt_bog.strftime("%Y-%m-%d")
            by_day[day_key] += 1

            if dt_bog >= week_threshold:
                week_count += 1

            worker_name = apt.get("worker_name") or "Sin asignar"
            by_worker[worker_name] += 1

            apt_dt = apt.get("appointment_dt")
            if apt_dt and apt_dt.tzinfo is None:
                apt_dt = apt_dt.replace(tzinfo=ZoneInfo("UTC"))

            details.append({
                "fecha_cita": apt_dt.astimezone(TZ).isoformat() if apt_dt else "",
                "nombre": apt.get("contact_name") or "Sin nombre",
                "telefono": apt.get("phone", ""),
                "asesor": worker_name,
                "canal": apt.get("canal") or "whatsapp",
            })

        # Delta vs período anterior
        prev_from = from_dt - timedelta(days=period_days)
        prev_to = from_dt
        prev_count = await mongo.count_completed_appointments(
            prev_from, prev_to, worker_id
        )
        if prev_count > 0:
            delta_pct = round((total - prev_count) / prev_count * 100, 1)
        else:
            delta_pct = None

        avg_per_day = round(total / period_days, 1) if period_days else 0

        return {
            "period": {"from": date_from, "to": date_to, "days": period_days},
            "total": total,
            "delta_pct": delta_pct,
            "week_count": week_count,
            "avg_per_day": avg_per_day,
            "by_day": dict(sorted(by_day.items())),
            "by_worker": dict(by_worker),
            "_details": details,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[AppointmentsMetrics] Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/metrics/appointments/export-excel")
async def export_appointments_excel(
    date_from: str = Query(..., description="YYYY-MM-DD"),
    date_to: str = Query(..., description="YYYY-MM-DD"),
    worker_id: Optional[str] = Query(None),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Exporta citas completadas a Excel para el área de mercadeo.
    3 hojas: Resumen, Citas (5 columnas), Por Trabajador (si >5 citas).
    """
    from fastapi.responses import Response
    from urllib.parse import quote as _url_quote
    import pandas as pd

    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        metrics_data = await get_appointments_metrics(
            date_from=date_from,
            date_to=date_to,
            worker_id=worker_id,
            x_api_key=x_api_key,
        )

        output = BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            workbook = writer.book
            header_format = workbook.add_format({
                'bold': True, 'font_color': 'white', 'bg_color': '#1F4E79',
                'border': 1, 'align': 'center', 'valign': 'vcenter',
                'text_wrap': True,
            })
            title_format = workbook.add_format({
                'bold': True, 'font_size': 14, 'font_color': 'white',
                'bg_color': '#1F4E79', 'align': 'center', 'valign': 'vcenter',
            })

            # Hoja 1: Resumen
            delta_display = (
                f"{metrics_data['delta_pct']:+.1f}%"
                if metrics_data.get('delta_pct') is not None else "N/A"
            )
            summary_data = {
                'Métrica': [
                    'Período Analizado',
                    'Total Citas Realizadas',
                    'Esta Semana',
                    'Promedio por Día',
                    'Δ vs. Período Anterior',
                ],
                'Valor': [
                    f"{date_from} a {date_to}",
                    metrics_data['total'],
                    metrics_data['week_count'],
                    metrics_data['avg_per_day'],
                    delta_display,
                ],
            }
            df_summary = pd.DataFrame(summary_data)
            df_summary.to_excel(writer, sheet_name='Resumen', index=False, startrow=1)
            ws_summary = writer.sheets['Resumen']
            ws_summary.merge_range(
                'A1:B1',
                'RESUMEN DE CITAS REALIZADAS',
                title_format,
            )
            ws_summary.set_column('A:A', 28)
            ws_summary.set_column('B:B', 25)
            ws_summary.freeze_panes(2, 0)
            for col_num, col_name in enumerate(df_summary.columns):
                ws_summary.write(1, col_num, col_name, header_format)

            # Hoja 2: Citas (4 columnas)
            details = metrics_data.get('_details', [])
            if details:
                rows = [{
                    'Fecha Cita': _format_datetime_bogota(d.get('fecha_cita', '')),
                    'Nombre': d.get('nombre', 'Sin nombre'),
                    'Teléfono': d.get('telefono', ''),
                    'Asesor': d.get('asesor', 'Sin asignar'),
                } for d in details]
                df_citas = pd.DataFrame(rows)
                df_citas.to_excel(writer, sheet_name='Citas', index=False)
                ws_citas = writer.sheets['Citas']
                for col_num, col_name in enumerate(df_citas.columns):
                    ws_citas.write(0, col_num, col_name, header_format)
                    try:
                        max_len = max(
                            df_citas[col_name].astype(str).map(len).max(),
                            len(col_name),
                        ) + 2
                        ws_citas.set_column(col_num, col_num, min(max_len, 40))
                    except Exception:
                        ws_citas.set_column(col_num, col_num, 18)
                ws_citas.freeze_panes(1, 0)
                ws_citas.autofilter(0, 0, len(df_citas), len(df_citas.columns) - 1)

                # Hojas por asesor si >5 citas
                # Hojas por trabajador de campo (si >5 citas)
                by_worker_groups: Dict[str, list] = {}
                for row in rows:
                    by_worker_groups.setdefault(row['Asesor'], []).append(row)
                for worker_name, worker_rows in by_worker_groups.items():
                    if len(worker_rows) > 5:
                        sheet_name = (worker_name or "Sin asignar")[:31]
                        df_worker = pd.DataFrame(worker_rows)
                        df_worker.to_excel(writer, sheet_name=sheet_name, index=False)
                        ws_worker = writer.sheets[sheet_name]
                        for col_num, col_name in enumerate(df_worker.columns):
                            ws_worker.write(0, col_num, col_name, header_format)
                            ws_worker.set_column(col_num, col_num, 22)
                        ws_worker.freeze_panes(1, 0)
                        ws_worker.autofilter(
                            0, 0, len(df_worker), len(df_worker.columns) - 1
                        )

        output.seek(0)
        filename = (
            f"citas_realizadas_{date_from}_a_{date_to}_"
            f"{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        )
        content_disposition = (
            f'attachment; filename="{filename}"; '
            f"filename*=UTF-8''{_url_quote(filename)}"
        )
        return Response(
            content=output.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": content_disposition},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[AppointmentsMetrics] Error export Excel: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _format_datetime_bogota(iso_str: str) -> str:
    """Formatea ISO datetime a 'DD/MM/YYYY HH:mm' en Bogotá."""
    if not iso_str:
        return ""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("America/Bogota")).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return iso_str[:16]


@router.get("/metrics/", response_class=HTMLResponse)
async def metrics_dashboard_ui(request: Request, x_api_key: str = Query(None, alias="key")):
    """
    Dashboard de metricas para analista de redes sociales.

    Acceso: /whatsapp/panel/metrics/?key=TU_API_KEY
    """
    # Validar API Key via query param para acceso web
    if not _validate_api_key(x_api_key):
        return HTMLResponse(
            content="""<!DOCTYPE html>
<html lang="es">
<head><title>Acceso Denegado</title></head>
<body style="font-family: Arial; padding: 50px; text-align: center;">
    <h1>Acceso Denegado</h1>
    <p>Se requiere API Key válida.</p>
    <p>Uso: /whatsapp/panel/metrics/?key=TU_API_KEY</p>
</body>
</html>""",
            status_code=401
        )

    return templates.TemplateResponse(request, "metrics.html", {
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
                        "timestamp": get_bogota_now().isoformat()
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
                    "timestamp": get_bogota_now().isoformat()
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
        state_manager = _get_state_manager()

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


# =============================================================================
# RECOVERY — Restaurar badges/orden tras outage de MongoDB
# =============================================================================

def _parse_iso_to_utc(iso_str: str) -> datetime:
    """Parsea ISO 8601 (con o sin tz) a datetime UTC-aware."""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@router.post("/admin/recover-outage")
async def recover_outage(
    start_iso: str = Query(..., description="Inicio ventana ISO 8601 ej: 2026-05-20T10:54:00-05:00"),
    end_iso: str = Query(..., description="Fin ventana ISO 8601 ej: 2026-05-20T14:00:00-05:00"),
    dry_run: bool = Query(True, description="True = solo listar candidatos, no modificar Redis"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Recupera badges y orden de contactos cuyo cliente envió mensajes durante un outage
    pero la asesora no ha respondido. Lee Redis (que estuvo UP durante el outage de MongoDB),
    filtra por ventana de tiempo, y restaura advisor_inbox + ZSET + WS broadcast.

    Use cases:
    - Tras outage de MongoDB que pudo desincronizar advisor_inbox
    - Tras crash del worker que pudo dejar contactos sin badge
    - Sweep manual para asegurar que todo contacto esperando respuesta aparece en panel
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida o no configurada")

    try:
        start_dt = _parse_iso_to_utc(start_iso)
        end_dt = _parse_iso_to_utc(end_iso)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=f"Fecha inválida: {ve}")

    if end_dt <= start_dt:
        raise HTTPException(status_code=400, detail="end_iso debe ser mayor que start_iso")

    rc = await _get_redis_client()
    state_manager = _get_state_manager()

    candidates: List[Dict[str, Any]] = []
    scanned = 0
    parse_errors = 0
    BATCH_SIZE = 500  # MGET en lotes de 500 para evitar N+1

    def _process_batch(keys_batch: List[str], values_batch: List[Any]) -> None:
        """Procesa un batch de (key, value) buscando candidatos en la ventana."""
        nonlocal parse_errors
        for key, raw in zip(keys_batch, values_batch):
            if not raw:
                continue
            try:
                meta = json.loads(raw)
                last_client_iso = meta.get("last_client_message")
                if not last_client_iso:
                    continue
                last_client_dt = _parse_iso_to_utc(last_client_iso)

                # Filtro por ventana de outage
                if not (start_dt <= last_client_dt <= end_dt):
                    continue

                # Cliente fue el último en hablar? (gate principal)
                last_advisor_iso = meta.get("last_advisor_message")
                if last_advisor_iso:
                    last_advisor_dt = _parse_iso_to_utc(last_advisor_iso)
                    if last_advisor_dt >= last_client_dt:
                        continue

                phone = meta.get("phone_normalized")
                canal = (meta.get("canal_origen") or "whatsapp").lower()
                owner_id = meta.get("assigned_owner_id")
                display_name = meta.get("display_name")

                if not phone:
                    continue

                candidates.append({
                    "phone": phone,
                    "canal": canal,
                    "owner_id": owner_id,
                    "display_name": display_name,
                    "last_client_message": last_client_iso,
                    "last_advisor_message": last_advisor_iso,
                    "redis_key": key,
                })
            except Exception as pe:
                parse_errors += 1
                if parse_errors <= 5:
                    logger.warning(f"[Recovery] Parse error en {key}: {pe}")

    # 1. Scan + MGET en batches (3-5 ordenes de magnitud más rápido que GET individual)
    logger.info(f"[Recovery] Iniciando scan con ventana {start_iso} → {end_iso}")
    keys_buffer: List[str] = []
    try:
        async for key_bytes in rc.scan_iter(match=f"{state_manager.META_PREFIX}*", count=500):
            key = key_bytes.decode() if isinstance(key_bytes, bytes) else key_bytes
            keys_buffer.append(key)

            # Flush cuando el buffer llegue a BATCH_SIZE
            if len(keys_buffer) >= BATCH_SIZE:
                values = await rc.mget(keys_buffer)
                _process_batch(keys_buffer, values)
                scanned += len(keys_buffer)
                keys_buffer = []
                logger.info(
                    f"[Recovery] Progreso: {scanned} metas escaneadas, "
                    f"{len(candidates)} candidatos hasta ahora"
                )

        # Flush del buffer restante
        if keys_buffer:
            values = await rc.mget(keys_buffer)
            _process_batch(keys_buffer, values)
            scanned += len(keys_buffer)
    except Exception as scan_err:
        logger.error(f"[Recovery] Error escaneando Redis: {scan_err}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error escaneando Redis: {scan_err}")

    logger.info(
        f"[Recovery] Scan completo: {scanned} metas revisadas, "
        f"{len(candidates)} candidatos en ventana, {parse_errors} errores parseo"
    )

    # 2. Aplicar recovery (si no es dry_run)
    applied: List[str] = []
    apply_errors: List[Dict[str, str]] = []
    skipped_no_owner: List[str] = []

    if not dry_run:
        for cand in candidates:
            phone = cand["phone"]
            canal = cand["canal"]
            owner_id = cand["owner_id"]
            try:
                last_client_dt = _parse_iso_to_utc(cand["last_client_message"])
                score = last_client_dt.timestamp()

                # 2a. ZADD active_conversations_sorted (mantiene orden por urgencia)
                await rc.zadd(
                    state_manager.ACTIVE_CONTACTS_ZSET,
                    {f"{phone}:{canal}": score}
                )

                # 2b. advisor_inbox (badge "no leído") — requiere owner_id
                if owner_id:
                    await state_manager.add_to_advisor_inbox(owner_id, phone, canal, score=score)
                else:
                    skipped_no_owner.append(phone)

                # 2c. WS broadcast para refrescar panel en vivo
                try:
                    await ws_manager.publish_broadcast(rc, {
                        "type": "contact_updated",
                        "phone": phone,
                        "canal": canal,
                        "action": "new_message",
                    })
                except Exception as we:
                    logger.warning(f"[Recovery] WS broadcast falló para {phone}: {we}")

                applied.append(phone)
            except Exception as ae:
                apply_errors.append({"phone": phone, "error": str(ae)})
                logger.error(f"[Recovery] Apply error para {phone}: {ae}", exc_info=True)

    return {
        "window": {"start": start_iso, "end": end_iso},
        "dry_run": dry_run,
        "scanned_metas": scanned,
        "candidates_found": len(candidates),
        "parse_errors": parse_errors,
        "applied_count": len(applied) if not dry_run else 0,
        "apply_errors": apply_errors,
        "skipped_no_owner": skipped_no_owner,
        # En dry_run devolvemos detalle completo; en aplicado solo lista de phones
        "candidates": candidates if dry_run else None,
        "applied": applied if not dry_run else None,
    }


# =============================================================================
# ONE-SHOT: Limpieza de advisor_inbox stale (BOT_ACTIVE entries)
# =============================================================================

@router.post("/admin/cleanup-stale-inbox")
async def cleanup_stale_inbox(
    dry_run: bool = Query(True, description="True = solo listar, no borrar"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Limpia entries de advisor_inbox cuyo contacto está en BOT_ACTIVE o sin estado.
    Estas entries son badges fantasma que no deberían existir.
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida o no configurada")

    from middleware.conversation_state import ConversationStatus
    state_mgr = _get_state_manager()
    ACTIVE_ADVISORS = ["89096378", "89096380", "89096379"]
    results = {}
    total_cleaned = 0

    for adv_id in ACTIVE_ADVISORS:
        inbox_key = f"{state_mgr.ADVISOR_INBOX_PREFIX}{adv_id}"
        members = await state_mgr.redis.zrange(inbox_key, 0, -1, withscores=True)
        if not members:
            results[adv_id] = {"total": 0, "stale": 0, "entries": []}
            continue
        stale_members = []
        stale_details = []
        for m, score in members:
            if isinstance(m, bytes):
                m = m.decode("utf-8", errors="ignore")
            parts = m.rsplit(":", 1)
            if len(parts) != 2:
                continue
            phone, canal = parts
            state_key = f"{state_mgr.STATE_PREFIX}{phone}:{canal}"
            raw_state = await state_mgr.redis.get(state_key)
            if raw_state and isinstance(raw_state, bytes):
                raw_state = raw_state.decode("utf-8", errors="ignore")
            if not raw_state or raw_state == ConversationStatus.BOT_ACTIVE.value:
                stale_members.append(m)
                stale_details.append({
                    "member": m,
                    "state": raw_state or "NO_STATE",
                    "score": score,
                })
        removed = 0
        if stale_members and not dry_run:
            removed = await state_mgr.redis.zrem(inbox_key, *stale_members)
            total_cleaned += removed
        results[adv_id] = {
            "total": len(members),
            "stale": len(stale_members),
            "removed": removed if not dry_run else "dry_run",
            "entries": stale_details[:50],
        }

    return {
        "dry_run": dry_run,
        "total_cleaned": total_cleaned if not dry_run else "dry_run",
        "advisors": results,
    }


# =============================================================================
# SCHEDULED MESSAGES — Mensajes WhatsApp plantilla programados por asesoras
# =============================================================================

@router.post("/contacts/{contact_id}/scheduled-messages", status_code=201)
async def create_scheduled_message(
    contact_id: str,
    body: ScheduledMessageCreateRequest,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """
    Programa un mensaje de plantilla WhatsApp para enviarse en una fecha/hora específica.
    El scheduler (check_scheduled_messages, cada 1 min) lo detecta y lo envía vía Twilio.
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    from datetime import datetime as dt
    from zoneinfo import ZoneInfo
    BOGOTA_TZ = ZoneInfo("America/Bogota")

    # Parsear scheduled_dt: tratamos naive como hora Colombia
    try:
        sched_dt = dt.fromisoformat(body.scheduled_dt)
        if sched_dt.tzinfo is None:
            sched_dt = sched_dt.replace(tzinfo=BOGOTA_TZ)
        sched_dt_bogota = sched_dt.astimezone(BOGOTA_TZ)
    except ValueError:
        raise HTTPException(status_code=422, detail="Formato de fecha inválido. Use ISO 8601.")

    # Validar que sea en el futuro (mínimo 1 minuto)
    now_bogota = dt.now(BOGOTA_TZ)
    if sched_dt_bogota <= now_bogota:
        raise HTTPException(status_code=422, detail="La fecha de envío debe ser en el futuro.")

    # Resolver phone y firstname del contacto
    phone = ""
    contact_firstname = ""
    try:
        hs_info = await _get_hubspot_contact_info(contact_id)
        if hs_info:
            phone = hs_info.get("phone", "")
            contact_firstname = (hs_info.get("firstname") or "").strip()
    except Exception:
        pass

    if not phone:
        raise HTTPException(status_code=422, detail="No se encontró teléfono para el contacto.")

    # Persistir en MongoDB (se guarda con tz-aware → Motor lo convierte a UTC en BSON)
    mongo_mgr = get_mongo_manager()
    message_id = await mongo_mgr.create_scheduled_message(
        contact_id=contact_id,
        phone=phone,
        canal=body.canal,
        template_sid=body.template_sid,
        template_name=body.template_name,
        template_variables=body.template_variables,
        scheduled_dt=sched_dt_bogota,
        advisor_id=body.advisor_id or "",
        notes=body.notes,
    )

    if not message_id:
        raise HTTPException(status_code=500, detail="Error guardando el mensaje en base de datos.")

    logger.info(
        f"[Panel] Mensaje programado creado: contact={contact_id}, "
        f"template={body.template_name}, dt={sched_dt_bogota.isoformat()}, id={message_id}"
    )
    return {
        "message_id": message_id,
        "template_name": body.template_name,
        "scheduled_dt": sched_dt_bogota.isoformat(),
    }


@router.get("/contacts/{contact_id}/scheduled-messages")
async def get_scheduled_messages(
    contact_id: str,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Lista todos los mensajes programados de un contacto."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    messages = await mongo_mgr.get_scheduled_messages(contact_id)
    return {"messages": messages, "total": len(messages)}


@router.delete("/scheduled-messages/{message_id}", status_code=200)
async def cancel_scheduled_message(
    message_id: str,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Cancela un mensaje programado pendiente."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    ok = await mongo_mgr.cancel_scheduled_message(message_id)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail="Mensaje no encontrado o ya no está en estado pending."
        )
    logger.info(f"[Panel] Mensaje programado cancelado: {message_id}")
    return {"ok": True}


# ============================================================================
# BULK CAMPAIGNS — Mensajes Masivos por Embudo (per-asesora)
# ============================================================================

def _validate_bulk_request(
    stage_id: str,
    advisor_id: Optional[str],
    template_sid: str,
    template_variables: Optional[Dict[str, str]],
) -> tuple[Optional[dict], Optional[str]]:
    """
    Valida un request de bulk:
      - advisor_id presente
      - stage no excluido
      - template_sid en BULK_ALLOWED_TEMPLATES
      - template_variables incluye texto para todas las vars NO auto-fill

    Retorna (template_meta, error_msg). Si error_msg != None, hubo fallo.
    """
    if not advisor_id:
        return None, "advisor_id requerido"
    if stage_id in BULK_EXCLUDED_STAGES:
        return None, f"stage_id '{stage_id}' no permite envío masivo"
    if stage_id not in PIPELINE_STAGES:
        return None, f"stage_id '{stage_id}' inválido"
    if not template_sid or not template_sid.startswith("HX"):
        return None, "template_sid inválido (debe comenzar con HX)"
    template_meta = BULK_ALLOWED_TEMPLATES.get(template_sid)
    if not template_meta:
        return None, f"template_sid '{template_sid}' no permitido para envío masivo"

    # Verificar que las variables NO auto-fill traigan texto
    template_variables = template_variables or {}
    missing = []
    for var in template_meta["vars"]:
        if var.get("auto_fill"):
            continue  # se resuelve per-contacto en el worker
        key = var["key"]
        val = (template_variables.get(key) or "").strip()
        if not val:
            missing.append(f"{{{key}}} ({var['label']})")
    if missing:
        return None, f"Faltan valores para: {', '.join(missing)}"
    return template_meta, None


# Fallback cuando el contacto no tiene un nombre real utilizable.
BULK_NAME_FALLBACK = "Buen dia"

# Marcadores comunes de "sin nombre" que llegan desde HubSpot. Case-insensitive.
import re as _re_bulk
_BULK_INVALID_NAME_RE = _re_bulk.compile(
    r"^\s*(none(\s+none)?|null|n/?a|undefined|sin\s*nombre|-+|\.+|cliente)\s*$",
    _re_bulk.IGNORECASE,
)


def _clean_firstname_for_template(firstname: Optional[str]) -> str:
    """
    Normaliza el firstname para uso en plantillas WhatsApp masivas.

    Devuelve BULK_NAME_FALLBACK ("Buen dia") cuando el valor recibido NO es
    un nombre real. Casos cubiertos:
      - vacío / None / solo whitespace
      - marcadores típicos: "none", "none none", "null", "n/a", "sin nombre", "---"
      - sin NINGUNA letra (ej. "+573001234567", "3001234567", "...", "0000")
    De lo contrario, devuelve el firstname trimmed tal cual.
    """
    if not firstname:
        return BULK_NAME_FALLBACK
    cleaned = firstname.strip()
    if not cleaned:
        return BULK_NAME_FALLBACK
    if _BULK_INVALID_NAME_RE.match(cleaned):
        return BULK_NAME_FALLBACK
    if not any(c.isalpha() for c in cleaned):
        return BULK_NAME_FALLBACK
    return cleaned


def _resolve_bulk_template_variables(
    template_meta: dict,
    contact: dict,
    literal_variables: Dict[str, str],
) -> dict:
    """
    Construye el dict content_variables para Twilio, combinando:
      - Variables auto_fill: resueltas per-contacto. Para 'firstname' usa
        _clean_firstname_for_template() que aplica BULK_NAME_FALLBACK
        cuando el valor no es un nombre real.
      - Variables literales: texto que escribió la asesora (aplicado a todos).

    Retorna {key: value} ej: {"1": "Juan", "2": "te escribo para..."}
    """
    content_variables = {}
    for var in template_meta["vars"]:
        key = var["key"]
        if var.get("auto_fill") == "firstname":
            content_variables[key] = _clean_firstname_for_template(contact.get("firstname"))
        elif var.get("auto_fill"):
            content_variables[key] = (contact.get(var["auto_fill"]) or "").strip() or ""
        else:
            content_variables[key] = (literal_variables.get(key) or "").strip()
    return content_variables


async def _resolve_advisor_name(advisor_id: str) -> str:
    """
    Resuelve display name de la asesora desde MongoDB panel_advisors.
    Fallback: 'tu asesora'.
    """
    try:
        mongo_mgr = get_mongo_manager()
        if not await mongo_mgr.connect():
            return "tu asesora"
        doc = await mongo_mgr.db.panel_advisors.find_one({"advisor_id": advisor_id})
        if doc:
            return doc.get("name") or doc.get("display_name") or "tu asesora"
    except Exception as e:
        logger.debug(f"[BulkCampaign] No se pudo resolver advisor_name: {e}")
    return "tu asesora"


async def _hubspot_search_contacts_for_bulk(
    stage_id: str,
    advisor_id: str,
) -> List[Dict[str, Any]]:
    """
    Busca todos los contactos del embudo asignados a la asesora vía HubSpot search.
    Pagina 100/page con sleep 0.15s. Cache Redis 30 min para no saturar HubSpot.

    Retorna lista de contactos con {contact_id, phone, firstname, owner_id}.
    """
    cache_key = f"{HUBSPOT_BULK_SEARCH_CACHE_PREFIX}{stage_id}:{advisor_id}"
    try:
        r = await _get_redis_client()
        cached = await r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        logger.debug(f"[BulkCampaign] cache miss/error: {e}")

    all_contacts: List[Dict[str, Any]] = []
    after: Optional[str] = None
    pages = 0
    max_pages = 50  # 5000 contactos máx — más que suficiente
    while pages < max_pages:
        try:
            resp = await _hs_singleton.search_contacts_by_lifecyclestage_and_owner(
                stage_id=stage_id,
                owner_id=advisor_id,
                limit=100,
                after=after,
            )
        except Exception as e:
            logger.error(f"[BulkCampaign] Error HubSpot search: {e}")
            break
        for r_item in resp.get("results", []):
            props = r_item.get("properties") or {}
            phone_val = (props.get("whatsapp_id") or props.get("phone") or "").strip()
            if not phone_val:
                continue
            all_contacts.append({
                "contact_id": str(r_item.get("id")),
                "phone": phone_val,
                "firstname": (props.get("firstname") or "").strip(),
                "owner_id": props.get("hubspot_owner_id") or advisor_id,
            })
        after = resp.get("next_after")
        if not after:
            break
        pages += 1
        await asyncio.sleep(0.15)

    try:
        r = await _get_redis_client()
        await r.set(cache_key, json.dumps(all_contacts), ex=HUBSPOT_BULK_SEARCH_CACHE_TTL)
    except Exception as e:
        logger.debug(f"[BulkCampaign] Error guardando cache: {e}")

    return all_contacts


async def _filter_contacts_by_last_message_range(
    contacts: List[Dict[str, Any]],
    date_from: Optional[str],
    date_to: Optional[str],
) -> List[Dict[str, Any]]:
    """
    Filtra contactos cuyo LAST_CLIENT_MESSAGE esté en el rango [date_from, date_to].
    Si no hay date_from ni date_to, retorna todos los contactos sin filtrar.
    Si un contacto no tiene last_client_message en Redis, se INCLUYE igual
    (cubre contactos importados sin historial reciente).
    """
    if not date_from and not date_to:
        return contacts
    try:
        from_dt = datetime.fromisoformat(date_from + "T00:00:00+00:00") if date_from else None
        to_dt = datetime.fromisoformat(date_to + "T23:59:59+00:00") if date_to else None
    except Exception as e:
        logger.warning(f"[BulkCampaign] date parse error: {e}")
        return contacts

    r = await _get_redis_client()
    keys = [f"{LAST_CLIENT_MESSAGE_PREFIX}{c['phone']}" for c in contacts]
    if not keys:
        return contacts
    pipe = r.pipeline()
    for k in keys:
        pipe.get(k)
    raw_values = await pipe.execute()

    filtered = []
    for contact, raw in zip(contacts, raw_values):
        if not raw:
            # Sin historial conocido → se incluye
            filtered.append(contact)
            continue
        try:
            ts_str = raw if isinstance(raw, str) else raw.decode()
            ts_str = ts_str.replace("Z", "+00:00")
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if from_dt and ts < from_dt:
                continue
            if to_dt and ts > to_dt:
                continue
            filtered.append(contact)
        except Exception:
            filtered.append(contact)
    return filtered


# ----------------------------------------------------------------------------
# Endpoints Bulk Campaigns
    # ----------------------------------------------------------------------------

@router.post("/bulk-campaigns/preview")
async def preview_bulk_campaign(
    stage_id: str = Body(...),
    advisor_id: str = Body(...),
    template_sid: str = Body(...),
    template_variables: Optional[Dict[str, str]] = Body(None),
    date_from: Optional[str] = Body(None),
    date_to: Optional[str] = Body(None),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Preview: cuántos contactos recibirían el bulk (sin crear campaña)."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    template_meta, err = _validate_bulk_request(stage_id, advisor_id, template_sid, template_variables)
    if err:
        raise HTTPException(status_code=400, detail=err)

    contacts = await _hubspot_search_contacts_for_bulk(stage_id, advisor_id)
    filtered = await _filter_contacts_by_last_message_range(contacts, date_from, date_to)

    sample = [{"firstname": c.get("firstname"), "phone": c.get("phone")[-4:]} for c in filtered[:5]]
    return {
        "status": "success",
        "total": len(filtered),
        "total_in_stage": len(contacts),
        "sample": sample,
        "stage_id": stage_id,
        "stage_name": PIPELINE_STAGES.get(stage_id, stage_id),
        "template_name": template_meta["name"],
    }


@router.post("/bulk-campaigns")
async def create_bulk_campaign(
    stage_id: str = Body(...),
    advisor_id: str = Body(...),
    template_sid: str = Body(...),
    template_variables: Optional[Dict[str, str]] = Body(None),
    date_from: Optional[str] = Body(None),
    date_to: Optional[str] = Body(None),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Crea una campaña masiva. El processor APScheduler la procesa async."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    template_meta, err = _validate_bulk_request(stage_id, advisor_id, template_sid, template_variables)
    if err:
        raise HTTPException(status_code=400, detail=err)

    contacts = await _hubspot_search_contacts_for_bulk(stage_id, advisor_id)
    filtered = await _filter_contacts_by_last_message_range(contacts, date_from, date_to)
    if not filtered:
        raise HTTPException(status_code=400, detail="No hay contactos en el rango especificado")

    advisor_name = await _resolve_advisor_name(advisor_id)
    auto_fill_keys = [v["key"] for v in template_meta["vars"] if v.get("auto_fill")]
    literal_vars = {
        v["key"]: (template_variables or {}).get(v["key"], "").strip()
        for v in template_meta["vars"] if not v.get("auto_fill")
    }

    mongo_mgr = get_mongo_manager()
    campaign_id = await mongo_mgr.create_bulk_campaign(
        stage_id=stage_id,
        stage_name=PIPELINE_STAGES.get(stage_id, stage_id),
        template_id=template_meta["name"],
        template_content_sid=template_sid,
        date_from=date_from,
        date_to=date_to,
        contacts=filtered,
        creator_advisor_id=advisor_id,
        creator_advisor_name=advisor_name,
        template_variables=literal_vars,
        auto_fill_keys=auto_fill_keys,
    )
    if not campaign_id:
        raise HTTPException(status_code=500, detail="Error creando campaña")

    logger.info(
        f"[BulkCampaign] Creada {campaign_id} stage={stage_id} advisor={advisor_id} "
        f"total={len(filtered)} template={template_meta['name']}"
    )
    return {
        "status": "success",
        "campaign_id": campaign_id,
        "total": len(filtered),
        "stage_name": PIPELINE_STAGES.get(stage_id, stage_id),
        "template_name": template_meta["name"],
    }


@router.get("/bulk-campaigns/{campaign_id}")
async def get_bulk_campaign_status(
    campaign_id: str,
    advisor_id: str = Query(...),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Polling endpoint — solo retorna progreso si el advisor es el creador."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    doc = await mongo_mgr.get_bulk_campaign(campaign_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Campaña no encontrada")
    if doc.get("creator_advisor_id") != advisor_id:
        raise HTTPException(status_code=403, detail="Sin acceso a esta campaña")
    return {
        "campaign_id": doc["_id"],
        "status": doc.get("status"),
        "total_contacts": doc.get("total_contacts"),
        "sent_count": doc.get("sent_count", 0),
        "failed_count": doc.get("failed_count", 0),
        "stage_name": doc.get("stage_name"),
        "template_id": doc.get("template_id"),
        "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
        "completed_at": doc.get("completed_at").isoformat() if doc.get("completed_at") else None,
    }


@router.get("/bulk-campaigns/last/{stage_id}")
async def get_last_bulk_campaign(
    stage_id: str,
    advisor_id: str = Query(...),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Última campaña de esta asesora en este embudo (para el banner del modal)."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    doc = await mongo_mgr.get_last_bulk_campaign_by_stage_and_advisor(stage_id, advisor_id)
    if not doc:
        return {"status": "success", "last": None}
    return {
        "status": "success",
        "last": {
            "campaign_id": doc["_id"],
            "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
            "date_from": doc.get("date_from"),
            "date_to": doc.get("date_to"),
            "total_contacts": doc.get("total_contacts"),
            "sent_count": doc.get("sent_count", 0),
            "failed_count": doc.get("failed_count", 0),
            "status": doc.get("status"),
            "template_id": doc.get("template_id"),
        },
    }


# ----------------------------------------------------------------------------
# Worker — Processor Bulk Campaigns
# ----------------------------------------------------------------------------

async def _send_bulk_template_message(
    phone: str,
    content_sid: str,
    content_variables: dict,
    contact_id: str,
    campaign_id: str,
    campaign_stage_id: str,
    template_name: str = "",
) -> dict:
    """
    Envío bulk: NO actualiza state_manager, NO ZSET, NO Timeline HubSpot.
    Solo Twilio + escritura mínima MongoDB messages.

    Caso especial 'No Responde' (stage_id == 'other'): tras éxito, setea flag
    Redis bulk_no_responde_pending:{phone} para auto-promoción al responder.

    El contacto permanece BOT_ACTIVE invisible al panel hasta que responda.
    """
    if not phone.startswith("+"):
        phone = f"+{phone}"

    # Audit explícito: deja constancia exacta de qué variables recibió este contacto.
    # Permite verificar post-envío si el {nombre} fue real o cayó al fallback.
    logger.info(
        f"[BulkCampaign] {campaign_id}: enviando a {phone} contact={contact_id} "
        f"vars={content_variables}"
    )

    result = await twilio_client.send_whatsapp_message(
        to=phone,
        body=None,
        content_sid=content_sid,
        content_variables=content_variables,
    )
    if result.get("status") != "success":
        return result

    message_sid = result.get("message_sid")

    # Guardar mensaje mínimo en MongoDB (audit + visibilidad si cliente responde)
    try:
        mongo_mgr = get_mongo_manager()
        await mongo_mgr.save_message(
            phone=phone,
            content=f"[BULK template:{template_name or campaign_id}]",
            sender="advisor",
            channel="whatsapp",
            hubspot_contact_id=contact_id,
            message_sid=message_sid,
            metadata={"is_bulk_send": True, "campaign_id": campaign_id, "stage_id": campaign_stage_id},
        )
    except Exception as e:
        logger.warning(f"[BulkCampaign] No se pudo guardar mensaje en Mongo: {e}")

    # Caso especial No Responde: setear flag para auto-promoción
    if campaign_stage_id == "other":
        try:
            r = await _get_redis_client()
            flag_key = f"{BULK_NO_RESPONDE_FLAG_PREFIX}{phone}"
            await r.set(flag_key, campaign_id, ex=BULK_NO_RESPONDE_FLAG_TTL)
        except Exception as e:
            logger.warning(f"[BulkCampaign] No se pudo setear flag No Responde: {e}")

    return result


async def _process_bulk_campaign_tick():
    """
    Tick del scheduler — procesa hasta BULK_BATCH_SIZE contactos pending de
    una campaña activa, con throttle conservador para no saturar Twilio/HubSpot.

    Lock Redis bulk_processor_lock evita doble procesamiento concurrente.
    Idempotencia por contacto: dedup key Redis bulk_send_dedup:{campaign}:{contact}.
    """
    r = None
    try:
        r = await _get_redis_client()
        # Lock global del processor (NX) — solo 1 instancia activa
        lock_acquired = await r.set(
            BULK_PROCESSOR_LOCK_KEY, "1", ex=BULK_PROCESSOR_LOCK_TTL, nx=True
        )
        if not lock_acquired:
            return
    except Exception as e:
        logger.warning(f"[BulkCampaign] No se pudo adquirir lock: {e}")
        return

    try:
        mongo_mgr = get_mongo_manager()
        active = await mongo_mgr.get_active_bulk_campaigns(limit=1)
        if not active:
            return

        campaign = active[0]
        campaign_id = campaign["_id"]
        contacts = await mongo_mgr.claim_next_pending_contacts(campaign_id, BULK_BATCH_SIZE)
        if not contacts:
            await mongo_mgr.finalize_bulk_campaign_if_done(campaign_id)
            return

        sem = asyncio.Semaphore(BULK_MAX_CONCURRENT)

        # Variables literales y auto_fill del documento Mongo (compartidas en toda la campaña)
        campaign_literal_vars = campaign.get("template_variables") or {}

        async def _process_one(contact):
            async with sem:
                contact_id = contact.get("contact_id")
                phone = contact.get("phone") or ""
                stage_id = contact.get("_campaign_stage_id")
                content_sid = contact.get("_campaign_template_content_sid")

                # Dedup por contacto (NX)
                dedup_key = f"{BULK_SEND_DEDUP_PREFIX}{campaign_id}:{contact_id}"
                try:
                    dedup_set = await r.set(dedup_key, "1", ex=BULK_MESSAGE_DEDUP_TTL, nx=True)
                except Exception:
                    dedup_set = True
                if not dedup_set:
                    logger.info(f"[BulkCampaign] {campaign_id}: skip {contact_id} (dedup)")
                    await mongo_mgr.mark_bulk_contact_sent(campaign_id, contact_id, None)
                    return

                # Resolver template_meta por SID (las plantillas viven en BULK_ALLOWED_TEMPLATES)
                template_meta = BULK_ALLOWED_TEMPLATES.get(content_sid)
                if not template_meta:
                    await mongo_mgr.mark_bulk_contact_failed(
                        campaign_id, contact_id, f"SID no permitido: {content_sid}"
                    )
                    return
                content_variables = _resolve_bulk_template_variables(
                    template_meta,
                    {"firstname": contact.get("firstname")},
                    campaign_literal_vars,
                )

                try:
                    result = await _send_bulk_template_message(
                        phone=phone,
                        content_sid=content_sid,
                        content_variables=content_variables,
                        contact_id=contact_id,
                        campaign_id=campaign_id,
                        campaign_stage_id=stage_id,
                        template_name=template_meta["name"],
                    )
                except Exception as e:
                    await mongo_mgr.mark_bulk_contact_failed(
                        campaign_id, contact_id, f"exception: {e}"
                    )
                    return

                if result.get("status") == "success":
                    await mongo_mgr.mark_bulk_contact_sent(
                        campaign_id, contact_id, result.get("message_sid")
                    )
                else:
                    await mongo_mgr.mark_bulk_contact_failed(
                        campaign_id, contact_id, result.get("message", "")[:300]
                    )

                # Throttle entre sends (incluso paralelos por el semaphore)
                await asyncio.sleep(BULK_SEND_SPACING_SEC)

        await asyncio.gather(*[_process_one(c) for c in contacts])

        # Reload counts
        updated = await mongo_mgr.get_bulk_campaign(campaign_id)
        if updated:
            logger.info(
                f"[BulkCampaign] {campaign_id}: "
                f"{updated.get('sent_count', 0)}/{updated.get('total_contacts', 0)} sent, "
                f"{updated.get('failed_count', 0)} failed"
            )
        await mongo_mgr.finalize_bulk_campaign_if_done(campaign_id)

    except Exception as e:
        logger.error(f"[BulkCampaign] Error en tick: {e}", exc_info=True)
    finally:
        try:
            if r is not None:
                await r.delete(BULK_PROCESSOR_LOCK_KEY)
        except Exception:
            pass


async def _process_bulk_campaign_tick_safe():
    """Wrapper para el scheduler — nunca propaga excepciones."""
    try:
        await _process_bulk_campaign_tick()
    except Exception as e:
        logger.error(f"[BulkCampaign] Error fatal en tick (capturado): {e}", exc_info=True)
