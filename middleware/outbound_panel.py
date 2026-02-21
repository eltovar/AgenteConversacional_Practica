# middleware/outbound_panel.py
"""
Este módulo proporciona endpoints API y UI para que los asesores envíen
mensajes de WhatsApp directamente, sustituyendo el Inbox bloqueado de HubSpot.
"""

import os
import json
import re
import html
from io import BytesIO
from typing import Optional
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass

from fastapi import APIRouter, Form, Header, HTTPException, BackgroundTasks, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
import redis.asyncio as redis

from logging_config import logger
from .phone_normalizer import PhoneNormalizer
from .conversation_state import ConversationStateManager, ConversationStatus
from .contact_manager import ContactManager
from utils.twilio_client import twilio_client
from integrations.hubspot import get_timeline_logger


# Router de FastAPI para el panel de envío
router = APIRouter(prefix="/whatsapp/panel", tags=["Panel de Envío"])

# Configuración de Jinja2 Templates
TEMPLATES_DIR = Path(__file__).parent / "PanelAsesores"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ============================================================================
# Configuración y constantes
# ============================================================================

# API Key para autenticación del panel
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

# Ventana de 24 horas de WhatsApp (en segundos)
WHATSAPP_WINDOW_SECONDS = 24 * 60 * 60

# Prefijo en Redis para almacenar último mensaje del cliente
LAST_CLIENT_MESSAGE_PREFIX = "last_client_msg:"

# Prefijo en Redis para almacenar templates de WhatsApp
TEMPLATE_PREFIX = "whatsapp_template:"

# Templates predefinidos (se cargan a Redis si no existen)
DEFAULT_TEMPLATES = {
    "reactivacion_general": {
        "id": "reactivacion_general",
        "name": "Reactivación General",
        "category": "reactivacion",
        "body": "¡Hola {nombre}! Soy del equipo de Inmobiliaria Proteger. ¿Sigues interesado/a en nuestros servicios inmobiliarios? Estamos aquí para ayudarte.",
        "variables": ["nombre"],
        "is_default": True
    },
    "cita_confirmacion": {
        "id": "cita_confirmacion",
        "name": "Confirmación de Cita",
        "category": "cita",
        "body": "¡Hola {nombre}! Te confirmamos tu cita para el {fecha} a las {hora}. Te esperamos en {direccion}. ¿Nos confirmas tu asistencia?",
        "variables": ["nombre", "fecha", "hora", "direccion"],
        "is_default": True
    },
    "cita_recordatorio": {
        "id": "cita_recordatorio",
        "name": "Recordatorio de Cita",
        "category": "cita",
        "body": "¡Hola {nombre}! Te recordamos que mañana {fecha} tienes cita a las {hora}. ¡Te esperamos!",
        "variables": ["nombre", "fecha", "hora"],
        "is_default": True
    },
    "seguimiento_visita": {
        "id": "seguimiento_visita",
        "name": "Seguimiento Post-Visita",
        "category": "seguimiento",
        "body": "¡Hola {nombre}! Esperamos que la visita al inmueble haya sido de tu agrado. ¿Te gustaría agendar otra visita o tienes alguna pregunta?",
        "variables": ["nombre"],
        "is_default": True
    },
    "seguimiento_24h": {
        "id": "seguimiento_24h",
        "name": "Seguimiento 24 horas",
        "category": "seguimiento",
        "body": "¡Hola {nombre}! ¿Pudiste revisar la información que te enviamos? Estamos aquí para resolver cualquier duda.",
        "variables": ["nombre"],
        "is_default": True
    },
    "cita_cancelacion": {
        "id": "cita_cancelacion",
        "name": "Cancelación de Cita",
        "category": "cita",
        "body": "¡Hola {nombre}! Lamentamos informarte que la cita del {fecha} a las {hora} ha sido cancelada. ¿Te gustaría reagendarla para otro momento?",
        "variables": ["nombre", "fecha", "hora"],
        "is_default": True
    },
    "cita_reagendar": {
        "id": "cita_reagendar",
        "name": "Reagendar Cita",
        "category": "cita",
        "body": "¡Hola {nombre}! ¿Te gustaría reagendar tu cita? Tenemos disponibilidad el {fecha} a las {hora}. ¿Te funciona?",
        "variables": ["nombre", "fecha", "hora"],
        "is_default": True
    },
    "promocion_general": {
        "id": "promocion_general",
        "name": "Promoción General",
        "category": "promocion",
        "body": "¡Hola {nombre}! Tenemos una promoción especial para ti. ¿Te gustaría conocer los detalles?",
        "variables": ["nombre"],
        "is_default": True
    },
    "agradecimiento": {
        "id": "agradecimiento",
        "name": "Agradecimiento",
        "category": "seguimiento",
        "body": "¡Hola {nombre}! Gracias por confiar en Inmobiliaria Proteger. Fue un placer atenderte. Si necesitas algo más, aquí estamos para ayudarte.",
        "variables": ["nombre"],
        "is_default": True
    },
}


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
# Funciones CRUD de Templates
# ============================================================================

async def _init_default_templates():
    """Inicializa templates predefinidos en Redis si no existen."""
    try:
        r = await _get_redis_client()
        for template_id, template_data in DEFAULT_TEMPLATES.items():
            key = f"{TEMPLATE_PREFIX}{template_id}"
            if not await r.exists(key):
                await r.set(key, json.dumps(template_data))
                logger.debug(f"[Templates] Template predefinido creado: {template_id}")
    except Exception as e:
        logger.error(f"[Templates] Error inicializando templates: {e}")


async def _get_all_templates() -> list:
    """Obtiene todos los templates de Redis."""
    try:
        r = await _get_redis_client()
        templates = []
        async for key in r.scan_iter(match=f"{TEMPLATE_PREFIX}*"):
            data = await r.get(key)
            if data:
                template = json.loads(data)
                templates.append(template)

        # Ordenar por categoría y nombre
        templates.sort(key=lambda x: (x.get("category", ""), x.get("name", "")))
        return templates
    except Exception as e:
        logger.error(f"[Templates] Error obteniendo templates: {e}")
        return []


async def _get_template(template_id: str) -> Optional[dict]:
    """Obtiene un template específico de Redis."""
    try:
        r = await _get_redis_client()
        key = f"{TEMPLATE_PREFIX}{template_id}"
        data = await r.get(key)
        if data:
            return json.loads(data)
        return None
    except Exception as e:
        logger.error(f"[Templates] Error obteniendo template {template_id}: {e}")
        return None


async def _save_template(template_data: dict) -> bool:
    """Guarda o actualiza un template en Redis."""
    try:
        r = await _get_redis_client()
        template_id = template_data.get("id")
        if not template_id:
            return False

        key = f"{TEMPLATE_PREFIX}{template_id}"
        await r.set(key, json.dumps(template_data))
        logger.info(f"[Templates] Template guardado: {template_id}")
        return True
    except Exception as e:
        logger.error(f"[Templates] Error guardando template: {e}")
        return False


async def _delete_template(template_id: str) -> bool:
    """Elimina un template de Redis."""
    try:
        r = await _get_redis_client()
        key = f"{TEMPLATE_PREFIX}{template_id}"

        # Verificar que existe y no es default
        data = await r.get(key)
        if data:
            template = json.loads(data)
            if template.get("is_default"):
                logger.warning(f"[Templates] No se puede eliminar template predefinido: {template_id}")
                return False

        result = await r.delete(key)
        if result > 0:
            logger.info(f"[Templates] Template eliminado: {template_id}")
            return True
        return False
    except Exception as e:
        logger.error(f"[Templates] Error eliminando template: {e}")
        return False


# ============================================================================
# Endpoints de API
# ============================================================================

@router.post("/send-message")
async def send_message(
    background_tasks: BackgroundTasks,
    to: str = Form(..., description="Número de destino (+573001234567)"),
    body: str = Form(..., description="Contenido del mensaje"),
    contact_id: Optional[str] = Form(None, description="ID del contacto en HubSpot"),
    canal: Optional[str] = Form(None, description="Canal de origen para segregación"),
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

    SEGREGACIÓN POR CANAL:
    Si se proporciona el parámetro canal, se usa para identificar
    la conversación correcta en sistemas multicanal.

    Headers requeridos:
        X-API-Key: Token de autenticación admin

    Form data:
        to: Número de destino
        body: Contenido del mensaje
        contact_id: ID del contacto en HubSpot (opcional)
        canal: Canal de origen para segregación (opcional)
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

    # Pausar Sofía y cambiar a IN_CONVERSATION (asesora está chateando activamente)
    # SEGREGACIÓN POR CANAL: Usar el canal proporcionado para operaciones de estado
    try:
        redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
        state_manager = ConversationStateManager(redis_url)

        canal_info = f":{canal}" if canal else ""

        # Verificar estado actual (con canal)
        current_status = await state_manager.get_status(phone_normalized, canal)

        if current_status in [ConversationStatus.HUMAN_ACTIVE, ConversationStatus.PENDING_HANDOFF]:
            # Ya está en espera, cambiar a IN_CONVERSATION (asesora está atendiendo)
            await state_manager.set_status(
                phone_normalized,
                ConversationStatus.IN_CONVERSATION,
                ttl=state_manager.HANDOFF_TTL_SECONDS,
                canal=canal
            )
            logger.info(f"[Panel] Estado cambiado a IN_CONVERSATION para {phone_normalized}{canal_info}")
        elif current_status == ConversationStatus.IN_CONVERSATION:
            # Ya está en conversación, solo refrescar TTL
            await state_manager.set_status(
                phone_normalized,
                ConversationStatus.IN_CONVERSATION,
                ttl=state_manager.HANDOFF_TTL_SECONDS,
                canal=canal
            )
            logger.info(f"[Panel] TTL refrescado para IN_CONVERSATION: {phone_normalized}{canal_info}")
        else:
            # Era BOT_ACTIVE o CLOSED, activar humano y cambiar a IN_CONVERSATION
            await state_manager.activate_human(phone_normalized, canal_origen=canal)
            await state_manager.set_status(
                phone_normalized,
                ConversationStatus.IN_CONVERSATION,
                ttl=state_manager.HANDOFF_TTL_SECONDS,
                canal=canal
            )
            logger.info(f"[Panel] Sofía pausada y estado IN_CONVERSATION para {phone_normalized}{canal_info}")
    except Exception as e:
        logger.warning(f"[Panel] Error manejando estado: {e}")

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

        # Actualizar timestamp del asesor para TTL diferenciado
        background_tasks.add_task(
            _update_advisor_timestamp,
            phone_normalized,
            canal
        )

        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message_sid": result.get("message_sid"),
                "to": phone_normalized,
                "contact_id": contact_id,
                "canal": canal,
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


@router.post("/send-template")
async def send_template_message(
    background_tasks: BackgroundTasks,
    to: str = Form(..., description="Número de destino (+573001234567)"),
    template_id: str = Form("reactivacion_general", description="ID del template a usar"),
    variables: str = Form("{}", description="JSON con variables para el template"),
    contact_id: Optional[str] = Form(None, description="ID del contacto en HubSpot"),
    canal: Optional[str] = Form(None, description="Canal de origen para segregación"),
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

    # Verificar disponibilidad de Twilio
    if not twilio_client.is_available:
        raise HTTPException(
            status_code=503,
            detail="Twilio no está configurado correctamente"
        )

    # Inicializar templates predefinidos si es necesario
    await _init_default_templates()

    # Obtener template de Redis
    template = await _get_template(template_id)
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

    # Enviar mensaje (usando force porque es template)
    result = await twilio_client.send_whatsapp_message(
        to=phone_normalized,
        body=template_message
    )

    if result["status"] == "success":
        # Registrar en HubSpot Timeline (background)
        if contact_id:
            background_tasks.add_task(
                _log_advisor_message_to_hubspot,
                contact_id,
                f"[TEMPLATE: {template.get('name', template_id)}] {template_message}",
                phone_normalized,
                "Template via Panel"
            )

        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message_sid": result.get("message_sid"),
                "to": phone_normalized,
                "contact_id": contact_id,
                "canal": canal,
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
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Lista todos los templates disponibles."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # Inicializar templates predefinidos si es necesario
    await _init_default_templates()

    templates = await _get_all_templates()

    # Agrupar por categoría
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
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Obtiene un template específico."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    template = await _get_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' no encontrado")

    return template


@router.post("/templates")
async def create_template(
    name: str = Form(..., description="Nombre del template"),
    category: str = Form(..., description="Categoría: reactivacion, cita, seguimiento, recordatorio, promocion"),
    body: str = Form(..., description="Cuerpo del mensaje con variables {nombre}, {fecha}, etc."),
    variables: str = Form("[]", description="JSON array de nombres de variables"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Crea un nuevo template."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # Generar ID único basado en nombre
    template_id = re.sub(r'[^a-z0-9_]', '_', name.lower().strip())
    template_id = re.sub(r'_+', '_', template_id).strip('_')

    # Verificar que no existe
    existing = await _get_template(template_id)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Ya existe un template con ID '{template_id}'"
        )

    # Parsear variables
    try:
        vars_list = json.loads(variables) if variables else []
    except json.JSONDecodeError:
        vars_list = []

    # Crear template
    template_data = {
        "id": template_id,
        "name": name.strip(),
        "category": category.strip(),
        "body": body.strip(),
        "variables": vars_list,
        "is_default": False,
        "created_at": datetime.now(timezone.utc).isoformat()
    }

    success = await _save_template(template_data)
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
    name: str = Form(None, description="Nombre del template"),
    category: str = Form(None, description="Categoría"),
    body: str = Form(None, description="Cuerpo del mensaje"),
    variables: str = Form(None, description="JSON array de variables"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Actualiza un template existente."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # Obtener template existente
    template = await _get_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' no encontrado")

    # Actualizar campos proporcionados
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

    template["updated_at"] = datetime.now(timezone.utc).isoformat()

    success = await _save_template(template)
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
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Elimina un template (no se pueden eliminar templates predefinidos)."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # Verificar que existe
    template = await _get_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' no encontrado")

    # Verificar que no es predefinido
    if template.get("is_default"):
        raise HTTPException(
            status_code=403,
            detail="No se pueden eliminar templates predefinidos"
        )

    success = await _delete_template(template_id)
    if not success:
        raise HTTPException(status_code=500, detail="Error eliminando template")

    return {
        "status": "success",
        "message": f"Template '{template_id}' eliminado"
    }


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

    import httpx

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
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.patch(
                url,
                headers={
                    "Authorization": f"Bearer {hubspot_api_key}",
                    "Content-Type": "application/json"
                },
                json=payload
            )

            logger.info(f"[Panel] Respuesta HubSpot: {response.status_code}")

            if response.status_code == 200:
                logger.info(f"[Panel] Nombre actualizado para contacto {contact_id}: {firstname} {lastname}")
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

    IMPORTANTE: Ya NO elimina la conversación de Redis, sino que la transiciona
    a BOT_ACTIVE para que Sofía pueda continuar con contexto.

    SEGREGACIÓN POR CANAL:
    Si se proporciona el parámetro canal, solo se cierra la conversación
    de ese canal específico.

    Args:
        phone: Número de teléfono normalizado (E.164)
        canal: Canal de origen (instagram, finca_raiz, etc.)
    """
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # Normalizar teléfono si no está normalizado
    normalizer = PhoneNormalizer()
    validation = normalizer.normalize(phone)

    phone_normalized = validation.normalized if validation.is_valid else phone

    try:
        redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
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
    canal: Optional[str] = Query(None, description="Canal de origen para filtrar mensajes"),
    phone: Optional[str] = Query(None, description="Teléfono para buscar historial de Sofía"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Obtiene el historial de conversación por contact_id.

    SEGREGACIÓN POR CANAL:
    Si se proporciona el parámetro canal y phone, también se obtiene
    el historial de Sofía desde Redis (segregado por canal).

    Args:
        contact_id: ID del contacto en HubSpot
        limit: Máximo de mensajes a retornar
        canal: Canal de origen para segregación
        phone: Teléfono normalizado para buscar historial de Sofía

    Returns:
        Historial de conversación como burbujas de chat
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

    try:
        timeline_logger = get_timeline_logger()

        # Obtener notas de HubSpot
        messages = await timeline_logger.get_notes_for_contact(
            contact_id=contact_id,
            limit=limit
        )

        # Log para debug de segregación
        if canal:
            logger.info(f"[Panel] Historial solicitado con canal={canal}, phone={phone}")

        # Asegurar que messages sea una lista válida
        if messages is None:
            messages = []

        canal_info = f", canal={canal}" if canal else ""
        logger.info(f"[Panel] Historial cargado: {len(messages)} mensajes para contact_id={contact_id}{canal_info}")

        return {
            "contact_id": contact_id,
            "messages": messages,
            "count": len(messages),
            "canal": canal,
            "phone": phone
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
                "error": f"Error interno: {str(e)}"
            }
        )


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
    advisor: Optional[str] = Query(None, description="Owner ID para filtrar contactos por asesora"),
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

        # === PASO 3.5: Filtrar contactos activos por fecha de activación ===
        # Esto asegura que si se filtra por "48h", solo muestre contactos
        # que fueron activados dentro de las últimas 48h
        if filter_time != "all":
            filtered_active = []
            for contact in active_contacts:
                activated_at = contact.get("activated_at")
                if activated_at:
                    try:
                        # Parsear fecha de activación
                        if isinstance(activated_at, str):
                            activated_dt = datetime.fromisoformat(activated_at.replace("Z", "+00:00"))
                        else:
                            activated_dt = activated_at

                        # Asegurar timezone
                        if activated_dt.tzinfo is None:
                            activated_dt = activated_dt.replace(tzinfo=TIMEZONE)

                        # Incluir si está dentro del rango
                        if since <= activated_dt <= until:
                            # Calcular tiempo desde activación para mostrar
                            time_ago = now - activated_dt.astimezone(TIMEZONE)
                            if time_ago.total_seconds() < 3600:
                                contact["time_ago"] = f"hace {int(time_ago.total_seconds() // 60)} min"
                            elif time_ago.total_seconds() < 86400:
                                contact["time_ago"] = f"hace {int(time_ago.total_seconds() // 3600)} h"
                            else:
                                contact["time_ago"] = f"hace {int(time_ago.days)} días"

                            filtered_active.append(contact)
                        else:
                            logger.debug(
                                f"[Panel] Contacto {contact.get('phone')} excluido por filtro de tiempo "
                                f"(activado: {activated_dt}, filtro desde: {since})"
                            )
                    except (ValueError, TypeError) as e:
                        # Si no podemos parsear la fecha, incluir el contacto
                        logger.debug(f"[Panel] Error parseando fecha de activación: {e}")
                        filtered_active.append(contact)
                else:
                    # Si no tiene fecha de activación, intentar usar last_activity como fallback
                    last_activity = contact.get("last_activity")
                    if last_activity:
                        try:
                            if isinstance(last_activity, str):
                                activity_dt = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
                            else:
                                activity_dt = last_activity

                            if activity_dt.tzinfo is None:
                                activity_dt = activity_dt.replace(tzinfo=TIMEZONE)

                            # Filtrar por last_activity si no hay activated_at
                            if since <= activity_dt <= until:
                                time_ago = now - activity_dt.astimezone(TIMEZONE)
                                if time_ago.total_seconds() < 3600:
                                    contact["time_ago"] = f"hace {int(time_ago.total_seconds() // 60)} min"
                                elif time_ago.total_seconds() < 86400:
                                    contact["time_ago"] = f"hace {int(time_ago.total_seconds() // 3600)} h"
                                else:
                                    contact["time_ago"] = f"hace {int(time_ago.days)} días"
                                filtered_active.append(contact)
                            else:
                                logger.debug(
                                    f"[Panel] Contacto {contact.get('phone')} excluido por filtro (usando last_activity)"
                                )
                        except (ValueError, TypeError) as e:
                            # Si tampoco podemos parsear last_activity, incluir como reciente
                            logger.debug(f"[Panel] Error parseando last_activity: {e}")
                            contact["time_ago"] = "reciente"
                            filtered_active.append(contact)
                    else:
                        # Sin ninguna fecha disponible, incluir como reciente
                        contact["time_ago"] = "reciente"
                        filtered_active.append(contact)

            logger.info(
                f"[Panel] Contactos activos después de filtro de tiempo: "
                f"{len(filtered_active)}/{len(active_contacts)}"
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

        # === PASO 6: Filtrar por advisor (owner_id) si se especificó ===
        if advisor:
            # Importar mapeo de canales a owners
            from integrations.hubspot.lead_assigner import LeadAssigner

            # Filtrar contactos que pertenezcan al advisor especificado
            filtered_contacts = []
            for contact in active_contacts:
                # Verificar si el contact tiene owner_id directo
                contact_owner = contact.get("owner_id") or contact.get("hubspot_owner_id")

                # Si no tiene owner_id, intentar inferir por canal_origen
                if not contact_owner:
                    canal = contact.get("canal_origen", "")
                    contact_owner = LeadAssigner.CHANNEL_TO_OWNER.get(canal)

                # Incluir si coincide con el advisor O si no tiene owner asignado
                # (para no perder contactos activos en espera)
                if contact_owner == advisor:
                    filtered_contacts.append(contact)
                elif not contact_owner:
                    # Sin owner asignado -> incluir para que no se pierda
                    contact["owner_status"] = "unassigned"
                    filtered_contacts.append(contact)
                    logger.debug(
                        f"[Panel] Contacto {contact.get('phone')} sin owner, "
                        f"incluido como unassigned"
                    )

            active_contacts = filtered_contacts
            logger.info(f"[Panel] Filtrado por advisor {advisor}: {len(active_contacts)} contactos")

        # === PASO 7: Ordenar (activos primero) ===
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

        # Log para diagnóstico
        logger.info(
            f"[Panel] Retornando {len(contacts_sorted[:limit])} contactos "
            f"(activos: {active_count}, advisor: {advisor})"
        )
        for c in contacts_sorted[:limit]:
            logger.debug(
                f"[Panel] -> {c.get('phone', 'N/A')} | "
                f"active={c.get('is_active')} | "
                f"owner={c.get('owner_id', 'N/A')} | "
                f"status={c.get('owner_status', 'assigned')}"
            )

        return {
            "contacts": contacts_sorted[:limit],
            "filter": filter_time,
            "advisor": advisor,
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

    # Mapeo de advisor ID a nombre para mostrar en la UI
    advisor_names = {
        '87367331': 'Luisa',
        '88251457': 'Yubeny',
        '88558384': 'Analista Redes'
    }

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


async def _update_advisor_timestamp(phone_normalized: str, canal: Optional[str] = None) -> None:
    """
    Actualiza timestamp del mensaje del asesor en ConversationMeta.
    Usado para calcular TTL de 72h si asesor deja de responder.
    """
    try:
        redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
        state_manager = ConversationStateManager(redis_url)
        await state_manager.update_advisor_message_timestamp(phone_normalized, canal)
        logger.debug(f"[Panel] Timestamp asesor actualizado: {phone_normalized}:{canal or 'default'}")
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

    Procesa:
    - Etiquetas HTML (<p>, <br>, <div>, etc.)
    - Entidades HTML (&amp;, &nbsp;, etc.)
    - Emojis y símbolos unicode
    - Tags de HubSpot ({{contact.name}}, hs-*, etc.)
    - URLs y enlaces
    - Caracteres de control
    - Espacios múltiples
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

        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {hubspot_api_key}"},
                json=payload,
                timeout=15.0
            )

            if response.status_code != 200:
                logger.error(f"[Metrics] HubSpot error: {response.status_code} - {response.text}")
                raise HTTPException(
                    status_code=503,
                    detail=f"Error consultando HubSpot: {response.status_code}. Intenta de nuevo en unos minutos."
                )

            data = response.json()
            contacts = data.get("results", [])

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
            <!DOCTYPE html>
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
