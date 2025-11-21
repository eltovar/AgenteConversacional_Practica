# app.py (FASTAPI WEBHOOK BACKEND)
"""
Servidor FastAPI para recibir mensajes vía webhooks.
Delega toda la lógica de negocio a orchestrator.py.
Compatible con Twilio, WhatsApp Business API, y otros proveedores.
"""

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from orchestrator import process_message
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


@app.get("/health")
async def health_check():
    """
    Endpoint de health check para monitoreo.
    """
    return {
        "status": "ok",
        "service": "Sofia - Asistente Virtual",
        "version": "1.0.0"
    }


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
            "docs": "/docs (GET)"
        }
    }


# ===== ENTRYPOINT =====

if __name__ == "__main__":
    # Para desarrollo local: python app.py
    # Para producción: gunicorn -w 4 -k uvicorn.workers.UvicornWorker app:app
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )