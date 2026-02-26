# utils/media_processor.py
"""
Procesador unificado de multimedia para WhatsApp.

Maneja:
- Descarga de archivos desde Twilio
- Subida permanente a Bunny.net Storage (CDN)
- Transcripción de audios con Whisper
- Análisis de imágenes con GPT-4o-mini

ARQUITECTURA:
- Cliente envía audio/imagen → Twilio → Bunny.net + Transcripción/Análisis
- Asesora envía desde panel → Bunny.net → Twilio → Cliente

MIGRACIÓN: Cloudinary → Bunny.net (más económico, mismo rendimiento)
"""

import os
import time
import httpx
from io import BytesIO
from typing import Optional, Dict, Any

from openai import AsyncOpenAI

from logging_config import logger

# ============================================================================
# CONFIGURACIÓN
# ============================================================================

# OpenAI para Whisper y GPT-4o-mini
client_openai = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Twilio auth para descargar media
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

# Bunny.net Storage Configuration
BUNNY_STORAGE_ZONE = os.getenv("BUNNY_STORAGE_ZONE_NAME")
BUNNY_API_KEY = os.getenv("BUNNY_STORAGE_API_KEY")
BUNNY_PULL_ZONE = os.getenv("BUNNY_PULL_ZONE_URL", "").rstrip('/')
BUNNY_ENDPOINT = os.getenv("BUNNY_STORAGE_ENDPOINT", "ny.storage.bunnycdn.com")


class MediaProcessor:
    """
    Procesador unificado de multimedia.

    Métodos principales:
    - process_incoming_media(): Para archivos que envía el CLIENTE
    - upload_outgoing_media(): Para archivos que envía la ASESORA
    """

    def __init__(self):
        """Inicializa el procesador con credenciales de Twilio."""
        self.twilio_auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    # =========================================================================
    # DESCARGA DESDE TWILIO
    # =========================================================================

    async def download_twilio_media(self, url: str) -> bytes:
        """
        Descarga contenido multimedia desde Twilio.

        Twilio requiere autenticación básica y puede usar redirecciones 302.
        """
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url, auth=self.twilio_auth)

            if response.status_code != 200:
                logger.error(f"[MediaProcessor] Error descargando de Twilio: {response.status_code}")
                raise Exception(f"Error descargando media de Twilio: {response.status_code}")

            logger.info(f"[MediaProcessor] Media descargada de Twilio: {len(response.content)} bytes")
            return response.content

    # =========================================================================
    # SUBIDA A BUNNY.NET STORAGE
    # =========================================================================

    async def upload_to_bunny(
        self,
        file_bytes: bytes,
        folder: str,
        filename: str
    ) -> str:
        """
        Sube archivo a Bunny.net Storage y devuelve URL pública del CDN.

        Args:
            file_bytes: Contenido del archivo en bytes
            folder: Carpeta en Bunny Storage (ej: "audios_clientes", "imagenes_asesores")
            filename: Nombre del archivo (ej: "+573001234567_1234567890.mp3")

        Returns:
            URL pública del archivo en el Pull Zone (CDN)
        """
        # Limpiar nombre de archivo para URLs seguras
        clean_filename = filename.replace(" ", "_").replace(":", "-").replace("+", "")

        # URL del Storage Zone (donde se sube)
        storage_url = f"https://{BUNNY_ENDPOINT}/{BUNNY_STORAGE_ZONE}/{folder}/{clean_filename}"

        headers = {
            "AccessKey": BUNNY_API_KEY,
            "Content-Type": "application/octet-stream",
            "accept": "application/json"
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.put(storage_url, content=file_bytes, headers=headers)

                if response.status_code in [200, 201]:
                    # URL pública del Pull Zone (CDN - desde donde se sirve)
                    # Twilio requiere URLs completas con https://
                    public_url = f"https://{BUNNY_PULL_ZONE}/{folder}/{clean_filename}"
                    logger.info(f"[BunnyStorage] Archivo subido exitosamente: {public_url}")
                    return public_url
                else:
                    logger.error(
                        f"[BunnyStorage] Error subiendo archivo: "
                        f"status={response.status_code}, response={response.text[:200]}"
                    )
                    raise Exception(f"Error en Bunny Storage: {response.status_code}")

        except httpx.TimeoutException:
            logger.error("[BunnyStorage] Timeout subiendo archivo a Bunny.net")
            raise Exception("Timeout subiendo archivo a Bunny.net")
        except Exception as e:
            logger.error(f"[BunnyStorage] Error inesperado: {e}")
            raise

    # =========================================================================
    # TRANSCRIPCIÓN DE AUDIO (WHISPER)
    # =========================================================================

    async def transcribe_audio(self, audio_bytes: bytes) -> str:
        """
        Transcribe audio usando OpenAI Whisper.

        Args:
            audio_bytes: Audio en bytes (OGG, MP3, WAV, WEBM, etc.)

        Returns:
            Texto transcrito en español
        """
        try:
            buffer = BytesIO(audio_bytes)
            buffer.name = "audio.mp3"  # Whisper necesita extensión para detectar formato

            transcript = await client_openai.audio.transcriptions.create(
                model="whisper-1",
                file=buffer,
                language="es"
            )

            text = transcript.text

            # --- MITIGACIÓN DE ALUCINACIONES DE WHISPER ---
            # Whisper a veces "alucina" texto cuando el audio está vacío o tiene ruido
            phrases_to_ignore = [
                "Subtítulos realizados por",
                "Amara.org",
                "you",
                "¡Gracias!",
                "Watching",
                "Gracias por ver",
                "Suscríbete",
                "Thanks for watching"
            ]

            if any(phrase.lower() in text.lower() for phrase in phrases_to_ignore) and len(text) < 60:
                logger.warning(f"[Whisper] Posible alucinación detectada: '{text}'")
                return "[Audio sin contenido verbal claro]"

            logger.info(f"[Whisper] Transcripción exitosa: {text[:100]}...")
            return text

        except Exception as e:
            logger.error(f"[Whisper] Error en transcripción: {e}")
            return ""

    # =========================================================================
    # ANÁLISIS DE IMAGEN (GPT-4o-mini)
    # =========================================================================

    async def analyze_image(self, image_url: str) -> str:
        """
        Analiza imagen usando GPT-4o-mini para extraer información relevante.

        Útil para:
        - Detectar códigos de inmuebles en fotos
        - Identificar tipo de propiedad
        - Extraer características visibles

        Args:
            image_url: URL pública de la imagen en Bunny.net CDN

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
            logger.info(f"[GPT-Vision] Análisis de imagen: {analysis[:100]}...")
            return analysis

        except Exception as e:
            logger.error(f"[GPT-Vision] Error analizando imagen: {e}")
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
        1. Descargar de Twilio (siguiendo redirecciones 302)
        2. Subir a Bunny.net Storage (CDN permanente)
        3. Si es audio: transcribir con Whisper
        4. Si es imagen: analizar con GPT-4o-mini

        Args:
            media_url: URL temporal de Twilio
            content_type: Tipo MIME (audio/ogg, image/jpeg, etc.)
            phone: Teléfono del cliente (para naming)

        Returns:
            {
                "permanent_url": str,      # URL en Bunny.net CDN
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
            content_lower = content_type.lower()
            is_audio = "audio" in content_lower or "ogg" in content_lower
            is_image = "image" in content_lower

            if is_audio:
                result["media_type"] = "audio"
                folder = "audios_clientes"
                extension = ".ogg"
            elif is_image:
                result["media_type"] = "image"
                folder = "imagenes_clientes"
                extension = ".jpg"
            else:
                result["media_type"] = "file"
                folder = "archivos_clientes"
                extension = ".bin"

            # 1. Descargar de Twilio
            media_bytes = await self.download_twilio_media(media_url)

            # 2. Generar nombre único y subir a Bunny.net
            clean_phone = phone.replace("+", "").replace(" ", "")
            filename = f"{clean_phone}_{int(time.time())}{extension}"
            result["permanent_url"] = await self.upload_to_bunny(media_bytes, folder, filename)

            # 3. Procesamiento específico por tipo
            if is_audio:
                # Transcribir para que Sofía "escuche"
                transcription = await self.transcribe_audio(media_bytes)
                result["transcription"] = transcription

                if transcription and transcription != "[Audio sin contenido verbal claro]":
                    result["body_for_ai"] = f"[Transcripción de Audio]: {transcription}"
                else:
                    result["body_for_ai"] = "[El cliente envió un audio sin contenido verbal claro]"

            elif is_image:
                # Analizar imagen para que Sofía "vea"
                analysis = await self.analyze_image(result["permanent_url"])
                result["analysis"] = analysis

                if analysis:
                    result["body_for_ai"] = f"[Imagen del cliente]: {analysis}"
                else:
                    result["body_for_ai"] = "[El cliente envió una imagen]"

            else:
                result["body_for_ai"] = "[El cliente envió un archivo]"

            logger.info(
                f"[MediaProcessor] Media procesada exitosamente: "
                f"type={result['media_type']}, url={result['permanent_url'][:60]}..."
            )

        except Exception as e:
            logger.error(f"[MediaProcessor] Error procesando media entrante: {e}")
            result["body_for_ai"] = "[Error procesando archivo multimedia]"

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
        Sube archivo enviado por la ASESORA desde el Panel a Bunny.net.

        Args:
            file_bytes: Contenido del archivo
            content_type: Tipo MIME (incluye audio/webm de grabaciones del navegador)
            phone: Teléfono destino (para naming)

        Returns:
            URL pública en Bunny.net CDN
        """
        content_lower = content_type.lower()

        # webm es el formato nativo de MediaRecorder en navegadores Chrome/Edge
        is_audio = "audio" in content_lower or "webm" in content_lower or "ogg" in content_lower
        folder = "audios_asesores" if is_audio else "imagenes_asesores"
        extension = ".ogg" if is_audio else ".jpg"

        clean_phone = phone.replace("+", "").replace(" ", "")
        filename = f"{clean_phone}_{int(time.time())}{extension}"

        return await self.upload_to_bunny(file_bytes, folder, filename)


# Instancia singleton
media_processor = MediaProcessor()