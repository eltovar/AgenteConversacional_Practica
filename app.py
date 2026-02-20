# app.py
"""
Servidor FastAPI asíncrono para chatbot Sofía.
Maneja webhooks de Twilio (WhatsApp) y requests JSON.
Incluye sistema de agregación de mensajes para manejar múltiples mensajes seguidos.
"""

from fastapi import FastAPI, HTTPException, Response, Form, Request, Header, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
from agents.orchestrator import process_message
from agents.InfoAgent.info_agent import agent
from utils.message_aggregator import message_aggregator, AGGREGATION_TIMEOUT
from utils.twilio_client import twilio_client
from logging_config import logger
import uvicorn
import os
import json
import asyncio
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# Scheduler para seguimiento automático
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Importar el router del middleware inteligente (lazy import)
from middleware import get_whatsapp_router, get_outbound_panel_router, get_contact_manager
from middleware.conversation_state import ConversationStateManager

# Importar el router de webhooks de salida HubSpot -> WhatsApp
from integrations.hubspot import get_outbound_router, get_timeline_logger

# Importar función para actualizar ventana de 24h
from middleware.outbound_panel import update_last_client_message

# ===== 1. CONFIGURACIÓN INICIAL Y VALIDACIÓN =====
load_dotenv()

# ✅ CAMBIO: Agregamos las variables de HubSpot a la lista de requeridos
REQUIRED = [
    "OPENAI_API_KEY",
    "HUBSPOT_API_KEY",
    "HUBSPOT_PIPELINE_ID",
    "HUBSPOT_DEAL_STAGE"
]

if missing := [k for k in REQUIRED if not os.getenv(k)]:
    # Esto detendrá el servidor inmediatamente si falta alguna
    raise EnvironmentError(f"❌ CRITICAL: Missing secrets: {', '.join(missing)}")

app = FastAPI(title="Sofía - Asistente Virtual", version="1.0.0")

# ===== MIDDLEWARE INTELIGENTE (Fase 2) =====
# Router para el nuevo sistema de WhatsApp con estados BOT_ACTIVE/HUMAN_ACTIVE
# Endpoints: /whatsapp/webhook, /whatsapp/admin/*
app.include_router(get_whatsapp_router())

# ===== HUBSPOT OUTBOUND (Fase 2.5) =====
# Router para webhooks de salida: HubSpot Inbox -> WhatsApp
# Endpoints: /hubspot/outbound, /hubspot/thread-mapping
app.include_router(get_outbound_router())

# ===== PANEL DE ENVÍO PARA ASESORES (Fase 3.2) =====
# UI y API para que asesores envíen mensajes directamente por WhatsApp
# Endpoints: /whatsapp/panel/, /whatsapp/panel/send-message, etc.
app.include_router(get_outbound_panel_router())

# ===== ARCHIVOS ESTÁTICOS DEL PANEL =====
# Servir CSS, JS y otros archivos estáticos del Panel de Asesores
PANEL_STATIC_PATH = Path(__file__).parent / "middleware" / "PanelAsesores"
app.mount("/whatsapp/panel/static", StaticFiles(directory=str(PANEL_STATIC_PATH)), name="panel_static")

# ===== 2. STARTUP EVENT (CRÍTICO PARA RAG) =====
@app.get("/")
def root():
    return {
        "service": "Sofía - Asistente Virtual Inmobiliaria",
        "version": "1.0.0",
        "status": "operational",
        "endpoints": {
            "webhook": "POST /webhook",
            "health": "GET /health",
            "docs": "GET /docs"
        }
    }

@app.on_event("startup")
async def startup_event():
    """Inicializa la Base de Conocimiento RAG para evitar timeouts."""
    logger.info("=" * 60)
    logger.info("[STARTUP] Iniciando carga de Base de Conocimiento RAG...")
    try:
        from rag.rag_service import rag_service
        # Ejecutar carga (puede tardar unos segundos)
        result = rag_service.reload_knowledge_base()

        if result["status"] == "error":
            raise RuntimeError(f"Fallo en carga KB: {result.get('message')}")

        logger.info(f"[STARTUP] ✅ KB Lista. Chunks indexados: {result.get('chunks_indexed')}")
        logger.info("[STARTUP] Servidor listo para aceptar tráfico HTTP")
    except Exception as e:
        logger.error(f"[STARTUP] ❌ Fallo crítico: {e}")
        raise

# ===== 3. MODELOS PYDANTIC =====
class MessageRequest(BaseModel):
    session_id: str
    message: str

class MessageResponse(BaseModel):
    response: str
    status: str


# ===== 4. FUNCIÓN DE PROCESAMIENTO EN BACKGROUND =====
async def process_aggregated_messages(session_id: str, to_number: str):
    """
    Esta función resuelve el problema del timeout de 15 segundos de Twilio.
    """
    try:
        # 1. Esperar y obtener mensajes combinados
        combined_message = await message_aggregator.wait_and_get_combined_message(session_id)

        if not combined_message:
            logger.warning(f"[BACKGROUND] No hay mensajes para procesar (session: {session_id})")
            return

        logger.info(f"[BACKGROUND] Procesando mensajes agregados: '{combined_message[:80]}...'")

        # 2. NORMALIZAR TELÉFONO
        phone_normalized = session_id.replace("whatsapp:", "").replace("+", "")
        if phone_normalized.startswith("57") and len(phone_normalized) == 12:
            phone_normalized = f"+{phone_normalized}"
        elif not phone_normalized.startswith("+"):
            phone_normalized = f"+57{phone_normalized}" if len(phone_normalized) == 10 else f"+{phone_normalized}"

        # 2.1 ACTUALIZAR VENTANA DE 24H (para que el Panel no muestre "ventana cerrada")
        try:
            await update_last_client_message(phone_normalized)
            logger.info(f"[BACKGROUND] ✅ Ventana 24h actualizada para {phone_normalized}")
        except Exception as window_err:
            logger.error(f"[BACKGROUND] Error actualizando ventana 24h: {window_err}")

        # 2.2 VERIFICAR SI BOT DEBE RESPONDER (HUMAN_ACTIVE = silenciar)
        redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL"))
        if redis_url:
            try:
                state_manager = ConversationStateManager(redis_url)
                is_bot_active = await state_manager.is_bot_active(phone_normalized)

                if not is_bot_active:
                    logger.info(f"[BACKGROUND] 👤 HUMAN_ACTIVE detectado para {phone_normalized}. Bot silenciado.")
                    # Aún así registramos el mensaje en HubSpot para el historial
                    try:
                        ContactManager = get_contact_manager()
                        contact_manager = ContactManager()
                        contact_info = await contact_manager.identify_or_create_contact(
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
                            logger.info(f"[BACKGROUND] 📱 Mensaje registrado en HubSpot (bot silenciado)")
                    except Exception as hs_err:
                        logger.error(f"[BACKGROUND] Error registrando en HubSpot (silenciado): {hs_err}")
                    return  # NO procesar con IA
            except Exception as state_err:
                logger.error(f"[BACKGROUND] Error verificando estado: {state_err}")
                # Continuar procesando en caso de error de Redis

        # 3. REGISTRAR MENSAJE DEL CLIENTE EN HUBSPOT (antes de procesar con IA)
        # Esto asegura que el asesor vea el mensaje en el Panel inmediatamente
        try:
            ContactManager = get_contact_manager()
            contact_manager = ContactManager()
            contact_info = await contact_manager.identify_or_create_contact(
                phone_raw=phone_normalized,
                source_channel="whatsapp_directo"
            )

            if contact_info and contact_info.contact_id:
                logger.info(f"[BACKGROUND] 📱 Registrando mensaje del cliente en HubSpot (contact_id={contact_info.contact_id})")
                timeline_logger = get_timeline_logger()
                await timeline_logger.log_client_message(
                    contact_id=contact_info.contact_id,
                    content=combined_message,
                    session_id=phone_normalized
                )
            else:
                logger.warning(f"[BACKGROUND] ⚠️ No se pudo identificar contacto para {phone_normalized}")
        except Exception as hubspot_err:
            logger.error(f"[BACKGROUND] Error registrando en HubSpot: {hubspot_err}")
            # Continuar con el procesamiento aunque falle HubSpot

        # 4. Procesar con el orchestrator
        result = await process_message(session_id, combined_message)

        if not result or not result.get("response"):
            logger.warning(f"[BACKGROUND] Orchestrator no generó respuesta para {session_id}")
            return

        # 4. Enviar respuesta via Twilio API
        if twilio_client.is_available:
            send_result = await twilio_client.send_whatsapp_message(
                to=to_number,
                body=result["response"]
            )
            if send_result["status"] == "success":
                logger.info(f"[BACKGROUND] Respuesta enviada exitosamente a {to_number}")
            else:
                logger.error(f"[BACKGROUND] Error enviando respuesta: {send_result}")
        else:
            logger.error(
                "[BACKGROUND] Twilio client no disponible. "
                "Configura TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER"
            )

    except Exception as e:
        logger.error(f"[BACKGROUND] Error en procesamiento: {e}", exc_info=True)


# ===== 5. ENDPOINT UNIFICADO (WEBHOOK) =====
@app.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    From: str = Form(None),
    Body: str = Form(None)
):
    """
    Maneja mensajes de Twilio (Form Data) y JSON estándar.
    Incluye sistema de AGREGACIÓN para manejar múltiples mensajes seguidos.
    """
    try:
        # A. Detectar origen (Twilio vs JSON)
        content_type = request.headers.get("content-type", "")
        is_twilio = "application/x-www-form-urlencoded" in content_type or From is not None

        if is_twilio:
            session_id = From.replace("whatsapp:", "")
            to_number = From  # Guardamos el número completo para enviar respuesta
            message = Body or ""
            logger.info(f"[WEBHOOK] Twilio msg recibido de: {session_id}")
        else:
            # Manejar JSON con posibles problemas de encoding
            try:
                data = await request.json()
            except UnicodeDecodeError as ue:
                # Intentar con encoding latin-1 si UTF-8 falla
                body_bytes = await request.body()
                body_text = body_bytes.decode('latin-1')
                import json
                data = json.loads(body_text)
                logger.warning(f"[WEBHOOK] JSON decodificado con latin-1 fallback")
            except Exception as json_err:
                logger.error(f"[WEBHOOK] Error parseando JSON: {json_err}")
                raise HTTPException(status_code=400, detail=f"JSON inválido: {str(json_err)}")

            session_id = data.get("session_id")
            message = data.get("message")
            to_number = session_id  # Para JSON, usamos session_id
            if not session_id or not message:
                raise HTTPException(status_code=400, detail="Faltan campos: session_id y message son requeridos")
            logger.info(f"[WEBHOOK] JSON msg recibido de: {session_id}")

        # B. SISTEMA DE AGREGACIÓN DE MENSAJES
        # Agrega el mensaje al buffer y determina si debe procesarse
        agg_result = await message_aggregator.add_message_to_buffer(session_id, message)

        if not agg_result["should_process"]:
            # Este mensaje se agregó a un buffer existente
            # No responder nada - el proceso principal responderá por todos
            logger.info(f"[WEBHOOK] Mensaje agregado a buffer. Total: {agg_result['buffer_count']}")
            if is_twilio:
                # Respuesta vacía para Twilio (no envía mensaje al usuario)
                return Response(
                    content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                    media_type="application/xml"
                )
            else:
                return MessageResponse(
                    response="",
                    status="aggregating"
                )

        # C. VERIFICAR SI HAY AGREGACIÓN ACTIVA (Redis disponible)
        if agg_result["is_aggregating"] and is_twilio:
            # Con agregación: procesar en background y responder inmediatamente
            # Esto evita el timeout de 15 segundos de Twilio
            logger.info(f"[WEBHOOK] Iniciando procesamiento en background para {session_id}")
            background_tasks.add_task(
                process_aggregated_messages,
                session_id,
                to_number
            )
            # Responder inmediatamente a Twilio con TwiML vacío
            return Response(
                content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                media_type="application/xml"
            )

        # D. SIN AGREGACIÓN: Procesar inmediatamente (modo legacy/sin Redis)
        # Esto se usa cuando Redis no está disponible o para requests JSON
        if agg_result.get("combined_message"):
            message = agg_result["combined_message"]

        result = await process_message(session_id, message)

        # E. Generar respuesta según cliente
        if is_twilio:
            xml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response><Message>{result['response']}</Message></Response>"""
            return Response(content=xml_response, media_type="application/xml")
        else:
            return MessageResponse(
                response=result["response"],
                status=str(result["status"])
            )

    except Exception as e:
        logger.error(f"[WEBHOOK] Error procesando mensaje: {e}", exc_info=True)
        if From:  # Fallback seguro para WhatsApp
            return Response(
                content='<?xml version="1.0"?><Response><Message>Lo siento, ocurrió un error.</Message></Response>',
                media_type="application/xml"
            )
        raise HTTPException(status_code=500, detail="Internal Server Error")

# ===== 5. ENDPOINTS UTILITARIOS Y ADMIN =====

@app.get("/health")
async def health_check():
    """Health check rápido para Railway."""
    status = {"status": "healthy", "redis": "unchecked", "postgres": "unchecked"}

    # Check Rápido Redis
    try:
        import redis
        if r_url := os.getenv("REDIS_URL"):
            redis.from_url(r_url, socket_connect_timeout=1).ping()
            status["redis"] = "connected"
    except Exception:
        status["redis"] = "error"

    # Check Rápido Postgres
    try:
        import psycopg
        if db_url := os.getenv("DATABASE_URL"):
            psycopg.connect(db_url.replace("postgres://", "postgresql://"), connect_timeout=1).close()
            status["postgres"] = "connected"
    except Exception:
        status["postgres"] = "error"

    return status

@app.get("/test-hubspot")
async def test_hubspot():
    """Endpoint temporal para validar conectividad con HubSpot API."""
    try:
        from integrations.hubspot import hubspot_client

        # Test 1: Verificar que el cliente esté inicializado
        if not hubspot_client:
            return {"status": "error", "message": "HubSpot client no inicializado"}

        # Test 2: Intentar búsqueda simple (operación READ)
        result = await hubspot_client.search_contacts_by_email("test@nonexistent-domain-12345.com")

        return {
            "status": "success",
            "hubspot_api": "reachable",
            "authentication": "valid",
            "permissions": "read_contacts_ok",
            "test_details": f"Búsqueda ejecutada correctamente. Resultados: {len(result.get('results', []))}"
        }
    except Exception as e:
        return {
            "status": "error",
            "hubspot_api": "error",
            "error_type": type(e).__name__,
            "error_message": str(e)
        }


@app.post("/test-hubspot-create")
async def test_hubspot_create():
    """
    Endpoint de diagnóstico para probar creación de contactos en HubSpot.
    Envía un payload mínimo para identificar la causa exacta del error 400.
    """
    import httpx
    from datetime import datetime

    api_key = os.getenv("HUBSPOT_API_KEY")
    base_url = "https://api.hubapi.com"

    # Timestamp único para evitar duplicados
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")

    # Prueba 1: Payload MÍNIMO (solo propiedades estándar de HubSpot)
    minimal_payload = {
        "properties": {
            "firstname": "Test",
            "lastname": f"Diagnostico-{ts}",
            "phone": f"+549110000{ts[-4:]}"
        }
    }

    # Prueba 2: Payload con propiedades custom (como lo envía CRMAgent)
    full_payload = {
        "properties": {
            "firstname": "Test",
            "lastname": f"FullDiag-{ts}",
            "phone": f"+549110001{ts[-4:]}",
            "whatsapp_id": f"+549110001{ts[-4:]}",
            "chatbot_property_type": "Departamento",
            "chatbot_rooms": "2",
            "chatbot_location": "Palermo",
            "chatbot_budget": "100000",
            "chatbot_conversation": "Test conversation",
            "chatbot_score": "75",  # Como string
            # HubSpot Date requiere timestamp a medianoche UTC
            "chatbot_timestamp": str(int(
                datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
            ))
        }
    }

    results = {}

    async with httpx.AsyncClient(timeout=15.0) as client:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        # Test 1: Payload mínimo
        try:
            resp = await client.post(
                f"{base_url}/crm/v3/objects/contacts",
                headers=headers,
                json=minimal_payload
            )
            results["test_1_minimal"] = {
                "status_code": resp.status_code,
                "payload_sent": minimal_payload,
                "response": resp.json() if resp.status_code < 400 else resp.text
            }
        except Exception as e:
            results["test_1_minimal"] = {"error": str(e)}

        # Test 2: Payload completo
        try:
            resp = await client.post(
                f"{base_url}/crm/v3/objects/contacts",
                headers=headers,
                json=full_payload
            )
            results["test_2_full"] = {
                "status_code": resp.status_code,
                "payload_sent": full_payload,
                "response": resp.json() if resp.status_code < 400 else resp.text
            }
        except Exception as e:
            results["test_2_full"] = {"error": str(e)}

    return {
        "diagnostic": "HubSpot Contact Creation Test",
        "timestamp": datetime.utcnow().isoformat(),
        "results": results
    }

@app.post("/admin/reload-kb")
async def reload_knowledge_base(x_api_key: str = Header(None, alias="X-API-Key")):
    """Endpoint administrativo protegido para recargar RAG."""
    # Validación de seguridad
    if x_api_key != os.getenv("ADMIN_API_KEY"):
        logger.warning("[API] Acceso no autorizado a /admin/reload-kb")
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        logger.info("[API] Recargando base de conocimiento...")
        # Nota: InfoAgent sigue siendo síncrono en esta operación, está bien para admin tasks
        result = agent.reload_knowledge_base()

        if result.get("status") == "success":
            logger.info(f"[API] Recarga exitosa: {result.get('files_loaded')} archivos")
            return JSONResponse(status_code=200, content=result)
        else:
            logger.error(f"[API] Error en recarga: {result.get('message')}")
            raise HTTPException(status_code=500, detail=result.get("message"))

    except Exception as e:
        logger.error(f"[API] Error crítico: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/update-deal-stages")
async def update_deal_stages(
    x_api_key: str = Header(None, alias="X-API-Key"),
    deal_id: str = None,
    contact_id: str = None
):
    """
    Endpoint administrativo para actualizar etapas de deals manualmente.

    Modo de uso:
    1. Actualizar un deal específico: Enviar deal_id y contact_id
    2. Actualizar todos los deals recientes (últimas 24h): No enviar parámetros
    """
    # Validación de seguridad
    if x_api_key != os.getenv("ADMIN_API_KEY"):
        logger.warning("[API] Acceso no autorizado a /admin/update-deal-stages")
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        from integrations.hubspot.hubspot_client import HubSpotClient
        from integrations.hubspot.deal_tracker import DealStageTracker

        # Inicializar tracker
        hubspot_client = HubSpotClient()
        tracker = DealStageTracker(hubspot_client)

        # Caso 1: Actualizar un deal específico
        if deal_id and contact_id:
            logger.info(f"[API] Actualizando deal específico: {deal_id}")
            new_stage = await tracker.check_and_update_stage(deal_id, contact_id, force_check=True)

            if new_stage:
                return JSONResponse(status_code=200, content={
                    "status": "success",
                    "message": f"Deal {deal_id} actualizado",
                    "new_stage": new_stage,
                    "deal_id": deal_id
                })
            else:
                return JSONResponse(status_code=200, content={
                    "status": "no_change",
                    "message": f"Deal {deal_id} no requiere actualización",
                    "deal_id": deal_id
                })

        # Caso 2: Actualizar deals recientes (batch)
        # TODO: Implementar búsqueda de deals creados en últimas 24h
        # Por ahora retornamos mensaje de no implementado
        return JSONResponse(status_code=501, content={
            "status": "not_implemented",
            "message": "Batch update no implementado aún. Use deal_id y contact_id específicos."
        })

    except Exception as e:
        logger.error(f"[API] Error actualizando deal stages: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/lead-stats")
async def get_lead_statistics(
    x_api_key: str = Header(None, alias="X-API-Key"),
    owner_id: str = None,
    check_unassigned: bool = False
):
    """
    Endpoint administrativo para obtener estadísticas de leads.

    Modos de uso:
    1. Estadísticas de un trabajador específico: ?owner_id=86909130
    2. Leads sin asignar: ?check_unassigned=true
    3. Resumen general: Sin parámetros)
    """
    # Validación de seguridad
    if x_api_key != os.getenv("ADMIN_API_KEY"):
        logger.warning("[API] Acceso no autorizado a /admin/lead-stats")
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        from integrations.hubspot.hubspot_client import HubSpotClient
        from integrations.hubspot.lead_counter import LeadCounter

        # Inicializar contador
        hubspot_client = HubSpotClient()
        counter = LeadCounter(hubspot_client)

        # Caso 1: Leads sin asignar (huérfanos)
        if check_unassigned:
            logger.info("[API] Consultando leads sin asignar...")
            data = await counter.get_unassigned_leads_count(hours_window=168)  # 7 días
            message = await counter.generate_unassigned_alert(hours_window=168)

            return JSONResponse(status_code=200, content={
                "status": "success",
                "type": "unassigned_leads",
                "total": data["total"],
                "por_canal": data["por_canal"],
                "leads": data["leads"][:10],  # Primeros 10
                "message": message
            })

        # Caso 2: Estadísticas de un trabajador específico
        if owner_id:
            logger.info(f"[API] Consultando leads pendientes para owner {owner_id}...")
            data = await counter.get_pending_leads_count(owner_id, hours_window=24)
            message = await counter.generate_notification_message(owner_id, hours_window=24)

            return JSONResponse(status_code=200, content={
                "status": "success",
                "type": "pending_leads",
                "owner_id": owner_id,
                "total": data["total"],
                "por_canal": data["por_canal"],
                "leads": data["leads"][:10],  # Primeros 10
                "message": message
            })

        # Caso 3: Resumen general (ambos trabajadores)
        from integrations.hubspot.lead_assigner import lead_assigner

        # Obtener IDs de todos los owners activos
        all_owner_ids = set()
        for team_config in lead_assigner.OWNERS_CONFIG.values():
            for owner in team_config:
                if owner.get("active", True):
                    all_owner_ids.add(owner["id"])

        # Generar resumen para cada owner
        summaries = {}
        for oid in all_owner_ids:
            data = await counter.get_pending_leads_count(oid, hours_window=24)
            summaries[oid] = {
                "name": lead_assigner.get_owner_name(oid),
                "total": data["total"],
                "por_canal": data["por_canal"]
            }

        # Verificar leads huérfanos
        unassigned_data = await counter.get_unassigned_leads_count(hours_window=168)

        return JSONResponse(status_code=200, content={
            "status": "success",
            "type": "general_summary",
            "por_trabajador": summaries,
            "leads_sin_asignar": {
                "total": unassigned_data["total"],
                "por_canal": unassigned_data["por_canal"]
            },
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

    except Exception as e:
        logger.error(f"[API] Error obteniendo estadísticas de leads: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/check-orphan-leads")
async def check_orphan_leads(
    x_api_key: str = Header(None, alias="X-API-Key"),
    hours: int = 24,
    send_alert: bool = True
):
    """
    Endpoint administrativo para buscar leads sin asignar en HubSpot.

    Este endpoint ejecuta una búsqueda activa en HubSpot de leads que:
    - NO tienen hubspot_owner_id asignado
    - Tienen chatbot_timestamp (son del chatbot)
    - Fueron creados en las últimas X horas

        X-API-Key: Token de autenticación admin
    """
    # Validación de seguridad
    if x_api_key != os.getenv("ADMIN_API_KEY"):
        logger.warning("[API] Acceso no autorizado a /admin/check-orphan-leads")
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        from integrations.hubspot.hubspot_client import HubSpotClient
        from integrations.hubspot.lead_assigner import OrphanLeadMonitor

        # Inicializar monitor
        hubspot_client = HubSpotClient()

        # Intentar conectar a Redis
        redis_client = None
        try:
            import redis
            redis_url = os.getenv("REDIS_URL")
            if redis_url:
                redis_client = redis.from_url(redis_url, decode_responses=True)
                redis_client.ping()
        except Exception as e:
            logger.warning(f"[API] Redis no disponible: {e}")

        # Crear instancia del monitor
        monitor = OrphanLeadMonitor(hubspot_client, redis_client)

        # Ejecutar búsqueda
        logger.info(f"[API] Ejecutando check de leads huérfanos (últimas {hours}h)...")
        orphan_leads = await monitor.check_orphan_leads(hours_window=hours)

        # Preparar respuesta
        result = {
            "status": "success",
            "total": len(orphan_leads),
            "hours_window": hours,
            "alert_sent": send_alert and len(orphan_leads) > 0 and monitor.webhook_url is not None,
            "webhook_configured": monitor.webhook_url is not None,
            "leads": []
        }

        # Agregar información de los leads (primeros 20)
        for lead in orphan_leads[:20]:
            props = lead.get("properties", {})
            result["leads"].append({
                "id": lead["id"],
                "name": f"{props.get('firstname', '')} {props.get('lastname', '')}".strip(),
                "phone": props.get("phone", ""),
                "canal": props.get("canal_origen", "desconocido"),
                "score": props.get("chatbot_score", "N/A"),
                "urgency": props.get("chatbot_urgency", "N/A"),
                "location": props.get("chatbot_location", "N/A")
            })

        if len(orphan_leads) > 20:
            result["note"] = f"Mostrando primeros 20 de {len(orphan_leads)} leads"

        logger.info(f"[API] Check completado: {len(orphan_leads)} leads sin asignar")

        return JSONResponse(status_code=200, content=result)

    except Exception as e:
        logger.error(f"[API] Error en check de leads huérfanos: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/orphan-leads")
async def get_orphan_leads(x_api_key: str = Header(None, alias="X-API-Key")):
    """
    Endpoint GET simplificado para consultar leads sin asignar.

    Retorna una vista formateada de los leads huérfanos encontrados en HubSpot.
    Este endpoint es útil para integraciones que necesitan consultar periódicamente
    el estado de leads sin asignar sin ejecutar la lógica de alertas.

    Headers requeridos:
        X-API-Key: Token de autenticación admin
    """
    # Validación de seguridad
    if x_api_key != os.getenv("ADMIN_API_KEY"):
        logger.warning("[API] Acceso no autorizado a /admin/orphan-leads")
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        from integrations.hubspot.hubspot_client import HubSpotClient
        from integrations.hubspot.lead_assigner import OrphanLeadMonitor

        # Inicializar cliente HubSpot
        hubspot_client = HubSpotClient()

        # Intentar conectar a Redis (opcional)
        redis_client = None
        try:
            import redis
            redis_url = os.getenv("REDIS_URL")
            if redis_url:
                redis_client = redis.from_url(redis_url, decode_responses=True)
                redis_client.ping()
        except Exception as e:
            logger.warning(f"[API] Redis no disponible (operando sin cache): {e}")

        # Crear instancia del monitor
        monitor = OrphanLeadMonitor(hubspot_client, redis_client)

        # Ejecutar búsqueda (últimas 24h por defecto)
        logger.info("[API] Consultando leads huérfanos...")
        orphans = await monitor.check_orphan_leads(hours_window=24)

        # Formatear respuesta
        formatted_leads = []
        for lead in orphans:
            props = lead.get("properties", {})
            formatted_leads.append({
                "id": lead.get("id"),
                "name": f"{props.get('firstname', '')} {props.get('lastname', '')}".strip() or "Sin nombre",
                "phone": props.get("phone", "N/A"),
                "canal": props.get("canal_origen", "desconocido"),
                "score": props.get("chatbot_score", "N/A"),
                "location": props.get("chatbot_location", "N/A"),
                "urgency": props.get("chatbot_urgency", "N/A")
            })

        logger.info(f"[API] Retornando {len(formatted_leads)} leads sin asignar")

        return JSONResponse(status_code=200, content={
            "count": len(formatted_leads),
            "leads": formatted_leads
        })

    except Exception as e:
        logger.error(f"[API] Error consultando leads huérfanos: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ===== 6. SCHEDULER PARA SEGUIMIENTO AUTOMÁTICO =====

# Scheduler global
scheduler = AsyncIOScheduler()

# Configuración de seguimiento (vía variables de entorno)
FOLLOWUP_ENABLED = os.getenv("FOLLOWUP_ENABLED", "false").lower() == "true"
FOLLOWUP_DELAY_HOURS = int(os.getenv("FOLLOWUP_DELAY_HOURS", "24"))
FOLLOWUP_TEMPLATE_ID = os.getenv("FOLLOWUP_TEMPLATE_ID", "seguimiento_24h")


async def check_and_send_followups():
    """
    Verifica contactos que no han respondido en 24h y envía template de seguimiento.

    Se ejecuta cada hora vía APScheduler.

    Lógica:
    1. Buscar contactos con último mensaje > 24h
    2. Verificar que no se les haya enviado followup reciente (7 días)
    3. Enviar template de seguimiento
    4. Marcar como enviado en Redis (TTL 7 días)
    """
    if not FOLLOWUP_ENABLED:
        return

    logger.info("[FOLLOWUP] Iniciando verificación de seguimientos pendientes...")

    try:
        import redis.asyncio as redis_async
        from utils.twilio_client import twilio_client

        redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
        r = redis_async.from_url(redis_url, encoding="utf-8", decode_responses=True)

        # Prefijos
        LAST_MSG_PREFIX = "last_client_msg:"
        FOLLOWUP_SENT_PREFIX = "followup_sent:"

        # Calcular umbral de tiempo (24h atrás)
        threshold = datetime.now(timezone.utc) - timedelta(hours=FOLLOWUP_DELAY_HOURS)

        followups_sent = 0
        contacts_checked = 0

        # Buscar contactos con ventana de 24h
        async for key in r.scan_iter(match=f"{LAST_MSG_PREFIX}*"):
            contacts_checked += 1
            phone = key.replace(LAST_MSG_PREFIX, "")

            try:
                # Obtener timestamp del último mensaje
                last_msg_str = await r.get(key)
                if not last_msg_str:
                    continue

                last_msg_time = datetime.fromisoformat(last_msg_str.replace("Z", "+00:00"))

                # Verificar si pasaron más de 24h
                if last_msg_time > threshold:
                    continue  # Aún en ventana activa, no necesita followup

                # Verificar si ya enviamos followup recientemente
                followup_key = f"{FOLLOWUP_SENT_PREFIX}{phone}"
                if await r.exists(followup_key):
                    continue  # Ya se envió followup

                # Verificar que el contacto no esté en conversación activa
                state_key = f"conv_state:{phone}"
                status = await r.get(state_key)
                if status in ["HUMAN_ACTIVE", "IN_CONVERSATION"]:
                    continue  # Hay conversación activa, no enviar followup automático

                # Enviar template de seguimiento
                logger.info(f"[FOLLOWUP] Enviando seguimiento a {phone}")

                # Obtener nombre del contacto (si está disponible)
                contact_name = "cliente"
                meta_key = f"conv_meta:{phone}"
                meta_str = await r.get(meta_key)
                if meta_str:
                    try:
                        meta = json.loads(meta_str)
                        contact_name = meta.get("display_name", "cliente").split()[0]  # Primer nombre
                    except Exception:
                        pass

                # Construir mensaje de seguimiento
                followup_message = f"¡Hola {contact_name}! ¿Pudiste revisar la información que te enviamos? Estamos aquí para resolver cualquier duda. 😊"

                # Enviar via Twilio
                if twilio_client.is_available:
                    result = await twilio_client.send_whatsapp_message(
                        to=phone,
                        body=followup_message
                    )

                    if result.get("status") == "success":
                        # Marcar como enviado (TTL 7 días para no repetir)
                        await r.set(followup_key, datetime.now(timezone.utc).isoformat(), ex=7*24*60*60)
                        followups_sent += 1
                        logger.info(f"[FOLLOWUP] ✅ Seguimiento enviado a {phone}")
                    else:
                        logger.warning(f"[FOLLOWUP] ❌ Error enviando a {phone}: {result.get('message')}")

            except Exception as contact_err:
                logger.error(f"[FOLLOWUP] Error procesando {phone}: {contact_err}")
                continue

        await r.close()
        logger.info(f"[FOLLOWUP] Completado. Contactos revisados: {contacts_checked}, Seguimientos enviados: {followups_sent}")

    except Exception as e:
        logger.error(f"[FOLLOWUP] Error general: {e}", exc_info=True)


def start_followup_scheduler():
    """Inicia el scheduler para seguimiento automático."""
    if not FOLLOWUP_ENABLED:
        logger.info("[FOLLOWUP] Sistema de seguimiento automático DESHABILITADO (FOLLOWUP_ENABLED=false)")
        return

    logger.info(f"[FOLLOWUP] Sistema de seguimiento automático HABILITADO")
    logger.info(f"[FOLLOWUP] - Delay: {FOLLOWUP_DELAY_HOURS} horas")
    logger.info(f"[FOLLOWUP] - Template: {FOLLOWUP_TEMPLATE_ID}")

    # Programar ejecución cada hora
    scheduler.add_job(
        check_and_send_followups,
        trigger=IntervalTrigger(hours=1),
        id="followup_job",
        name="Seguimiento automático 24h",
        replace_existing=True
    )

    scheduler.start()
    logger.info("[FOLLOWUP] Scheduler iniciado. Próxima ejecución en 1 hora.")


# ===== 6.2 SCHEDULER PARA RECORDATORIOS DE CITAS =====

APPOINTMENT_REMINDERS_ENABLED = os.getenv("APPOINTMENT_REMINDERS_ENABLED", "false").lower() == "true"


async def check_appointment_reminders():
    """
    Verifica citas que necesitan recordatorio (24h antes) o seguimiento (24h después).

    Se ejecuta cada hora via APScheduler.
    - 24h ANTES: Envía recordatorio de la cita
    - 24h DESPUÉS (si status=completed): Envía template preguntando experiencia
    """
    if not APPOINTMENT_REMINDERS_ENABLED:
        return

    logger.info("[APPOINTMENTS] Iniciando verificación de recordatorios...")

    try:
        from middleware.appointment_manager import AppointmentManager, AppointmentStatus
        from middleware.outbound_panel import _get_template
        from utils.twilio_client import twilio_client

        redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
        apt_manager = AppointmentManager(redis_url)

        reminders_sent = 0
        followups_sent = 0

        # ========== RECORDATORIOS (24h ANTES) ==========
        try:
            appointments_for_reminder = await apt_manager.get_appointments_needing_reminder()
            logger.info(f"[APPOINTMENTS] Citas que necesitan recordatorio: {len(appointments_for_reminder)}")

            for apt in appointments_for_reminder:
                try:
                    # Obtener template de recordatorio
                    template = await _get_template("cita_recordatorio")
                    if not template:
                        logger.error("[APPOINTMENTS] Template 'cita_recordatorio' no encontrado")
                        continue

                    # Formatear mensaje
                    from utils.date_parser import AppointmentDateParser
                    apt_dt = apt.scheduled_dt
                    fecha_formateada = AppointmentDateParser.format_appointment_for_message(apt_dt)

                    message = template["body"].format(
                        nombre=apt.contact_name or "cliente",
                        fecha=apt_dt.strftime("%d de %B"),
                        hora=apt_dt.strftime("%H:%M")
                    )

                    # Enviar vía Twilio
                    if twilio_client.is_available:
                        result = await twilio_client.send_whatsapp_message(
                            to=apt.phone_normalized,
                            body=message
                        )

                        if result.get("status") == "success":
                            await apt_manager.mark_reminder_sent(apt.phone_normalized, apt.canal)
                            reminders_sent += 1
                            logger.info(f"[APPOINTMENTS] Recordatorio enviado a {apt.phone_normalized}")
                        else:
                            logger.warning(f"[APPOINTMENTS] Error enviando recordatorio: {result.get('message')}")
                    else:
                        logger.warning("[APPOINTMENTS] Twilio no disponible para enviar recordatorio")

                except Exception as e:
                    logger.error(f"[APPOINTMENTS] Error procesando recordatorio: {e}")

        except Exception as e:
            logger.error(f"[APPOINTMENTS] Error obteniendo citas para recordatorio: {e}")

        # ========== SEGUIMIENTO POST-CITA (24h DESPUÉS) ==========
        try:
            appointments_for_followup = await apt_manager.get_appointments_needing_followup()
            logger.info(f"[APPOINTMENTS] Citas que necesitan seguimiento: {len(appointments_for_followup)}")

            for apt in appointments_for_followup:
                try:
                    # Obtener template de seguimiento
                    template = await _get_template("seguimiento_visita")
                    if not template:
                        logger.error("[APPOINTMENTS] Template 'seguimiento_visita' no encontrado")
                        continue

                    # Formatear mensaje
                    message = template["body"].format(
                        nombre=apt.contact_name or "cliente"
                    )

                    # Marcar flag para detectar respuesta → HUMAN_ACTIVE
                    import redis.asyncio as redis_async
                    r = redis_async.from_url(redis_url, encoding="utf-8", decode_responses=True)
                    await r.set(
                        f"appointment_followup_pending:{apt.phone_normalized}:{apt.canal}",
                        "true",
                        ex=7 * 24 * 60 * 60  # 7 días TTL
                    )
                    await r.close()

                    # Enviar vía Twilio
                    if twilio_client.is_available:
                        result = await twilio_client.send_whatsapp_message(
                            to=apt.phone_normalized,
                            body=message
                        )

                        if result.get("status") == "success":
                            await apt_manager.mark_followup_sent(apt.phone_normalized, apt.canal)
                            followups_sent += 1
                            logger.info(f"[APPOINTMENTS] Seguimiento enviado a {apt.phone_normalized}")
                        else:
                            logger.warning(f"[APPOINTMENTS] Error enviando seguimiento: {result.get('message')}")
                    else:
                        logger.warning("[APPOINTMENTS] Twilio no disponible para enviar seguimiento")

                except Exception as e:
                    logger.error(f"[APPOINTMENTS] Error procesando seguimiento: {e}")

        except Exception as e:
            logger.error(f"[APPOINTMENTS] Error obteniendo citas para seguimiento: {e}")

        await apt_manager.close()

        logger.info(
            f"[APPOINTMENTS] Completado. Recordatorios: {reminders_sent}, Seguimientos: {followups_sent}"
        )

    except Exception as e:
        logger.error(f"[APPOINTMENTS] Error general: {e}", exc_info=True)


# ===== 6.3 VERIFICADOR DE TIMEOUTS (CLIENTE vs ASESOR) =====

async def check_conversation_timeouts():
    """
    Verifica conversaciones con timeout y reactiva Sofía si corresponde.

    Reglas:
    - Si CLIENTE no responde en 24h: Sofía retoma CON contexto
    - Si ASESOR no responde en 72h: Sofía retoma automáticamente

    Se ejecuta cada hora via APScheduler.
    """
    logger.info("[TIMEOUTS] Verificando timeouts de conversaciones...")

    try:
        from middleware.conversation_state import ConversationStateManager, ConversationStatus

        redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
        state_manager = ConversationStateManager(redis_url)

        import redis.asyncio as redis_async
        r = redis_async.from_url(redis_url, encoding="utf-8", decode_responses=True)

        timeouts_processed = 0
        client_timeouts = 0
        advisor_timeouts = 0

        # Buscar todas las conversaciones HUMAN_ACTIVE o IN_CONVERSATION
        async for key in r.scan_iter(match="conv_state:*"):
            try:
                status_str = await r.get(key)
                if status_str not in ["HUMAN_ACTIVE", "IN_CONVERSATION"]:
                    continue

                # Extraer phone y canal de la key
                key_without_prefix = key.replace("conv_state:", "")
                parts = key_without_prefix.rsplit(":", 1)
                phone = parts[0]
                canal = parts[1] if len(parts) > 1 else None

                # Verificar timeout
                timeout_type = await state_manager.check_conversation_timeout(phone, canal)

                if timeout_type == "client_timeout":
                    # Cliente no respondió en 24h - Sofía retoma con contexto
                    logger.info(f"[TIMEOUTS] Cliente timeout: {phone}:{canal or 'default'} - Sofía retoma con contexto")

                    # Marcar flag para que Sofía sepa dar contexto
                    await r.set(
                        f"sofia_retake_context:{phone}:{canal or 'default'}",
                        "client_timeout",
                        ex=24 * 60 * 60  # 24h TTL
                    )

                    # Reactivar bot
                    await state_manager.activate_bot(phone, canal)
                    client_timeouts += 1
                    timeouts_processed += 1

                elif timeout_type == "advisor_timeout":
                    # Asesor no respondió en 72h - Sofía retoma
                    logger.info(f"[TIMEOUTS] Asesor timeout: {phone}:{canal or 'default'} - Sofía retoma")

                    # Marcar flag para contexto
                    await r.set(
                        f"sofia_retake_context:{phone}:{canal or 'default'}",
                        "advisor_timeout",
                        ex=24 * 60 * 60
                    )

                    # Reactivar bot
                    await state_manager.activate_bot(phone, canal)
                    advisor_timeouts += 1
                    timeouts_processed += 1

            except Exception as e:
                logger.error(f"[TIMEOUTS] Error procesando {key}: {e}")

        await r.close()

        logger.info(
            f"[TIMEOUTS] Completado. Total: {timeouts_processed} "
            f"(cliente: {client_timeouts}, asesor: {advisor_timeouts})"
        )

    except Exception as e:
        logger.error(f"[TIMEOUTS] Error general: {e}", exc_info=True)


def start_appointment_scheduler():
    """Inicia el scheduler para recordatorios de citas."""
    if not APPOINTMENT_REMINDERS_ENABLED:
        logger.info("[APPOINTMENTS] Sistema de recordatorios DESHABILITADO (APPOINTMENT_REMINDERS_ENABLED=false)")
        return

    logger.info("[APPOINTMENTS] Sistema de recordatorios HABILITADO")

    scheduler.add_job(
        check_appointment_reminders,
        trigger=IntervalTrigger(hours=1),
        id="appointment_reminders_job",
        name="Recordatorios de citas",
        replace_existing=True
    )

    logger.info("[APPOINTMENTS] Scheduler de citas iniciado")


def start_timeout_checker():
    """Inicia el job de verificación de timeouts."""
    scheduler.add_job(
        check_conversation_timeouts,
        trigger=IntervalTrigger(hours=1),
        id="timeout_checker_job",
        name="Verificador de timeouts",
        replace_existing=True
    )
    logger.info("[TIMEOUTS] Verificador de timeouts iniciado")


# Iniciar scheduler en startup
@app.on_event("startup")
async def startup_scheduler():
    """Inicia todos los schedulers."""
    start_followup_scheduler()
    start_appointment_scheduler()
    start_timeout_checker()


@app.on_event("shutdown")
async def shutdown_scheduler():
    """Detiene el scheduler al cerrar la aplicación."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("[FOLLOWUP] Scheduler detenido")


# ===== 7. ENTRYPOINT =====
if __name__ == "__main__":
    # Railway inyecta la variable PORT automáticamente
    # Default cambiado a 8001 para evitar conflictos con otros proyectos locales
    port = int(os.getenv("PORT", "8001"))
    uvicorn.run(app, host="0.0.0.0", port=port)