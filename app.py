"""
Servidor FastAPI para recibir mensajes vía webhooks.
Delega toda la lógica de negocio a orchestrator.py.
Compatible con Twilio, WhatsApp Business API, y otros proveedores.
"""

from fastapi import FastAPI, HTTPException, Response, Header, Form, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel
from orchestrator import process_message
from info_agent import agent
from logging_config import logger
import uvicorn
import json
import os
from dotenv import load_dotenv

# ===== VALIDACIÓN DE SECRETS =====
load_dotenv()

REQUIRED_SECRETS = ["OPENAI_API_KEY"]
missing = [key for key in REQUIRED_SECRETS if not os.getenv(key)]
if missing:
    raise EnvironmentError(
        f"❌ Missing required secrets: {', '.join(missing)}\n"
        f"💡 Copy .env.example to .env and add your API keys"
    )


# ===== FASTAPI APP =====

app = FastAPI(
    title="Sofía - Asistente Virtual",
    description="API de Inmobiliaria Proteger para procesamiento de mensajes",
    version="1.0.0"
)


# ===== MIDDLEWARE PARA UTF-8 =====

@app.middleware("http")
async def add_charset_to_content_type(request, call_next):
    """Asegurar que todas las respuestas tengan charset=utf-8."""
    response = await call_next(request)
    if "application/json" in response.headers.get("content-type", ""):
        response.headers["content-type"] = "application/json; charset=utf-8"
    return response


# ===== MODELOS PYDANTIC =====

class MessageRequest(BaseModel):
    """Modelo para solicitud de mensaje."""
    session_id: str
    message: str

    class Config:
        json_schema_extra = {
            "example": {
                "session_id": "user_12345",
                "message": "¿Cuál es la misión de la empresa?"
            }
        }


class MessageResponse(BaseModel):
    """Modelo para respuesta de mensaje."""
    response: str
    status: str

    class Config:
        json_schema_extra = {
            "example": {
                "response": "La misión de Inmobiliaria Proteger es...",
                "status": "reception_start"
            }
        }


# ===== ENDPOINTS =====

@app.post("/webhook", response_model=MessageResponse)
async def webhook(request: MessageRequest):
    """
    Endpoint principal para recibir mensajes de usuarios.
    Compatible con Twilio, WhatsApp Business API, etc.
    """
    try:
        logger.info(f"[WEBHOOK] Mensaje recibido de session_id: {request.session_id}")
        logger.debug(f"[WEBHOOK] Contenido: '{request.message}'")

        # DELEGAR A ORCHESTRATOR (misma lógica que CLI)
        result = process_message(request.session_id, request.message)

        logger.info(f"[WEBHOOK] Respuesta enviada. Status: {result['status']}")

        return MessageResponse(
            response=result["response"],
            status=str(result["status"])
        )

    except Exception as e:
        logger.error(f"[WEBHOOK] Error procesando mensaje: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Internal server error. Por favor, intenta de nuevo."
        )


@app.post("/webhook/twilio")
async def twilio_webhook(
    From: str = Form(...),
    Body: str = Form(...),
    MessageSid: str = Form(None),
    ProfileName: str = Form(None)
):
    """
    Webhook específico para Twilio WhatsApp.

    Twilio envía los mensajes como form data con los siguientes campos:
    - From: Número del usuario (ej: whatsapp:+573001234567)
    - Body: Contenido del mensaje
    - MessageSid: ID del mensaje de Twilio
    - ProfileName: Nombre del perfil de WhatsApp del usuario

    Retorna: TwiML response (XML) para que Twilio pueda procesar la respuesta
    """
    try:
        # Extraer session_id del número de teléfono (sin el prefijo whatsapp:)
        session_id = From.replace("whatsapp:", "")

        logger.info(f"[TWILIO] Mensaje de WhatsApp recibido")
        logger.info(f"[TWILIO] De: {From} (Profile: {ProfileName})")
        logger.info(f"[TWILIO] MessageSid: {MessageSid}")
        logger.info(f"[TWILIO] Body: {Body[:100]}...")

        # DELEGAR A ORCHESTRATOR (misma lógica que webhook JSON)
        result = process_message(session_id, Body)

        logger.info(f"[TWILIO] Respuesta generada. Status: {result['status']}")

        # Twilio espera respuesta en formato TwiML (XML)
        twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{result['response']}</Message>
</Response>"""

        return Response(
            content=twiml_response,
            media_type="application/xml"
        )

    except Exception as e:
        logger.error(f"[TWILIO] Error procesando mensaje: {e}", exc_info=True)

        # Responder con mensaje de error en TwiML
        error_twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>Lo siento, ocurrió un error procesando tu mensaje. Por favor, intenta de nuevo.</Message>
</Response>"""

        return Response(
            content=error_twiml,
            media_type="application/xml"
        )


@app.get("/health")
async def health_check():
    """
    Health check mejorado para Railway con verificación de dependencias.
    Crea conexiones independientes para evitar import circular.
    """
    health_status = {
        "status": "ok",
        "service": "Sofia - Asistente Virtual",
        "version": "1.0.0"
    }

    # Verificar conexión a Redis
    try:
        import redis
        redis_url = os.getenv("REDIS_URL")
        if redis_url:
            client = redis.from_url(redis_url, decode_responses=True)
            client.ping()
            client.close()
            health_status["redis"] = "connected"
        else:
            health_status["redis"] = "not_configured"
    except Exception as e:
        health_status["redis"] = f"error: {str(e)[:50]}"

    # Verificar conexión a PostgreSQL
    try:
        import psycopg
        database_url = os.getenv("DATABASE_URL")
        if database_url:
            with psycopg.connect(database_url) as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT 1")
            health_status["postgres"] = "connected"
        else:
            health_status["postgres"] = "not_configured"
    except Exception as e:
        health_status["postgres"] = f"error: {str(e)[:50]}"

    return health_status


@app.get("/")
async def root():
    """
    Endpoint raíz con información básica del servicio.
    """
    return {
        "service": "Sofía - Asistente Virtual de Inmobiliaria Proteger",
        "endpoints": {
            "webhook": "/webhook (POST)",
            "health": "/health (GET)",
            "admin_reload": "/admin/reload-kb (POST)",
            "docs": "/docs (GET)"
        }
    }


# ===== ENDPOINTS ADMINISTRATIVOS =====

@app.post("/admin/reload-kb")
async def reload_knowledge_base(x_api_key: str = Header(None, alias="X-API-Key")):
    """
    Endpoint administrativo para recargar la base de conocimiento RAG.

    Permite actualizar los documentos en memoria sin reiniciar el servidor.
    Útil para aplicar cambios en knowledge_base/ de forma inmediata.

    Requiere autenticación mediante X-API-Key header.
    """
    # Verificar autenticación
    admin_api_key = os.getenv("ADMIN_API_KEY")
    if not admin_api_key or x_api_key != admin_api_key:
        logger.warning(f"[API] Intento de acceso no autorizado a /admin/reload-kb")
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Invalid or missing API key"
        )

    try:
        logger.info("[API] Solicitud de recarga de base de conocimiento recibida (autenticada)")

        # Delegar a InfoAgent
        result = agent.reload_knowledge_base()

        # Verificar resultado
        if result.get("status") == "success":
            logger.info(f"[API] Recarga exitosa: {result.get('files_loaded')} archivos")
            return JSONResponse(
                status_code=200,
                content={
                    "status": "success",
                    "files_loaded": result.get("files_loaded"),
                    "message": result.get("message"),
                    "timestamp": None  # Se puede añadir datetime.now() si se requiere
                }
            )
        else:
            # Caso de error controlado desde RAGService
            logger.error(f"[API] Error en recarga: {result.get('message')}")
            raise HTTPException(
                status_code=500,
                detail=result.get("message", "Error desconocido al recargar")
            )

    except HTTPException:
        # Re-lanzar excepciones HTTP ya manejadas
        raise
    except Exception as e:
        # Capturar errores inesperados
        logger.error(f"[API] Error crítico en /admin/reload-kb: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error interno al intentar recargar: {str(e)}"
        )


# ===== ENTRYPOINT =====

if __name__ == "__main__":
    import os
    
    # Railway proporciona $PORT, fallback a 8000 para desarrollo local
    port = int(os.getenv("PORT", "8000"))
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,  # ← Dinámico
        log_level="info"
    )