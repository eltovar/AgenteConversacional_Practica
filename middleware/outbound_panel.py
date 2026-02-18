# middleware/outbound_panel.py
"""
Este módulo proporciona endpoints API y UI para que los asesores envíen
mensajes de WhatsApp directamente, sustituyendo el Inbox bloqueado de HubSpot.

Características:
- UI mínima con caja de texto y botón de envío
- Validación de ventana de 24 horas de WhatsApp
- Marcado de mensaje con message_source="Manual via Panel"
- Pausa automática de Sofía al enviar mensaje manual
- Registro en Timeline de HubSpot
"""

import os
from typing import Optional
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass

from fastapi import APIRouter, Form, Header, HTTPException, BackgroundTasks, Query
from fastapi.responses import HTMLResponse, JSONResponse
import redis.asyncio as redis

from logging_config import logger
from .phone_normalizer import PhoneNormalizer
from .conversation_state import ConversationStateManager, ConversationStatus
from .contact_manager import ContactManager
from utils.twilio_client import twilio_client
from integrations.hubspot import get_timeline_logger


# Router de FastAPI para el panel de envío
router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])


# ============================================================================
# Configuración y constantes
# ============================================================================

# API Key para autenticación del panel
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

# Ventana de 24 horas de WhatsApp (en segundos)
WHATSAPP_WINDOW_SECONDS = 24 * 60 * 60

# Prefijo en Redis para almacenar último mensaje del cliente
LAST_CLIENT_MESSAGE_PREFIX = "last_client_msg:"


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
    if not ADMIN_API_KEY:
        logger.warning("[Panel] ADMIN_API_KEY no configurada - Panel deshabilitado")
        return False
    return api_key == ADMIN_API_KEY


async def _get_redis_client():
    """Obtiene cliente Redis."""
    redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
    return redis.from_url(redis_url, encoding="utf-8", decode_responses=True)


async def check_24h_window(phone_normalized: str) -> WindowStatus:
    """
    Verifica el estado de la ventana de 24 horas de WhatsApp.

    WhatsApp solo permite enviar mensajes de texto libre durante 24 horas
    después del último mensaje del cliente. Fuera de esa ventana,
    solo se pueden enviar Templates pre-aprobados.

    Args:
        phone_normalized: Número en formato E.164

    Returns:
        WindowStatus con el estado de la ventana
    """
    try:
        r = await _get_redis_client()
        key = f"{LAST_CLIENT_MESSAGE_PREFIX}{phone_normalized}"

        last_msg_str = await r.get(key)
        await r.close()

        if not last_msg_str:
            # No hay registro - asumir ventana cerrada por seguridad
            return WindowStatus(
                is_open=False,
                last_message_time=None,
                time_remaining_seconds=None,
                requires_template=True,
                message="No hay registro de mensaje reciente del cliente. Se requiere Template de WhatsApp."
            )

        last_msg_time = datetime.fromisoformat(last_msg_str)
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

    Args:
        phone_normalized: Número en formato E.164
    """
    try:
        r = await _get_redis_client()
        key = f"{LAST_CLIENT_MESSAGE_PREFIX}{phone_normalized}"

        # Guardar con TTL de 25 horas (un poco más que la ventana)
        await r.set(
            key,
            datetime.now(timezone.utc).isoformat(),
            ex=25 * 60 * 60
        )
        await r.close()

        logger.debug(f"[Panel] Actualizado último mensaje del cliente: {phone_normalized}")

    except Exception as e:
        logger.error(f"[Panel] Error actualizando último mensaje: {e}")


# ============================================================================
# Endpoints de API
# ============================================================================

@router.post("/send-message")
async def send_message(
    background_tasks: BackgroundTasks,
    to: str = Form(..., description="Número de destino (+573001234567)"),
    body: str = Form(..., description="Contenido del mensaje"),
    contact_id: Optional[str] = Form(None, description="ID del contacto en HubSpot"),
    force_send: bool = Form(False, description="Forzar envío aunque ventana esté cerrada"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Envía un mensaje de WhatsApp desde el panel de asesores.

    Este endpoint:
    1. Valida la API Key
    2. Normaliza el número telefónico
    3. Verifica la ventana de 24 horas de WhatsApp
    4. Pausa automáticamente a Sofía (HUMAN_ACTIVE)
    5. Envía el mensaje por Twilio
    6. Registra en Timeline de HubSpot (background)

    Headers requeridos:
        X-API-Key: Token de autenticación admin

    Form data:
        to: Número de destino
        body: Contenido del mensaje
        contact_id: ID del contacto en HubSpot (opcional)
        force_send: Enviar aunque ventana esté cerrada (requiere Template)
    """
    # Validar API Key
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida o no configurada")

    # Validar campos requeridos
    if not body.strip():
        raise HTTPException(status_code=400, detail="El mensaje no puede estar vacío")

    # Normalizar número
    normalizer = PhoneNormalizer()
    validation = normalizer.normalize(to)

    if not validation.is_valid:
        raise HTTPException(
            status_code=400,
            detail=f"Número inválido: {validation.error_message}"
        )

    phone_normalized = validation.normalized

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
                phone_raw=to,
                source_channel="panel_asesor"
            )
            contact_id = contact_info.contact_id
        except Exception as e:
            logger.warning(f"[Panel] No se pudo obtener contacto: {e}")
            # Continuar sin contact_id

    # Pausar Sofía automáticamente (activar modo humano)
    try:
        redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
        state_manager = ConversationStateManager(redis_url)
        await state_manager.activate_human(phone_normalized)
        logger.info(f"[Panel] Sofía pausada automáticamente para {phone_normalized}")
    except Exception as e:
        logger.warning(f"[Panel] No se pudo pausar Sofía: {e}")

    # Enviar mensaje
    result = await twilio_client.send_whatsapp_message(
        to=phone_normalized,
        body=body
    )

    if result["status"] == "success":
        # Registrar en HubSpot Timeline (background)
        if contact_id:
            background_tasks.add_task(
                _log_advisor_message_to_hubspot,
                contact_id,
                body,
                phone_normalized,
                "Manual via Panel"  # message_source
            )

        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message_sid": result.get("message_sid"),
                "to": phone_normalized,
                "contact_id": contact_id,
                "window_status": {
                    "is_open": window_status.is_open,
                    "time_remaining": window_status.time_remaining_seconds
                },
                "sofia_paused": True,
                "message_source": "Manual via Panel"
            }
        )
    else:
        raise HTTPException(
            status_code=500,
            detail=f"Error enviando mensaje: {result.get('message')}"
        )


@router.get("/window-status/{phone}")
async def get_window_status(
    phone: str,
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Consulta el estado de la ventana de 24 horas para un número.

    Args:
        phone: Número telefónico

    Returns:
        Estado de la ventana de 24 horas
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


@router.get("/conversations/{phone}")
async def get_conversation_history(
    phone: str,
    limit: int = Query(50, ge=1, le=100),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Obtiene el historial de conversación de un contacto por teléfono.

    Este endpoint consulta las notas en HubSpot asociadas al contacto
    para mostrar el historial de mensajes.

    Args:
        phone: Número telefónico
        limit: Máximo de mensajes a retornar

    Returns:
        Historial de conversación como burbujas de chat
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    normalizer = PhoneNormalizer()
    validation = normalizer.normalize(phone)

    if not validation.is_valid:
        raise HTTPException(status_code=400, detail=f"Número inválido: {validation.error_message}")

    # Obtener contacto
    try:
        contact_manager = ContactManager()
        contact_id = await contact_manager._search_contact(validation.normalized)

        if not contact_id:
            return {
                "phone": validation.normalized,
                "contact_id": None,
                "messages": [],
                "message": "Contacto no encontrado en HubSpot"
            }

        # Obtener historial de notas desde HubSpot
        timeline_logger = get_timeline_logger()
        messages = await timeline_logger.get_notes_for_contact(
            contact_id=contact_id,
            limit=limit
        )

        return {
            "phone": validation.normalized,
            "contact_id": contact_id,
            "messages": messages,
            "count": len(messages)
        }

    except Exception as e:
        logger.error(f"[Panel] Error obteniendo historial: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history/{contact_id}")
async def get_history_by_contact_id(
    contact_id: str,
    limit: int = Query(50, ge=1, le=100),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Obtiene el historial de conversación por contact_id.

    Args:
        contact_id: ID del contacto en HubSpot
        limit: Máximo de mensajes a retornar

    Returns:
        Historial de conversación como burbujas de chat
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        timeline_logger = get_timeline_logger()
        messages = await timeline_logger.get_notes_for_contact(
            contact_id=contact_id,
            limit=limit
        )

        return {
            "contact_id": contact_id,
            "messages": messages,
            "count": len(messages)
        }

    except Exception as e:
        logger.error(f"[Panel] Error obteniendo historial: {e}")
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
        redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))

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

        await r.close()

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


@router.get("/contacts")
async def get_active_contacts(
    filter_time: str = Query("24h", description="Filtro de tiempo: 24h, 48h, 1week, custom"),
    date_from: Optional[str] = Query(None, description="Fecha desde (ISO) para filtro custom"),
    date_to: Optional[str] = Query(None, description="Fecha hasta (ISO) para filtro custom"),
    limit: int = Query(30, ge=1, le=100),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Retorna lista de contactos combinando dos fuentes:

    1. **Redis (Tiempo Real)** - Contactos en estado HUMAN_ACTIVE
       - Aparecen primero con badge "En espera"
       - Se detectan automáticamente cuando Sofía hace handoff

    2. **HubSpot Notes (Histórico)** - Contactos con interacción previa de asesor
       - Filtrados por rango de tiempo
       - Aparecen después de los activos

    Esto permite que los contactos aparezcan automáticamente en el panel
    cuando se activa HUMAN_ACTIVE, como en WhatsApp Web.

    Filtros disponibles:
    - 24h: Últimas 24 horas
    - 48h: Últimas 48 horas
    - 1week: Última semana
    - custom: Usar date_from y date_to
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        from zoneinfo import ZoneInfo
        TIMEZONE = ZoneInfo("America/Bogota")

        now = datetime.now(TIMEZONE)

        # === PASO 1: Obtener contactos ACTIVOS de Redis ===
        redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
        logger.info(f"[Panel] Usando Redis URL: {redis_url}")
        state_manager = ConversationStateManager(redis_url)

        active_contacts = await state_manager.get_all_human_active_contacts()

        logger.info(f"[Panel] Encontrados {len(active_contacts)} contactos activos en HUMAN_ACTIVE")
        if active_contacts:
            for contact in active_contacts:
                logger.debug(f"[Panel] Contacto activo: {contact}")

        # === PASO 2: Enriquecer contactos activos con HubSpot ===
        contact_manager = ContactManager()
        for contact in active_contacts:
            phone = contact.get("phone", "")
            if phone and not contact.get("contact_id"):
                # Intentar buscar contact_id si no lo tenemos
                try:
                    contact_id = await contact_manager._search_contact(phone)
                    if contact_id:
                        contact["contact_id"] = contact_id
                except Exception:
                    pass

            # Si tenemos contact_id, obtener nombre de HubSpot
            if contact.get("contact_id"):
                try:
                    # Obtener info básica del contacto
                    timeline_logger = get_timeline_logger()
                    hs_info = await _get_hubspot_contact_info(contact["contact_id"])
                    if hs_info:
                        firstname = hs_info.get("firstname", "")
                        lastname = hs_info.get("lastname", "")
                        contact["display_name"] = f"{firstname} {lastname}".strip() or "Sin nombre"
                        contact["email"] = hs_info.get("email")
                except Exception as e:
                    logger.debug(f"[Panel] No se pudo enriquecer contacto: {e}")

            # Si aún no tenemos nombre, usar teléfono
            if not contact.get("display_name"):
                contact["display_name"] = phone or "Sin nombre"

            # Formatear TTL para mostrar
            ttl = contact.get("ttl_remaining")
            if ttl and ttl > 0:
                hours = ttl // 3600
                minutes = (ttl % 3600) // 60
                contact["ttl_display"] = f"Expira en {hours}h {minutes}m"

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

        # === PASO 4: Obtener historial de HubSpot (si hay espacio) ===
        remaining_slots = limit - len(active_contacts)
        historical_contacts = []

        if remaining_slots > 0:
            try:
                timeline_logger = get_timeline_logger()
                historical_contacts = await timeline_logger.get_contacts_with_advisor_activity(
                    since=since,
                    until=until,
                    limit=remaining_slots
                )

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

        # === PASO 6: Ordenar (activos primero) ===
        contacts_sorted = sorted(
            active_contacts,
            key=lambda x: (
                not x.get("is_active", False),  # Activos primero
                x.get("activated_at", "") or ""  # Luego por fecha
            ),
            reverse=False
        )

        # Invertir para que activos estén al inicio
        contacts_sorted = sorted(
            contacts_sorted,
            key=lambda x: (0 if x.get("is_active", False) else 1, x.get("activated_at", "") or ""),
        )

        active_count = len([c for c in contacts_sorted if c.get("is_active")])

        return {
            "contacts": contacts_sorted[:limit],
            "filter": filter_time,
            "active_count": active_count,
            "historical_count": len(contacts_sorted) - active_count,
            "total_count": len(contacts_sorted),
            "since": since.isoformat(),
            "until": until.isoformat()
        }

    except Exception as e:
        logger.error(f"[Panel] Error obteniendo contactos: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def _get_hubspot_contact_info(contact_id: str) -> Optional[dict]:
    """
    Obtiene información básica de un contacto de HubSpot.

    Args:
        contact_id: ID del contacto en HubSpot

    Returns:
        Diccionario con firstname, lastname, email o None si falla
    """
    import httpx

    hubspot_api_key = os.getenv("HUBSPOT_API_KEY")
    if not hubspot_api_key:
        return None

    try:
        url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
        params = {"properties": "firstname,lastname,email,phone"}

        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {hubspot_api_key}"},
                params=params,
                timeout=10.0
            )

            if response.status_code == 200:
                data = response.json()
                return data.get("properties", {})

    except Exception as e:
        logger.debug(f"[Panel] Error obteniendo info de HubSpot: {e}")

    return None


# ============================================================================
# UI del Panel
# ============================================================================

@router.get("/", response_class=HTMLResponse)
async def panel_ui(x_api_key: str = Query(None, alias="key")):
    """
    Interfaz web del panel de envío para asesores - WhatsApp Web Style.

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
                <p>Se requiere API Key válida.</p>
                <p>Uso: /whatsapp/panel/?key=TU_API_KEY</p>
            </body>
            </html>
            """,
            status_code=401
        )

    html_content = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Panel de Asesores - WhatsApp</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <style>
            /* Custom scrollbar */
            ::-webkit-scrollbar {{ width: 6px; }}
            ::-webkit-scrollbar-track {{ background: #f1f1f1; }}
            ::-webkit-scrollbar-thumb {{ background: #c1c1c1; border-radius: 3px; }}
            ::-webkit-scrollbar-thumb:hover {{ background: #a1a1a1; }}

            /* Chat bubbles */
            .bubble-client {{
                background: white;
                border-radius: 0 8px 8px 8px;
                max-width: 80%;
            }}
            .bubble-bot {{
                background: #dcf8c6;
                border-radius: 8px 0 8px 8px;
                max-width: 80%;
            }}
            .bubble-advisor {{
                background: #e3f2fd;
                border-radius: 8px 0 8px 8px;
                max-width: 80%;
            }}

            /* WhatsApp background pattern */
            .chat-bg {{
                background-color: #e5ddd5;
                background-image: url("data:image/svg+xml,%3Csvg width='60' height='60' viewBox='0 0 60 60' xmlns='http://www.w3.org/2000/svg'%3E%3Cg fill='none' fill-rule='evenodd'%3E%3Cg fill='%23d4cfc5' fill-opacity='0.4'%3E%3Cpath d='M36 34v-4h-2v4h-4v2h4v4h2v-4h4v-2h-4zm0-30V0h-2v4h-4v2h4v4h2V6h4V4h-4zM6 34v-4H4v4H0v2h4v4h2v-4h4v-2H6zM6 4V0H4v4H0v2h4v4h2V6h4V4H6z'/%3E%3C/g%3E%3C/g%3E%3C/svg%3E");
            }}

            /* Contact list item */
            .contact-item {{
                transition: background-color 0.2s;
            }}
            .contact-item:hover {{
                background-color: #f5f5f5;
            }}
            .contact-item.active {{
                background-color: #e8f5e9;
            }}

            /* Pulse animation for polling indicator */
            .pulse {{
                animation: pulse 2s infinite;
            }}
            @keyframes pulse {{
                0%, 100% {{ opacity: 1; }}
                50% {{ opacity: 0.5; }}
            }}
        </style>
    </head>
    <body class="bg-gray-100 h-screen overflow-hidden">
        <div class="flex h-full">
            <!-- ═══════════════════════════════════════════════════════════════ -->
            <!-- SIDEBAR: Lista de Contactos -->
            <!-- ═══════════════════════════════════════════════════════════════ -->
            <div class="w-1/3 bg-white border-r flex flex-col">
                <!-- Header -->
                <div class="bg-green-600 text-white p-4">
                    <div class="flex items-center justify-between">
                        <h1 class="text-lg font-semibold">Asesores Comerciales</h1>
                        <div class="flex items-center gap-2">
                            <span class="pulse text-xs bg-green-500 px-2 py-1 rounded-full">En vivo</span>
                        </div>
                    </div>
                    <p class="text-sm opacity-90 mt-1">Inmobiliaria Proteger</p>
                </div>

                <!-- Filtros de tiempo -->
                <div class="p-3 bg-gray-50 border-b">
                    <div class="flex flex-wrap gap-2">
                        <select id="timeFilter" class="text-sm border rounded px-2 py-1 flex-1">
                            <option value="24h">Últimas 24 horas</option>
                            <option value="48h">Últimas 48 horas</option>
                            <option value="1week">Última semana</option>
                            <option value="custom">Personalizado</option>
                        </select>
                        <button id="refreshBtn" class="bg-green-600 text-white px-3 py-1 rounded text-sm hover:bg-green-700">
                            ⟳
                        </button>
                    </div>
                    <!-- Fechas personalizadas (oculto por defecto) -->
                    <div id="customDates" class="hidden mt-2 flex gap-2">
                        <input type="date" id="dateFrom" class="text-sm border rounded px-2 py-1 flex-1">
                        <input type="date" id="dateTo" class="text-sm border rounded px-2 py-1 flex-1">
                        <button id="applyDatesBtn" class="bg-blue-600 text-white px-2 py-1 rounded text-sm">OK</button>
                    </div>
                </div>

                <!-- Lista de contactos -->
                <div id="contactsList" class="flex-1 overflow-y-auto">
                    <div class="p-4 text-center text-gray-500">
                        <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-green-600 mx-auto"></div>
                        <p class="mt-2 text-sm">Cargando contactos...</p>
                    </div>
                </div>

                <!-- Info de actualización y contadores -->
                <div class="p-2 bg-gray-50 border-t text-xs text-gray-500">
                    <div class="flex justify-between items-center">
                        <span id="activeCounter" class="text-green-600 font-medium"></span>
                        <span id="lastUpdate">Actualización: --</span>
                    </div>
                </div>
            </div>

            <!-- ═══════════════════════════════════════════════════════════════ -->
            <!-- MAIN: Área de Chat -->
            <!-- ═══════════════════════════════════════════════════════════════ -->
            <div class="w-2/3 flex flex-col">
                <!-- Header del chat (contacto seleccionado) -->
                <div id="chatHeader" class="bg-gray-100 p-4 border-b flex items-center justify-between">
                    <div>
                        <h2 id="contactName" class="font-semibold text-gray-700">Selecciona un contacto</h2>
                        <p id="contactPhone" class="text-sm text-gray-500"></p>
                    </div>
                    <div id="windowStatus" class="hidden text-sm"></div>
                </div>

                <!-- Área de mensajes -->
                <div id="chatMessages" class="flex-1 overflow-y-auto chat-bg p-4">
                    <div class="flex items-center justify-center h-full text-gray-500">
                        <div class="text-center">
                            <svg class="w-16 h-16 mx-auto mb-4 text-gray-300" fill="currentColor" viewBox="0 0 24 24">
                                <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/>
                            </svg>
                            <p>Selecciona un contacto para ver la conversación</p>
                        </div>
                    </div>
                </div>

                <!-- Input de mensaje -->
                <div class="bg-gray-100 p-4 border-t">
                    <form id="sendForm" class="flex gap-2">
                        <input type="hidden" id="selectedPhone" value="">
                        <input type="hidden" id="selectedContactId" value="">
                        <textarea
                            id="messageInput"
                            placeholder="Escribe un mensaje..."
                            class="flex-1 border rounded-lg p-3 resize-none focus:outline-none focus:ring-2 focus:ring-green-500"
                            rows="2"
                            disabled
                        ></textarea>
                        <button
                            type="submit"
                            id="sendBtn"
                            class="bg-green-600 text-white px-6 rounded-lg hover:bg-green-700 disabled:bg-gray-400 disabled:cursor-not-allowed transition-colors"
                            disabled
                        >
                            <svg class="w-6 h-6" fill="currentColor" viewBox="0 0 24 24">
                                <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
                            </svg>
                        </button>
                    </form>
                    <div id="sendResult" class="mt-2 text-sm hidden"></div>
                </div>
            </div>
        </div>

        <script>
            // ═══════════════════════════════════════════════════════════════════
            // CONFIGURACIÓN
            // ═══════════════════════════════════════════════════════════════════
            const API_KEY = '{x_api_key}';
            const BASE_URL = '/whatsapp/panel';
            const POLLING_INTERVAL = 5000; // 5 segundos

            let currentContactId = null;
            let currentPhone = null;
            let pollingInterval = null;

            // ═══════════════════════════════════════════════════════════════════
            // FUNCIONES DE CARGA DE DATOS
            // ═══════════════════════════════════════════════════════════════════

            async function loadContacts() {{
                const filter = document.getElementById('timeFilter').value;
                let url = `${{BASE_URL}}/contacts?filter_time=${{filter}}`;

                // Agregar fechas si es filtro custom
                if (filter === 'custom') {{
                    const dateFrom = document.getElementById('dateFrom').value;
                    const dateTo = document.getElementById('dateTo').value;
                    if (dateFrom) url += `&date_from=${{dateFrom}}T00:00:00`;
                    if (dateTo) url += `&date_to=${{dateTo}}T23:59:59`;
                }}

                try {{
                    const response = await fetch(url, {{
                        headers: {{ 'X-API-Key': API_KEY }}
                    }});

                    if (!response.ok) throw new Error('Error al cargar contactos');

                    const data = await response.json();
                    renderContactsList(data.contacts);
                    updateLastUpdateTime();

                    // Actualizar contador de activos
                    const activeCounter = document.getElementById('activeCounter');
                    if (data.active_count > 0) {{
                        activeCounter.innerHTML = `<span class="inline-block w-2 h-2 bg-green-500 rounded-full mr-1 animate-pulse"></span>${{data.active_count}} en espera`;
                    }} else {{
                        activeCounter.textContent = '';
                    }}

                }} catch (error) {{
                    console.error('Error cargando contactos:', error);
                    document.getElementById('contactsList').innerHTML = `
                        <div class="p-4 text-center text-red-500">
                            <p>Error al cargar contactos</p>
                            <p class="text-sm">${{error.message}}</p>
                        </div>
                    `;
                }}
            }}

            async function loadChatHistory(contactId) {{
                try {{
                    const response = await fetch(`${{BASE_URL}}/history/${{contactId}}?limit=50`, {{
                        headers: {{ 'X-API-Key': API_KEY }}
                    }});

                    if (!response.ok) throw new Error('Error al cargar historial');

                    const data = await response.json();
                    renderChatBubbles(data.messages);

                }} catch (error) {{
                    console.error('Error cargando historial:', error);
                    document.getElementById('chatMessages').innerHTML = `
                        <div class="flex items-center justify-center h-full text-red-500">
                            <p>Error al cargar historial: ${{error.message}}</p>
                        </div>
                    `;
                }}
            }}

            async function checkWindowStatus(phone) {{
                try {{
                    const response = await fetch(
                        `${{BASE_URL}}/window-status/${{encodeURIComponent(phone)}}`,
                        {{ headers: {{ 'X-API-Key': API_KEY }} }}
                    );

                    const data = await response.json();
                    const statusDiv = document.getElementById('windowStatus');
                    statusDiv.classList.remove('hidden');

                    if (data.window_open) {{
                        statusDiv.className = 'text-sm bg-green-100 text-green-700 px-3 py-1 rounded-full';
                        statusDiv.textContent = `Ventana: ${{data.message}}`;
                    }} else {{
                        statusDiv.className = 'text-sm bg-orange-100 text-orange-700 px-3 py-1 rounded-full';
                        statusDiv.textContent = 'Ventana cerrada (requiere template)';
                    }}
                }} catch (error) {{
                    console.error('Error verificando ventana:', error);
                }}
            }}

            // ═══════════════════════════════════════════════════════════════════
            // FUNCIONES DE RENDERIZADO
            // ═══════════════════════════════════════════════════════════════════

            function renderContactsList(contacts) {{
                const container = document.getElementById('contactsList');

                if (!contacts || contacts.length === 0) {{
                    container.innerHTML = `
                        <div class="p-4 text-center text-gray-500">
                            <p>No hay contactos esperando atención</p>
                            <p class="text-sm mt-1">Los contactos aparecerán automáticamente cuando Sofía haga handoff</p>
                        </div>
                    `;
                    return;
                }}

                container.innerHTML = contacts.map(contact => {{
                    const isActive = contact.is_active === true;
                    const contactId = contact.contact_id || contact.id || '';
                    const phone = contact.phone || '';
                    const displayName = contact.display_name || 'Sin nombre';

                    return `
                        <div class="contact-item p-3 border-b cursor-pointer ${{isActive ? 'bg-green-50 border-l-4 border-green-500' : ''}} ${{contactId === currentContactId ? 'active' : ''}}"
                             onclick="selectContact('${{contactId}}', '${{phone}}', '${{displayName.replace(/'/g, "\\'")}}')">
                            <div class="flex items-center gap-3">
                                <div class="w-10 h-10 ${{isActive ? 'bg-green-500' : 'bg-gray-300'}} rounded-full flex items-center justify-center text-white font-semibold">
                                    ${{(displayName || '?').charAt(0).toUpperCase()}}
                                </div>
                                <div class="flex-1 min-w-0">
                                    <p class="font-medium text-gray-800 truncate">${{displayName}}</p>
                                    <p class="text-sm text-gray-500 truncate">${{phone || contact.email || 'Sin contacto'}}</p>
                                    ${{contact.handoff_reason ? `<p class="text-xs text-gray-400 truncate">${{contact.handoff_reason}}</p>` : ''}}
                                </div>
                                <div class="text-right">
                                    ${{isActive
                                        ? `<span class="text-xs bg-green-100 text-green-700 px-2 py-0.5 rounded-full animate-pulse">En espera</span>
                                           ${{contact.ttl_display ? `<p class="text-xs text-gray-400 mt-1">${{contact.ttl_display}}</p>` : ''}}`
                                        : contact.conversation_status === 'BOT_ACTIVE'
                                        ? '<span class="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded-full">Bot</span>'
                                        : '<span class="text-xs bg-gray-100 text-gray-500 px-2 py-0.5 rounded-full">Historial</span>'
                                    }}
                                </div>
                            </div>
                        </div>
                    `;
                }}).join('');
            }}

            function renderChatBubbles(messages) {{
                const container = document.getElementById('chatMessages');

                if (!messages || messages.length === 0) {{
                    container.innerHTML = `
                        <div class="flex items-center justify-center h-full text-gray-500">
                            <p>No hay mensajes en el historial</p>
                        </div>
                    `;
                    return;
                }}

                container.innerHTML = messages.map(msg => {{
                    const isRight = msg.align === 'right';
                    const bubbleClass = msg.sender === 'client' ? 'bubble-client'
                                      : msg.sender === 'bot' ? 'bubble-bot'
                                      : 'bubble-advisor';

                    const timestamp = msg.timestamp
                        ? new Date(msg.timestamp).toLocaleTimeString('es-CO', {{hour: '2-digit', minute: '2-digit'}})
                        : '';

                    return `
                        <div class="flex ${{isRight ? 'justify-end' : 'justify-start'}} mb-3">
                            <div class="${{bubbleClass}} p-3 shadow-sm">
                                <p class="text-xs font-semibold text-gray-600 mb-1">${{msg.sender_name || msg.sender}}</p>
                                <p class="text-gray-800 whitespace-pre-wrap">${{escapeHtml(msg.message)}}</p>
                                <p class="text-xs text-gray-500 text-right mt-1">${{timestamp}}</p>
                            </div>
                        </div>
                    `;
                }}).join('');

                // Scroll al final
                container.scrollTop = container.scrollHeight;
            }}

            function escapeHtml(text) {{
                if (!text) return '';
                const div = document.createElement('div');
                div.textContent = text;
                return div.innerHTML;
            }}

            function updateLastUpdateTime() {{
                const now = new Date().toLocaleTimeString('es-CO');
                document.getElementById('lastUpdate').textContent = `Última actualización: ${{now}}`;
            }}

            // ═══════════════════════════════════════════════════════════════════
            // FUNCIONES DE INTERACCIÓN
            // ═══════════════════════════════════════════════════════════════════

            function selectContact(contactId, phone, displayName) {{
                currentContactId = contactId;
                currentPhone = phone;

                // Actualizar header
                document.getElementById('contactName').textContent = displayName;
                document.getElementById('contactPhone').textContent = phone;

                // Habilitar input
                document.getElementById('messageInput').disabled = false;
                document.getElementById('sendBtn').disabled = false;
                document.getElementById('selectedPhone').value = phone;
                document.getElementById('selectedContactId').value = contactId;

                // Cargar historial
                loadChatHistory(contactId);

                // Verificar ventana de 24h
                if (phone) {{
                    checkWindowStatus(phone);
                }}

                // Actualizar lista (marcar activo)
                document.querySelectorAll('.contact-item').forEach(el => {{
                    el.classList.remove('active');
                }});
                event.currentTarget.classList.add('active');
            }}

            async function sendMessage(e) {{
                e.preventDefault();

                const phone = document.getElementById('selectedPhone').value;
                const contactId = document.getElementById('selectedContactId').value;
                const message = document.getElementById('messageInput').value.trim();
                const resultDiv = document.getElementById('sendResult');

                if (!phone || !message) {{
                    resultDiv.className = 'mt-2 text-sm text-red-600';
                    resultDiv.textContent = 'Selecciona un contacto y escribe un mensaje';
                    resultDiv.classList.remove('hidden');
                    return;
                }}

                // Deshabilitar mientras envía
                document.getElementById('sendBtn').disabled = true;
                document.getElementById('messageInput').disabled = true;

                try {{
                    const formData = new FormData();
                    formData.append('to', phone);
                    formData.append('body', message);
                    formData.append('contact_id', contactId);

                    const response = await fetch(`${{BASE_URL}}/send-message`, {{
                        method: 'POST',
                        headers: {{ 'X-API-Key': API_KEY }},
                        body: formData
                    }});

                    const data = await response.json();

                    if (data.status === 'success') {{
                        resultDiv.className = 'mt-2 text-sm text-green-600';
                        resultDiv.textContent = 'Mensaje enviado correctamente';
                        document.getElementById('messageInput').value = '';

                        // Recargar historial
                        setTimeout(() => loadChatHistory(contactId), 1000);
                    }} else if (data.status === 'warning') {{
                        resultDiv.className = 'mt-2 text-sm text-orange-600';
                        resultDiv.textContent = data.message;
                    }} else {{
                        throw new Error(data.detail || data.message || 'Error desconocido');
                    }}

                }} catch (error) {{
                    resultDiv.className = 'mt-2 text-sm text-red-600';
                    resultDiv.textContent = `Error: ${{error.message}}`;
                }} finally {{
                    document.getElementById('sendBtn').disabled = false;
                    document.getElementById('messageInput').disabled = false;
                    resultDiv.classList.remove('hidden');

                    // Ocultar mensaje después de 5 segundos
                    setTimeout(() => resultDiv.classList.add('hidden'), 5000);
                }}
            }}

            // ═══════════════════════════════════════════════════════════════════
            // POLLING
            // ═══════════════════════════════════════════════════════════════════

            function startPolling() {{
                if (pollingInterval) clearInterval(pollingInterval);

                pollingInterval = setInterval(async () => {{
                    // Actualizar lista de contactos
                    await loadContacts();

                    // Actualizar chat si hay contacto seleccionado
                    if (currentContactId) {{
                        await loadChatHistory(currentContactId);
                    }}
                }}, POLLING_INTERVAL);
            }}

            function stopPolling() {{
                if (pollingInterval) {{
                    clearInterval(pollingInterval);
                    pollingInterval = null;
                }}
            }}

            // ═══════════════════════════════════════════════════════════════════
            // EVENT LISTENERS
            // ═══════════════════════════════════════════════════════════════════

            document.addEventListener('DOMContentLoaded', () => {{
                // Cargar contactos iniciales
                loadContacts();

                // Iniciar polling
                startPolling();

                // Filtro de tiempo
                document.getElementById('timeFilter').addEventListener('change', function() {{
                    const customDates = document.getElementById('customDates');
                    if (this.value === 'custom') {{
                        customDates.classList.remove('hidden');
                    }} else {{
                        customDates.classList.add('hidden');
                        loadContacts();
                    }}
                }});

                // Botón refresh
                document.getElementById('refreshBtn').addEventListener('click', loadContacts);

                // Aplicar fechas custom
                document.getElementById('applyDatesBtn').addEventListener('click', loadContacts);

                // Enviar mensaje
                document.getElementById('sendForm').addEventListener('submit', sendMessage);

                // Enviar con Ctrl+Enter
                document.getElementById('messageInput').addEventListener('keydown', function(e) {{
                    if (e.ctrlKey && e.key === 'Enter') {{
                        sendMessage(e);
                    }}
                }});
            }});

            // Detener polling cuando se cierra la pestaña
            window.addEventListener('beforeunload', stopPolling);
        </script>
    </body>
    </html>
    """

    return HTMLResponse(content=html_content)


# ============================================================================
# Funciones de background
# ============================================================================

async def _log_advisor_message_to_hubspot(
    contact_id: str,
    message: str,
    phone: str,
    message_source: str
) -> None:
    """
    Registra un mensaje del asesor en HubSpot Timeline.
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

    except Exception as e:
        logger.error(f"[Panel] Error registrando en HubSpot: {e}")