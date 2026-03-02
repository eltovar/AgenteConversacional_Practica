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
from datetime import timedelta
# Scheduler para seguimiento automático
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Importar componentes del middleware (Sesión 1)
from middleware import get_whatsapp_router, get_outbound_panel_router, get_contact_manager
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

# Importar función para actualizar ventana de 24h
from middleware.outbound_panel import update_last_client_message



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


# ═══════════════════════════════════════════════════════════════════════════════
# APLICACIÓN FASTAPI
# ═══════════════════════════════════════════════════════════════════════════════

app = FastAPI(title="Sofía IA - Middleware", version="2.0.0")

# Montar archivos estáticos para el panel
PANEL_STATIC_PATH = Path(__file__).parent / "middleware" / "PanelAsesores"
if PANEL_STATIC_PATH.exists():
    app.mount("/whatsapp/panel/static", StaticFiles(directory=str(PANEL_STATIC_PATH)), name="panel_static")

# Incluir routers
app.include_router(get_whatsapp_router())
app.include_router(get_outbound_panel_router())
app.include_router(get_outbound_router())

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
        redis_url = get_redis_url()
        state_manager = ConversationStateManager(redis_url)

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
                    ContactManager = get_contact_manager()
                    contact_manager = ContactManager()
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
            ContactManager = get_contact_manager()
            contact_manager = ContactManager()
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
    Verifica citas próximas y envía recordatorios 1 hora antes.
    Implementa idempotencia verificando 'reminder_sent' en Redis.

    COMPATIBLE con AppointmentManager de Sesión 1.
    """
    if not APPOINTMENT_REMINDERS_ENABLED:
        return

    await apply_jitter()

    redis_url = get_redis_url()
    apt_manager = AppointmentManager(redis_url)
    state_manager = ConversationStateManager(redis_url)
    now = get_bogota_now()

    logger.info("[Scheduler] Ejecutando verificación de recordatorios de citas...")
    logger.info("[Scheduler] Hora actual (Bogotá): %s", now.strftime('%Y-%m-%d %H:%M:%S %Z'))

    try:
        # Obtener citas pendientes usando método correcto
        upcoming_appointments = await apt_manager.get_appointments_needing_reminder()

        logger.info("[Scheduler] Citas encontradas para recordatorio: %d", len(upcoming_appointments))

        for apt in upcoming_appointments:
            scheduled_dt = apt.scheduled_dt  # Property que ya convierte a Bogotá
            diff = scheduled_dt - now
            diff_hours = diff.total_seconds() / 3600

            logger.info(
                "[Scheduler] Verificando cita %s: Diferencia %.2f horas, Estado: %s",
                apt.phone_normalized, diff_hours, apt.status.value
            )

            # Verificar si el bot está activo para este cliente
            status = await state_manager.get_status(apt.phone_normalized, apt.canal)

            if status == ConversationStatus.BOT_ACTIVE:
                logger.info("[Scheduler] Enviando recordatorio a %s", apt.phone_normalized)

                # Construir mensaje de recordatorio
                contact_name = apt.contact_name or "cliente"
                message = (
                    f"¡Hola {contact_name}! 👋 Te recuerdo tu cita programada para hoy "
                    f"a las {scheduled_dt.strftime('%H:%M')}. ¡Te esperamos!"
                )

                # Enviar vía Twilio
                if twilio_client.is_available:
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
                                logger.error("[Scheduler] Error registrando en HubSpot: %s", hs_err)

                        logger.info("[Scheduler] ✅ Recordatorio enviado a %s", apt.phone_normalized)
                    else:
                        logger.warning(
                            "[Scheduler] ❌ Error enviando recordatorio a %s: %s",
                            apt.phone_normalized, result.get("message")
                        )
                else:
                    logger.warning("[Scheduler] Twilio no disponible para recordatorios")
            else:
                logger.warning(
                    "[Scheduler] Recordatorio omitido para %s: Estado %s (humano activo).",
                    apt.phone_normalized, status.value
                )

        await apt_manager.close()

    except Exception as e:
        logger.error("[Scheduler] Error en check_appointment_reminders: %s", e, exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════════
# LÓGICA DE TIMEOUTS DE CONVERSACIÓN
# ═══════════════════════════════════════════════════════════════════════════════

async def check_conversation_timeouts():
    """
    Detecta conversaciones en HUMAN_ACTIVE inactivas por > 24h
    y las devuelve a BOT_ACTIVE.

    COMPATIBLE con ConversationStateManager de Sesión 1.
    """
    await apply_jitter()

    redis_url = get_redis_url()
    state_manager = ConversationStateManager(redis_url)
    now = get_bogota_now()

    logger.info("[Scheduler] Verificando timeouts de conversaciones...")
    logger.info("[Scheduler] Hora actual (Bogotá): %s", now.strftime('%Y-%m-%d %H:%M:%S %Z'))

    try:
        # Usar método correcto de Sesión 1
        active_contacts = await state_manager.get_all_human_active_contacts()

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

            # Si han pasado más de 24 horas en estado humano sin actividad
            if diff_hours > 24 and status in ["HUMAN_ACTIVE", "IN_CONVERSATION"]:
                logger.info(
                    "[Scheduler] Timeout detectado para %s:%s (%.1f horas inactivo). "
                    "Reseteando a BOT_ACTIVE.",
                    phone, canal or "default", diff_hours
                )

                # Resetear estado usando método correcto
                await state_manager.activate_bot(phone, canal)
                timeouts_processed += 1

        await state_manager.close()

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

    try:
        import redis.asyncio as redis_async

        r = redis_async.from_url(redis_url, encoding="utf-8", decode_responses=True)

        LAST_MSG_PREFIX = "last_client_msg:"
        FOLLOWUP_SENT_PREFIX = "followup_sent:"
        FOLLOWUP_LOCK_PREFIX = "followup_lock:"

        threshold = now - timedelta(hours=FOLLOWUP_DELAY_HOURS)

        followups_sent = 0
        contacts_checked = 0

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
                state_manager = ConversationStateManager(redis_url)
                status = await state_manager.get_status(phone)

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
                    meta = await state_manager.get_meta(phone)
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

        await r.close()
        logger.info("[FOLLOWUP] Completado. Revisados: %d, Enviados: %d", contacts_checked, followups_sent)

    except Exception as e:
        logger.error("[FOLLOWUP] Error general: %s", e, exc_info=True)


async def check_appointment_followups():
    """
    Verifica citas que ocurrieron hace ~1h30min y envía mensaje de experiencia.
    Se ejecuta cada 15 minutos para capturar la ventana 1h15-1h45 post-cita.
    """
    if not APPOINTMENT_REMINDERS_ENABLED:
        return

    await apply_jitter()

    redis_url = get_redis_url()
    apt_manager = AppointmentManager(redis_url)
    state_manager = ConversationStateManager(redis_url)
    now = get_bogota_now()

    logger.info("[Scheduler] Ejecutando verificación de seguimientos post-cita (ventana 1h30min)...")

    try:
        appointments = await apt_manager.get_appointments_needing_followup()
        logger.info("[Scheduler] Citas para seguimiento post-cita: %d", len(appointments))

        for apt in appointments:
            try:
                contact_name = apt.contact_name or "cliente"
                apt_dt = apt.scheduled_dt
                minutes_since = (now - apt_dt).total_seconds() / 60

                logger.info(
                    "[Scheduler] Seguimiento post-cita %s: %.0f min después de la cita",
                    apt.phone_normalized, minutes_since
                )

                message = (
                    f"¡Hola {contact_name}! 😊 Esperamos que tu visita haya sido de tu agrado. "
                    f"¿Tienes alguna pregunta sobre el inmueble que visitaste? "
                    f"Estamos aquí para ayudarte."
                )

                if twilio_client.is_available:
                    result = await twilio_client.send_whatsapp_message(
                        to=apt.phone_normalized,
                        body=message
                    )

                    if result.get("status") == "success":
                        await apt_manager.mark_followup_sent(apt.phone_normalized, apt.canal)

                        if apt.contact_id:
                            try:
                                timeline = get_timeline_logger()
                                await timeline.log_bot_message(
                                    contact_id=apt.contact_id,
                                    content="Mensaje de experiencia post-visita enviado automáticamente.",
                                    session_id=apt.phone_normalized
                                )
                            except Exception as hs_err:
                                logger.error("[Scheduler] Error HubSpot followup: %s", hs_err)

                        logger.info("[Scheduler] ✅ Seguimiento post-cita enviado a %s", apt.phone_normalized)
                    else:
                        logger.warning("[Scheduler] ❌ Error enviando followup a %s", apt.phone_normalized)
                else:
                    logger.warning("[Scheduler] Twilio no disponible para followup post-cita")

            except Exception as apt_err:
                logger.error("[Scheduler] Error en followup post-cita %s: %s", apt.phone_normalized, apt_err)

        await apt_manager.close()

    except Exception as e:
        logger.error("[Scheduler] Error en check_appointment_followups: %s", e, exc_info=True)


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
        import redis
        redis_url = get_redis_url()
        redis.from_url(redis_url, socket_connect_timeout=1).ping()
        status["redis"] = "connected"
    except Exception:
        status["redis"] = "error"

    return status


@app.on_event("startup")
async def startup_event():
    """Inicializa RAG y schedulers."""
    logger.info("=" * 60)
    logger.info("[STARTUP] Iniciando servidor Sofía v2.0...")

    # Cargar Knowledge Base
    try:
        from rag.rag_service import rag_service
        result = rag_service.reload_knowledge_base()
        if result["status"] == "error":
            raise RuntimeError(f"Fallo en carga KB: {result.get('message')}")
        logger.info("[STARTUP] ✅ KB Lista. Chunks indexados: %s", result.get('chunks_indexed'))
    except Exception as e:
        logger.error("[STARTUP] ⚠️ Error cargando KB: %s", e)

    # Configurar jobs del scheduler
    if APPOINTMENT_REMINDERS_ENABLED:
        scheduler.add_job(
            check_appointment_reminders,
            trigger=IntervalTrigger(minutes=30),
            id="apt_reminders",
            replace_existing=True
        )
        logger.info("[STARTUP] ✅ Scheduler de recordatorios de citas HABILITADO (cada 30 min)")

    if FOLLOWUP_ENABLED:
        scheduler.add_job(
            check_and_send_followups,
            trigger=IntervalTrigger(hours=1),
            id="followup_24h",
            replace_existing=True
        )
        logger.info("[STARTUP] ✅ Scheduler de seguimiento 24h HABILITADO")

    if APPOINTMENT_REMINDERS_ENABLED:
        scheduler.add_job(
            check_appointment_followups,
            trigger=IntervalTrigger(minutes=15),
            id="apt_followups",
            replace_existing=True
        )
        logger.info("[STARTUP] ✅ Scheduler de seguimiento post-cita HABILITADO (cada 15 min, ventana 1h30min)")

    scheduler.add_job(
        check_conversation_timeouts,
        trigger=IntervalTrigger(hours=2),
        id="conv_timeouts",
        replace_existing=True
    )
    logger.info("[STARTUP] ✅ Scheduler de timeouts HABILITADO (cada 2 horas)")

    scheduler.start()
    logger.info("[STARTUP] Schedulers iniciados (Timezone: %s)", TIMEZONE_BOGOTA)
    logger.info("[STARTUP] Servidor listo para aceptar tráfico HTTP")


@app.on_event("shutdown")
async def shutdown_event():
    """Limpieza al cerrar el servidor - Cierre Grácil de conexiones."""
    logger.info("[SHUTDOWN] Iniciando proceso de cierre grácil...")

    # 1. Detener schedulers
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("[SHUTDOWN] Schedulers detenidos")

    # 2. Cerrar conexión de Redis (State Manager)
    try:
        redis_url = get_redis_url()
        state_manager = ConversationStateManager(redis_url)
        await state_manager.close()
        logger.info("[SHUTDOWN] Conexión a Redis cerrada")
    except Exception as e:
        logger.warning(f"[SHUTDOWN] Error cerrando Redis: {e}")

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