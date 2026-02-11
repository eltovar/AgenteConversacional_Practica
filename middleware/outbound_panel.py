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
    limit: int = Query(20, ge=1, le=100),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Obtiene el historial de conversación de un contacto.

    Este endpoint consulta las notas en HubSpot asociadas al contacto
    para mostrar el historial de mensajes.

    Args:
        phone: Número telefónico
        limit: Máximo de mensajes a retornar

    Returns:
        Historial de conversación
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

        # TODO: Implementar búsqueda de notas en HubSpot
        # Por ahora retornamos estructura vacía
        return {
            "phone": validation.normalized,
            "contact_id": contact_id,
            "messages": [],
            "message": "Historial disponible en HubSpot Timeline"
        }

    except Exception as e:
        logger.error(f"[Panel] Error obteniendo historial: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# UI del Panel
# ============================================================================

@router.get("/", response_class=HTMLResponse)
async def panel_ui(x_api_key: str = Query(None, alias="key")):
    """
    Interfaz web del panel de envío para asesores.

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
        <title>Panel de Envío - WhatsApp</title>
        <style>
            * {{
                box-sizing: border-box;
                margin: 0;
                padding: 0;
            }}

            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }}

            .container {{
                max-width: 600px;
                margin: 0 auto;
                background: white;
                border-radius: 16px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                overflow: hidden;
            }}

            .header {{
                background: #25D366;
                color: white;
                padding: 20px;
                text-align: center;
            }}

            .header h1 {{
                font-size: 1.5rem;
                margin-bottom: 5px;
            }}

            .header p {{
                opacity: 0.9;
                font-size: 0.9rem;
            }}

            .form-container {{
                padding: 30px;
            }}

            .form-group {{
                margin-bottom: 20px;
            }}

            label {{
                display: block;
                margin-bottom: 8px;
                font-weight: 600;
                color: #333;
            }}

            input[type="text"],
            input[type="tel"],
            textarea {{
                width: 100%;
                padding: 12px 15px;
                border: 2px solid #e0e0e0;
                border-radius: 8px;
                font-size: 1rem;
                transition: border-color 0.3s;
            }}

            input:focus,
            textarea:focus {{
                outline: none;
                border-color: #25D366;
            }}

            textarea {{
                min-height: 120px;
                resize: vertical;
            }}

            .window-status {{
                padding: 15px;
                border-radius: 8px;
                margin-bottom: 20px;
                display: none;
            }}

            .window-status.open {{
                background: #e8f5e9;
                border: 1px solid #4caf50;
                color: #2e7d32;
                display: block;
            }}

            .window-status.closed {{
                background: #fff3e0;
                border: 1px solid #ff9800;
                color: #e65100;
                display: block;
            }}

            .btn {{
                width: 100%;
                padding: 15px;
                border: none;
                border-radius: 8px;
                font-size: 1rem;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.3s;
            }}

            .btn-primary {{
                background: #25D366;
                color: white;
            }}

            .btn-primary:hover {{
                background: #1da851;
                transform: translateY(-2px);
            }}

            .btn-primary:disabled {{
                background: #ccc;
                cursor: not-allowed;
                transform: none;
            }}

            .btn-secondary {{
                background: #f5f5f5;
                color: #333;
                margin-top: 10px;
            }}

            .btn-secondary:hover {{
                background: #e0e0e0;
            }}

            .result {{
                margin-top: 20px;
                padding: 15px;
                border-radius: 8px;
                display: none;
            }}

            .result.success {{
                background: #e8f5e9;
                border: 1px solid #4caf50;
                color: #2e7d32;
                display: block;
            }}

            .result.error {{
                background: #ffebee;
                border: 1px solid #f44336;
                color: #c62828;
                display: block;
            }}

            .result.warning {{
                background: #fff3e0;
                border: 1px solid #ff9800;
                color: #e65100;
                display: block;
            }}

            .info-box {{
                background: #e3f2fd;
                border: 1px solid #2196f3;
                border-radius: 8px;
                padding: 15px;
                margin-bottom: 20px;
                font-size: 0.9rem;
                color: #1565c0;
            }}

            .loader {{
                display: none;
                text-align: center;
                padding: 20px;
            }}

            .loader.active {{
                display: block;
            }}

            .spinner {{
                border: 3px solid #f3f3f3;
                border-top: 3px solid #25D366;
                border-radius: 50%;
                width: 30px;
                height: 30px;
                animation: spin 1s linear infinite;
                margin: 0 auto;
            }}

            @keyframes spin {{
                0% {{ transform: rotate(0deg); }}
                100% {{ transform: rotate(360deg); }}
            }}

            .footer {{
                text-align: center;
                padding: 15px;
                background: #f5f5f5;
                font-size: 0.8rem;
                color: #666;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Panel de Envío WhatsApp</h1>
                <p>Envía mensajes a clientes directamente</p>
            </div>

            <div class="form-container">
                <div class="info-box">
                    <strong>Nota:</strong> Al enviar un mensaje, Sofía se pausará automáticamente
                    para este contacto. El cliente será atendido manualmente hasta reactivar el bot.
                </div>

                <form id="sendForm">
                    <div class="form-group">
                        <label for="phone">Número de WhatsApp</label>
                        <input
                            type="tel"
                            id="phone"
                            name="to"
                            placeholder="+573001234567"
                            required
                        >
                    </div>

                    <div id="windowStatus" class="window-status"></div>

                    <div class="form-group">
                        <label for="message">Mensaje</label>
                        <textarea
                            id="message"
                            name="body"
                            placeholder="Escribe tu mensaje aquí..."
                            required
                        ></textarea>
                    </div>

                    <input type="hidden" id="contactId" name="contact_id" value="">

                    <button type="submit" class="btn btn-primary" id="sendBtn">
                        Enviar Mensaje
                    </button>

                    <button type="button" class="btn btn-secondary" id="checkWindowBtn">
                        Verificar Ventana 24h
                    </button>
                </form>

                <div class="loader" id="loader">
                    <div class="spinner"></div>
                    <p style="margin-top: 10px;">Enviando mensaje...</p>
                </div>

                <div id="result" class="result"></div>
            </div>

            <div class="footer">
                Panel de Asesores - Inmobiliaria Proteger
            </div>
        </div>

        <script>
            const API_KEY = '{x_api_key}';
            const BASE_URL = '/whatsapp/panel';

            // Verificar ventana de 24 horas
            async function checkWindow() {{
                const phone = document.getElementById('phone').value;
                if (!phone) {{
                    alert('Ingresa un número primero');
                    return;
                }}

                const statusDiv = document.getElementById('windowStatus');
                statusDiv.className = 'window-status';
                statusDiv.textContent = 'Verificando...';
                statusDiv.style.display = 'block';

                try {{
                    const response = await fetch(
                        `${{BASE_URL}}/window-status/${{encodeURIComponent(phone)}}`,
                        {{
                            headers: {{ 'X-API-Key': API_KEY }}
                        }}
                    );

                    const data = await response.json();

                    if (data.window_open) {{
                        statusDiv.className = 'window-status open';
                        statusDiv.innerHTML = `
                            <strong>Ventana ABIERTA</strong><br>
                            ${{data.message}}
                        `;
                    }} else {{
                        statusDiv.className = 'window-status closed';
                        statusDiv.innerHTML = `
                            <strong>Ventana CERRADA</strong><br>
                            ${{data.message}}<br>
                            <small>El mensaje se enviará como Template si está disponible.</small>
                        `;
                    }}
                }} catch (error) {{
                    statusDiv.className = 'window-status closed';
                    statusDiv.textContent = 'Error verificando ventana: ' + error.message;
                }}
            }}

            document.getElementById('checkWindowBtn').addEventListener('click', checkWindow);

            // Verificar al cambiar número (con debounce)
            let debounceTimer;
            document.getElementById('phone').addEventListener('input', function() {{
                clearTimeout(debounceTimer);
                debounceTimer = setTimeout(checkWindow, 1000);
            }});

            // Enviar mensaje
            document.getElementById('sendForm').addEventListener('submit', async function(e) {{
                e.preventDefault();

                const form = e.target;
                const sendBtn = document.getElementById('sendBtn');
                const loader = document.getElementById('loader');
                const resultDiv = document.getElementById('result');

                // Validación básica
                const phone = document.getElementById('phone').value;
                const message = document.getElementById('message').value;

                if (!phone || !message) {{
                    resultDiv.className = 'result error';
                    resultDiv.textContent = 'Completa todos los campos';
                    return;
                }}

                // UI: mostrar loader
                sendBtn.disabled = true;
                loader.classList.add('active');
                resultDiv.className = 'result';
                resultDiv.style.display = 'none';

                try {{
                    const formData = new FormData(form);

                    const response = await fetch(`${{BASE_URL}}/send-message`, {{
                        method: 'POST',
                        headers: {{ 'X-API-Key': API_KEY }},
                        body: formData
                    }});

                    const data = await response.json();

                    if (data.status === 'success') {{
                        resultDiv.className = 'result success';
                        resultDiv.innerHTML = `
                            <strong>Mensaje enviado</strong><br>
                            A: ${{data.to}}<br>
                            ID: ${{data.message_sid}}<br>
                            <small>Sofía pausada automáticamente</small>
                        `;
                        // Limpiar formulario
                        document.getElementById('message').value = '';
                    }} else if (data.status === 'warning') {{
                        resultDiv.className = 'result warning';
                        resultDiv.innerHTML = `
                            <strong>Advertencia</strong><br>
                            ${{data.message}}<br>
                            <small>${{data.hint || ''}}</small>
                        `;
                    }} else {{
                        throw new Error(data.detail || data.message || 'Error desconocido');
                    }}

                }} catch (error) {{
                    resultDiv.className = 'result error';
                    resultDiv.textContent = 'Error: ' + error.message;
                }} finally {{
                    sendBtn.disabled = false;
                    loader.classList.remove('active');
                }}
            }});
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

    Args:
        contact_id: ID del contacto en HubSpot
        message: Contenido del mensaje
        phone: Número normalizado
        message_source: Origen del mensaje (ej: "Manual via Panel")
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