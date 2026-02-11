# app.py
"""
Servidor FastAPI asíncrono para chatbot Sofía.
Maneja webhooks de Twilio (WhatsApp) y requests JSON.
Incluye sistema de agregación de mensajes para manejar múltiples mensajes seguidos.
"""

from fastapi import FastAPI, HTTPException, Response, Form, Request, Header, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from agents.orchestrator import process_message
from agents.InfoAgent.info_agent import agent
from utils.message_aggregator import message_aggregator, AGGREGATION_TIMEOUT
from utils.twilio_client import twilio_client
from logging_config import logger
import uvicorn
import os
import json
import asyncio
from datetime import datetime, timezone
from dotenv import load_dotenv

# Importar el router del middleware inteligente (lazy import)
from middleware import get_whatsapp_router, get_outbound_panel_router

# Importar el router de webhooks de salida HubSpot -> WhatsApp
from integrations.hubspot import get_outbound_router

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
    Función que se ejecuta en background para:
    1. Esperar el timeout de agregación
    2. Obtener todos los mensajes combinados
    3. Procesar con el orchestrator
    4. Enviar respuesta via Twilio API

    Esta función resuelve el problema del timeout de 15 segundos de Twilio.
    """
    try:
        # 1. Esperar y obtener mensajes combinados
        combined_message = await message_aggregator.wait_and_get_combined_message(session_id)

        if not combined_message:
            logger.warning(f"[BACKGROUND] No hay mensajes para procesar (session: {session_id})")
            return

        logger.info(f"[BACKGROUND] Procesando mensajes agregados: '{combined_message[:80]}...'")

        # 2. Procesar con el orchestrator
        result = await process_message(session_id, combined_message)

        if not result or not result.get("response"):
            logger.warning(f"[BACKGROUND] Orchestrator no generó respuesta para {session_id}")
            return

        # 3. Enviar respuesta via Twilio API
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

    Flujo con agregación (30 segundos por defecto):
    1. Mensaje llega → se agrega al buffer
    2. Si es el primer mensaje → inicia procesamiento en BACKGROUND
    3. Si llegan más mensajes en ese tiempo → se agregan al buffer
    4. Después del timeout → procesa todos los mensajes juntos via Twilio API
    5. Todos los webhooks responden inmediatamente con TwiML vacío

    IMPORTANTE: Cuando hay agregación activa, la respuesta se envía via
    Twilio Messages API (no via TwiML response) para evitar el timeout de 15s.
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

    Headers requeridos:
        X-API-Key: Token de autenticación admin

    Body (opcional):
        {
            "deal_id": "123456789",
            "contact_id": "987654321"
        }
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
    3. Resumen general: Sin parámetros

    Headers requeridos:
        X-API-Key: Token de autenticación admin

    Query Parameters:
        owner_id: ID del trabajador (opcional)
        check_unassigned: Si es true, retorna leads huérfanos (opcional)
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

    Headers requeridos:
        X-API-Key: Token de autenticación admin

    Query Parameters:
        hours: Ventana de tiempo en horas (default: 24)
        send_alert: Si es true, envía alerta a webhook configurado (default: true)

    Environment Variables requeridas:
        ORPHAN_LEAD_WEBHOOK_URL: URL del webhook (Slack/Discord) - opcional
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

    Response:
        {
            "count": 3,
            "leads": [
                {
                    "id": "123",
                    "name": "Juan Pérez",
                    "phone": "+549...",
                    "canal": "whatsapp_directo",
                    "score": "75"
                },
                ...
            ]
        }
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


# ===== 6. ENTRYPOINT =====
if __name__ == "__main__":
    # Railway inyecta la variable PORT automáticamente
    # Default cambiado a 8001 para evitar conflictos con otros proyectos locales
    port = int(os.getenv("PORT", "8001"))
    uvicorn.run(app, host="0.0.0.0", port=port)