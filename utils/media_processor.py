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
import subprocess
import tempfile
from io import BytesIO
from typing import Optional, Dict, Any, Tuple

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

def detect_image_format_by_magic_bytes(file_bytes: bytes) -> tuple[str, str]:
    """
    Detecta el formato real de una imagen analizando sus magic bytes.
    
    Returns:
        Tuple de (formato, content_type). Ej: ("jpg", "image/jpeg")
    """
    if not file_bytes or len(file_bytes) < 8:
        logger.warning(f"[MediaProcessor] Imagen muy pequeña ({len(file_bytes)} bytes), asumiendo JPEG")
        return ("jpg", "image/jpeg")
    
    magic = file_bytes[:12]
    
    # JPEG: empieza con FF D8 FF
    if magic[:3] == b"\xFF\xD8\xFF":
        logger.debug("[MediaProcessor] Imagen detectada: JPEG")
        return ("jpg", "image/jpeg")
    
    # PNG: empieza con 89 50 4E 47 0D 0A 1A 0A
    if magic[:8] == b"\x89PNG\r\n\x1a\n":
        logger.debug("[MediaProcessor] Imagen detectada: PNG")
        return ("png", "image/png")
    
    # GIF: empieza con GIF87a o GIF89a
    if magic[:6] in (b"GIF87a", b"GIF89a"):
        logger.debug("[MediaProcessor] Imagen detectada: GIF")
        return ("gif", "image/gif")
    
    # WebP: empieza con RIFF...WEBP
    if magic[:4] == b"RIFF" and magic[8:12] == b"WEBP":
        logger.debug("[MediaProcessor] Imagen detectada: WebP")
        return ("webp", "image/webp")
    
    # BMP: empieza con BM
    if magic[:2] == b"BM":
        logger.debug("[MediaProcessor] Imagen detectada: BMP")
        return ("bmp", "image/bmp")
    
    # Por defecto asumir JPEG (formato más común en WhatsApp)
    logger.warning(f"[MediaProcessor] Formato imagen no identificado (bytes: {magic[:8].hex()}), asumiendo JPEG")
    return ("jpg", "image/jpeg")


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
    # UTILIDADES PARA CONTENT-TYPE Y DETECCIÓN DE FORMATO
    # =========================================================================

    def _get_content_type_from_filename(self, filename: str) -> str:
        """
        Infiere el Content-Type MIME correcto basándose en la extensión del archivo.
        
        IMPORTANTE: Twilio/WhatsApp REQUIEREN Content-Types específicos.
        application/octet-stream causa Error 63019 y 63021.
        """
        extension_map = {
            # Audio
            ".ogg": "audio/ogg",
            ".opus": "audio/ogg",
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".m4a": "audio/mp4",
            ".aac": "audio/aac",
            ".webm": "audio/webm",
            ".flac": "audio/flac",
            # Imágenes
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            # Video
            ".mp4": "video/mp4",
            ".mov": "video/quicktime",
            # Documentos  
            ".pdf": "application/pdf",
        }
        
        # Obtener extensión en minúsculas
        ext = ""
        if "." in filename:
            ext = "." + filename.rsplit(".", 1)[1].lower()
        
        content_type = extension_map.get(ext, "application/octet-stream")
        
        if content_type == "application/octet-stream":
            logger.warning(
                f"[BunnyStorage] ⚠️ Extensión desconocida '{ext}' - "
                f"usando application/octet-stream (puede causar errores en Twilio)"
            )
        
        return content_type

    # =========================================================================
    # SUBIDA A BUNNY.NET STORAGE
    # =========================================================================

    async def upload_to_bunny(
        self,
        file_bytes: bytes,
        folder: str,
        filename: str,
        content_type: str = None
    ) -> str:
        """
        Sube archivo a Bunny.net Storage y devuelve URL pública del CDN.

        IMPORTANTE: 
        - Usa el Content-Type correcto para que Twilio/WhatsApp puedan identificar el archivo
        - Después de subir, verifica que el archivo esté disponible en el Pull Zone (CDN)
          antes de retornar. Esto evita el error 63019 de Twilio donde intenta descargar 
          antes de que el CDN haya propagado.
        
        Args:
            file_bytes: Contenido del archivo
            folder: Carpeta en el Storage Zone
            filename: Nombre del archivo (incluye extensión)
            content_type: MIME type del archivo. Si no se especifica, se infiere de la extensión.
        """
        import asyncio

        # Limpiar nombre de archivo para URLs seguras
        clean_filename = filename.replace(" ", "_").replace(":", "-").replace("+", "")

        # URL del Storage Zone (donde se sube)
        storage_url = f"https://{BUNNY_ENDPOINT}/{BUNNY_STORAGE_ZONE}/{folder}/{clean_filename}"

        # Inferir Content-Type si no se especifica
        if not content_type:
            content_type = self._get_content_type_from_filename(clean_filename)
        
        logger.info(f"[BunnyStorage] Subiendo con Content-Type: {content_type}")

        headers = {
            "AccessKey": BUNNY_API_KEY,
            "Content-Type": content_type,
            "accept": "application/json"
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.put(storage_url, content=file_bytes, headers=headers)

                if response.status_code in [200, 201]:
                    # URL pública del Pull Zone (CDN - desde donde se sirve)
                    public_url = f"{BUNNY_PULL_ZONE}/{folder}/{clean_filename}"
                    logger.info(f"[BunnyStorage] Archivo subido exitosamente: {public_url}")

                    # =========================================================
                    # VERIFICAR DISPONIBILIDAD EN CDN (evita Error 63019)
                    # =========================================================
                    # Bunny.net puede tardar unos segundos en propagar al Pull Zone
                    # Verificamos con HEAD request antes de dar la URL a Twilio

                    max_retries = 5
                    retry_delay = 1.0  # segundos

                    for attempt in range(max_retries):
                        try:
                            head_response = await client.head(
                                public_url,
                                timeout=5.0,
                                follow_redirects=True
                            )

                            if head_response.status_code == 200:
                                logger.info(
                                    f"[BunnyStorage] ✅ CDN verificado (intento {attempt + 1}): "
                                    f"{public_url} - status={head_response.status_code}"
                                )
                                return public_url
                            elif head_response.status_code == 404:
                                logger.warning(
                                    f"[BunnyStorage] ⏳ CDN aún no disponible (intento {attempt + 1}/{max_retries}), "
                                    f"esperando {retry_delay}s..."
                                )
                                await asyncio.sleep(retry_delay)
                                retry_delay *= 1.5  # Backoff exponencial
                            else:
                                logger.warning(
                                    f"[BunnyStorage] CDN respuesta inesperada: {head_response.status_code}"
                                )
                                # Continuar de todas formas, Twilio puede tener éxito
                                return public_url

                        except Exception as verify_err:
                            logger.warning(
                                f"[BunnyStorage] Error verificando CDN (intento {attempt + 1}): {verify_err}"
                            )
                            await asyncio.sleep(retry_delay)
                            retry_delay *= 1.5

                    # Si llegamos aquí, no pudimos verificar pero el upload fue exitoso
                    # Retornamos la URL de todas formas y dejamos que Twilio lo intente
                    logger.warning(
                        f"[BunnyStorage] ⚠️ No se pudo verificar CDN después de {max_retries} intentos. "
                        f"Retornando URL de todas formas: {public_url}"
                    )
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

            # 1. Descargar de Twilio primero (para todos los tipos)
            media_bytes = await self.download_twilio_media(media_url)

            if is_audio:
                result["media_type"] = "audio"
                folder = "audios_clientes"
                
                # IMPORTANTE: Detectar formato REAL por magic bytes
                actual_audio_format = detect_audio_format_by_magic_bytes(media_bytes)
                logger.info(f"[MediaProcessor] Formato audio detectado por magic bytes: {actual_audio_format} (Content-Type reportaba: {content_type})")
                extension = f".{actual_audio_format}"
                
            elif is_image:
                result["media_type"] = "image"
                folder = "imagenes_clientes"
                # Detectar formato real de imagen por magic bytes
                img_format, _ = detect_image_format_by_magic_bytes(media_bytes)
                extension = f".{img_format}"
                logger.info(f"[MediaProcessor] Imagen cliente detectada: formato={img_format}")
            else:
                result["media_type"] = "file"
                folder = "archivos_clientes"
                extension = ".bin"
            
            # 2. Generar nombre único y subir a Bunny.net
            clean_phone = phone.replace("+", "").replace(" ", "")
            filename = f"{clean_phone}_{int(time.time())}{extension}"
            result["permanent_url"] = await self.upload_to_bunny(media_bytes, folder, filename)

            # 3. Procesamiento específico por tipo
            if is_audio:
                # Transcribir para que Sofía "escuche"
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
    # CONVERSIÓN DE AUDIO A OGG/OPUS (PARA WHATSAPP)
    # =========================================================================

    def _convert_audio_to_ogg(self, audio_bytes: bytes, source_format: str = "auto") -> Tuple[bytes, bool]:
        """
        Convierte cualquier formato de audio a OGG (Opus) para compatibilidad con WhatsApp.

        WhatsApp tiene soporte limitado de formatos de audio:
        - OGG/Opus: ✅ Nativo, funciona perfecto
        - MP3: ✅ Funciona
        - M4A/AAC: ⚠️ Soporte inconsistente, puede fallar
        - WebM: ❌ No soportado
        - MP4 audio: ⚠️ Puede fallar dependiendo del codec

        Esta función usa ffmpeg para convertir CUALQUIER formato a OGG/Opus.

        """
        try:
            # Verificar que ffmpeg esté disponible
            try:
                subprocess.run(
                    ["ffmpeg", "-version"],
                    capture_output=True,
                    timeout=5
                )
            except FileNotFoundError:
                logger.warning("[MediaProcessor] ffmpeg no encontrado - no se puede convertir audio a OGG")
                return audio_bytes, False
            except subprocess.TimeoutExpired:
                logger.warning("[MediaProcessor] ffmpeg timeout en verificación")
                return audio_bytes, False

            # Detectar formato real si es auto
            if source_format == "auto":
                try:
                    source_format = detect_audio_format_by_magic_bytes(audio_bytes)
                except Exception:
                    source_format = "bin"  # Genérico

            # Si ya es OGG, no necesita conversión
            if source_format == "ogg":
                logger.info("[MediaProcessor] Audio ya es OGG - no requiere conversión")
                return audio_bytes, True

            logger.info(f"[MediaProcessor] 🔄 Convirtiendo {source_format.upper()} → OGG/Opus para WhatsApp...")

            # Crear archivos temporales para la conversión
            input_suffix = f'.{source_format}' if source_format != "bin" else '.bin'
            with tempfile.NamedTemporaryFile(suffix=input_suffix, delete=False) as input_file:
                input_path = input_file.name
                input_file.write(audio_bytes)

            output_path = input_path.rsplit('.', 1)[0] + '.ogg'

            try:
                # Ejecutar ffmpeg para convertir a OGG (Opus)
                # -y: sobrescribir sin preguntar
                # -i: input file
                # -c:a libopus: codec Opus (nativo de WhatsApp)
                # -b:a 64k: bitrate 64kbps (suficiente para voz)
                # -ar 48000: sample rate 48kHz (estándar para Opus)
                # -ac 1: mono (reduce tamaño)
                # -vn: ignorar video si existe
                result = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i", input_path,
                        "-c:a", "libopus",
                        "-b:a", "64k",
                        "-ar", "48000",
                        "-ac", "1",
                        "-vn",
                        output_path
                    ],
                    capture_output=True,
                    timeout=30
                )

                if result.returncode != 0:
                    stderr = result.stderr.decode('utf-8', errors='ignore')
                    logger.error(f"[MediaProcessor] ffmpeg error: {stderr[:500]}")
                    return audio_bytes, False

                # Leer archivo convertido
                with open(output_path, 'rb') as ogg_file:
                    ogg_bytes = ogg_file.read()

                logger.info(
                    f"[MediaProcessor] ✅ {source_format.upper()} convertido a OGG exitosamente: "
                    f"{len(audio_bytes)} bytes → {len(ogg_bytes)} bytes"
                )
                return ogg_bytes, True

            finally:
                # Limpiar archivos temporales
                try:
                    os.unlink(input_path)
                except OSError:
                    pass
                try:
                    os.unlink(output_path)
                except OSError:
                    pass

        except subprocess.TimeoutExpired:
            logger.error("[MediaProcessor] Timeout en conversión de audio → OGG")
            return audio_bytes, False
        except Exception as e:
            logger.error(f"[MediaProcessor] Error en conversión de audio → OGG: {e}")
            return audio_bytes, False

    # Alias para compatibilidad con código existente
    def _convert_webm_to_ogg(self, webm_bytes: bytes) -> Tuple[bytes, bool]:
        """Alias para _convert_audio_to_ogg con formato webm."""
        return self._convert_audio_to_ogg(webm_bytes, "webm")

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

        IMPORTANTE: Convierte CUALQUIER formato de audio problemático a OGG/Opus
        para garantizar compatibilidad con WhatsApp.

        Formatos problemáticos que se convierten:
        - WebM: No soportado por WhatsApp
        - MP4/M4A/AAC: Soporte inconsistente, puede fallar (Error 63021)
        - WAV: Se convierte para reducir tamaño
        - FLAC: No soportado por WhatsApp

        Formatos que NO se convierten:
        - OGG/Opus: Nativo de WhatsApp
        - MP3: Compatible con WhatsApp
        """
        content_lower = content_type.lower()

        # Formatos de audio soportados (webm, ogg, wav, mp4 del panel de asesoras)
        is_audio = "audio" in content_lower or "webm" in content_lower or "ogg" in content_lower or "wav" in content_lower

        if is_audio:
            folder = "audios_asesores"

            # =========================================================================
            # DETECCIÓN Y CONVERSIÓN DE AUDIO PARA WHATSAPP
            # =========================================================================
            # Detectar formato real por magic bytes (más confiable que content-type)
            detected_format = detect_audio_format_by_magic_bytes(file_bytes)
            logger.info(
                f"[MediaProcessor] Audio recibido: content-type={content_type}, "
                f"formato detectado={detected_format}"
            )

            # Lista de formatos que REQUIEREN conversión a OGG
            # (formatos que WhatsApp no soporta bien o rechaza con Error 63021)
            formats_requiring_conversion = ["webm", "mp4", "m4a", "wav", "flac"]

            # Lista de formatos que funcionan bien en WhatsApp
            formats_whatsapp_native = ["ogg", "mp3"]

            if detected_format in formats_requiring_conversion:
                logger.info(
                    f"[MediaProcessor] 🔄 Formato {detected_format.upper()} detectado - "
                    f"convirtiendo a OGG/Opus para WhatsApp..."
                )

                # Convertir a OGG usando ffmpeg
                converted_bytes, was_converted = self._convert_audio_to_ogg(
                    file_bytes,
                    detected_format
                )

                if was_converted:
                    file_bytes = converted_bytes
                    extension = ".ogg"
                    logger.info(
                        f"[MediaProcessor] ✅ {detected_format.upper()} convertido a OGG - "
                        f"compatible con WhatsApp"
                    )
                else:
                    # Fallback: subir en formato original con advertencia
                    extension = f".{detected_format}"
                    logger.warning(
                        f"[MediaProcessor] ⚠️ No se pudo convertir {detected_format.upper()} a OGG. "
                        f"El audio puede NO reproducirse en WhatsApp (Error 63021). "
                        f"Instala ffmpeg para habilitar la conversión."
                    )

            elif detected_format in formats_whatsapp_native:
                # OGG y MP3 funcionan bien en WhatsApp
                extension = f".{detected_format}"
                logger.info(
                    f"[MediaProcessor] ✅ Formato {detected_format.upper()} nativo de WhatsApp - "
                    f"no requiere conversión"
                )

            else:
                # Formato desconocido - intentar convertir por si acaso
                logger.warning(
                    f"[MediaProcessor] ⚠️ Formato desconocido ({detected_format}) - "
                    f"intentando conversión a OGG..."
                )
                converted_bytes, was_converted = self._convert_audio_to_ogg(
                    file_bytes,
                    detected_format
                )
                if was_converted:
                    file_bytes = converted_bytes
                    extension = ".ogg"
                else:
                    extension = f".{detected_format}"
            
            # Content-Type final para audio
            final_content_type = self._get_content_type_from_filename(f"audio{extension}")
        else:
            # =========================================================================
            # DETECCIÓN DE FORMATO DE IMAGEN
            # =========================================================================
            folder = "imagenes_asesores"
            
            # Detectar formato real de la imagen (no asumir siempre JPEG)
            img_format, final_content_type = detect_image_format_by_magic_bytes(file_bytes)
            extension = f".{img_format}"
            
            logger.info(
                f"[MediaProcessor] Imagen detectada: formato={img_format}, "
                f"content-type={final_content_type}"
            )

        clean_phone = phone.replace("+", "").replace(" ", "")
        filename = f"{clean_phone}_{int(time.time())}{extension}"

        # Pasar el Content-Type correcto a Bunny.net
        return await self.upload_to_bunny(file_bytes, folder, filename, final_content_type)


# Instancia singleton
media_processor = MediaProcessor()