import os
import time
import httpx
from io import BytesIO
import cloudinary
import cloudinary.uploader
from openai import AsyncOpenAI
from logging_config import logger

# Configuración de Cloudinary
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("API_KEY_CLOUDINARY"),
    api_secret=os.getenv("API_SECRET_CLOUDINARY"),
    secure=True
)

client_openai = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

async def download_audio(url: str) -> bytes:
    """Descarga audio desde Twilio con autenticación."""
    auth = (os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
    async with httpx.AsyncClient() as client:
        response = await client.get(url, auth=auth)
        if response.status_code != 200:
            logger.error(f"[AudioProcessor] Error descargando audio: {response.status_code}")
            raise Exception(f"Error al descargar audio de Twilio: {response.status_code}")
        logger.info(f"[AudioProcessor] Audio descargado: {len(response.content)} bytes")
        return response.content
    
async def upload_to_permanent_storage(audio_bytes: bytes, phone: str) -> str:
    # Convertimos bytes a un objeto "file-like"
    file_to_upload = BytesIO(audio_bytes)
    
    result = cloudinary.uploader.upload(
        file_to_upload,
        resource_type="video", # Cloudinary trata audios como videos sin imagen
        folder="proteger_audios",
        public_id=f"{phone}_{int(time.time())}",
        format="mp3" # Conversión automática a MP3
    )
    return result["secure_url"]

async def transcribe_with_whisper(audio_bytes: bytes) -> str:
    buffer = BytesIO(audio_bytes)
    buffer.name = "audio.ogg" # Necesario para que Whisper identifique el formato inicial
    
    transcript = await client_openai.audio.transcriptions.create(
        model="whisper-1",
        file=buffer,
        language="es" # Optimiza para español colombiano
    )
    return transcript.text

async def process_whatsapp_audio(media_url: str, phone: str):
    """
    Flujo completo: Descarga -> Sube Permanente -> Transcribe
    """
    # 1. Descargar
    audio_data = await download_audio(media_url)
    
    # 2. Procesar en paralelo (opcional para velocidad) o secuencial
    permanent_url = await upload_to_permanent_storage(audio_data, phone)
    text = await transcribe_with_whisper(audio_data)
    
    return {
        "url_permanente": permanent_url,
        "transcripcion": text
    }

