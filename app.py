# ═══════════════════════════════════════════════════════════════════════════════
# CARGA DE VARIABLES DE ENTORNO (DEBE IR PRIMERO)
# ═══════════════════════════════════════════════════════════════════════════════
from dotenv import load_dotenv

from utils.message_aggregator import message_aggregator
load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINT PARA SERVIR MEDIA DEL PANEL
# ═══════════════════════════════════════════════════════════════════════════════
from fastapi.responses import RedirectResponse
from database.mongodb_client import get_mongo_manager
from bson import ObjectId

async def get_panel_media(message_id: str, as_json: bool = False):
    """
    Handler para servir media (audio, imagen) del historial del panel.
    Se registra como ruta después de crear la instancia `app`.
    """
    mongo = get_mongo_manager()
    msg = await mongo.db.messages.find_one({"_id": ObjectId(message_id)})
    if not msg or "media" not in msg or not msg["media"].get("permanent_url"):
        return JSONResponse({"error": "Media no encontrada"}, status_code=404)
    media = msg["media"]
    if as_json:
        return JSONResponse({
            "permanent_url": media.get("permanent_url"),
            "type": media.get("type"),
            "transcription": media.get("transcription"),
            "analysis": media.get("analysis"),
            "size_bytes": media.get("size_bytes"),
            "format": media.get("format"),
            "duration_seconds": media.get("duration_seconds"),
            "processed_at": media.get("processed_at"),
        })
    return RedirectResponse(media["permanent_url"])
# app.py
"""
Servidor FastAPI asíncrono para chatbot Sofía.
Maneja webhooks de Twilio (WhatsApp) y requests JSON.

OPTIMIZACIONES v2.0:
- Schedulers con idempotencia (verificación de flags en Redis antes de enviar)
- Jitter de 5 segundos para evitar envíos simultáneos
- Timezone consistente America/Bogota en todas las comparaciones
- Logs detallados con diferencias de tiempo
- Compatibilidad completa con managers de Sesión 1
"""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
from agents.orchestrator import process_message
from utils.twilio_client import twilio_client
from logging_config import logger
import uvicorn
import os
import asyncio
import random
from typing import Optional
from datetime import timedelta
# Scheduler para seguimiento automático
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Importar componentes del middleware (Sesión 1)
from middleware import get_whatsapp_router, get_outbound_panel_router, get_contact_manager, get_contact_manager_singleton
from middleware.conversation_state import (
    ConversationStateManager,
    ConversationStatus,
    TIMEZONE_BOGOTA,
    get_bogota_now,
    get_bogota_now_iso
)

from middleware.appointment_manager import AppointmentManager

# Importar el router de webhooks de salida HubSpot -> WhatsApp
from integrations.hubspot import get_outbound_router, get_timeline_logger

# Importar endpoint de auditoría temporal
from audit_endpoint import audit_router

# Importar función para actualizar ventana de 24h
from middleware.outbound_panel import update_last_client_message
from middleware.websocket_manager import ws_manager



# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN INICIAL
# ═══════════════════════════════════════════════════════════════════════════════

# Validar variables requeridas
REQUIRED = [
    "OPENAI_API_KEY",
    "HUBSPOT_API_KEY",
]

if missing := [k for k in REQUIRED if not os.getenv(k)]:
    logger.warning(f"⚠️ Variables faltantes (no críticas): {', '.join(missing)}")


def get_redis_url() -> str:
    """Obtiene la URL de Redis desde variables de entorno."""
    return os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))


# Singleton del ConversationStateManager — evita crear una nueva pool Redis por request.
# Sin esto, process_aggregated_messages() creaba una pool completa por cada mensaje entrante,
# acumulando cientos de conexiones TCP huérfanas en RAM.
_global_state_manager: Optional[ConversationStateManager] = None


def get_state_manager() -> ConversationStateManager:
    global _global_state_manager
    if _global_state_manager is None:
        _global_state_manager = ConversationStateManager(get_redis_url())
    return _global_state_manager


# ═══════════════════════════════════════════════════════════════════════════════
# APLICACIÓN FASTAPI
# ═══════════════════════════════════════════════════════════════════════════════

app = FastAPI(title="Sofía IA - Middleware", version="2.0.0")

# ═══════════════════════════════════════════════════════════════════════════════
# RATE LIMITING — protección contra abuso y reducción de carga en hot paths
# ═══════════════════════════════════════════════════════════════════════════════
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Manejador global de errores para devolver JSON en caso de error 500
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
import traceback

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    # Loguear el error completo
    _diag_logger.error(f"[500 ERROR] path={request.url.path} error={exc} traceback={traceback.format_exc()}")
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Error interno del servidor. Intenta nuevamente o contacta soporte.",
            "error_type": exc.__class__.__name__,
            "path": str(request.url.path)
        }
    )

# Handler para loguear detalles de errores de validación (diagnóstico 422)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import logging as _logging
_diag_logger = _logging.getLogger("agent_system")

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    content_type = request.headers.get("content-type", "MISSING")
    try:
        raw_body = await request.body()
        body_preview = raw_body[:300].decode("utf-8", errors="replace")
    except Exception as be:
        body_preview = f"[error leyendo body: {be}]"
    _diag_logger.error(
        f"[422 DETAIL] path={request.url.path} "
        f"content-type='{content_type}' "
        f"body='{body_preview}' "
        f"errors={exc.errors()}"
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

# Montar archivos estáticos para el panel
PANEL_STATIC_PATH = Path(__file__).parent / "middleware" / "PanelAsesores"
if PANEL_STATIC_PATH.exists():
    app.mount("/whatsapp/panel/static", StaticFiles(directory=str(PANEL_STATIC_PATH)), name="panel_static")

# Incluir routers
app.include_router(get_whatsapp_router())
app.include_router(get_outbound_panel_router())
app.include_router(get_outbound_router())
app.include_router(audit_router)  # Endpoint de auditoría temporal

# Registrar la ruta de media del panel (handler definido arriba)
app.get("/whatsapp/panel/media/{message_id}")(get_panel_media)


# ═══════════════════════════════════════════════════════════════════════════════
# MODELOS PYDANTIC
# ═══════════════════════════════════════════════════════════════════════════════

class MessageRequest(BaseModel):
    session_id: str
    message: str


class MessageResponse(BaseModel):
    response: str
    status: str


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN DEL SCHEDULER
# ═══════════════════════════════════════════════════════════════════════════════

scheduler = AsyncIOScheduler(timezone=str(TIMEZONE_BOGOTA))
SCHEDULER_JITTER_SECONDS = 5

# Configuración de seguimiento
FOLLOWUP_ENABLED = os.getenv("FOLLOWUP_ENABLED", "false").lower() == "true"
FOLLOWUP_DELAY_HOURS = int(os.getenv("FOLLOWUP_DELAY_HOURS", "24"))
APPOINTMENT_REMINDERS_ENABLED = os.getenv("APPOINTMENT_REMINDERS_ENABLED", "false").lower() == "true"


async def apply_jitter():
    """Aplica un delay aleatorio de 0-5 segundos para evitar envíos simultáneos."""
    jitter = random.uniform(0, SCHEDULER_JITTER_SECONDS)
    logger.debug("[Scheduler] Aplicando jitter de %.2f segundos", jitter)
    await asyncio.sleep(jitter)


# ═══════════════════════════════════════════════════════════════════════════════
# FUNCIÓN DE PROCESAMIENTO EN BACKGROUND
# ═══════════════════════════════════════════════════════════════════════════════

async def process_aggregated_messages(session_id: str, to_number: str):
    """
    Procesa mensajes agregados en background.
    Resuelve el problema del timeout de 15 segundos de Twilio.
    """
    try:
        # 1. Esperar y obtener mensajes combinados
        combined_message = await message_aggregator.wait_and_get_combined_message(session_id)

        if not combined_message:
            logger.warning("[BACKGROUND] No hay mensajes para procesar (session: %s)", session_id)
            return

        logger.info("[BACKGROUND] Procesando mensajes agregados: '%s...'", combined_message[:80])

        # 2. Normalizar teléfono
        phone_normalized = session_id.replace("whatsapp:", "").replace("+", "")
        if phone_normalized.startswith("57") and len(phone_normalized) == 12:
            phone_normalized = f"+{phone_normalized}"
        elif not phone_normalized.startswith("+"):
            phone_normalized = f"+57{phone_normalized}" if len(phone_normalized) == 10 else f"+{phone_normalized}"

        # 3. Actualizar ventana de 24h
        try:
            await update_last_client_message(phone_normalized)
            logger.info("[BACKGROUND] ✅ Ventana 24h actualizada para %s", phone_normalized)
        except Exception as window_err:
            logger.error("[BACKGROUND] Error actualizando ventana 24h: %s", window_err)

        # 4. Verificar si bot debe responder
        state_manager = get_state_manager()

        try:
            is_bot_active = await state_manager.is_bot_active(phone_normalized)

            if not is_bot_active:
                current_status = await state_manager.get_status(phone_normalized)
                logger.info(
                    "[BACKGROUND] 👤 Estado %s detectado para %s. Bot silenciado.",
                    current_status.value, phone_normalized
                )

                # Registrar mensaje en HubSpot aunque bot esté silenciado
                try:
                    contact_manager = get_contact_manager_singleton()
                    contact_info = await contact_manager.identify_or_create_contact_safe(
                        phone_raw=phone_normalized,
                        source_channel="whatsapp_directo"
                    )
                    if contact_info and contact_info.contact_id:
                        timeline_logger = get_timeline_logger()
                        await timeline_logger.log_client_message(
                            contact_id=contact_info.contact_id,
                            content=combined_message,
                            session_id=phone_normalized
                        )
                        logger.info("[BACKGROUND] 📱 Mensaje registrado en HubSpot (bot silenciado)")
                except Exception as hs_err:
                    logger.error("[BACKGROUND] Error registrando en HubSpot: %s", hs_err)

                return  # No procesar con IA

        except Exception as state_err:
            logger.error("[BACKGROUND] Error verificando estado: %s", state_err)

        # 5. Registrar mensaje en HubSpot
        try:
            contact_manager = get_contact_manager_singleton()
            contact_info = await contact_manager.identify_or_create_contact_safe(
                phone_raw=phone_normalized,
                source_channel="whatsapp_directo"
            ) 

            if contact_info and contact_info.contact_id:
                logger.info("[BACKGROUND] 📱 Registrando mensaje en HubSpot (contact_id=%s)", contact_info.contact_id)
                timeline_logger = get_timeline_logger()
                await timeline_logger.log_client_message(
                    contact_id=contact_info.contact_id,
                    content=combined_message,
                    session_id=phone_normalized
                )
        except Exception as hubspot_err:
            logger.error("[BACKGROUND] Error registrando en HubSpot: %s", hubspot_err)

        # 6. Procesar con orchestrator
        result = await process_message(session_id, combined_message)

        if not result or not result.get("response"):
            logger.warning("[BACKGROUND] Orchestrator no generó respuesta para %s", session_id)
            return

        # 7. CHECK FINAL antes de enviar (anti race-condition)
        try:
            final_status = await state_manager.get_status(phone_normalized)
            if final_status in [ConversationStatus.HUMAN_ACTIVE, ConversationStatus.IN_CONVERSATION]:
                logger.warning(
                    "[BACKGROUND] ⚠️ RACE CONDITION EVITADA: Estado cambió a %s. NO se enviará respuesta.",
                    final_status.value
                )
                return
        except Exception:
            pass

        # 8. Enviar respuesta via Twilio
        if twilio_client.is_available:
            send_result = await twilio_client.send_whatsapp_message(
                to=to_number,
                body=result["response"]
            )
            if send_result["status"] == "success":
                logger.info("[BACKGROUND] Respuesta enviada exitosamente a %s", to_number)
            else:
                logger.error("[BACKGROUND] Error enviando respuesta: %s", send_result)
        else:
            logger.error("[BACKGROUND] Twilio client no disponible")

    except Exception as e:
        logger.error("[BACKGROUND] Error en procesamiento: %s", e, exc_info=True)
        
# ═══════════════════════════════════════════════════════════════════════════════
# LÓGICA DE RECORDATORIOS DE CITAS
# ═══════════════════════════════════════════════════════════════════════════════
async def check_appointment_reminders():
    """
    Verifica citas próximas y envía recordatorios 5 horas antes.
    Implementa idempotencia verificando 'reminder_sent' en Redis.

    COMPATIBLE con AppointmentManager de Sesión 1.
    """
    if not APPOINTMENT_REMINDERS_ENABLED:
        return

    await apply_jitter()

    apt_manager = AppointmentManager(get_redis_url())
    state_manager = get_state_manager()
    now = get_bogota_now()

    logger.info("[Scheduler][Reminder] ▶ Ciclo iniciado — Hora Bogotá: %s", now.strftime('%Y-%m-%d %H:%M:%S %Z'))

    total, sent, skipped = 0, 0, 0
    VALID_STATUSES = {ConversationStatus.BOT_ACTIVE, ConversationStatus.HUMAN_ACTIVE, ConversationStatus.IN_CONVERSATION}

    try:
        upcoming_appointments = await apt_manager.get_appointments_needing_reminder()

        logger.info("[Scheduler][Reminder] Citas encontradas para recordatorio: %d", len(upcoming_appointments))

        for apt in upcoming_appointments:
            total += 1
            scheduled_dt = apt.scheduled_dt  # Property que ya convierte a Bogotá
            diff = scheduled_dt - now
            diff_hours = diff.total_seconds() / 3600

            logger.info(
                "[Scheduler][Reminder] Verificando cita %s: Diferencia %.2f horas, Estado cita: %s",
                apt.phone_normalized, diff_hours, apt.status.value
            )

            # Verificar estado de conversación — enviar si bot o asesor activo
            status = await state_manager.get_status(apt.phone_normalized, apt.canal)

            if status not in VALID_STATUSES:
                logger.info(
                    "[Scheduler][Reminder] Skip %s — estado inactivo: %s",
                    apt.phone_normalized, status.value if status else "None"
                )
                skipped += 1
                continue

            logger.info(
                "[Scheduler][Reminder] Enviando recordatorio a %s (estado conv: %s)",
                apt.phone_normalized, status.value if status else "N/A"
            )

            # Construir mensaje de recordatorio — resolver nombre con fallback de 3 niveles
            contact_name = apt.contact_name
            _name_source = "appointment_redis"
            if not contact_name:
                # Nivel 2: Redis conversation meta (display_name se popula desde webhook)
                try:
                    _meta = await state_manager.get_meta(apt.phone_normalized, apt.canal)
                    if _meta and _meta.display_name:
                        contact_name = _meta.display_name.split()[0]
                        _name_source = "redis_meta"
                except Exception:
                    pass
            if not contact_name:
                contact_name = "cliente"
                _name_source = "fallback"
            logger.info(
                "[Scheduler][Naming] %s → nombre='%s' (source=%s)",
                apt.phone_normalized, contact_name, _name_source
            )
            message = (
                f"¡Hola {contact_name}! 👋 Te recuerdo tu cita programada para hoy "
                f"a las {scheduled_dt.strftime('%H:%M')}. ¡Te esperamos!"
            )

            # Enviar vía Twilio
            if twilio_client.is_available:
                _reminder_sid = os.getenv("TWILIO_REMINDER_TEMPLATE_SID")
                if _reminder_sid:
                    result = await twilio_client.send_whatsapp_message(
                        to=apt.phone_normalized,
                        body=message,
                        content_sid=_reminder_sid,
                        content_variables={"1": contact_name, "2": scheduled_dt.strftime('%H:%M')}
                    )
                else:
                    result = await twilio_client.send_whatsapp_message(
                        to=apt.phone_normalized,
                        body=message
                    )

                if result.get("status") == "success":
                    # Marcar como enviado (método correcto de Sesión 1)
                    await apt_manager.mark_reminder_sent(apt.phone_normalized, apt.canal)

                    # Registrar nota en HubSpot
                    if apt.contact_id:
                        try:
                            timeline = get_timeline_logger()
                            await timeline.log_bot_message(
                                contact_id=apt.contact_id,
                                content="Recordatorio de cita enviado automáticamente por Sofía.",
                                session_id=apt.phone_normalized
                            )
                        except Exception as hs_err:
                            logger.error("[Scheduler][Reminder] Error registrando en HubSpot: %s", hs_err)

                    sent += 1
                    logger.info("[Scheduler][Reminder] ✅ Recordatorio enviado a %s", apt.phone_normalized)
                    # Guardar en MongoDB para que aparezca en el panel
                    try:
                        canal_apt = apt.canal or "whatsapp"
                        mongo_mgr = get_mongo_manager()
                        await mongo_mgr.save_message(
                            phone=apt.phone_normalized,
                            content=message,
                            sender="bot",
                            channel=canal_apt,
                            hubspot_contact_id=apt.contact_id,
                            message_sid=result.get("message_sid"),
                            metadata={"source": "Recordatorio automático de cita"}
                        )
                        await state_manager.update_activity(apt.phone_normalized, canal=canal_apt)
                        await ws_manager.publish_broadcast(state_manager.redis, {
                            "type": "contact_updated",
                            "action": "new_message",
                            "phone": apt.phone_normalized,
                            "canal": canal_apt,
                            "sender": "bot",
                            "preview": message[:100],
                            "contact_name": contact_name
                        })
                    except Exception as _panel_err:
                        logger.warning(
                            "[Scheduler][Reminder] No se pudo guardar en panel: %s", _panel_err
                        )
                else:
                    logger.warning(
                        "[Scheduler][Reminder] ❌ Error enviando recordatorio a %s: %s",
                        apt.phone_normalized, result.get("message")
                    )
            else:
                logger.warning("[Scheduler][Reminder] Twilio no disponible para recordatorios")

        logger.info(
            "[Scheduler][Reminder] ✓ Completado — evaluadas: %d, enviadas: %d, omitidas: %d",
            total, sent, skipped
        )

    except Exception as e:
        logger.error("[Scheduler][Reminder] Error en check_appointment_reminders: %s", e, exc_info=True)
    finally:
        await apt_manager.close()


# ═══════════════════════════════════════════════════════════════════════════════
# LÓGICA DE TIMEOUTS DE CONVERSACIÓN
# ═══════════════════════════════════════════════════════════════════════════════

async def check_conversation_timeouts():
    """
    Detecta conversaciones en HUMAN_ACTIVE inactivas por > 96h (4 días)
    y las devuelve a BOT_ACTIVE.

    COMPATIBLE con ConversationStateManager de Sesión 1.
    """
    await apply_jitter()

    state_manager = get_state_manager()
    now = get_bogota_now()

    logger.info("[Scheduler] Verificando timeouts de conversaciones...")
    logger.info("[Scheduler] Hora actual (Bogotá): %s", now.strftime('%Y-%m-%d %H:%M:%S %Z'))

    try:
        # Paginar para procesar contactos en posición >100 del ZSET
        all_active_contacts: list = []
        _offset = 0
        _page_size = 100
        while True:
            page = await state_manager.get_all_human_active_contacts(limit=_page_size, offset=_offset)
            all_active_contacts.extend(page)
            if len(page) < _page_size:
                break
            _offset += _page_size
        active_contacts = all_active_contacts

        logger.info("[Scheduler] Contactos activos encontrados: %d", len(active_contacts))

        timeouts_processed = 0

        for contact in active_contacts:
            phone = contact.get("phone")
            canal = contact.get("canal_origen")
            last_activity_str = contact.get("last_activity")
            status = contact.get("status")

            if not last_activity_str:
                continue

            # Usar función de parsing segura
            from middleware.conversation_state import parse_datetime_safe
            last_activity = parse_datetime_safe(last_activity_str)
            diff_hours = (now - last_activity).total_seconds() / 3600

            logger.debug(
                "[Scheduler] Verificando timeout %s:%s: %.1f horas inactivo, Estado %s",
                phone, canal or "default", diff_hours, status
            )

            # Si han pasado más de 96 horas (4 días) en estado humano sin actividad
            # Antes era 24h — se aumentó para no solapar con el HUMAN_PANEL_STATE_TTL de 7 días
            if diff_hours > 96 and status in ["HUMAN_ACTIVE", "IN_CONVERSATION"]:
                logger.info(
                    "[Scheduler] Timeout detectado para %s:%s (%.1f horas inactivo). "
                    "Reseteando a BOT_ACTIVE.",
                    phone, canal or "default", diff_hours
                )

                # Resetear estado usando método correcto
                await state_manager.activate_bot(phone, canal)
                timeouts_processed += 1

        logger.info("[Scheduler] Timeouts procesados: %d", timeouts_processed)

    except Exception as e:
        logger.error("[Scheduler] Error en check_conversation_timeouts: %s", e, exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════════
# LÓGICA DE SEGUIMIENTO 24H
# ═══════════════════════════════════════════════════════════════════════════════

async def check_and_send_followups():
    """
    Verifica contactos que no han respondido en 24h y envía mensaje de seguimiento.

    Implementa idempotencia con flags en Redis.
    """
    if not FOLLOWUP_ENABLED:
        return

    await apply_jitter()

    redis_url = get_redis_url()
    now = get_bogota_now()

    logger.info("[FOLLOWUP] Iniciando verificación de seguimientos pendientes...")
    logger.info("[FOLLOWUP] Hora actual (Bogotá): %s", now.strftime('%Y-%m-%d %H:%M:%S %Z'))

    r = None
    try:
        import redis.asyncio as redis_async

        r = redis_async.from_url(redis_url, encoding="utf-8", decode_responses=True)

        LAST_MSG_PREFIX = "last_client_msg:"
        FOLLOWUP_SENT_PREFIX = "followup_sent:"
        FOLLOWUP_LOCK_PREFIX = "followup_lock:"

        threshold = now - timedelta(hours=FOLLOWUP_DELAY_HOURS)

        followups_sent = 0
        contacts_checked = 0

        # Una sola instancia CSM para todo el ciclo — evita crear una pool Redis por contacto.
        followup_state_manager = get_state_manager()

        async for key in r.scan_iter(match=f"{LAST_MSG_PREFIX}*"):
            contacts_checked += 1
            phone = key.replace(LAST_MSG_PREFIX, "")

            try:
                # 1. Verificar idempotencia
                followup_key = f"{FOLLOWUP_SENT_PREFIX}{phone}"
                if await r.exists(followup_key):
                    continue

                # 2. Obtener timestamp del último mensaje
                last_msg_str = await r.get(key)
                if not last_msg_str:
                    continue

                from middleware.conversation_state import parse_datetime_safe
                last_msg_time = parse_datetime_safe(last_msg_str)
                hours_since = (now - last_msg_time).total_seconds() / 3600

                # 3. Verificar si pasaron más de 24h
                if last_msg_time > threshold:
                    continue

                # 4. Verificar estado de conversación
                status = await followup_state_manager.get_status(phone)

                if status in [ConversationStatus.HUMAN_ACTIVE, ConversationStatus.IN_CONVERSATION]:
                    logger.info(
                        "[Scheduler] Verificando contacto %s: %.1f horas, Estado %s (omitido)",
                        phone, hours_since, status.value
                    )
                    continue

                # 5. Adquirir lock
                lock_key = f"{FOLLOWUP_LOCK_PREFIX}{phone}"
                if not await r.set(lock_key, "processing", nx=True, ex=300):
                    continue

                try:
                    logger.info(
                        "[Scheduler] Verificando contacto %s: %.1f horas, Estado %s → Enviando followup",
                        phone, hours_since, status.value
                    )

                    # 6. Obtener nombre del contacto
                    contact_name = "cliente"
                    meta = await followup_state_manager.get_meta(phone)
                    if meta and meta.display_name:
                        contact_name = meta.display_name.split()[0]

                    # 7. Enviar mensaje
                    followup_message = (
                        f"¡Hola {contact_name}! ¿Pudiste revisar la información que te enviamos? "
                        "Estamos aquí para resolver cualquier duda. 😊"
                    )

                    # Marcar antes de enviar (idempotencia)
                    await r.set(followup_key, "pending", ex=60)

                    if twilio_client.is_available:
                        result = await twilio_client.send_whatsapp_message(
                            to=phone,
                            body=followup_message
                        )

                        if result.get("status") == "success":
                            await r.set(followup_key, get_bogota_now_iso(), ex=7 * 24 * 60 * 60)
                            followups_sent += 1
                            logger.info("[FOLLOWUP] ✅ Seguimiento enviado a %s", phone)
                        else:
                            await r.delete(followup_key)
                            logger.warning("[FOLLOWUP] ❌ Error enviando a %s", phone)

                finally:
                    await r.delete(lock_key)

            except Exception as contact_err:
                logger.error("[FOLLOWUP] Error procesando %s: %s", phone, contact_err)

        logger.info("[FOLLOWUP] Completado. Revisados: %d, Enviados: %d", contacts_checked, followups_sent)

    except Exception as e:
        logger.error("[FOLLOWUP] Error general: %s", e, exc_info=True)
    finally:
        if r:
            await r.aclose()


async def check_appointment_followups():
    """
    Verifica citas que ocurrieron hace ~1h30min y envía mensaje de experiencia.
    Se ejecuta cada 15 minutos para capturar la ventana 1h15-1h45 post-cita.
    """
    if not APPOINTMENT_REMINDERS_ENABLED:
        return

    await apply_jitter()

    apt_manager = AppointmentManager(get_redis_url())
    state_manager = get_state_manager()
    now = get_bogota_now()

    logger.info("[Scheduler][Followup] ▶ Ciclo iniciado — Hora Bogotá: %s", now.strftime('%Y-%m-%d %H:%M:%S %Z'))

    total, sent, skipped = 0, 0, 0
    VALID_STATUSES = {ConversationStatus.BOT_ACTIVE, ConversationStatus.HUMAN_ACTIVE, ConversationStatus.IN_CONVERSATION}

    try:
        appointments = await apt_manager.get_appointments_needing_followup()
        logger.info("[Scheduler][Followup] Citas para seguimiento post-cita: %d", len(appointments))

        for apt in appointments:
            total += 1
            try:
                # Enviar si bot o asesor activo — no omitir si hay conversación con asesor
                status = await state_manager.get_status(apt.phone_normalized, apt.canal)
                if status not in VALID_STATUSES:
                    logger.info(
                        "[Scheduler][Followup] Skip %s — estado inactivo: %s",
                        apt.phone_normalized, status.value if status else "None"
                    )
                    skipped += 1
                    continue

                # Resolver nombre con fallback de 3 niveles
                contact_name = apt.contact_name
                _name_source = "appointment_redis"
                if not contact_name:
                    try:
                        _meta = await state_manager.get_meta(apt.phone_normalized, apt.canal)
                        if _meta and _meta.display_name:
                            contact_name = _meta.display_name.split()[0]
                            _name_source = "redis_meta"
                    except Exception:
                        pass
                if not contact_name:
                    contact_name = "cliente"
                    _name_source = "fallback"

                apt_dt = apt.scheduled_dt
                minutes_since = (now - apt_dt).total_seconds() / 60

                logger.info(
                    "[Scheduler][Followup] Seguimiento post-cita %s: %.0f min después (estado conv: %s, nombre='%s' source=%s)",
                    apt.phone_normalized, minutes_since, status.value if status else "N/A",
                    contact_name, _name_source
                )

                message = (
                    f"¡Hola {contact_name}! 😊 Esperamos que la visita haya sido de tu agrado. "
                    f"Cuéntanos, ¿Te ha gustado el inmueble?\n"
                    f"Para nosotros es importante conocer tu experiencia y seguir "
                    f"mejorando la calidad de nuestro servicio. 📈\n"
                    f"¿Nos podrías regalar tu opinión? https://forms.gle/W3bQbDVFkR4ybVbW6\n"
                    f"Tus respuestas tomarán solo un minuto, serán anónimas y se usarán "
                    f"para optimizar nuestros procesos de atención. 💪\n\n"
                    f"¡Gracias por confiar en Inmobiliaria Proteger! 💛"
                )

                if twilio_client.is_available:
                    # Template seguimiento_cita (aprobado Meta). Env var mantiene precedencia
                    # por compatibilidad, pero si está ausente usamos el SID oficial.
                    _followup_sid = os.getenv(
                        "TWILIO_FOLLOWUP_TEMPLATE_SID",
                        "HX9696fc73c9dbb5f76382bd77b5f410c8",
                    )
                    if _followup_sid:
                        result = await twilio_client.send_whatsapp_message(
                            to=apt.phone_normalized,
                            body=message,
                            content_sid=_followup_sid,
                            content_variables={"1": contact_name}
                        )
                    else:
                        result = await twilio_client.send_whatsapp_message(
                            to=apt.phone_normalized,
                            body=message
                        )

                    if result.get("status") == "success":
                        await apt_manager.mark_followup_sent(apt.phone_normalized, apt.canal)

                        # Transición automática de ciclo de vida (fire-and-forget)
                        # Replica el patrón de _update_contact_to_en_conversacion
                        if apt.contact_id:
                            try:
                                from middleware.outbound_panel import (
                                    _update_contact_to_visita_realizada,
                                )
                                asyncio.create_task(
                                    _update_contact_to_visita_realizada(apt.contact_id)
                                )
                                asyncio.create_task(
                                    get_mongo_manager().mark_visit_completed(
                                        apt.contact_id, apt.phone_normalized
                                    )
                                )
                            except Exception as lc_err:
                                logger.error(
                                    "[Scheduler][Followup] Error disparando transición lifecycle: %s",
                                    lc_err,
                                )

                        if apt.contact_id:
                            try:
                                timeline = get_timeline_logger()
                                await timeline.log_bot_message(
                                    contact_id=apt.contact_id,
                                    content="Mensaje de experiencia post-visita enviado automáticamente.",
                                    session_id=apt.phone_normalized
                                )
                            except Exception as hs_err:
                                logger.error("[Scheduler][Followup] Error HubSpot followup: %s", hs_err)

                        sent += 1
                        logger.info("[Scheduler][Followup] ✅ Seguimiento post-cita enviado a %s", apt.phone_normalized)
                        # Guardar en MongoDB para que aparezca en el panel
                        try:
                            canal_apt = apt.canal or "whatsapp"
                            mongo_mgr = get_mongo_manager()
                            await mongo_mgr.save_message(
                                phone=apt.phone_normalized,
                                content=message,
                                sender="bot",
                                channel=canal_apt,
                                hubspot_contact_id=apt.contact_id,
                                message_sid=result.get("message_sid"),
                                metadata={"source": "Seguimiento post-cita automático", "template_id": "seguimiento_cita"}
                            )
                            await state_manager.update_activity(apt.phone_normalized, canal=canal_apt)
                            await ws_manager.publish_broadcast(state_manager.redis, {
                                "type": "contact_updated",
                                "action": "new_message",
                                "phone": apt.phone_normalized,
                                "canal": canal_apt,
                                "sender": "bot",
                                "preview": message[:100],
                                "contact_name": contact_name
                            })
                        except Exception as _panel_err:
                            logger.warning(
                                "[Scheduler][Followup] No se pudo guardar en panel: %s", _panel_err
                            )
                    else:
                        logger.warning("[Scheduler][Followup] ❌ Error enviando followup a %s", apt.phone_normalized)
                else:
                    logger.warning("[Scheduler][Followup] Twilio no disponible para followup post-cita")

            except Exception as apt_err:
                logger.error("[Scheduler][Followup] Error en followup post-cita %s: %s", apt.phone_normalized, apt_err)

        logger.info(
            "[Scheduler][Followup] ✓ Completado — evaluadas: %d, enviadas: %d, omitidas: %d",
            total, sent, skipped
        )

    except Exception as e:
        logger.error("[Scheduler][Followup] Error en check_appointment_followups: %s", e, exc_info=True)
    finally:
        await apt_manager.close()


# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS BÁSICOS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/")
def root():
    return {
        "service": "Sofía - Asistente Virtual Inmobiliaria",
        "version": "2.0.0",
        "status": "operational",
        "timezone": str(TIMEZONE_BOGOTA),
        "endpoints": {
            "webhook": "POST /whatsapp/webhook",
            "health": "GET /health",
            "docs": "GET /docs"
        }
    }


@app.get("/health")
async def health_check():
    """Health check rápido."""
    status = {
        "status": "healthy",
        "timestamp": get_bogota_now_iso(),
        "redis": "unchecked",
        "scheduler": "running" if scheduler.running else "stopped"
    }

    try:
        # Reusar singleton — antes creaba redis.from_url() por cada /health call
        # sin cerrar el pool, generando ~120 pools huérfanos/hora con Railway health checks
        sm = get_state_manager()
        await sm.redis.ping()
        status["redis"] = "connected"
    except Exception:
        status["redis"] = "error"

    return status


@app.on_event("startup")
async def startup_event():
    """Inicializa RAG y schedulers."""
    import os
    pid = os.getpid()
    logger.info("=" * 60)
    logger.info("[STARTUP] Iniciando servidor Sofía v2.0... (Worker PID: %s)", pid)

    # Cargar Knowledge Base — un solo worker indexa (lock Redis para evitar stampede)
    # Con 4 workers Gunicorn, sin lock todos ejecutan reload_knowledge_base() simultáneamente:
    # 4× DELETE + 4× INSERT + 4× embedding API calls → estado no determinístico en pgvector.
    # Redis SET NX (only if Not eXists) garantiza que solo 1 worker indexa; los demás
    # solo inicializan la conexión SQLAlchemy sin tocar los datos.
    try:
        from rag.rag_service import rag_service
        from rag.vector_store import pg_vector_store
        import redis.asyncio as _redis_rag

        _rag_redis = _redis_rag.from_url(
            get_redis_url(), encoding="utf-8", decode_responses=True,
            socket_timeout=2.0, socket_connect_timeout=2.0
        )
        # Atómico: solo el primer worker en ganar el lock indexa la KB.
        # TTL 120s cubre sobrecarga de embeddings; se limpia solo al expirar.
        _rag_lock = await _rag_redis.set("rag_kb_lock", str(pid), nx=True, ex=120)
        await _rag_redis.aclose()

        if _rag_lock:
            # Este worker ganó el lock → ejecuta DELETE + embeddings + INSERT
            result = rag_service.reload_knowledge_base()
            if result["status"] == "error":
                raise RuntimeError(f"Fallo en carga KB: {result.get('message')}")
            logger.info("[STARTUP] ✅ KB Lista. Chunks: %s (líder PID: %s)", result.get('chunks_indexed'), pid)
        else:
            # Otro worker ya está indexando → solo inicializar la conexión DB (sin re-indexar)
            pg_vector_store.initialize_db()
            logger.info("[STARTUP] ⏭  KB indexada por otro worker. Conexión DB lista (PID: %s)", pid)

    except Exception as e:
        logger.error("[STARTUP] ⚠️ Error cargando KB: %s", e)

    # Scheduler: solo UN worker por instancia Railway ejecuta los jobs.
    # Sin lock, N workers corren cada job independientemente:
    #   - check_appointment_reminders efectivo cada 15 min → Rate Limit HubSpot + duplicados
    #   - check_appointment_followups efectivo cada 7.5 min → mensajes duplicados post-cita
    # Redis SET NX garantiza atomicamente que solo 1 worker adquiere el liderazgo.
    _is_scheduler_leader = False
    try:
        import redis.asyncio as _redis_sched
        _sched_redis = _redis_sched.from_url(
            get_redis_url(), encoding="utf-8", decode_responses=True,
            socket_timeout=2.0, socket_connect_timeout=2.0
        )
        # TTL 300s: si el worker lider muere o se hace redeploy, otro toma el liderazgo
        # en maximos 5min — sin necesidad de DEL manual antes de cada deploy
        _sched_lock = await _sched_redis.set(
            "scheduler_leader", str(pid), nx=True, ex=300
        )
        await _sched_redis.aclose()
        _is_scheduler_leader = bool(_sched_lock)
        if _is_scheduler_leader:
            logger.info("[STARTUP] Worker %s es el scheduler leader", pid)
        else:
            logger.info("[STARTUP] Worker %s omite scheduler (otro worker es leader)", pid)
    except Exception as _sched_err:
        # Fallback: si Redis no disponible al startup, arrancar scheduler de todas formas
        _is_scheduler_leader = True
        logger.warning("[STARTUP] Lock scheduler Redis fallo (%s) — scheduler sin coordinacion", _sched_err)

    if _is_scheduler_leader:
        if APPOINTMENT_REMINDERS_ENABLED:
            scheduler.add_job(
                check_appointment_reminders,
                trigger=IntervalTrigger(minutes=30),
                id="apt_reminders",
                replace_existing=True
            )
            logger.info("[STARTUP] Scheduler de recordatorios de citas HABILITADO (cada 30 min)")

        if FOLLOWUP_ENABLED:
            scheduler.add_job(
                check_and_send_followups,
                trigger=IntervalTrigger(hours=1),
                id="followup_24h",
                replace_existing=True
            )
            logger.info("[STARTUP] Scheduler de seguimiento 24h HABILITADO")

        if APPOINTMENT_REMINDERS_ENABLED:
            scheduler.add_job(
                check_appointment_followups,
                trigger=IntervalTrigger(minutes=15),
                id="apt_followups",
                replace_existing=True
            )
            logger.info("[STARTUP] Scheduler post-cita HABILITADO (cada 15 min, ventana 1h30min)")

        scheduler.add_job(
            check_conversation_timeouts,
            trigger=IntervalTrigger(hours=2),
            id="conv_timeouts",
            replace_existing=True
        )
        logger.info("[STARTUP] Scheduler de timeouts HABILITADO (cada 2 horas)")

        # ── Memory Watchdog: fuerza reciclaje del worker si RSS > umbral ──
        # Python no devuelve arenas de memoria al OS (fragmentación).
        # --max-requests de gunicorn no recicla si hay WebSockets abiertos.
        # Este watchdog es la red de seguridad definitiva.
        _MEM_LIMIT_MB = int(os.getenv("WORKER_MEM_LIMIT_MB", "4096"))  # 4 GB default

        async def _memory_watchdog():
            try:
                import resource
                rss_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024  # Linux: KB → bytes
                rss_mb = rss_bytes / (1024 * 1024)
            except ImportError:
                # Fallback: leer /proc/self/status (Linux Docker)
                try:
                    with open("/proc/self/status") as f:
                        for line in f:
                            if line.startswith("VmRSS:"):
                                rss_mb = int(line.split()[1]) / 1024  # KB → MB
                                break
                        else:
                            return
                except Exception:
                    return

            if rss_mb > _MEM_LIMIT_MB:
                logger.warning(
                    "[MEMORY WATCHDOG] RSS=%.0f MB excede límite de %d MB — "
                    "forzando reciclaje del worker",
                    rss_mb, _MEM_LIMIT_MB
                )
                import signal
                os.kill(os.getpid(), signal.SIGTERM)
            else:
                logger.info("[MEMORY WATCHDOG] RSS=%.0f MB (límite: %d MB)", rss_mb, _MEM_LIMIT_MB)

        scheduler.add_job(
            _memory_watchdog,
            trigger=IntervalTrigger(minutes=2),
            id="memory_watchdog",
            replace_existing=True
        )
        logger.info(
            "[STARTUP] Memory watchdog HABILITADO (cada 2 min, límite %d MB)",
            _MEM_LIMIT_MB
        )

        # ── Mensajes programados: envío de plantillas WhatsApp en fecha específica ──
        async def check_scheduled_messages():
            """
            Detecta mensajes de plantilla programados que ya vencieron y los envía vía Twilio.
            Corre cada 1 minuto. Usa Redis SET NX como lock de idempotencia (TTL 24h)
            para garantizar un único envío aunque el job se solape.
            """
            from datetime import datetime as _dt, timezone as _utc_tz
            import redis.asyncio as _redis_async

            now_utc = _dt.now(_utc_tz.utc)
            mongo_mgr = get_mongo_manager()

            pending = await mongo_mgr.get_pending_messages_due(now_utc)
            if not pending:
                return

            logger.info("[SchedMsg] %d mensaje(s) pendiente(s) para enviar", len(pending))

            r = None
            try:
                r = _redis_async.from_url(
                    get_redis_url(), encoding="utf-8", decode_responses=True
                )
                for msg in pending:
                    msg_id = msg["_id"]
                    lock_key = f"sched_msg_lock:{msg_id}"

                    acquired = await r.set(lock_key, "1", nx=True, ex=86400)
                    if not acquired:
                        logger.debug("[SchedMsg] Lock ya existe para %s — skip", msg_id)
                        continue

                    try:
                        if not twilio_client.is_available:
                            logger.warning("[SchedMsg] Twilio no disponible, skip %s", msg_id)
                            continue

                        await twilio_client.send_whatsapp_message(
                            to=msg["phone"],
                            body="",
                            content_sid=msg["template_sid"],
                            content_variables=msg["template_variables"],
                        )
                        await mongo_mgr.mark_scheduled_message_sent(msg_id)
                        logger.info(
                            "[SchedMsg] Enviado: id=%s template=%s phone=%s",
                            msg_id, msg["template_name"], msg["phone"]
                        )
                    except Exception as send_err:
                        await mongo_mgr.mark_scheduled_message_failed(msg_id, str(send_err))
                        logger.error("[SchedMsg] Error enviando %s: %s", msg_id, send_err)

                    await asyncio.sleep(0.1)

            except Exception as e:
                logger.error("[SchedMsg] Error en ciclo: %s", e, exc_info=True)
            finally:
                if r:
                    await r.aclose()

        scheduler.add_job(
            check_scheduled_messages,
            trigger=IntervalTrigger(minutes=1),
            id="scheduled_messages",
            replace_existing=True
        )
        logger.info("[STARTUP] Scheduler de mensajes programados HABILITADO (cada 1 min)")

        # Bulk campaigns processor (cada 15s) — procesa lotes pequeños de mensajes masivos
        from middleware.outbound_panel import _process_bulk_campaign_tick_safe
        scheduler.add_job(
            _process_bulk_campaign_tick_safe,
            trigger=IntervalTrigger(seconds=15),
            id="bulk_campaign_processor",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
            replace_existing=True,
        )
        logger.info("[STARTUP] Bulk campaign processor HABILITADO (cada 15s)")

        scheduler.start()
        logger.info("[STARTUP] Schedulers iniciados (Timezone: %s, PID lider: %s)", TIMEZONE_BOGOTA, pid)

    # Iniciar Redis Pub/Sub listener para broadcast WebSocket cross-worker
    try:
        import redis.asyncio as _redis_async
        from middleware.websocket_manager import ws_manager as _ws_manager

        async def _make_redis_for_pubsub():
            return _redis_async.from_url(
                get_redis_url(),
                encoding="utf-8",
                decode_responses=True
            )

        await _ws_manager.start_redis_listener(_make_redis_for_pubsub)
        logger.info("[STARTUP] ✅ WebSocket Redis Pub/Sub listener iniciado (PID: %s)", pid)
    except Exception as _ws_err:
        logger.error("[STARTUP] ⚠️ Error iniciando WS Redis listener: %s", _ws_err)

    # Migrar SET legacy Redis → ZSET y eliminar la clave obsoleta (operación única)
    try:
        _csm_legacy = ConversationStateManager(get_redis_url())
        await _csm_legacy.cleanup_legacy_set()
        await _csm_legacy.close()
    except Exception as e:
        logger.warning("[STARTUP] cleanup_legacy_set: %s (no crítico)", e)

    logger.info("[STARTUP] Servidor listo para aceptar tráfico HTTP")


@app.on_event("shutdown")
async def shutdown_event():
    """Limpieza al cerrar el servidor - Cierre Grácil de conexiones."""
    logger.info("[SHUTDOWN] Iniciando proceso de cierre grácil...")

    # 1. Detener schedulers
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("[SHUTDOWN] Schedulers detenidos")

    # 2. Cerrar el singleton de ConversationStateManager
    try:
        if _global_state_manager is not None:
            await _global_state_manager.close()
        logger.info("[SHUTDOWN] Conexión a Redis (singleton CSM) cerrada")
    except Exception as e:
        logger.warning(f"[SHUTDOWN] Error cerrando Redis singleton: {e}")

    # 3. Cerrar conexión de MongoDB
    try:
        mongo_manager = get_mongo_manager()
        if hasattr(mongo_manager, 'client') and mongo_manager.client:
            mongo_manager.client.close()
            logger.info("[SHUTDOWN] Conexión a MongoDB cerrada")
    except Exception as e:
        logger.warning(f"[SHUTDOWN] Error cerrando MongoDB: {e}")

    logger.info("[SHUTDOWN] Proceso de cierre completado")


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8001"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)