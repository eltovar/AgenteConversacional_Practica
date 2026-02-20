# utils/message_aggregator.py
"""
Sistema de agregación de mensajes para manejar múltiples mensajes
enviados en rápida sucesión por el mismo usuario.
"""

import asyncio
import os
import time
from typing import Optional, Dict, Any
from logging_config import logger

# Timeout de agregación configurable (default: 30 segundos)
AGGREGATION_TIMEOUT = int(os.getenv("MESSAGE_AGGREGATION_TIMEOUT", "30"))


class MessageAggregator:
    """
    Agregador de mensajes usando Redis para persistencia y locks distribuidos.
    """

    def __init__(self):
        self.redis = None
        self._redis_available = False
        self._init_redis()

    def _init_redis(self):
        """Inicializa conexión a Redis."""
        try:
            import redis
            redis_url = os.getenv("REDIS_URL") or os.getenv("REDIS_PUBLIC_URL")
            if redis_url:
                self.redis = redis.from_url(redis_url, decode_responses=True)
                self.redis.ping()
                self._redis_available = True
                logger.info("[MessageAggregator] Redis conectado para agregación de mensajes")
            else:
                logger.warning("[MessageAggregator] REDIS_URL no configurado - agregación deshabilitada")
        except Exception as e:
            logger.warning(f"[MessageAggregator] Redis no disponible: {e}")
            self._redis_available = False

    def _get_buffer_key(self, session_id: str) -> str:
        """Genera la clave Redis para el buffer de mensajes."""
        return f"msg_buffer:{session_id}"

    def _get_lock_key(self, session_id: str) -> str:
        """Genera la clave Redis para el lock de procesamiento."""
        return f"msg_lock:{session_id}"

    def _get_processing_key(self, session_id: str) -> str:
        """Genera la clave Redis para indicar que hay procesamiento pendiente."""
        return f"msg_processing:{session_id}"

    async def add_message_to_buffer(self, session_id: str, message: str) -> Dict[str, Any]:
        """
        Agrega un mensaje al buffer y determina si debe procesarse ahora o esperar
        """
        if not self._redis_available:
            # Sin Redis, procesar inmediatamente (comportamiento legacy)
            return {
                "should_process": True,
                "is_aggregating": False,
                "buffer_count": 1,
                "combined_message": message
            }

        buffer_key = self._get_buffer_key(session_id)
        lock_key = self._get_lock_key(session_id)
        processing_key = self._get_processing_key(session_id)

        try:
            # 1. Verificar si ya hay un procesamiento en curso
            is_processing = self.redis.get(processing_key)

            if is_processing:
                # Ya hay un proceso esperando - agregar al buffer y no hacer nada más
                self.redis.rpush(buffer_key, message)
                buffer_count = self.redis.llen(buffer_key)
                logger.info(f"[Aggregator] Mensaje agregado a buffer existente. Total: {buffer_count}")
                return {
                    "should_process": False,
                    "is_aggregating": True,
                    "buffer_count": buffer_count,
                    "wait_message": None  # No responder nada, el proceso principal responderá
                }

            # 2. No hay procesamiento en curso - iniciar uno nuevo
            # Intentar adquirir lock (NX = solo si no existe)
            lock_acquired = self.redis.set(lock_key, "1", nx=True, ex=AGGREGATION_TIMEOUT + 5)

            if not lock_acquired:
                # Otro proceso adquirió el lock justo ahora - agregar al buffer
                self.redis.rpush(buffer_key, message)
                buffer_count = self.redis.llen(buffer_key)
                return {
                    "should_process": False,
                    "is_aggregating": True,
                    "buffer_count": buffer_count,
                    "wait_message": None
                }

            # 3. Tenemos el lock - somos el proceso principal
            # Marcar que hay procesamiento pendiente
            self.redis.set(processing_key, "1", ex=AGGREGATION_TIMEOUT + 10)

            # Agregar mensaje al buffer
            self.redis.rpush(buffer_key, message)
            # Establecer TTL en el buffer
            self.redis.expire(buffer_key, AGGREGATION_TIMEOUT + 60)

            return {
                "should_process": True,  # Este proceso debe esperar y luego procesar
                "is_aggregating": True,
                "buffer_count": 1,
                "combined_message": None  # Se obtendrá después de esperar
            }

        except Exception as e:
            logger.error(f"[Aggregator] Error en add_message_to_buffer: {e}")
            # En caso de error, procesar normalmente
            return {
                "should_process": True,
                "is_aggregating": False,
                "buffer_count": 1,
                "combined_message": message
            }

    async def wait_and_get_combined_message(self, session_id: str) -> str:
        """
        Espera el timeout de agregación y luego retorna todos los mensajes combinados.
        Solo debe llamarse si add_message_to_buffer retornó should_process=True.
        """
        if not self._redis_available:
            return ""

        buffer_key = self._get_buffer_key(session_id)
        lock_key = self._get_lock_key(session_id)
        processing_key = self._get_processing_key(session_id)

        try:
            # Esperar el timeout de agregación
            logger.info(f"[Aggregator] Esperando {AGGREGATION_TIMEOUT}s para agregar mensajes...")
            await asyncio.sleep(AGGREGATION_TIMEOUT)

            # Obtener todos los mensajes del buffer
            messages = self.redis.lrange(buffer_key, 0, -1)

            if not messages:
                logger.warning("[Aggregator] Buffer vacío después de esperar")
                return ""

            # Combinar mensajes con un espacio
            combined = " ".join(messages)

            # Limpiar buffer y locks
            self.redis.delete(buffer_key)
            self.redis.delete(lock_key)
            self.redis.delete(processing_key)

            logger.info(f"[Aggregator] Mensajes combinados ({len(messages)}): '{combined[:100]}...'")
            return combined

        except Exception as e:
            logger.error(f"[Aggregator] Error en wait_and_get_combined_message: {e}")
            # Limpiar en caso de error
            try:
                self.redis.delete(buffer_key)
                self.redis.delete(lock_key)
                self.redis.delete(processing_key)
            except:
                pass
            return ""

    def clear_buffer(self, session_id: str):
        """Limpia el buffer de un usuario (útil para testing/admin)."""
        if not self._redis_available:
            return

        try:
            buffer_key = self._get_buffer_key(session_id)
            lock_key = self._get_lock_key(session_id)
            processing_key = self._get_processing_key(session_id)
            self.redis.delete(buffer_key, lock_key, processing_key)
        except Exception as e:
            logger.warning(f"[Aggregator] Error limpiando buffer: {e}")


# Instancia global (singleton)
message_aggregator = MessageAggregator()