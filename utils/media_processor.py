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
BUNNY_ENDPOINT = os.getenv("BUNNY_STORAGE_ENDPOINT", "ny.storage.bunnycdn.com")


def _get_bunny_pull_zone() -> str:
    """
    Obtiene y valida la URL del Pull Zone de Bunny.net.
    
    IMPORTANTE: Twilio requiere URLs con protocolo https://
    Si la variable de entorno no lo incluye, lo agregamos.
    
    Returns:
        URL normalizada con https:// o cadena vacía si no está configurada.
    """
    url = os.getenv("BUNNY_PULL_ZONE_URL", "").strip().rstrip('/')
    
    if not url:
        logger.warning("[BunnyStorage] BUNNY_PULL_ZONE_URL no configurada - multimedia puede fallar")
        return ""
    
    # Asegurar que tenga protocolo https://
    if not url.startswith("https://"):
        if url.startswith("http://"):
            url = url.replace("http://", "https://", 1)
            logger.info(f"[BunnyStorage] URL normalizada de http a https: {url}")
        else:
            url = f"https://{url}"
            logger.info(f"[BunnyStorage] URL normalizada con https://: {url}")
    
    return url


BUNNY_PULL_ZONE = _get_bunny_pull_zone()


# ============================================================================
# DETECCIÓN DE FORMATO POR MAGIC BYTES
# ============================================================================

def detect_audio_format_by_magic_bytes(file_bytes: bytes) -> str:
    """
    Detecta el formato real del audio analizando su contenido (magic bytes),
    NO confiando en el Content-Type enviado por Twilio.
    """
    if not file_bytes or len(file_bytes) < 4:
        logger.warning(f"[MediaProcessor] Archivo muy pequeño ({len(file_bytes)} bytes), asumiendo OGG")
        return "ogg"
    
    # Leer magic bytes
    magic = file_bytes[:12]
    
    # ⚠️ DETECTAR HTML/ERROR - Twilio a veces devuelve página de error
    if (magic.startswith(b"<!DOCTYPE") or 
        magic.startswith(b"<html") or 
        magic.startswith(b"<HTML") or
        magic.startswith(b"<?xml")):
        logger.error(f"[MediaProcessor] ❌ Twilio devolvió HTML/XML en lugar de audio!")
        logger.error(f"    Primeros 100 bytes: {file_bytes[:100]}")
        raise Exception(
            "Error descargando audio de Twilio: El servidor devolvió HTML/XML. "
            "Probable causa: URL expirada, archivo eliminado, o error de autenticación en Twilio"
        )
    
    # OGG Vorbis/Opus
    if magic[:4] == b"OggS":
        logger.debug("[MediaProcessor] Formato detectado: OGG (magic bytes)")
        return "ogg"
    
    # FLAC
    if magic[:4] == b"fLaC":
        logger.debug("[MediaProcessor] Formato detectado: FLAC (magic bytes)")
        return "flac"
    
    # WAV
    if magic[:4] == b"RIFF" and magic[8:12] == b"WAVE":
        logger.debug("[MediaProcessor] Formato detectado: WAV (magic bytes)")
        return "wav"
    
    # WebM
    if magic[:4] == b"\x1A\x45\xDF\xA3":
        logger.debug("[MediaProcessor] Formato detectado: WebM (magic bytes)")
        return "webm"
    
    # MP3 (MPEG frames)
    if magic[0:2] == b"\xFF\xFB" or magic[0:2] == b"\xFF\xFA":
        logger.debug("[MediaProcessor] Formato detectado: MP3 (MPEG frame sync)")
        return "mp3"
    
    # ID3 tag (MP3 con metadatos)
    if magic[:3] == b"ID3":
        logger.debug("[MediaProcessor] Formato detectado: MP3 (ID3 tag)")
        return "mp3"
    
    # M4A/MP4 (buscar "ftyp" en los primeros 12 bytes)
    if b"ftyp" in magic:
        logger.debug("[MediaProcessor] Formato detectado: M4A/MP4 (magic bytes)")
        return "mp4"
    
    # Default: OGG (formato que Twilio usa para WhatsApp)
    logger.warning(f"[MediaProcessor] Formato no identificado (primeros 12 bytes: {magic.hex()}), asumiendo OGG")
    return "ogg"


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
                    # BUNNY_PULL_ZONE ya incluye https:// (del .env)
                    public_url = f"{BUNNY_PULL_ZONE}/{folder}/{clean_filename}"
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

    async def transcribe_audio(self, audio_bytes: bytes, audio_format: str = "ogg") -> str:
        """
        Transcribe audio usando OpenAI Whisper.
        """
        try:
            # Asegurar que el formato sea válido
            valid_formats = ["ogg", "webm", "mp4", "wav", "mpeg", "mp3", "flac", "m4a"]
            audio_format = audio_format.lower().strip(".")
            
            if audio_format not in valid_formats:
                logger.warning(f"[Whisper] Formato desconocido: {audio_format}, asumiendo ogg")
                audio_format = "ogg"
            
            # Mapear mpeg a mp3 para Whisper
            if audio_format == "mpeg":
                audio_format = "mp3"

            buffer = BytesIO(audio_bytes)
            buffer.name = f"audio.{audio_format}"  # Whisper usa esta extensión

            logger.debug(f"[Whisper] Transcribiendo con formato: {audio_format} (buffer.name={buffer.name})")
            
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
                # Detectar extensión correcta basándose en content_type
                if "ogg" in content_lower or "vorbis" in content_lower:
                    extension = ".ogg"
                elif "webm" in content_lower:
                    extension = ".webm"
                elif "mp4" in content_lower or "m4a" in content_lower or "aac" in content_lower:
                    extension = ".mp4"
                elif "wav" in content_lower:
                    extension = ".wav"
                elif "mpeg" in content_lower or "mp3" in content_lower:
                    extension = ".mp3"
                elif "flac" in content_lower:
                    extension = ".flac"
                else:
                    extension = ".ogg"  # Default a ogg (formato de Twilio)
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

            # IMPORTANTE: Detectar formato REAL del archivo por magic bytes
            # NO confiar en Content-Type de Twilio
            if is_audio:
                try:
                    actual_audio_format = detect_audio_format_by_magic_bytes(media_bytes)
                    logger.info(f"[MediaProcessor] Formato detectado por magic bytes: {actual_audio_format} (Content-Type reportaba: {content_type})")
                    extension = f".{actual_audio_format}"
                except Exception as e:
                    logger.error(f"[MediaProcessor] ❌ Error detectando formato de audio: {e}")
                    raise  # Re-lanzar la excepción para que se maneje en el bloque except general
            
            # 2. Generar nombre único y subir a Bunny.net
            clean_phone = phone.replace("+", "").replace(" ", "")
            filename = f"{clean_phone}_{int(time.time())}{extension}"
            result["permanent_url"] = await self.upload_to_bunny(media_bytes, folder, filename)

            # 3. Procesamiento específico por tipo
            if is_audio:
                # Transcribir para que Sofía "escuche"
                # Usar el formato detectado por magic bytes, no el Content-Type
                transcription = await self.transcribe_audio(media_bytes, actual_audio_format)
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

        # Formatos de audio soportados (webm, ogg, wav del panel de asesoras)
        is_audio = "audio" in content_lower or "webm" in content_lower or "ogg" in content_lower or "wav" in content_lower
        
        if is_audio:
            folder = "audios_asesores"
            # Detectar extensión correcta basándose en content_type
            # PRIORIDAD: Formatos compatibles con WhatsApp primero
            if "ogg" in content_lower or "vorbis" in content_lower:
                extension = ".ogg"  # Ideal para WhatsApp
            elif "wav" in content_lower:
                extension = ".wav"  # Compatible con WhatsApp (convertido desde WebM)
            elif "mp4" in content_lower or "m4a" in content_lower or "aac" in content_lower:
                extension = ".mp4"
            elif "mpeg" in content_lower or "mp3" in content_lower:
                extension = ".mp3"
            elif "webm" in content_lower:
                # WebM NO es compatible con WhatsApp directamente
                # Pero lo guardamos por si acaso (el cliente debería convertir a WAV)
                extension = ".webm"
                logger.warning("[MediaProcessor] Audio WebM recibido - puede no reproducirse en WhatsApp")
            elif "flac" in content_lower:
                extension = ".flac"
            else:
                extension = ".ogg"  # Default a ogg
        else:
            folder = "imagenes_asesores"
            extension = ".jpg"

        clean_phone = phone.replace("+", "").replace(" ", "")
        filename = f"{clean_phone}_{int(time.time())}{extension}"

        return await self.upload_to_bunny(file_bytes, folder, filename)


# Instancia singleton
media_processor = MediaProcessor()