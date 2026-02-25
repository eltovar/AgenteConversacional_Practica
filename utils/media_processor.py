# utils/media_processor.py
"""
Procesador unificado de multimedia para WhatsApp.

Maneja:
- Descarga de archivos desde Twilio
- Subida permanente a Cloudinary
- Transcripción de audios con Whisper
- Análisis de imágenes con GPT-4o-mini

ARQUITECTURA:
- Cliente envía audio/imagen → Twilio → Cloudinary + Transcripción/Análisis
- Asesora envía desde panel → Cloudinary → Twilio → Cliente
"""

import os
import time
import httpx
from io import BytesIO
from typing import Optional, Dict, Any

import cloudinary
import cloudinary.uploader
from openai import AsyncOpenAI

from logging_config import logger

# ============================================================================
# CONFIGURACIÓN
# ============================================================================

# Cloudinary
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("API_KEY_CLOUDINARY"),
    api_secret=os.getenv("API_SECRET_CLOUDINARY"),
    secure=True
)

# OpenAI
client_openai = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Twilio auth para descargar media
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")


class MediaProcessor:
    """
    Procesador unificado de multimedia.

    Métodos principales:
    - process_incoming_media(): Para archivos que envía el CLIENTE
    - upload_outgoing_media(): Para archivos que envía la ASESORA
    """

    # =========================================================================
    # DESCARGA DESDE TWILIO
    # =========================================================================

    @staticmethod
    async def download_twilio_media(url: str) -> bytes:
        """
        Descarga contenido multimedia desde Twilio.

        Twilio requiere autenticación básica para acceder a los archivos.
        """
        auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, auth=auth)

            if response.status_code != 200:
                logger.error(f"[MediaProcessor] Error descargando de Twilio: {response.status_code}")
                raise Exception(f"Error descargando media de Twilio: {response.status_code}")

            logger.info(f"[MediaProcessor] Media descargada: {len(response.content)} bytes")
            return response.content

    # =========================================================================
    # SUBIDA A CLOUDINARY
    # =========================================================================

    @staticmethod
    async def upload_to_cloudinary(
        file_bytes: bytes,
        folder: str,
        resource_type: str = "auto",
        phone: Optional[str] = None
    ) -> str:
        """
        Sube archivo a Cloudinary y devuelve URL permanente.

        Args:
            file_bytes: Contenido del archivo en bytes
            folder: Carpeta en Cloudinary (ej: "audios", "imagenes")
            resource_type: "image", "video" (para audio), o "auto"
            phone: Teléfono para generar public_id único

        Returns:
            URL segura del archivo en Cloudinary
        """
        try:
            # Generar public_id único
            public_id = f"{phone}_{int(time.time())}" if phone else str(int(time.time()))

            # Subir a Cloudinary
            result = cloudinary.uploader.upload(
                file_bytes,
                folder=f"inmobiliaria/{folder}",
                public_id=public_id,
                resource_type=resource_type
            )

            url = result.get("secure_url", "")
            logger.info(f"[MediaProcessor] Subido a Cloudinary: {url}")
            return url

        except Exception as e:
            logger.error(f"[MediaProcessor] Error subiendo a Cloudinary: {e}")
            return ""

    # =========================================================================
    # TRANSCRIPCIÓN DE AUDIO (WHISPER)
    # =========================================================================

    @staticmethod
    async def transcribe_audio(audio_bytes: bytes) -> str:
        """
        Transcribe audio usando OpenAI Whisper.

        Args:
            audio_bytes: Audio en bytes (OGG, MP3, WAV, etc.)

        Returns:
            Texto transcrito en español
        """
        try:
            buffer = BytesIO(audio_bytes)
            buffer.name = "audio.ogg"  # Whisper necesita extensión para detectar formato

            transcript = await client_openai.audio.transcriptions.create(
                model="whisper-1",
                file=buffer,
                language="es"
            )

            text = transcript.text
            logger.info(f"[MediaProcessor] Transcripción Whisper: {text[:100]}...")
            return text

        except Exception as e:
            logger.error(f"[MediaProcessor] Error en transcripción Whisper: {e}")
            return ""

    # =========================================================================
    # ANÁLISIS DE IMAGEN (GPT-4o-mini)
    # =========================================================================

    @staticmethod
    async def analyze_image(image_url: str) -> str:
        """
        Analiza imagen usando GPT-4o-mini para extraer información relevante.

        Útil para:
        - Detectar códigos de inmuebles en fotos
        - Identificar tipo de propiedad
        - Extraer características visibles

        Args:
            image_url: URL de la imagen en Cloudinary

        Returns:
            Descripción/análisis de la imagen
        """
        try:
            response = await client_openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Analiza esta imagen brevemente. "
                                    "Si es una propiedad inmobiliaria, identifica: "
                                    "tipo (casa, apartamento, local), características visibles, "
                                    "y cualquier código o texto visible. "
                                    "Responde en máximo 2 oraciones."
                                )
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": image_url}
                            }
                        ],
                    }
                ],
                max_tokens=200,
            )

            analysis = response.choices[0].message.content
            logger.info(f"[MediaProcessor] Análisis de imagen: {analysis[:100]}...")
            return analysis

        except Exception as e:
            logger.error(f"[MediaProcessor] Error analizando imagen: {e}")
            return ""

    # =========================================================================
    # FLUJO COMPLETO: MENSAJE ENTRANTE DEL CLIENTE
    # =========================================================================

    async def process_incoming_media(
        self,
        media_url: str,
        content_type: str,
        phone: str
    ) -> Dict[str, Any]:
        """
        Procesa multimedia enviada por el CLIENTE.

        Flujo:
        1. Descargar de Twilio
        2. Subir a Cloudinary (almacenamiento permanente)
        3. Si es audio: transcribir con Whisper
        4. Si es imagen: analizar con GPT-4o-mini (opcional)

        Args:
            media_url: URL temporal de Twilio
            content_type: Tipo MIME (audio/ogg, image/jpeg, etc.)
            phone: Teléfono del cliente

        Returns:
            {
                "permanent_url": str,      # URL en Cloudinary
                "transcription": str,      # Solo si es audio
                "analysis": str,           # Solo si es imagen
                "media_type": str,         # "audio" o "image"
                "body_for_ai": str         # Texto para que Sofía procese
            }
        """
        result = {
            "permanent_url": "",
            "transcription": "",
            "analysis": "",
            "media_type": "",
            "body_for_ai": ""
        }

        try:
            # Determinar tipo de media
            is_audio = "audio" in content_type.lower()
            is_image = "image" in content_type.lower()

            if is_audio:
                result["media_type"] = "audio"
                folder = "audios"
                resource_type = "video"  # Cloudinary trata audios como video
            elif is_image:
                result["media_type"] = "image"
                folder = "imagenes"
                resource_type = "image"
            else:
                result["media_type"] = "file"
                folder = "archivos"
                resource_type = "auto"

            # 1. Descargar de Twilio
            media_bytes = await self.download_twilio_media(media_url)

            # 2. Subir a Cloudinary
            result["permanent_url"] = await self.upload_to_cloudinary(
                media_bytes, folder, resource_type, phone
            )

            # 3. Procesamiento específico por tipo
            if is_audio:
                # Transcribir para que Sofía "escuche"
                result["transcription"] = await self.transcribe_audio(media_bytes)
                result["body_for_ai"] = f"[Audio del cliente]: {result['transcription']}"

            elif is_image:
                # Analizar imagen (opcional - puede ser costoso)
                # Solo analizamos si queremos que Sofía "vea" la imagen
                result["analysis"] = await self.analyze_image(result["permanent_url"])
                result["body_for_ai"] = f"[Imagen del cliente]: {result['analysis']}"

            logger.info(
                f"[MediaProcessor] Media procesada: type={result['media_type']}, "
                f"url={result['permanent_url'][:50]}..."
            )

        except Exception as e:
            logger.error(f"[MediaProcessor] Error procesando media entrante: {e}")
            result["body_for_ai"] = "[El cliente envió un archivo que no pudo procesarse]"

        return result

    # =========================================================================
    # FLUJO: ENVÍO DE MULTIMEDIA POR ASESORA
    # =========================================================================

    async def upload_outgoing_media(
        self,
        file_bytes: bytes,
        content_type: str,
        phone: str
    ) -> str:
        """
        Sube archivo enviado por la ASESORA a Cloudinary.

        Args:
            file_bytes: Contenido del archivo
            content_type: Tipo MIME (incluye audio/webm de grabaciones del navegador)
            phone: Teléfono destino (para naming)

        Returns:
            URL permanente en Cloudinary
        """
        content_lower = content_type.lower()
        # webm es el formato nativo de MediaRecorder en navegadores Chrome/Edge
        is_audio = "audio" in content_lower or "webm" in content_lower
        folder = "audios_asesores" if is_audio else "imagenes_asesores"
        resource_type = "video" if is_audio else "image"

        return await self.upload_to_cloudinary(
            file_bytes, folder, resource_type, phone
        )


# Instancia singleton
media_processor = MediaProcessor()