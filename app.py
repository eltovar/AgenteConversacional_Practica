# app.py
"""
Servidor FastAPI asíncrono para chatbot Sofía.
Maneja webhooks de Twilio (WhatsApp) y requests JSON.
"""

from fastapi import FastAPI, HTTPException, Response, Form, Request, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from agents.orchestrator import process_message
from agents.InfoAgent.info_agent import agent
from logging_config import logger
import uvicorn
import os
from dotenv import load_dotenv

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

# ===== 4. ENDPOINT UNIFICADO (WEBHOOK) =====
@app.post("/webhook")
async def webhook(
    request: Request,
    From: str = Form(None),
    Body: str = Form(None)
):
    """
    Maneja mensajes de Twilio (Form Data) y JSON estándar.
    Totalmente ASÍNCRONO para soportar alta concurrencia.
    """
    try:
        # A. Detectar origen (Twilio vs JSON)
        content_type = request.headers.get("content-type", "")
        is_twilio = "application/x-www-form-urlencoded" in content_type or From is not None

        if is_twilio:
            session_id = From.replace("whatsapp:", "")
            message = Body or ""
            logger.info(f"[WEBHOOK] Twilio msg recibido de: {session_id}")
        else:
            data = await request.json()
            session_id = data.get("session_id")
            message = data.get("message")
            logger.info(f"[WEBHOOK] JSON msg recibido de: {session_id}")

        # B. PROCESAMIENTO ASÍNCRONO (La clave del cambio)
        # Usamos await para liberar el worker mientras el agente piensa o llama a HubSpot
        result = await process_message(session_id, message)

        # C. Generar respuesta según cliente
        if is_twilio:
            # Respuesta TwiML (XML)
            xml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response><Message>{result['response']}</Message></Response>"""
            return Response(content=xml_response, media_type="application/xml")
        else:
            # Respuesta JSON
            return MessageResponse(
                response=result["response"],
                status=str(result["status"])
            )

    except Exception as e:
        logger.error(f"[WEBHOOK] Error procesando mensaje: {e}", exc_info=True)
        if From:  # Fallback seguro para WhatsApp
            return Response(
                content="""<?xml version="1.0"?><Response><Message>Lo siento, ocurrió un error técnico momentáneo.</Message></Response>""",
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

# ===== 6. ENTRYPOINT =====
if __name__ == "__main__":
    # Railway inyecta la variable PORT automáticamente
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)