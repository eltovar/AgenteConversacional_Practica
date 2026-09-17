# middleware/outbound_panel.py
"""
Este módulo proporciona endpoints API y UI para que los asesores envíen
mensajes de WhatsApp directamente, sustituyendo el Inbox bloqueado de HubSpot.
"""

import os
import json
import re
import html
import time
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
from .whatsapp_identity import is_bsuid_identity_key, is_whatsapp_bsuid
from .identity_migration import migrate_identity_to_phone
from .conversation_state import ConversationStateManager, ConversationStatus, get_bogota_now, get_bogota_now_iso, TIMEZONE_BOGOTA
from .contact_manager import ContactManager
from .websocket_manager import ws_manager
from .templates.templates import (  # Templates predefinidos
    DEFAULT_TEMPLATES,
    SOLO_AUTOMATICAS,
)
from utils.twilio_client import twilio_client
from utils.date_parser import (
    DIAS_SEMANA_ABREV,
    DIAS_SEMANA_CAP,
    MESES_ABREV,
    MESES_ABREV_CAP,
    hora_12h,
)
from .confirmacion_cita import (
    DatosCita,
    IDENTIFICADOR_PLANTILLA as PLANTILLA_CONFIRMACION_CITA,
    MOTIVO_SIN_TELEFONO_CLIENTE,
    componer as componer_confirmacion,
    hace_falta_reenviar,
)
from utils.advisors_registry import (
    get_advisor_names,
    get_lead_receiving_ids,
    get_panel_advisor_ids,
    get_panel_templates,
    get_transfer_target,
)
from integrations.hubspot import (
    get_timeline_logger,
    hubspot_client as _hs_singleton,
    register_contact_update_hook as _register_contact_update_hook,
)
from slowapi import Limiter
from slowapi.util import get_remote_address
limiter = Limiter(key_func=get_remote_address)
from database.mongodb_client import get_mongo_manager
from utils.media_processor import media_processor, DOCUMENT_MIME_TYPES, MAX_DOCUMENT_SIZE_BYTES, MAX_VIDEO_SIZE_BYTES
from utils.reply_quote_formatter import inject_quote, reply_audio_intro
from utils.safe_logging import obs_event, safe_error, safe_id, safe_phone, safe_text, safe_url


# Router de FastAPI para el panel de envío
router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])


# ============================================================================
# Modelos Pydantic para JSON requests
# ============================================================================

# MESSAGES — ver services/panel/messages_service.py

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
HUBSPOT_STAGE_NUEVO_LEAD = "1417459250"       # Entrada de todo lead nuevo (sin contacto humano aún)
HUBSPOT_STAGE_EN_CONVERSACION = "1326623075"  # Destino cuando la asesora contesta manualmente
HUBSPOT_STAGE_VISITA_AGENDADA = "marketingqualifiedlead"
HUBSPOT_STAGE_VISITA_REALIZADA = "salesqualifiedlead"
HUBSPOT_STAGE_NO_RESPONDE = "other"          # Embudo de reactivacion por masivos
HUBSPOT_STAGE_CERRADO_PERDIDO = "evangelist" # Terminal: sale del panel, Sofia retoma
# Destino de las transferencias por embudo. Sale del registro de asesoras, no de
# aqui: cambiar quien recibe las transferencias es editar una entrada de
# utils/advisors_registry.py, no buscar un ID por el codigo.
LUISA_TRANSFER_TARGET = get_transfer_target()
STAGES_TRANSFER_TO_LUISA = {
    "1407668893": "Seguimiento",
    "1326623067": "Hasta 1.5M",
    "1326631573": "Hasta 2M",
    "1326632625": "Hasta 2.5M",
    "1326631574": "De 3M en adelante",
    HUBSPOT_STAGE_NO_RESPONDE: "No responde",
    "1326623069": "Propietarios",
    "1326632628": "Otros Municipios",
    "1326623539": "Local o Bodega",
    "subscriber": "Reubicados",
}
# Stages que cierran la conversación SIN transferir owner — cierre puro.
# Se diferencian de STAGES_TRANSFER_TO_LUISA: allí el contacto cambia de dueño y
# reaparece en el panel destino; aquí sale del panel y Sofía retoma.
STAGES_AUTO_CLOSE = {
    HUBSPOT_STAGE_CERRADO_PERDIDO: "Cerrado perdido",
}
# Stages comerciales que NO se deben sobreescribir (progreso manual de asesora)
PROTECTED_STAGES_POST_VISITA = {
    "salesqualifiedlead", "opportunity", "customer", "evangelist",
    "1407668893", "1326623067", "1326631573", "1326632625", "1326631574",
    "1326623069", "1326632628", "1326623539",
}

# ============================================================================
# Bulk Campaigns (mensajes masivos) — constantes
# ============================================================================
BULK_EXCLUDED_STAGES = {
    "1417459250",  # Nuevo Lead — merece atención directa, no un masivo
    "1326623075",  # En Conversación (stage unificado)
    "evangelist",  # Cerrado perdido
    "1326632628",  # Otros Municipios
    "1326632209",  # Otras Areas
}
BULK_PROCESSOR_LOCK_KEY = "bulk_processor_lock"
BULK_PROCESSOR_LOCK_TTL = 30
BULK_BATCH_SIZE = 5
BULK_SEND_SPACING_SEC = 0.5
BULK_MAX_CONCURRENT = 2
BULK_MESSAGE_DEDUP_TTL = 86400 * 7
# Un contacto reclamado y no resuelto en este plazo se da por huerfano: el
# proceso que lo tomo murio. Holgado frente al lote real (5 envios, ~3s).
BULK_CLAIM_LEASE_SECONDS = 900
# Pasada esta edad, una campana ya no reintenta lo huerfano: lo cancela. Un
# masivo comercial que sale dias tarde llega fuera de contexto.
BULK_CAMPAIGN_MAX_AGE_HOURS = 24
# Campanas que mira el tick antes de rendirse. >1 para que una atascada no
# pueda volver a matar de hambre a las que van detras.
BULK_CAMPAIGNS_PER_TICK = 5
HUBSPOT_BULK_SEARCH_CACHE_TTL = 1800
BULK_NO_RESPONDE_FLAG_PREFIX = "bulk_no_responde_pending:"
BULK_NO_RESPONDE_FLAG_TTL = 86400 * 30
BULK_SEND_DEDUP_PREFIX = "bulk_send_dedup:"
HUBSPOT_BULK_SEARCH_CACHE_PREFIX = "hubspot_bulk_search:"

# Plantillas permitidas para envío masivo. Mapeo SID → metadata.
# La variable key="1" (nombre) SIEMPRE se auto-fill desde HubSpot firstname per-contacto.
# Las demás variables se reciben del frontend como texto literal (aplicado a todos).
# Los SIDs viven en middleware/templates/content_sids.py — cambian al migrar de cuenta Twilio.
from middleware.templates import content_sids as _content_sids

BULK_ALLOWED_TEMPLATES = _content_sids.BULK_ALLOWED_TEMPLATES

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

# Caché de lifecyclestage del Contact en Redis (compartida entre workers Railway)
# ── Cache de la respuesta de GET /contacts ──────────────────────────────────
# El panel hace polling cada 10s (POLLING_INTERVAL_IDLE en index.js). Con el TTL
# anterior de 5s el cache expiraba ANTES del siguiente poll, así que no acertaba
# nunca entre peticiones sucesivas: cada poll reconstruía la lista entera.
#
# Cuando la reconstrucción se ralentiza —429 de HubSpot— eso se realimenta: la
# petición tarda minutos, los polls siguen entrando cada 10s y se apilan decenas
# de reconstrucciones simultáneas, cada una con cientos de llamadas a HubSpot.
# Medido el 10-ago-2026: GET /contacts de hasta 508s.
#
# El TTL debe cubrir el intervalo de polling. El WebSocket sigue siendo el canal
# de tiempo real (badges y banner no pasan por aquí), así que el desfase máximo
# de la lista por polling es este TTL.
CONTACTS_RESPONSE_CACHE_TTL = 20
# Single-flight: mientras una petición reconstruye la lista, las demás esperan su
# resultado en vez de duplicar el trabajo. TTL de seguridad por si el proceso muere
# a mitad — sin él, un crash dejaría el flag puesto y nadie reconstruiría.
# Cuando el inbox de no-leidos no responde, el corte normal de `limit` dejaria
# fuera a los contactos nuevos (pierden el pase que les da estar sin leer). Se
# amplia por este factor: el ZSET viene ordenado por actividad reciente, asi que
# un contacto que acaba de escribir entra de sobra. No se muestran todos porque
# serian ~1122 y volveria el coste que el resto de este modulo acaba de arreglar.
CONTACTS_INBOX_FALLBACK_MULTIPLIER = 3

# Un contacto cuyo ultimo mensaje es del cliente entra SIEMPRE en la lista, tenga
# badge o no. Sin esta regla, la asesora que abre un chat pierde el no-leido y el
# contacto cae al monton ordenado por fecha; pasada la posicion `limit` desaparece
# de la barra y la UI no pagina, asi que no hay forma de volver a el.
# Medido el 11-ago-2026: 13 de 40 pendientes estaban en ese agujero.
# El tope existe porque cada contacto extra cuesta enriquecimiento de HubSpot: si
# `last_message_sender` viniera mal para cientos, volveria el coste de los 508s.
CONTACTS_PENDING_MAX = 100

CONTACTS_INFLIGHT_TTL = 30
CONTACTS_INFLIGHT_WAIT_SECONDS = 3.0
CONTACTS_INFLIGHT_POLL_INTERVAL = 0.25
CONTACTS_PERF_TOTAL_SLOW_MS = float(os.getenv("CONTACTS_PERF_TOTAL_SLOW_MS", "1000"))
CONTACTS_PERF_STEP_SLOW_MS = float(os.getenv("CONTACTS_PERF_STEP_SLOW_MS", "250"))

CONTACT_STAGE_CACHE_TTL = 3600  # 1 hora
CONTACT_STAGE_CACHE_PREFIX = "contact_stage:"
CONTACT_NAME_CACHE_TTL = 14400   # 4 horas — invalidación explícita al editar nombre (ver PATCH /contacts/{id})
CONTACT_NAME_CACHE_PREFIX = "contact_name:"

# Propiedades de HubSpot que componen el nombre mostrado en el panel. Fuente
# única: cualquier escritura que las toque debe refrescar el caché de nombres.
# Los tres escritores (CRMAgent, ContactManager y el renombrado del panel) se
# apoyan en esta tupla en vez de repetir los literales.
CONTACT_NAME_PROPERTIES = ("firstname", "lastname")


class _ContactsPerfTrace:
    """Traza interna para ubicar PERF-001 sin cambiar contrato de /contacts."""

    def __init__(self, advisor: Optional[str], filter_time: str, page: int, limit: int):
        self.advisor = advisor
        self.filter_time = filter_time
        self.page = page
        self.limit = limit
        self._start = time.perf_counter()
        self._last = self._start
        self._steps: List[Tuple[str, float]] = []

    def mark(self, step: str) -> None:
        now = time.perf_counter()
        self._steps.append((step, (now - self._last) * 1000.0))
        self._last = now

    def emit(self, *, status: str, contact_count: Optional[int] = None) -> None:
        total_ms = (time.perf_counter() - self._start) * 1000.0
        slow_steps = [
            f"{name}={elapsed:.0f}ms"
            for name, elapsed in self._steps
            if elapsed >= CONTACTS_PERF_STEP_SLOW_MS
        ]
        if status == "ok" and total_ms < CONTACTS_PERF_TOTAL_SLOW_MS and not slow_steps:
            return

        logger.warning(
            obs_event(
                "panel",
                "contacts",
                "perf_trace",
                status=status,
                advisor=self.advisor if self.advisor else "all",
                filter=self.filter_time,
                page=self.page,
                limit=self.limit,
                contact_count=contact_count if contact_count is not None else 0,
                duration_ms=round(total_ms),
                slow_steps=";".join(slow_steps) if slow_steps else "none",
            )
        )


def _contact_visible_for_advisor(
    contact: Dict[str, Any],
    *,
    advisor_id: str,
    allowed_channels: set[str],
    other_advisor_ids: set[str],
) -> bool:
    """
    Regla central de segregacion del inbox.

    El owner real manda sobre el canal. La atribucion por canal solo rescata
    conversaciones huerfanas; no puede mostrarle a una asesora un contacto que
    ya pertenece a otra.
    """
    advisor_str = str(advisor_id)
    canal_origen = (contact.get("canal_origen") or "").lower().strip()
    contact_owner = (
        contact.get("owner_id")
        or contact.get("assigned_owner_id")
        or contact.get("hubspot_owner_id")
    )
    contact_owner_str = str(contact_owner) if contact_owner else ""
    assigned_owner_ids = contact.get("assigned_owner_ids") or []
    assigned_owner_ids_str = [str(x) for x in assigned_owner_ids if x]

    if contact_owner_str == advisor_str:
        return True
    if advisor_str in assigned_owner_ids_str:
        return True
    if contact_owner_str:
        return contact_owner_str not in other_advisor_ids and canal_origen in allowed_channels
    return canal_origen in allowed_channels


def _advisor_visibility_scope(advisor_id: str) -> Tuple[set[str], set[str], bool]:
    """
    Calcula los canales y otros owners para aplicar la misma segregacion en
    lista de contactos e historial de chat.
    """
    from integrations.hubspot.lead_assigner import LeadAssigner

    advisor_str = str(advisor_id or "").strip()
    advisor_team = None

    for team_name, team_members in LeadAssigner.OWNERS_CONFIG.items():
        for member in team_members:
            if str(member.get("id")) == advisor_str:
                advisor_team = team_name
                break
        if advisor_team:
            break

    all_advisor_ids = set()
    for team_members in LeadAssigner.OWNERS_CONFIG.values():
        for member in team_members:
            member_id = member.get("id")
            if member_id:
                all_advisor_ids.add(str(member_id))

    if not advisor_team:
        return set(), all_advisor_ids - {advisor_str}, False

    allowed_channels = {
        canal
        for canal, team in LeadAssigner.CHANNEL_TO_TEAM.items()
        if team == advisor_team
    }
    if advisor_team == "default":
        allowed_channels.update(
            canal
            for canal, team in LeadAssigner.CHANNEL_TO_TEAM.items()
            if team == "default"
        )

    return allowed_channels, all_advisor_ids - {advisor_str}, True


async def _resolve_chat_visibility_contact(
    *,
    phone: Optional[str] = None,
    contact_id: Optional[str] = None,
    canal: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Obtiene el mínimo sujeto de autorización para abrir historial.

    Usa Redis/HubSpot vía _hydrate_contact cuando hay teléfono y cae a MongoDB
    para chats antiguos donde la metadata no está completa.
    """
    if phone:
        target = _resolve_panel_target(phone)
        if target.error:
            return None

        if not target.is_bsuid:
            hydrated = await _hydrate_contact(target.key, canal_hint=canal)
            if hydrated:
                return hydrated

        try:
            mongo_mgr = get_mongo_manager()
            query: Dict[str, Any] = {"phone": target.key}
            if canal:
                query["canal"] = canal.lower().strip()
            doc = await mongo_mgr.db.conversations.find_one(
                query,
                sort=[("last_activity", -1)],
                projection={
                    "phone": 1,
                    "canal": 1,
                    "canal_origen": 1,
                    "owner_id": 1,
                    "contact_id": 1,
                    "_id": 0,
                },
            )
            if doc:
                return {
                    "phone": doc.get("phone") or target.key,
                    "contact_id": doc.get("contact_id"),
                    "canal_origen": doc.get("canal_origen") or doc.get("canal") or canal,
                    "owner_id": doc.get("owner_id"),
                    "assigned_owner_ids": [],
                }
        except Exception as e:
            logger.debug(f"[Panel][Guard] Fallback Mongo conversations falló: {safe_error(e)}")

    if contact_id and str(contact_id).isdigit():
        try:
            batch = await _hubspot_batch_get_contacts([str(contact_id)])
            props = batch.get(str(contact_id)) or {}
            if props:
                return {
                    "phone": props.get("phone"),
                    "contact_id": str(contact_id),
                    "canal_origen": props.get("canal_origen") or canal,
                    "owner_id": props.get("hubspot_owner_id"),
                    "assigned_owner_ids": [],
                }
        except Exception as e:
            logger.debug(f"[Panel][Guard] HubSpot batch falló para auth historial: {safe_error(e)}")

        try:
            mongo_mgr = get_mongo_manager()
            doc = await mongo_mgr.db.messages.find_one(
                {"hubspot_contact_id": str(contact_id)},
                sort=[("timestamp", -1)],
                projection={"phone": 1, "channel": 1, "canal": 1, "_id": 0},
            )
            if doc and doc.get("phone"):
                return await _resolve_chat_visibility_contact(
                    phone=doc.get("phone"),
                    contact_id=contact_id,
                    canal=canal or doc.get("channel") or doc.get("canal"),
                )
        except Exception as e:
            logger.debug(f"[Panel][Guard] Fallback Mongo messages falló: {safe_error(e)}")

    return None


async def _assert_advisor_can_open_chat(
    *,
    advisor_id: Optional[str],
    phone: Optional[str] = None,
    contact_id: Optional[str] = None,
    canal: Optional[str] = None,
) -> None:
    """
    Bloquea la carga de historial si el chat pertenece a otra asesora.
    """
    if not advisor_id:
        return

    advisor_str = str(advisor_id).strip()
    allowed_channels, other_advisor_ids, known_advisor = _advisor_visibility_scope(advisor_str)
    if not known_advisor:
        raise HTTPException(status_code=403, detail="Asesora no autorizada para abrir este chat")

    contact = await _resolve_chat_visibility_contact(
        phone=phone,
        contact_id=contact_id,
        canal=canal,
    )
    if not contact:
        raise HTTPException(status_code=404, detail="No se pudo validar el contacto del chat")

    if not _contact_visible_for_advisor(
        contact,
        advisor_id=advisor_str,
        allowed_channels=allowed_channels,
        other_advisor_ids=other_advisor_ids,
    ):
        logger.warning(
            f"[Panel][Guard] Historial bloqueado para advisor={safe_id(advisor_str, 'advisor')} "
            f"phone={safe_phone(phone or contact.get('phone'))} "
            f"contact={safe_id(contact_id or contact.get('contact_id'), 'contact')}"
        )
        raise HTTPException(status_code=403, detail="Este chat pertenece a otra asesora")


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
        # Hooks de instrumentación — miden latencia por llamada a HubSpot.
        # Dict vacío si el profiler está apagado (sin overhead alguno).
        try:
            from middleware.query_profiler import build_httpx_event_hooks
            _event_hooks = build_httpx_event_hooks()
        except Exception:
            _event_hooks = {}

        _httpx_client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=limits,
            http2=False,  # HTTP/2 puede causar problemas con algunos CDNs
            event_hooks=_event_hooks,
        )
        logger.info("[Panel] Cliente HTTP global inicializado con connection pooling")
    return _httpx_client

# Flag para inicializar templates predefinidos solo una vez por proceso
_TEMPLATES_INITIALIZED: bool = False

# ============================================================================
# Etapas del Pipeline de HubSpot
# ============================================================================
PIPELINE_STAGES = {
    "1417459250": "Nuevo Lead",
    "1326623075": "En conversación",
    "marketingqualifiedlead": "Visita agendada",
    "salesqualifiedlead": "Visita realizada",
    "opportunity": "En estudio",
    "customer": "Cerrado ganado",
    "evangelist": "Cerrado perdido",
    "other": "No responde",
    "1407668893": "Seguimiento",
    "1326623067": "Hasta 1.5M",
    "1326631573": "Hasta 2M",
    "1326632625": "Hasta 2.5M",
    "1326631574": "De 3M en adelante",
    "1326623539": "Local o Bodega",
    "1326632628": "Otros Municipios",
    "1326623069": "Propietarios",
    "1326632209": "Otras Areas",
    "1326623541": "Ya encontro",
    "subscriber": "Reubicados",
    "lead": "Aprobado",
    "1353539189": "Venta",
    "1435775659": "Posible Espia"
}

# Lista ordenada de etapas para el frontend
PIPELINE_STAGES_LIST = [
    {"id": "1417459250", "name": "Nuevo Lead"},
    {"id": "1326623075", "name": "En conversación"},
    {"id": "marketingqualifiedlead", "name": "Visita agendada"},
    {"id": "salesqualifiedlead", "name": "Visita realizada"},
    {"id": "opportunity", "name": "En estudio"},
    {"id": "customer", "name": "Cerrado ganado"},
    {"id": "evangelist", "name": "Cerrado perdido"},
    {"id": "other", "name": "No responde"},
    {"id": "1407668893", "name": "Seguimiento"},
    {"id": "1326623067", "name": "Hasta 1.5M"},
    {"id": "1326631573", "name": "Hasta 2M"},
    {"id": "1326632625", "name": "Hasta 2.5M"},
    {"id": "1326631574", "name": "De 3M en adelante"},
    {"id": "1326623539", "name": "Local o Bodega"},
    {"id": "1326632628", "name": "Otros Municipios"},
    {"id": "1326623069", "name": "Propietarios"},
    {"id": "1326632209", "name": "Otras Areas"},
    {"id": "1326623541", "name": "Ya encontro"},
    {"id": "subscriber", "name": "Reubicados"},
    {"id": "lead", "name": "Aprobado"},
    {"id": "1353539189", "name": "Venta"},
    {"id": "1435775659", "name": "Posible Espia"}
]

@dataclass
class WindowStatus:
    """Estado de la ventana de 24 horas."""
    is_open: bool
    last_message_time: Optional[datetime]
    time_remaining_seconds: Optional[int]
    requires_template: bool
    message: str


@dataclass(frozen=True)
class PanelTarget:
    """Identidad que el panel puede usar como clave de conversación."""
    key: str
    is_bsuid: bool = False
    error: Optional[str] = None


def _plain_template_closed_window_error(
    content_sid: Optional[str],
    window_status: WindowStatus,
) -> Optional[str]:
    """Retorna el error de negocio para plantillas no aprobadas fuera de ventana."""
    if content_sid or window_status.is_open:
        return None

    return (
        "Esta plantilla no tiene ContentSid aprobado por WhatsApp y el contacto "
        "no tiene ventana de 24 horas abierta. Selecciona una plantilla aprobada "
        "o espera a que el cliente responda."
    )


async def _invalidate_contacts_response_cache(reason: str = "") -> None:
    """Borra el cache de GET /contacts tras cambios visibles en panel."""
    try:
        r = await _get_redis_client()
        keys = [key async for key in r.scan_iter("contacts_resp:*", count=200)]
        if keys:
            await r.delete(*keys)
        logger.debug(
            f"[Panel] Cache contacts_resp invalidado ({len(keys)} keys)"
            f"{f' reason={reason}' if reason else ''}"
        )
    except Exception as e:
        logger.debug(f"[Panel] No se pudo invalidar cache contacts_resp: {safe_error(e)}")


async def _publish_panel_contact_update(
    *,
    phone: Optional[str],
    canal: Optional[str],
    action: str,
    advisor_id: Optional[str] = None,
    contact_id: Optional[str] = None,
    **extra: Any,
) -> None:
    """Publica un cambio visible del panel por Redis Pub/Sub, dirigido si hay owner."""
    if not phone:
        return

    try:
        r = await _get_redis_client()
        payload = {
            "type": "contact_updated",
            "action": action,
            "phone": phone,
            "canal": canal or "whatsapp",
            "contact_id": contact_id,
            "timestamp": get_bogota_now_iso(),
            **extra,
        }
        if advisor_id:
            payload["advisor_id"] = str(advisor_id)
            await ws_manager.publish_to_advisor(r, str(advisor_id), payload)
        else:
            await ws_manager.publish_broadcast(r, payload)
    except Exception as e:
        logger.warning(f"[Panel] WS contact update falló action={action}: {safe_error(e)}")


def _normalize_contact_phone_key(phone: Optional[str]) -> str:
    """Llave estable para deduplicar contactos por teléfono."""
    if not phone:
        return ""
    value = str(phone).strip()
    if is_bsuid_identity_key(value):
        return value.lower()
    try:
        validation = PhoneNormalizer().normalize(value)
        if validation.is_valid:
            return validation.normalized
    except Exception:
        pass
    return re.sub(r"\D+", "", value)


def _contact_identity_key(contact: Dict[str, Any]) -> str:
    """Identidad canónica de contacto para impedir duplicados en el panel."""
    contact_id = contact.get("contact_id") or contact.get("id")
    if contact_id:
        return f"cid:{contact_id}"
    phone_key = _normalize_contact_phone_key(contact.get("phone") or contact.get("whatsapp"))
    return f"phone:{phone_key}" if phone_key else ""


def _contact_dedup_rank(contact: Dict[str, Any]) -> Tuple[int, str]:
    """Prioriza la copia más útil cuando varias fuentes traen el mismo contacto."""
    status = contact.get("conversation_status") or contact.get("status") or ""
    priority = 0
    if contact.get("has_unread"):
        priority += 100
    if contact.get("pending_reply"):
        priority += 50
    if status in {"HUMAN_ACTIVE", "PENDING_HANDOFF", "IN_CONVERSATION"}:
        priority += 25
    if contact.get("contact_id") or contact.get("id"):
        priority += 10
    if contact.get("display_name") and contact.get("display_name") != contact.get("phone"):
        priority += 5
    return priority, str(contact.get("last_activity") or contact.get("activated_at") or "")


def _dedupe_panel_contacts(contacts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Dedup final del panel por contact_id o teléfono normalizado.

    Redis, Mongo y HubSpot pueden traer la misma persona con formas distintas
    de teléfono o por canal. La UI debe ver un solo contacto operativo.
    """
    by_key: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    for contact in contacts:
        key = _contact_identity_key(contact)
        if not key:
            order.append(f"anon:{len(order)}")
            by_key[order[-1]] = contact
            continue

        if key not in by_key:
            by_key[key] = contact
            order.append(key)
            continue

        current = by_key[key]
        if _contact_dedup_rank(contact) > _contact_dedup_rank(current):
            merged = {**current, **contact}
        else:
            merged = {**contact, **current}

        merged["has_unread"] = bool(current.get("has_unread") or contact.get("has_unread"))
        merged["pending_reply"] = bool(current.get("pending_reply") or contact.get("pending_reply"))
        by_key[key] = merged

    deduped = [by_key[key] for key in order]
    removed = len(contacts) - len(deduped)
    if removed:
        logger.warning(f"[Panel][Dedup] {removed} contactos duplicados removidos antes de responder")
    return deduped


def _build_contacts_page_after_dedupe(
    always_contacts: List[Dict[str, Any]],
    rest_contacts: List[Dict[str, Any]],
    limit: int,
) -> List[Dict[str, Any]]:
    """
    Arma la pagina final sin perder cupos por duplicados.

    Los contactos que deben permanecer visibles entran todos. El resto se
    completa hasta `limit` contactos unicos; asi un duplicado dentro de los
    primeros N no hace que alguien desaparezca del inbox aunque exista mas abajo.
    """
    by_key: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    def _add(contact: Dict[str, Any]) -> bool:
        key = _contact_identity_key(contact) or f"anon:{len(order)}"
        is_new = key not in by_key
        if is_new:
            by_key[key] = contact
            order.append(key)
            return True

        current = by_key[key]
        if _contact_dedup_rank(contact) > _contact_dedup_rank(current):
            merged = {**current, **contact}
        else:
            merged = {**contact, **current}
        merged["has_unread"] = bool(current.get("has_unread") or contact.get("has_unread"))
        merged["pending_reply"] = bool(current.get("pending_reply") or contact.get("pending_reply"))
        by_key[key] = merged
        return False

    for contact in always_contacts:
        _add(contact)

    rest_added = 0
    for contact in rest_contacts:
        if _add(contact):
            rest_added += 1
        if rest_added >= limit:
            break

    result = [by_key[key] for key in order]
    removed = len(always_contacts) + min(len(rest_contacts), max(limit, 0)) - len(result)
    if removed > 0:
        logger.warning(f"[Panel][Dedup] {removed} duplicados removidos y pagina rellenada antes de responder")
    return result


# ============================================================================
# Funciones auxiliares
# ============================================================================

def _resolve_panel_target(raw: Optional[str]) -> PanelTarget:
    """
    Resuelve el identificador recibido por el panel.

    Teléfonos pasan por PhoneNormalizer como antes. Las llaves BSUID se
    conservan intactas: no son teléfonos y no deben entrar al normalizador.
    """
    value = (raw or "").strip()
    if not value:
        return PanelTarget(key="", error="Campo 'to' (o 'phone') es requerido")

    if is_bsuid_identity_key(value):
        return PanelTarget(key=value.lower(), is_bsuid=True)

    normalizer = PhoneNormalizer()
    validation = normalizer.normalize(value)
    if not validation.is_valid:
        return PanelTarget(
            key="",
            error=f"Número inválido: {validation.error_message}",
        )
    return PanelTarget(key=validation.normalized, is_bsuid=False)


async def _resolve_outbound_address(target: PanelTarget, canal: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Devuelve la dirección real que Twilio debe recibir.

    Para teléfono es la propia clave E.164. Para BSUID la clave interna solo
    sirve en Redis/Mongo; el routing real vive en conv_meta.routing_address.
    """
    if target.error:
        return None, target.error
    if not target.is_bsuid:
        return target.key, None

    try:
        meta = await _get_state_manager().get_meta(target.key, canal or "whatsapp")
        routing_address = getattr(meta, "routing_address", None) if meta else None
    except Exception as e:
        logger.warning(f"[Panel][BSUID] No se pudo leer routing_address: {safe_error(e)}")
        routing_address = None

    if not is_whatsapp_bsuid(routing_address):
        return (
            None,
            "La conversación BSUID no tiene dirección de routing válida para responder por WhatsApp.",
        )
    return routing_address, None


async def _ensure_bsuid_hubspot_contact(
    target: PanelTarget,
    canal: str,
    contact_id: Optional[str] = None,
) -> Optional[str]:
    """Busca o crea un contacto HubSpot para BSUID usando whatsapp_id, nunca phone."""
    if contact_id or not target.is_bsuid:
        return contact_id

    state_manager = _get_state_manager()
    meta = await state_manager.get_meta(target.key, canal or "whatsapp")

    hubspot = _get_contact_manager().hubspot
    existing_id = await hubspot.search_contact_by_phone(target.key)
    if existing_id:
        await state_manager.ensure_meta_with_channel(
            phone=target.key,
            canal=canal or "whatsapp",
            contact_id=existing_id,
            identity_type="bsuid",
            has_phone=False,
            add_to_zset=True,
        )
        return existing_id

    display_name = (
        getattr(meta, "display_name", None)
        or getattr(meta, "username", None)
        or "Contacto WhatsApp"
    )
    firstname = str(display_name).strip()[:80] or "Contacto WhatsApp"
    owner_id = getattr(meta, "assigned_owner_id", None) if meta else None
    hs_canal = "whatsapp_directo" if (canal or "whatsapp") == "whatsapp" else (canal or "whatsapp")

    midnight_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    properties: Dict[str, Any] = {
        "firstname": firstname,
        "lastname": "WhatsApp",
        "whatsapp_id": target.key,
        "canal_origen": hs_canal,
        "chatbot_timestamp": str(int(midnight_utc.timestamp() * 1000)),
        "lifecyclestage": HUBSPOT_STAGE_NUEVO_LEAD,
    }
    if owner_id:
        properties["hubspot_owner_id"] = owner_id

    created_id = await hubspot.create_contact(properties)
    await state_manager.ensure_meta_with_channel(
        phone=target.key,
        canal=canal or "whatsapp",
        contact_id=created_id,
        display_name=firstname,
        owner_id=owner_id,
        identity_type="bsuid",
        has_phone=False,
        add_to_zset=True,
    )
    try:
        r = await _get_redis_client()
        await r.set(f"phone_cache:{target.key}", created_id, ex=86400)
        await r.set(f"phone_cache:{created_id}", target.key, ex=86400)
    except Exception:
        pass
    logger.info(f"[Panel][BSUID] Contacto HubSpot asegurado: {safe_id(created_id, 'contact')} para {target.key}")
    return created_id

from utils.panel_auth import _validate_api_key  # canonical home; re-exported for patches


async def _get_contact_lifecyclestage(contact_id: str, strict: bool = False) -> Optional[str]:
    """
    Lee la propiedad lifecyclestage del Contact en HubSpot.
    Usa caché en Redis (1h) para reducir llamadas a la API.

    strict=False (por defecto): si no se puede leer, devuelve "En conversación".
    Es un fail-open deliberado — STAGES_VISIBLES_WORKER lo contiene, así que un
    429 de HubSpot deja la cita visible en el reporte por asesor en vez de
    borrarla. NO cambiar sin revisar los 5 llamantes que dependen de ello.

    strict=True: devuelve None cuando la lectura falla, para que el llamante
    distinga "la etapa es otra" de "no pude averiguar la etapa". Lo usa la
    promoción de Nuevo Lead, donde tragarse el error significa perder el lead.
    """
    _fallback = None if strict else HUBSPOT_STAGE_EN_CONVERSACION

    if not contact_id or not HUBSPOT_API_KEY:
        return _fallback

    cache_key = _contact_stage_cache_key(contact_id)
    try:
        redis_client = await _get_redis_client()
        cached = await redis_client.get(cache_key)
        if cached:
            # El pool se crea con decode_responses=True, así que esto ya es str.
            # Llamar .decode() a secas lanzaba AttributeError, el except lo tragaba
            # y el cache NUNCA acertaba: cada contacto pedía su etapa a HubSpot en
            # cada carga del panel. El isinstance cubre ambos clientes.
            return cached if isinstance(cached, str) else cached.decode()
    except Exception:
        pass

    try:
        url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}?properties=lifecyclestage"
        response = await _hubspot_get(None, url, HUBSPOT_API_KEY)
        if response.status_code == 200:
            stage = response.json().get("properties", {}).get("lifecyclestage") or HUBSPOT_STAGE_EN_CONVERSACION
            try:
                redis_client = await _get_redis_client()
                # nx: escritor best-effort. Si la key ya existe es porque alguien
                # acaba de cambiar la etapa y dejó ahí el valor bueno; este stage
                # se leyó de HubSpot antes de ese cambio y lo pisaría.
                await redis_client.set(
                    cache_key, stage, ex=CONTACT_STAGE_CACHE_TTL, nx=True
                )
            except Exception:
                pass
            return stage
    except Exception as e:
        logger.debug(f"[Panel] Error leyendo lifecyclestage de contacto {safe_id(contact_id, 'contact')}: {safe_error(e)}")

    return _fallback


async def _invalidate_contact_stage_cache(
    contact_id: str, new_stage: Optional[str] = None
) -> None:
    """
    Sincroniza el caché de lifecyclestage tras un cambio de etapa confirmado.

    Con new_stage escribe el valor nuevo en vez de borrar la key. Dos motivos:

    1. Ahorra la llamada a HubSpot que el DELETE forzaba en la siguiente lectura.
    2. Cierra la carrera que dejaba el chip del panel pegado hasta 1 hora:
       GET /contacts tarda 7-9s y al terminar dispara _cache_contact_stage() con
       la etapa que leyó AL EMPEZAR. Si la asesora responde en medio, esa tarea
       de fondo resucitaba "Nuevo Lead" con TTL de 1h sobre la key recién
       borrada. Dejando aquí el valor bueno, el `nx` de los escritores
       best-effort hace que la escritura tardía se decline sola.

    Los cuatro llamantes invocan esto dentro de un `if status_code == 200`, así
    que new_stage es siempre la etapa que HubSpot acaba de aceptar.
    """
    try:
        redis_client = await _get_redis_client()
        cache_key = _contact_stage_cache_key(contact_id)
        if new_stage:
            await redis_client.set(cache_key, new_stage, ex=CONTACT_STAGE_CACHE_TTL)
            logger.debug(f"[Panel] Caché de stage fijado a '{new_stage}' para {safe_id(contact_id, 'contact')}")
        else:
            await redis_client.delete(cache_key)
            logger.debug(f"[Panel] Caché de stage invalidado para {safe_id(contact_id, 'contact')}")
    except Exception as e:
        logger.debug(f"[Panel] Error sincronizando contact stage cache: {safe_error(e)}")


async def _release_contacts_inflight(redis_client, inflight_key: Optional[str]) -> None:
    """Suelta el turno de reconstrucción para que el siguiente poll no espere en balde."""
    if not inflight_key:
        return
    try:
        await redis_client.delete(inflight_key)
    except Exception:
        pass  # El TTL lo limpia igual; no vale romper la respuesta por esto.


async def _await_contacts_inflight(redis_client, cache_key: str):
    """
    Espera a que la petición en curso publique su resultado.

    Devuelve la respuesta cacheada si aparece dentro de la ventana, o None si se
    agota el tiempo — en ese caso el llamador reconstruye por su cuenta
    (fail-open: preferimos duplicar trabajo antes que devolver un error).
    """
    _waited = 0.0
    while _waited < CONTACTS_INFLIGHT_WAIT_SECONDS:
        await asyncio.sleep(CONTACTS_INFLIGHT_POLL_INTERVAL)
        _waited += CONTACTS_INFLIGHT_POLL_INTERVAL
        try:
            _cached = await redis_client.get(cache_key)
            if _cached:
                return json.loads(_cached)
        except Exception:
            return None
    return None


async def _cache_contact_stage(contact_id: str, stage: str) -> None:
    """
    Guarda lifecyclestage en Redis cache en background (sin bloquear el request).

    """
    try:
        redis_client = await _get_redis_client()
        await redis_client.set(
            _contact_stage_cache_key(contact_id), stage,
            ex=CONTACT_STAGE_CACHE_TTL, nx=True,
        )
    except Exception:
        pass


def _contact_stage_cache_key(contact_id: str) -> str:
    """Única forma de construir la key del caché de etapas."""
    return f"{CONTACT_STAGE_CACHE_PREFIX}{contact_id}"


def _contact_name_cache_key(contact_id: str) -> str:
    """Única forma de construir la key del caché de nombres."""
    return f"{CONTACT_NAME_CACHE_PREFIX}{contact_id}"


def _contact_name_payload(firstname: str, lastname: str) -> str:
    """Único formato serializado del caché de nombres."""
    return json.dumps({"firstname": firstname or "", "lastname": lastname or ""})


async def _cache_contact_name(contact_id: str, firstname: str, lastname: str) -> None:
    """
    Guarda nombre del contacto en Redis cache (4h TTL). No bloquea el request.

    nx: escritor best-effort. La tarea nace del enriquecimiento de GET /contacts
    —que tarda segundos— con el nombre leído al empezar el request. Si mientras
    tanto Sofía capturó el nombre real o una asesora lo editó, la key ya tiene
    el valor bueno y esta escritura debe declinarse. Sin el nx, el nombre viejo
    (normalmente el propio teléfono) revivía durante 4 horas.
    """
    if not contact_id:
        return
    try:
        r = await _get_redis_client()
        await r.set(
            _contact_name_cache_key(contact_id),
            _contact_name_payload(firstname, lastname),
            ex=CONTACT_NAME_CACHE_TTL,
            nx=True,
        )
    except Exception:
        pass


async def _invalidate_contact_name_cache(
    contact_id: str,
    firstname: Optional[str] = None,
    lastname: Optional[str] = None,
) -> None:
    """
    Escritor AUTORITATIVO del caché de nombres. Se llama tras un cambio que
    HubSpot ya confirmó.

    Con firstname/lastname deja el valor nuevo, lo que además ahorra la ida a
    HubSpot que el DELETE forzaba en el siguiente poll. Sin ellos, borra.
    """
    try:
        r = await _get_redis_client()
        key = _contact_name_cache_key(contact_id)
        if firstname is not None or lastname is not None:
            await r.set(
                key,
                _contact_name_payload(firstname or "", lastname or ""),
                ex=CONTACT_NAME_CACHE_TTL,
            )
            logger.debug(f"[Panel] Caché de nombre fijado para {safe_id(contact_id, 'contact')}")
        else:
            await r.delete(key)
            logger.debug(f"[Panel] Caché de nombre invalidado para {safe_id(contact_id, 'contact')}")
    except Exception as e:
        logger.debug(f"[Panel] Error sincronizando contact name cache: {safe_error(e)}")


async def _on_hubspot_contact_updated(
    contact_id: str, properties: Dict[str, Any]
) -> None:
    """
    Hook registrado en hubspot_client: refresca el caché de nombres cuando
    cualquiera de los tres escritores toca el nombre en HubSpot.

    El más frecuente es ContactManager.update_contact_info(), que dispara cuando
    Sofía captura el nombre real a mitad de conversación. Sin esto, el panel
    seguía mostrando el valor viejo —casi siempre el teléfono, que es como se
    crean los leads— hasta 4 horas.
    """
    if not contact_id or not properties:
        return
    presentes = [p for p in CONTACT_NAME_PROPERTIES if p in properties]
    if not presentes:
        return  # el cambio no toca el nombre: nada que refrescar

    if len(presentes) == len(CONTACT_NAME_PROPERTIES):
        # Update completo: sabemos el nombre entero, lo dejamos escrito y el
        # panel se ahorra la ida a HubSpot.
        await _invalidate_contact_name_cache(
            contact_id,
            firstname=properties.get("firstname"),
            lastname=properties.get("lastname"),
        )
    else:
        # Update parcial (p.ej. solo firstname): escribir el payload completo
        # borraría la otra mitad del nombre. Se invalida y que HubSpot mande.
        await _invalidate_contact_name_cache(contact_id)


_register_contact_update_hook(_on_hubspot_contact_updated)


async def _get_cached_contact_name(contact_id: str) -> Optional[Dict[str, Any]]:
    """Lee nombre del contacto desde Redis cache. Retorna None si no está cacheado."""
    if not contact_id:
        return None
    try:
        r = await _get_redis_client()
        raw = await r.get(_contact_name_cache_key(contact_id))
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return None


async def _get_cached_contact_names_batch(
    contact_ids: list,
) -> Dict[str, Dict[str, Any]]:
    """
    Lee N nombres cacheados en UN solo round-trip a Redis (MGET).

    ⚠️ Reemplaza el patrón `for cid in ids: await _get_cached_contact_name(cid)`,
    que hacía un viaje a Redis POR CONTACTO. Medido en producción con el
    profiler (9-ago-2026): con ~300 contactos eran ~5 s de los ~8 s totales de
    GET /contacts — el 69% del tiempo del endpoint, más que HubSpot.

    Fail-open: ante cualquier error devuelve {} (equivale a cache miss total),
    y los nombres se resuelven vía _hubspot_batch_get_contacts como siempre.

    Args:
        contact_ids: IDs de HubSpot. Acepta duplicados.

    Returns:
        {contact_id: {"firstname": str, "lastname": str}} solo con los que
        estaban cacheados. Los ausentes simplemente no aparecen.
    """
    if not contact_ids:
        return {}

    result: Dict[str, Dict[str, Any]] = {}
    try:
        r = await _get_redis_client()
        keys = [_contact_name_cache_key(cid) for cid in contact_ids]
        raws = await r.mget(keys)
        for cid, raw in zip(contact_ids, raws):
            if not raw:
                continue
            try:
                result[cid] = json.loads(raw)
            except (ValueError, TypeError):
                continue  # entrada corrupta → tratar como miss
    except Exception as e:
        logger.warning(
            f"[Panel] MGET de nombres cacheados falló ({len(contact_ids)} ids), "
            f"se resolverán vía HubSpot: {e}"
        )
        return {}
    return result


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


# ── Circuit breaker de HubSpot 
# Dormir 12s dentro del request no descongestiona nada: HubSpot sigue saturado
# porque el resto de peticiones sigue llegando. Y como las llamadas van detrás de
# un Semaphore(2), esas esperas se SUMAN en vez de solaparse — de ahí los
# GET /contacts de 300-500s medidos el 10-ago-2026.
#
# Al primer 429 se abre el breaker: durante los siguientes segundos las llamadas
# fallan rápido en vez de dormir. El panel muestra el valor por defecto (o el de
# cache) en lugar de quedarse colgado minutos.
HUBSPOT_BREAKER_SECONDS = 10   # ventana de HubSpot: TEN_SECONDLY_ROLLING
# Espera entre reintentos cuando el breaker aún no estaba abierto. Antes eran 12s,
# calibrados para "cubrir la ventana de 10s de HubSpot" — pero con el breaker
# delante los reintentos son raros, y 12s dentro del request era el multiplicador
# que llevaba el panel a minutos.
HUBSPOT_RETRY_BASE_SECONDS = 3
_hubspot_breaker_until: float = 0.0
_hubspot_breaker_trips: int = 0


class HubSpotRateLimited(Exception):
    """HubSpot está rechazando por rate limit; no reintentar dentro del request."""


def _hubspot_breaker_open() -> bool:
    """True si hay un 429 reciente y conviene no volver a llamar todavía."""
    return time.monotonic() < _hubspot_breaker_until


def _hubspot_breaker_trip() -> None:
    """
    Abre el breaker tras un 429.

    Loguea solo la transición cerrado→abierto, no cada 429: durante un pico entran
    decenas por segundo y el log dejaría de ser legible. El contador dice cuántas
    veces se ha abierto desde que arrancó el proceso, que es la señal a vigilar.
    """
    global _hubspot_breaker_until, _hubspot_breaker_trips
    _was_open = _hubspot_breaker_open()
    _hubspot_breaker_until = time.monotonic() + HUBSPOT_BREAKER_SECONDS
    if not _was_open:
        _hubspot_breaker_trips += 1
        logger.warning(
            "[HubSpot] Circuit breaker ABIERTO %ss tras 429 — las lecturas "
            "degradan a valor por defecto (apertura #%d)",
            HUBSPOT_BREAKER_SECONDS, _hubspot_breaker_trips
        )


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
            # Los POST no cortan por breaker (suelen ser escrituras que no
            # conviene perder), pero sí lo abren para que las lecturas —que son
            # el grueso del volumen— dejen de insistir.
            _hubspot_breaker_trip()
            wait = HUBSPOT_RETRY_BASE_SECONDS * attempt
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

    # Breaker abierto: no gastar el turno del Semaphore ni sumar otra llamada a
    # una API que ya está rechazando. El llamador cae a su valor por defecto.
    if _hubspot_breaker_open():
        raise HubSpotRateLimited("circuit breaker abierto tras 429 reciente")

    headers = {"Authorization": f"Bearer {api_key}"}
    for attempt in range(1, max_retries + 1):
        response = await client.get(url, headers=headers, params=params)
        if response.status_code == 429:
            _hubspot_breaker_trip()
            wait = HUBSPOT_RETRY_BASE_SECONDS * attempt
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

    Como en el POST, un 429 aquí abre el breaker para que las lecturas —el grueso
    del volumen— dejen de insistir, pero la escritura sí se reintenta: perderla
    dejaría el CRM desincronizado.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    client = get_httpx_client()  # cliente global con pool persistente
    for attempt in range(1, max_retries + 1):
        response = await client.patch(url, headers=headers, json=payload)
        if response.status_code == 429:
            _hubspot_breaker_trip()
            wait = HUBSPOT_RETRY_BASE_SECONDS * attempt
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
        "properties": ["firstname", "lastname", "email", "phone", "lifecyclestage", "hubspot_owner_id", "canal_origen"],
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
                    "canal_origen": props.get("canal_origen") or "",
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
        logger.error(f"[Panel] Error en batch HubSpot: {type(e).__name__}: {safe_error(e)}", exc_info=True)
        return {}


# ============================================================================
# Contact Hydration Service — pipeline unificada Redis → HubSpot → Mongo
# ============================================================================

async def _build_zset_phone_index() -> Dict[str, Dict[str, float]]:
    """
    Lee el ZSET del panel UNA vez y lo indexa por teléfono.

    Returns:
        {phone: {canal: score}}. Vacío si Redis falla — los llamadores deben
        tratar el dict vacío como "sin traza en el ZSET", que es exactamente
        lo que hacía el ZSCAN al fallar.
    """
    index: Dict[str, Dict[str, float]] = {}
    try:
        r = await _get_redis_client()
        members = await r.zrange(
            ConversationStateManager.ACTIVE_CONTACTS_ZSET, 0, -1, withscores=True
        )
        for raw_member, score in members:
            m = raw_member if isinstance(raw_member, str) else raw_member.decode()
            if ":" not in m:
                continue
            _p, canal_part = m.split(":", 1)
            index.setdefault(_p, {})[canal_part.lower()] = (
                float(score) if score is not None else 0.0
            )
    except Exception as e:
        logger.warning(f"[Panel] No se pudo indexar el ZSET: {safe_error(e)}")
        return {}
    return index


async def _hydrate_contact(
    phone: str,
    canal_hint: Optional[str] = None,
    add_to_zset: bool = False,
    zset_index: Optional[Dict[str, Dict[str, float]]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Materializa un contacto completo desde Redis + HubSpot + MongoDB.
    """
    # ── Paso A: Normalizar phone ────────────────────────────────────────────
    try:
        validation = PhoneNormalizer().normalize(phone)
    except Exception as e:
        logger.warning(f"[Hydrate] Error normalizando phone {safe_phone(phone)}: {safe_error(e)}")
        return None
    if not validation.is_valid:
        logger.debug(f"[Hydrate] Phone inválido {safe_phone(phone)}: {safe_error(validation.error_message)}")
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

    # ── Paso B: detectar canales en el panel ────────────────────────────────
    # Si el llamador ya indexó el ZSET (hidratación en lote), se usa ese índice
    # y nos ahorramos el ZSCAN entero. Ver _build_zset_phone_index().
    score_by_canal: Dict[str, float] = {}
    if zset_index is not None:
        score_by_canal = dict(zset_index.get(phone_norm, {}))
    else:
        try:
            cursor = 0
            while True:
                cursor, results = await redis_client.zscan(
                    ConversationStateManager.ACTIVE_CONTACTS_ZSET,
                    cursor,
                    match=f"{phone_norm}:*",
                    # count=20 obligaba a ~N/20 round-trips para recorrer el ZSET
                    # completo (MATCH filtra DESPUES de escanear, no antes).
                    count=1000,
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
            logger.warning(f"[Hydrate] Error zscan ZSET para {safe_phone(phone_norm)}: {safe_error(e)}")

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
        logger.warning(f"[Hydrate] Error pipeline lectura state/meta para {safe_phone(phone_norm)}: {safe_error(e)}")

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
            logger.debug(f"[Hydrate] _search_contact falló para {safe_phone(phone_norm)}: {safe_error(e)}")

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
            logger.debug(f"[Hydrate] HubSpot direct lookup falló para {safe_phone(phone_norm)}: {safe_error(e)}")

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
            logger.debug(f"[Hydrate] batch HubSpot falló para {safe_id(contact_id, 'contact')}: {safe_error(e)}")

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
            logger.debug(f"[Hydrate] search_contact_by_phone_with_properties falló: {safe_error(e)}")

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
        # Identidad: sin esto el panel solo puede deducir que un contacto no
        # tiene teléfono mirando si la clave empieza por "bsuid_", y no sabría
        # cuándo ofrecer el botón de agregar número.
        "identity_type": best_meta_dict.get("identity_type") or "phone",
        "has_phone": bool(
            best_meta_dict.get("has_phone", not is_bsuid_identity_key(phone_norm))
        ),
        "routing_address": best_meta_dict.get("routing_address"),
    }

    # Si no hay rastro alguno (ni Redis ni HubSpot), retornar None — contacto inexistente
    if source == "phone" and not best_meta_dict:
        logger.info(f"[Hydrate] {safe_phone(phone_norm)} sin rastro en Redis/HubSpot → None")
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
            logger.warning(f"[Hydrate] Error persistiendo {safe_phone(phone_norm)} al panel: {safe_error(e)}")

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


async def _update_contact_to_visita_agendada(contact_id: str) -> Tuple[bool, str]:
    """
    Pone el contacto en 'Visita agendada' al crear una cita en el panel.

    POR QUÉ EXISTE
        Medido el 19-ago-2026 sobre los 310 contactos con cita de los últimos 90
        días: 300 (96.8%) ya pasaban por 'Visita agendada', y la marca cae a una
        mediana de 12 segundos ANTES de crearse la cita. Es decir, la asesora ya
        hacía este paso a mano, justo antes de agendar. Esto no inventa una regla
        nueva: automatiza un ritual de dos clics.
        Los 10 restantes (3.2%) hicieron la visita y el embudo nunca se enteró:
        `_update_contact_to_visita_realizada` sólo avanza desde 'Visita agendada',
        así que sin esta marca nunca llegaron a 'Visita realizada'.

    SIN GUARD DE ETAPAS PROTEGIDAS, a propósito
        Al contrario que `_update_contact_to_visita_realizada`, aquí no se respeta
        `PROTECTED_STAGES_POST_VISITA`. De esos 310 contactos, 10 (3.2%) venían de
        una etapa avanzada y la asesora la pisó igual, a mano. Agendar una visita
        es un acto explícito y más reciente que la etapa vieja; bloquearlo
        obligaría a la asesora a ir a moverla a mano, que es justo lo que esto
        quita. Decisión del usuario, 19-ago-2026.

    Devuelve (ok, detalle). Nunca lanza: el llamador ya guardó la cita y un fallo
    de etapa no puede tumbar el agendamiento.
    """
    if not HUBSPOT_API_KEY or not contact_id:
        return False, "sin_credenciales"

    etapa_previa = None
    try:
        etapa_previa = await _get_contact_lifecyclestage(contact_id)
        if etapa_previa == HUBSPOT_STAGE_VISITA_AGENDADA:
            return True, "ya_estaba"

        url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"

        # Dos pasos obligatorios: lifecyclestage en HubSpot es unidireccional y
        # sólo deja avanzar. Limpiar primero permite moverlo en cualquier
        # dirección. Mismo patrón que `update_contact_stage`.
        limpiado = await _hubspot_patch(url, {"properties": {"lifecyclestage": ""}}, HUBSPOT_API_KEY)
        if limpiado.status_code != 200:
            # No se llegó a tocar nada: el contacto sigue en su etapa.
            return False, f"limpiar_HTTP_{limpiado.status_code}"

        fijado = await _hubspot_patch(
            url, {"properties": {"lifecyclestage": HUBSPOT_STAGE_VISITA_AGENDADA}}, HUBSPOT_API_KEY
        )
        if fijado.status_code == 200:
            await _invalidate_contact_stage_cache(contact_id, HUBSPOT_STAGE_VISITA_AGENDADA)
            logger.info(
                f"[Lifecycle] Contacto {contact_id}: {etapa_previa} → "
                f"{HUBSPOT_STAGE_VISITA_AGENDADA} (cita agendada)"
            )
            return True, "movido"

        # ── Entre limpiar y fijar el contacto NO tiene etapa ──────────────────
        # Ahí es invisible en el panel y fuera del alcance de cualquier regla que
        # filtre por embudo. Pasó de verdad el 18-ago-2026 con la depuración de
        # "No responde" y dejó un contacto atrapado. Se deshace.
        return False, await _deshacer_etapa(
            contact_id, etapa_previa, f"fijar_HTTP_{fijado.status_code}"
        )
    except Exception as e:
        # Un corte de red entre los dos pasos deja el mismo hueco que un HTTP malo.
        logger.warning(f"[Lifecycle] Error moviendo {safe_id(contact_id, 'contact')} a Visita agendada: {safe_error(e)}")
        if etapa_previa:
            return False, await _deshacer_etapa(contact_id, etapa_previa, "error_de_red")
        return False, "error_de_red"


async def _deshacer_etapa(contact_id: str, etapa_previa: Optional[str], causa: str) -> str:
    """
    Devuelve el contacto a su etapa original tras un cambio a medias.

    Un contacto sin etapa no lo ve nadie: ni el panel, ni las reglas de embudo, ni
    la asesora. Es peor que no haber intentado el cambio, así que si el segundo
    paso falla se restaura lo que había.
    """
    if not etapa_previa:
        return f"{causa}; quedó SIN ETAPA (no se conocía la anterior)"
    try:
        url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
        vuelta = await _hubspot_patch(
            url, {"properties": {"lifecyclestage": etapa_previa}}, HUBSPOT_API_KEY
        )
        if vuelta.status_code == 200:
            await _invalidate_contact_stage_cache(contact_id, etapa_previa)
            logger.warning(
                f"[Lifecycle] {contact_id}: {causa} — deshecho, sigue en {etapa_previa}"
            )
            return f"{causa} (deshecho: sigue en {etapa_previa})"
    except Exception as e:
        logger.error(f"[Lifecycle] {safe_id(contact_id, 'contact')}: fallo al deshacer tras {causa}: {safe_error(e)}")
    logger.error(
        f"[Lifecycle] CONTACTO SIN ETAPA: {contact_id} tras {causa} — "
        f"no se pudo devolver a {etapa_previa}"
    )
    return f"{causa}; ADEMÁS quedó SIN ETAPA (no se pudo deshacer)"


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
            await _invalidate_contact_stage_cache(
                contact_id, HUBSPOT_STAGE_VISITA_REALIZADA
            )
        else:
            logger.warning(
                f"[Lifecycle] Error actualizando lifecyclestage: {r.status_code} - {r.text}"
            )
    except Exception as e:
        logger.error(f"[Lifecycle] Error en _update_contact_to_visita_realizada: {safe_error(e)}")


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
            await _invalidate_contact_stage_cache(
                contact_id, HUBSPOT_STAGE_EN_CONVERSACION
            )
            await r.delete(flag_key)
            return True
        logger.warning(
            f"[BulkNoResponde] Error PATCH HubSpot {contact_id}: "
            f"{resp.status_code} - {resp.text[:200]}"
        )
        return False
    except Exception as e:
        logger.error(f"[BulkNoResponde] Error en promoción {safe_id(contact_id, 'contact')}: {safe_error(e)}")
        return False


async def _promote_nuevo_lead_to_en_conversacion(
    contact_id: str,
    phone_normalized: str,
) -> bool:
    """
    Promueve 'Nuevo Lead' (1417459250) → 'En conversación' (1326623075) cuando una
    asesora responde manualmente.

    Se llama desde _update_advisor_timestamp(), cuyo único origen es
    POST /send-message. Los masivos (_send_bulk_template_message) y Sofía NO pasan
    por ahí, así que un lead solo sale de "Nuevo Lead" por contacto humano real.

    El guard es lo importante: solo promueve si la etapa actual es exactamente
    "Nuevo Lead". Un contacto en Visita agendada, Seguimiento o Cerrado ganado no
    se toca aunque la asesora le escriba.
    """
    if not HUBSPOT_API_KEY or not contact_id or not phone_normalized:
        return False
    try:
        # strict: sin esto, un 429 o un timeout de HubSpot devolvía
        # "En conversación" y el guard de abajo daba el lead por promovido. El
        # lead se quedaba en "Nuevo Lead" para siempre y no quedaba ni un log.
        current_stage = await _get_contact_lifecyclestage(contact_id, strict=True)
        if current_stage is None:
            logger.warning(
                f"[NuevoLead] No se pudo leer la etapa de {contact_id} — "
                f"promoción pospuesta al siguiente mensaje de la asesora"
            )
            return False
        if current_stage != HUBSPOT_STAGE_NUEVO_LEAD:
            # Caso normal y mayoritario: el lead ya avanzó del embudo de entrada.
            logger.debug(
                f"[NuevoLead] Contacto {contact_id} en '{current_stage}' — "
                f"no es 'Nuevo Lead', skip"
            )
            return False  # caso normal — el lead ya avanzó del embudo de entrada

        url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
        headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}", "Content-Type": "application/json"}
        client = get_httpx_client()
        resp = await client.patch(
            url, headers=headers,
            json={"properties": {"lifecyclestage": HUBSPOT_STAGE_EN_CONVERSACION}},
        )
        if resp.status_code == 200:
            logger.info(
                f"[NuevoLead] Contacto {contact_id}: Nuevo Lead → En conversación "
                f"(la asesora respondió)"
            )
            await _invalidate_contact_stage_cache(
                contact_id, HUBSPOT_STAGE_EN_CONVERSACION
            )
            return True
        logger.warning(
            f"[NuevoLead] Error PATCH HubSpot {contact_id}: "
            f"{resp.status_code} - {resp.text[:200]}"
        )
        return False
    except Exception as e:
        logger.error(f"[NuevoLead] Error promoviendo {safe_id(contact_id, 'contact')}: {safe_error(e)}")
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
            if is_bsuid_identity_key(phone_normalized):
                try:
                    mongo_manager = get_mongo_manager()
                    if await mongo_manager.connect():
                        doc = await mongo_manager.db.messages.find_one(
                            {"phone": phone_normalized, "sender": "client"},
                            sort=[("timestamp", -1)],
                            projection={"timestamp": 1},
                        )
                        last_ts = doc.get("timestamp") if doc else None
                        if isinstance(last_ts, datetime):
                            last_msg_time_mongo = (
                                last_ts.replace(tzinfo=timezone.utc)
                                if last_ts.tzinfo is None
                                else last_ts.astimezone(timezone.utc)
                            )
                            last_msg_str = last_msg_time_mongo.isoformat()
                            now = datetime.now(timezone.utc)
                            ttl = max(60, int((last_msg_time_mongo + timedelta(hours=25) - now).total_seconds()))
                            await r.set(key, last_msg_str, ex=ttl)
                            logger.info(f"[Panel][BSUID] Ventana 24h recuperada desde Mongo para {phone_normalized}")
                except Exception as e:
                    logger.warning(f"[Panel][BSUID] No se pudo recuperar ventana 24h desde Mongo: {safe_error(e)}")

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
        logger.error(f"[Panel] Error verificando ventana 24h: {safe_error(e)}")
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

        logger.debug(f"[Panel] Actualizado último mensaje del cliente: {safe_phone(phone_normalized)}")

    except Exception as e:
        logger.error(f"[Panel] Error actualizando último mensaje: {safe_error(e)}")


# Funciones CRUD de Templates

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
        logger.error(f"[Templates] Error inicializando templates: {safe_error(e)}")


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


def _picker_visible(template: dict, permitidas: Optional[list]) -> bool:
    """
    Decide si una plantilla se ofrece en el picker del chat de esta asesora.

    Why: el reparto por asesora se decide aquí y no en el navegador porque
    `ADVISOR_ID` sale de un query param de la URL (index.js:126-133) — filtrar en
    cliente sería cosmético. El endpoint sigue devolviendo TODAS las plantillas:
    el modal de administración las necesita. Esto solo las etiqueta.
    """
    tid = template.get("id")

    # Plantilla creada por la asesora desde el modal del panel: intocable.
    # Se mira la pertenencia al catálogo y no el flag `is_default` porque editar
    # una predefinida la copia al namespace personal y el flag deja de ser fiable.
    if tid not in DEFAULT_TEMPLATES:
        return True

    if tid in SOLO_AUTOMATICAS:
        return False

    # `permitidas is None` = asesora sin reparto definido: ve el catálogo entero.
    return permitidas is None or tid in permitidas


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
    logger.info(f"[Templates] Template guardado: {safe_id(template_id, 'template')} para {safe_id(advisor_id, 'advisor')}")
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
        logger.info(f"[Templates] Template eliminado: {safe_id(template_id, 'template')} para {safe_id(advisor_id, 'advisor')}")
        return True
    return False

# ============================================================================
# Endpoints de API
# ============================================================================

# MESSAGES — ver services/panel/messages_service.py


# =============================================================================
# Edición y eliminación de mensajes propios del asesor (Conversations API)
# =============================================================================
# MESSAGES — ver services/panel/messages_service.py


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
        logger.warning(f"[Panel][Backfill] resolve conv_sid fallo: {safe_error(e)}")
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
        logger.error(f"[Panel][Backfill] Excepción: {safe_error(e)}")


# MESSAGES — ver services/panel/messages_service.py


# MESSAGES — ver services/panel/messages_service.py


# MESSAGES — ver services/panel/messages_service.py


# MESSAGES — ver services/panel/messages_service.py


# TEMPLATES — ver services/panel/templates_service.py


# ============================================================================
# Endpoints CRUD de Templates
# ============================================================================

# TEMPLATES — ver services/panel/templates_service.py


# ============================================================================
# Endpoint para CREAR contacto manualmente
# ============================================================================

# CONTACTS — ver services/panel/contacts_service.py

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


# ============================================================================
# Endpoints de Solicitud de Transferencia
# ============================================================================

# TRANSFER — ver services/panel/transfer_service.py


# ============================================================================
# Endpoint para editar nombre de contacto
# ============================================================================

# CONTACTS — ver services/panel/contacts_service.py

# ============================================================================
# Endpoint para editar el teléfono de un contacto
# ============================================================================

@router.patch("/contacts/{contact_id}/phone")
async def update_contact_phone(
    contact_id: str,
    phone: str = Form(..., description="Teléfono del contacto"),
    canal: str = Form("whatsapp", description="Canal de la conversación"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Fija el teléfono de un contacto, migrando la clave si venía de un BSUID.

    Gemelo de PATCH /contacts/{contact_id}/name. Existe porque las
    conversaciones que llegan por username de WhatsApp no traen número: la
    asesora lo consigue hablando y aquí lo deja escrito.

    Cuando la conversación estaba indexada bajo una clave BSUID, esto NO es un
    simple update de HubSpot: `migrate_identity_to_phone` mueve la clave en
    Redis y Mongo. El BSUID sobrevive como dirección de entrega — el routing de
    Twilio no cambia.
    """
    logger.info(f"[Panel] PATCH teléfono - contact_id={safe_id(contact_id, 'contact')}")

    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    if not contact_id or contact_id in ("null", "undefined"):
        raise HTTPException(status_code=400, detail="ID de contacto inválido")
    try:
        int(contact_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="ID de contacto debe ser numérico")

    validation = PhoneNormalizer().normalize(phone or "")
    if not validation.is_valid:
        logger.warning(f"[Panel] Teléfono rechazado: {safe_error(validation.error_message)}")
        raise HTTPException(
            status_code=400,
            detail=f"Número inválido: {validation.error_message}",
        )
    phone_norm = validation.normalized

    redis_client = await _get_redis_client()
    clave_actual = await redis_client.get(f"phone_cache:{contact_id}")
    clave_actual = clave_actual if isinstance(clave_actual, str) else (
        clave_actual.decode() if clave_actual else None
    )

    # ── Caso 1: la conversación venía de un BSUID → migrar la clave ──
    if clave_actual and is_bsuid_identity_key(clave_actual):
        resultado = await migrate_identity_to_phone(
            clave_actual, phone_norm, canal or "whatsapp", source="panel",
        )
        if not resultado.ok:
            _detalles = {
                "invalid_phone": (400, "El número no es válido"),
                "locked": (409, "Hay otra actualización en curso, intenta de nuevo"),
            }
            code, msg = _detalles.get(resultado.outcome, (500, "No se pudo actualizar el teléfono"))
            raise HTTPException(status_code=code, detail=msg)

        return {
            "status": "success",
            "message": "Teléfono actualizado correctamente",
            "contact_id": resultado.contact_id or contact_id,
            "phone": resultado.phone,
            "old_phone": clave_actual,
            "migrated": True,
            "outcome": resultado.outcome,
        }

    # ── Caso 2: contacto telefónico → corregir el número en HubSpot ──
    # No se migran claves aquí: cambiar el teléfono de un contacto que YA tenía
    # uno es una operación distinta (y hoy no soportada desde el panel), no una
    # migración de identidad.
    hubspot_api_key = os.getenv("HUBSPOT_API_KEY")
    if not hubspot_api_key:
        raise HTTPException(status_code=500, detail="HUBSPOT_API_KEY no configurada")

    url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
    try:
        response = await _hubspot_patch(url, {"properties": {"phone": phone_norm}}, hubspot_api_key)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Timeout conectando con HubSpot")
    except Exception as e:
        logger.error(f"[Panel] Error actualizando teléfono: {safe_error(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno actualizando el teléfono")

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Contacto no encontrado en HubSpot")
    if response.status_code != 200:
        logger.error(
            f"[Panel] HubSpot rechazó el teléfono: {response.status_code} - "
            f"{safe_error(response.text, 200)}"
        )
        raise HTTPException(status_code=response.status_code, detail="HubSpot rechazó el cambio")

    return {
        "status": "success",
        "message": "Teléfono actualizado correctamente",
        "contact_id": contact_id,
        "phone": phone_norm,
        "migrated": False,
        "outcome": "updated",
    }


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
    canal_safe = (canal or "whatsapp").lower()
    close_owner_id = None
    try:
        _pre_close_meta = await state_manager.get_meta(phone_normalized, canal_safe)
        close_owner_id = _pre_close_meta.assigned_owner_id if _pre_close_meta else None
    except Exception:
        close_owner_id = None

    await state_manager.activate_bot(phone_normalized, canal=canal)
    if phone != phone_normalized:
        await state_manager.activate_bot(phone, canal=canal)

    try:
        r = await _get_redis_client()
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
                await r.set(meta_key_close, json.dumps(meta_close))
            except Exception as e_meta:
                logger.warning(f"[Panel] No se pudo setear in_panel=False al cerrar {safe_phone(phone_normalized)}: {safe_error(e_meta)}")
    except Exception as e:
        logger.warning(f"[Panel] No se pudo remover del ZSET al cerrar {safe_phone(phone_normalized)}: {safe_error(e)}")

    # Espejo en MongoDB: sin esto el contacto reaparece en el panel, porque el
    # fallback GET /contacts (find_conversations_by_owner) y el rebuild nocturno
    # (find_recent_conversations) releen `conversations` y ambos filtran por `archived`.
    try:
        from database.mongodb_client import get_mongo_manager
        await get_mongo_manager().update_conversation_meta(
            phone=phone_normalized,
            canal=(canal or "whatsapp").lower(),
            archived=True,
        )
    except Exception as e_arch:
        logger.warning(f"[Panel] No se pudo archivar en MongoDB al cerrar {safe_phone(phone_normalized)}: {safe_error(e_arch)}")

    await _invalidate_contacts_response_cache("close_conversation")

    try:
        _close_meta = await state_manager.get_meta(phone_normalized, canal or "whatsapp")
        if _close_meta and _close_meta.assigned_owner_id:
            close_owner_id = close_owner_id or _close_meta.assigned_owner_id
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
        "advisor_id": close_owner_id,
    }


# CONTACTS — ver services/panel/contacts_service.py

# CONTACTS — ver services/panel/contacts_service.py

async def _transfer_to_luisa(
    phone: str,
    canal: Optional[str],
    contact_id: Optional[str],
    stage_id: str,
) -> dict:
    """
    Cadena atómica cuando Jubeny mueve un contacto a un embudo de Luisa:
    1. Cerrar conversación en panel de Jubeny
    2. transfer_ownership() — Redis + MongoDB + HubSpot en una sola llamada
    3. Activar en panel de Luisa con HUMAN_ACTIVE
    4. Guardar transfer_origin en meta
    5. Agregar al inbox de Luisa
    6. Notificar vía WebSocket a ambas asesoras
    """
    stage_name = STAGES_TRANSFER_TO_LUISA.get(stage_id, stage_id)
    tag = f"[Transfer:{stage_name}]"
    result = {"closed": False, "transferred": False, "transfer_error": None}
    state_manager = _get_state_manager()
    canal_safe = (canal or "whatsapp").lower()
    phone_normalized = phone

    # Quien transfiere es el dueno ACTUAL del contacto, no un ID fijo. Antes era
    # "89096378" a fuego, asi que si transferia cualquier otra asesora la
    # notificacion decia que venia de Jubeny.
    try:
        _meta_previa = await state_manager.get_meta(phone_normalized, canal_safe)
        from_advisor_id = _meta_previa.assigned_owner_id if _meta_previa else None
    except Exception:
        from_advisor_id = None

    try:
        normalizer = PhoneNormalizer()
        validation = normalizer.normalize(phone)
        if validation.is_valid:
            phone_normalized = validation.normalized
    except Exception:
        pass

    # ① Cerrar conversación de Jubeny
    try:
        await _close_conversation_internal(phone_normalized, canal_safe)
        result["closed"] = True
    except Exception as e_close:
        logger.error(f"{tag} Close falló para {safe_phone(phone_normalized)}: {safe_error(e_close)}")
        result["transfer_error"] = f"Close falló: {e_close}"

    # ② Transferir owner en Redis + MongoDB + HubSpot (centralizado)
    meta_key = f"conv_meta:{phone_normalized}:{canal_safe}"
    try:
        r = await _get_redis_client()
        raw_meta = await r.get(meta_key)
        if not raw_meta:
            # transfer_contact() aborta si no hay conv_meta. Sin este bootstrap la
            # transferencia se saltaba en silencio y HubSpot conservaba al owner
            # anterior, aunque el contacto sí llegara al panel destino vía ③.
            # add_to_zset=False: el paso ① acaba de cerrarlo y ③ lo re-activa.
            logger.info(f"{tag} Sin meta previa para {safe_phone(phone_normalized)} — creando antes de transferir")
            await state_manager.ensure_meta_with_channel(
                phone=phone_normalized,
                canal=canal_safe,
                canal_origen=canal_safe,
                contact_id=contact_id,
                add_to_zset=False,
            )

        transfer_result = await state_manager.transfer_ownership(
            phone=phone_normalized,
            canal=canal_safe,
            to_owner_id=LUISA_TRANSFER_TARGET,
            contact_id=contact_id,
            reason=f"Embudo {stage_name} — transferencia automática",
            mode="exclusive",
        )
        if transfer_result.get("status") != "success":
            logger.warning(f"{tag} Transfer falló: {transfer_result}")
            result["transfer_error"] = transfer_result.get("message")
    except Exception as e_transfer:
        logger.error(f"{tag} Transfer error: {safe_error(e_transfer)}")
        result["transfer_error"] = str(e_transfer)

    # ③ Activar en panel de Luisa con HUMAN_ACTIVE
    try:
        existing_meta = {}
        try:
            r = await _get_redis_client()
            raw = await r.get(meta_key)
            if raw:
                existing_meta = json.loads(raw)
        except Exception:
            pass

        await state_manager.activate_human(
            phone_normalized=phone_normalized,
            canal_origen=canal_safe,
            owner_id=LUISA_TRANSFER_TARGET,
            reason=f"{stage_name} — transferido desde Jubeny",
            display_name=existing_meta.get("display_name"),
            contact_id=contact_id or existing_meta.get("contact_id"),
        )
        result["transferred"] = True
    except Exception as e_activate:
        logger.error(f"{tag} Activate human falló: {safe_error(e_activate)}")
        if not result["transfer_error"]:
            result["transfer_error"] = str(e_activate)

    # ④ Guardar transfer_origin en meta
    try:
        r = await _get_redis_client()
        raw = await r.get(meta_key)
        if raw:
            meta_dict = json.loads(raw)
            from middleware.conversation_state import get_bogota_now_iso
            meta_dict["seguimiento_origin"] = get_bogota_now_iso()
            meta_dict["assigned_owner_id"] = LUISA_TRANSFER_TARGET
            await r.set(meta_key, json.dumps(meta_dict))
    except Exception as e_meta:
        logger.warning(f"{tag} Meta update falló (non-fatal): {safe_error(e_meta)}")

    # ⑤ Agregar al inbox de Luisa
    try:
        await state_manager.add_to_advisor_inbox(
            LUISA_TRANSFER_TARGET, phone_normalized, canal_safe
        )
    except Exception as e_inbox:
        logger.warning(f"{tag} Inbox add falló (non-fatal): {safe_error(e_inbox)}")

    # ⑥ WebSocket notify
    try:
        meta = await state_manager.get_meta(phone_normalized, canal_safe)
        contact_name = meta.display_name if meta else phone_normalized
        await ws_manager.notify_contact_transferred(
            phone=phone_normalized,
            from_advisor=from_advisor_id,
            to_advisor=LUISA_TRANSFER_TARGET,
            contact_name=contact_name,
            mode="exclusive",
            redis_client=state_manager.redis
        )
    except Exception as e_ws:
        logger.warning(f"{tag} WS notify falló (non-fatal): {safe_error(e_ws)}")

    result["to_owner"] = LUISA_TRANSFER_TARGET
    logger.info(
        f"{tag} Transferencia completada — phone={phone_normalized} "
        f"contact={contact_id} closed={result['closed']} transferred={result['transferred']}"
    )
    return result


# ============================================================================
# Helper: Cierre automático por embudo (sin transferencia de owner)
# ============================================================================

async def _auto_close_by_stage(
    phone: str,
    canal: Optional[str],
    stage_id: str,
) -> dict:
    """
    Cierra la conversación cuando la asesora mueve el contacto a un embudo
    terminal (STAGES_AUTO_CLOSE, hoy "Cerrado perdido").

    A diferencia de _transfer_to_luisa, aquí NO hay cambio de dueño: el contacto
    sale del panel y Sofía retoma. Notifica por WebSocket para que la lista se
    refresque sin que la asesora tenga que recargar.
    """
    stage_name = STAGES_AUTO_CLOSE.get(stage_id, stage_id)
    tag = f"[AutoClose:{stage_name}]"
    canal_safe = (canal or "whatsapp").lower()
    result = {"closed": False, "close_error": None}

    state_manager = _get_state_manager()

    # Capturar el estado real ANTES de cerrar — _close_conversation_internal
    # lo deja en BOT_ACTIVE y se perdería el valor de origen.
    try:
        _prev = await state_manager.get_status(phone, canal_safe)
        old_status = _prev.value if _prev else ConversationStatus.BOT_ACTIVE.value
    except Exception:
        old_status = ConversationStatus.BOT_ACTIVE.value

    try:
        close_result = await _close_conversation_internal(phone, canal_safe)
        phone_normalized = close_result["phone"]
        result["closed"] = True
    except Exception as e_close:
        logger.error(f"{tag} Close falló para {safe_phone(phone)}: {safe_error(e_close)}")
        result["close_error"] = str(e_close)
        return result

    try:
        meta = await state_manager.get_meta(phone_normalized, canal_safe)
        await ws_manager.notify_status_change(
            phone=phone_normalized,
            canal=canal_safe,
            old_status=old_status,
            new_status=stage_name,
            contact_name=(meta.display_name if meta else "") or "",
        )
    except Exception as e_ws:
        logger.warning(f"{tag} WS notify falló (non-fatal): {safe_error(e_ws)}")

    logger.info(f"{tag} Conversación cerrada por embudo — phone={safe_phone(phone_normalized)}")
    return result


# ============================================================================
# Endpoint para actualizar lifecyclestage del Contact en HubSpot
# ============================================================================

# CONTACTS — ver services/panel/contacts_service.py


# CONTACTS — ver services/panel/contacts_service.py


# CONTACTS — ver services/panel/contacts_service.py


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
        "1326623075",              # En Conversación
        # Sofía puede agendar una cita antes de que ninguna asesora escriba: ese
        # contacto sigue en "Nuevo Lead" y sin esta entrada quedaría descartado
        # del filtro por encargado pese a tener visita.
        "1417459250",              # Nuevo Lead
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
                    time_ago_str = f"{DIAS_SEMANA_ABREV[local.weekday()]} {t}"
                else:
                    time_ago_str = f"{local.day} {MESES_ABREV[local.month - 1]}"
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


async def _resolve_pending_reply_phones(contacts: list) -> set:
    """
    Telefonos cuyo ultimo mensaje es del CLIENTE, o sea que esperan respuesta.

    Fuente: MongoDB `conversations`, que ya guarda `last_message_sender` en cada
    upsert (mongodb_client.upsert_conversation_on_message). Una sola query con
    $in sobre una coleccion indexada.

    Basta con que UNO de los canales del telefono tenga al cliente esperando —
    ver find_phones_awaiting_reply(), que explica por que no se puede resolver
    esto colapsando los documentos por telefono.

    Ante cualquier fallo devuelve un set vacio: el panel se comporta exactamente
    como antes de esta regla. Es preferible perder el rescate que romper la lista.
    """
    phones = sorted({c.get("phone") for c in contacts if c.get("phone")})
    if not phones:
        return set()
    try:
        return await get_mongo_manager().find_phones_awaiting_reply(phones)
    except Exception as e:
        logger.warning(f"[Panel] No se pudo resolver pendientes (non-fatal): {safe_error(e)}")
        return set()


def _nunca_se_corta(contact: dict) -> bool:
    """
    Si un contacto entra en la respuesta pase lo que pase con el limite.

    GET /contacts recorta la lista en TRES sitios encadenados: la seleccion
    (_split_always_visible), el pre-limite previo al enriquecimiento con HubSpot
    (_cuenta_como_prioridad) y el corte final que arma la respuesta. Arreglar uno
    solo no cambia nada en pantalla — se comprobo dos veces en produccion el
    11-ago-2026 —, asi que los tres comparten criterio: no leido, o rescatado por
    esperar respuesta.

    Mira `pending_rescued` (el pase, acotado por el tope) y NO `pending_reply`
    (la verdad, sin acotar): si mirara la verdad, el tope no serviria de nada y
    entrarian todos los pendientes historicos.
    """
    return bool(contact.get("has_unread", False) or contact.get("pending_rescued"))


def _cuenta_como_prioridad(contact: dict) -> bool:
    """
    Si un contacto sobrevive al pre-limite previo al enriquecimiento con HubSpot.

    Ademas de los del ZSET de prioridad, los rescatados por esperar respuesta.
    Sin ellos aqui, ese corte deshacia el rescate de _split_always_visible: el
    11-ago-2026 se seleccionaban 110 contactos y quedaban 62, con lo que el
    arreglo no cambiaba nada en pantalla.

    Mira `pending_rescued`, no `pending_reply` — ver _nunca_se_corta().
    """
    return bool(contact.get("in_priority_zset", True) or contact.get("pending_rescued"))


def _split_always_visible(
    advisor_contacts: list,
    unread_phones: Optional[set],
    pending_phones: set,
    page: int,
    limit: int,
    pending_max: int = CONTACTS_PENDING_MAX,
) -> tuple[list, dict]:
    """
    Decide que contactos entran en la respuesta.

    Dos grupos: los que van SIEMPRE (sin leer, o con el cliente esperando) y el
    resto, que se corta por `limit`. Sin IO, para poder probar el corte sin
    levantar el endpoint.

    Marca `pending_reply=True` en los contactos rescatados. No es cosmetico: aguas
    abajo hay un SEGUNDO corte antes del enriquecimiento con HubSpot que solo
    respetaba `in_priority_zset`, y sin esta marca deshacia el trabajo de aqui
    —medido en produccion: 110 contactos seleccionados, 62 tras el pre-limite—.
    El campo viaja tambien en la respuesta, donde el panel puede usarlo.

    `unread_phones=None` significa que el inbox no respondio; en ese caso el corte
    del resto se amplia por CONTACTS_INBOX_FALLBACK_MULTIPLIER, como ya hacia
    antes, pero los pendientes siguen entrando sin cortar.

    Devuelve (contactos, stats) — stats alimenta el log.
    """
    inbox_down = unread_phones is None
    unread = unread_phones or set()

    # Que 100 pendientes se rescatan: los MAS RECIENTES, no los primeros que
    # aparezcan recorriendo la lista. `advisor_contacts` llega en dos tramos —
    # primero el ZSET, despues el respaldo de MongoDB— y dentro de cada uno hay
    # orden por actividad, pero entre ellos no. Aplicando el tope por posicion,
    # un cliente que escribio hace 11 horas y viene del respaldo perdia su plaza
    # frente a otro de hace 40 dias que estaba en el ZSET. Medido en produccion
    # el 12-ago-2026: 3 de los pendientes mas recientes quedaban fuera.
    _candidatos = [
        c for c in advisor_contacts
        if (c.get("phone") or "") in pending_phones
        and (c.get("phone") or "") not in unread
    ]
    _candidatos.sort(key=lambda c: c.get("last_activity") or "", reverse=True)
    _con_pase = {c.get("phone") for c in _candidatos[:pending_max]}

    always: list = []
    rest: list = []
    unread_count = 0
    pending_count = 0
    pending_truncated = 0

    for contact in advisor_contacts:
        phone = contact.get("phone") or ""
        espera_respuesta = bool(phone and phone in pending_phones)

        # `pending_reply` dice la verdad SIEMPRE — tambien si el contacto ademas
        # tiene mensajes sin leer, y tambien si queda por encima del tope. Es la
        # senal que el panel pinta: el cliente espera y nadie ha contestado.
        if espera_respuesta:
            contact["pending_reply"] = True

        if phone and phone in unread:
            always.append(contact)
            unread_count += 1
            continue

        if espera_respuesta:
            # El tope protege el coste del enriquecimiento aguas abajo. Los que
            # sobran no se pierden: caen al resto y compiten por el corte normal.
            # `pending_rescued` es la marca INTERNA del pase — la que miran los
            # otros dos cortes. Separada de `pending_reply` a proposito: si los
            # cortes leyeran la verdad en vez del pase, el tope no existiria.
            if phone in _con_pase:
                contact["pending_rescued"] = True
                always.append(contact)
                pending_count += 1
            else:
                pending_truncated += 1
                rest.append(contact)
            continue

        rest.append(contact)

    cut = limit * CONTACTS_INBOX_FALLBACK_MULTIPLIER if inbox_down else limit
    offset = (page - 1) * cut
    active = always + rest[offset:offset + cut]

    return active, {
        "unread": unread_count,
        "pending": pending_count,
        "pending_truncated": pending_truncated,
        "otros": len(active) - len(always),
        "total": len(active),
        "inbox_down": inbox_down,
        "cut": cut,
    }


# CONTACTS — ver services/panel/contacts_service.py


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
            logger.warning(f"[Panel] HubSpot 429 persistente al obtener contacto {safe_id(contact_id, 'contact')}")

    except Exception as e:
        logger.debug(f"[Panel] Error obteniendo info de HubSpot: {safe_error(e)}")

    return None


# ============================================================================
# ADVISORS (Asesores del panel - nombres editables)
# ============================================================================

# ============================================================================
# WORKERS (Equipo de campo para citas)
# ============================================================================

# ============================================================================
# APPOINTMENTS (Citas agendadas)
# ============================================================================

# APPOINTMENTS — ver services/panel/appointments_service.py


# APPOINTMENTS — ver services/panel/appointments_service.py


# APPOINTMENTS — ver services/panel/appointments_service.py


# APPOINTMENTS — ver services/panel/appointments_service.py


# APPOINTMENTS — ver services/panel/appointments_service.py


# APPOINTMENTS — ver services/panel/appointments_service.py


# APPOINTMENTS — ver services/panel/appointments_service.py


# APPOINTMENTS — ver services/panel/appointments_service.py


# APPOINTMENTS — ver services/panel/appointments_service.py


# APPOINTMENTS — ver services/panel/appointments_service.py


# APPOINTMENTS — ver services/panel/appointments_service.py


# ============================================================================
# Notas Internas de Contacto — ver services/panel/notes_service.py
# ============================================================================


# ============================================================================
# UI del Panel
# ============================================================================

# ver services/panel/ (extraido sin cambios)

# Funciones de background

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

        logger.info(f"[Panel] Mensaje del asesor registrado en Timeline: {safe_id(contact_id, 'contact')}")

        # Marcar mensaje como sincronizado en MongoDB
        if mongo_message_id:
            try:
                mongo_manager = get_mongo_manager()
                await mongo_manager.mark_as_synced_to_hubspot(mongo_message_id)
                logger.debug(f"[Panel] MongoDB mensaje {mongo_message_id} marcado como sincronizado")
            except Exception as sync_error:
                logger.warning(f"[Panel] Error marcando sincronización: {sync_error}")

    except Exception as e:
        logger.error(f"[Panel] Error registrando en HubSpot: {safe_error(e)}")


async def _update_advisor_timestamp(phone_normalized: str, canal: Optional[str] = None) -> None:
    """
    Actualiza timestamp del mensaje del asesor en ConversationMeta.
    Usado para calcular TTL de 72h si asesor deja de responder.
    También remueve del inbox, limpia notificaciones de inactividad y promueve
    el lead fuera de "Nuevo Lead".
    """
    try:
        state_manager = _get_state_manager()
        await state_manager.update_advisor_message_timestamp(phone_normalized, canal)
        logger.info(f"[Panel] ✓ Timestamp asesor actualizado: {safe_phone(phone_normalized)}:{canal or 'default'}")
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
        # Único punto donde un lead sale de "Nuevo Lead": lo dispara una respuesta
        # manual de la asesora, nunca Sofía ni un envío masivo. No-fatal: si falla
        # la promoción, el mensaje ya salió y el lead se promueve en el siguiente.
        if meta and meta.contact_id:
            try:
                await _promote_nuevo_lead_to_en_conversacion(
                    meta.contact_id, phone_normalized
                )
            except Exception as promo_err:
                logger.warning(f"[NuevoLead] Promoción falló (non-fatal): {promo_err}")
        elif meta:
            # GET /contacts sí resuelve el contact_id por teléfono cuando falta en
            # el meta, pero solo en memoria: nunca lo devuelve a Redis. Un contacto
            # así se ve perfecto en el panel y jamás se promueve, sin dejar rastro.
            # Teléfono enmascarado: CLAUDE.md prohíbe loggearlo entero, y los 4
            # últimos dígitos bastan para localizarlo en el panel.
            logger.warning(
                f"[NuevoLead] conv_meta sin contact_id para "
                f"···{phone_normalized[-4:]}:{canal or 'default'} — no se puede promover"
            )
    except Exception as e:
        logger.error(f"[Panel] Error actualizando timestamp asesor: {safe_error(e)}")


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


# ============================================================================
# MÉTRICAS — ver services/panel/metrics_service.py
# ============================================================================


# ============================================================================
# WebSocket para notificaciones en tiempo real
# ============================================================================

# WebSocket realtime — ver services/panel/realtime_service.py
# ============================================================================
# ENDPOINT RECOVERY: Restaurar contactos desaparecidos desde HubSpot
# ============================================================================

# ADMIN — ver services/panel/admin_service.py



# SCHEDULED MESSAGES — Mensajes WhatsApp plantilla programados por asesoras


# SCHEDULED MESSAGES — ver services/panel/scheduled_messages_service.py


# BULK CAMPAIGNS — Mensajes Masivos por Embudo (per-asesora)

# BULK HTTP — ver services/panel/bulk_campaigns_service.py


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


# BULK HTTP — ver services/panel/bulk_campaigns_service.py


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
        logger.warning(f"[BulkCampaign] No se pudo guardar mensaje en Mongo: {safe_error(e)}")

    # Caso especial No Responde: setear flag para auto-promoción
    if campaign_stage_id == "other":
        try:
            r = await _get_redis_client()
            flag_key = f"{BULK_NO_RESPONDE_FLAG_PREFIX}{phone}"
            await r.set(flag_key, campaign_id, ex=BULK_NO_RESPONDE_FLAG_TTL)
        except Exception as e:
            logger.warning(f"[BulkCampaign] No se pudo setear flag No Responde: {safe_error(e)}")

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
        logger.warning(f"[BulkCampaign] No se pudo adquirir lock: {safe_error(e)}")
        return

    try:
        mongo_mgr = get_mongo_manager()
        activas = await mongo_mgr.get_active_bulk_campaigns(limit=BULK_CAMPAIGNS_PER_TICK)
        if not activas:
            return

        # Se recorre hasta encontrar una campana con trabajo real. Antes se
        # tomaba siempre la primera: si esa no tenia nada reclamable pero
        # tampoco podia cerrarse, el tick se iba de vacio y las siguientes no
        # se procesaban nunca. Asi es como 217 envios quedaron congelados 33
        # dias sin un solo error en los logs.
        campaign = None
        contacts = []
        for candidata in activas:
            cid_campana = candidata["_id"]
            # Rescatar lo reclamado por un proceso muerto ANTES de reclamar mas:
            # es lo que devuelve trabajo a la cola y permite cerrar la campana.
            await mongo_mgr.recuperar_contactos_caducados(
                cid_campana, BULK_CLAIM_LEASE_SECONDS, BULK_CAMPAIGN_MAX_AGE_HOURS
            )
            contacts = await mongo_mgr.claim_next_pending_contacts(
                cid_campana, BULK_BATCH_SIZE
            )
            if contacts:
                campaign = candidata
                break
            await mongo_mgr.finalize_bulk_campaign_if_done(cid_campana)

        if not campaign:
            return
        campaign_id = campaign["_id"]

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
                    logger.info(f"[BulkCampaign] {safe_id(campaign_id, 'campaign')}: skip {safe_id(contact_id, 'contact')} (dedup)")
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
        logger.error(f"[BulkCampaign] Error en tick: {safe_error(e)}", exc_info=True)
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
        logger.error(f"[BulkCampaign] Error fatal en tick (capturado): {safe_error(e)}", exc_info=True)
