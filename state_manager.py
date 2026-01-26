# state_manager.py (REFACTORIZADO CON REDIS)
from enum import Enum
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime
import os
import redis
from logging_config import logger

class ConversationStatus(str, Enum):
    RECEPTION_START = "RECEPTION_START"
    AWAITING_CLARIFICATION = "AWAITING_CLARIFICATION"
    AWAITING_PROPERTY_DATA = "AWAITING_PROPERTY_DATA"  # DEPRECATED - No usado en arquitectura conversacional
    AWAITING_LEAD_NAME = "AWAITING_LEAD_NAME"  # DEPRECATED - No usado en arquitectura conversacional
    TRANSFERRED_INFO = "TRANSFERRED_INFO"
    TRANSFERRED_CRM = "TRANSFERRED_CRM"
    CRM_CONVERSATION = "CRM_CONVERSATION"
    WELCOME_SENT = "WELCOME_SENT"

class ConversationState(BaseModel):
    session_id: str
    status: ConversationStatus = ConversationStatus.RECEPTION_START
    lead_data: Dict[str, Any] = Field(default_factory=dict)
    history: List = Field(default_factory=list)
    last_interaction_timestamp: Optional[datetime] = None

class StateManager:
    def __init__(self):
        """
        Inicializa el StateManager con lazy initialization.
        """
        # Auto-detectar si estamos en Railway
        is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None

        # Seleccionar URL apropiada según el entorno
        if is_railway:
            # Estamos en Railway → usar URL interna (más rápida)
            self.redis_url = os.getenv("REDIS_URL")
            logger.info("[StateManager] Entorno detectado: Railway (usando REDIS_URL interna)")
        else:
            # Estamos en local → usar URL pública (accesible desde fuera)
            self.redis_url = os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL")
            logger.info("[StateManager] Entorno detectado: Local (usando REDIS_PUBLIC_URL)")

        self.session_ttl = int(os.getenv("SESSION_TTL", "86400"))

        # Cliente Redis (se inicializará bajo demanda)
        self.client = None
        self._redis_initialized = False

        logger.info("[StateManager] StateManager creado (lazy initialization habilitada)")

    def _mask_redis_url(self, url: str) -> str:
        """
        Enmascara credenciales en la URL de Redis para logging seguro.
        """
        if not url:
            return "None"

        try:
            # Extraer esquema (redis://, rediss://)
            if "://" not in url:
                return url

            scheme, rest = url.split("://", 1)

            # Si tiene credenciales (usuario:password@host)
            if "@" in rest:
                _, host_part = rest.rsplit("@", 1)
                return f"{scheme}://***:***@{host_part}"
            else:
                # No tiene credenciales, mostrar completo
                return url

        except Exception:
            # Si falla el parsing, ocultar todo
            return "redis://***:***@[masked]"

    def _ensure_redis_initialized(self):
        """
        Inicializa la conexión a Redis bajo demanda (lazy initialization).
        """
        if not self._redis_initialized:
            logger.info("[StateManager] Inicializando conexión a Redis (lazy)...")

            # Validar que REDIS_URL esté configurada
            if not self.redis_url:
                error_msg = "REDIS_URL no encontrada en variables de entorno"
                logger.error(f"[StateManager] {error_msg}")
                raise ValueError(error_msg)

            try:
                # Crear cliente Redis
                self.client = redis.from_url(self.redis_url, decode_responses=True)

                # Log seguro: ocultar credenciales (solo mostrar host:puerto)
                safe_url = self._mask_redis_url(self.redis_url)
                logger.info(f"[StateManager] Cliente Redis creado: {safe_url}")

                # Verificar conexión con ping
                self.client.ping()
                logger.info("[StateManager] Conexión a Redis verificada exitosamente (ping OK)")

                # Marcar como inicializado
                self._redis_initialized = True

            except redis.ConnectionError as e:
                logger.error(f"[StateManager] Error de conexión a Redis: {e}")
                raise ConnectionError(
                    f"No se pudo conectar a Redis. "
                    f"Verifica que REDIS_URL esté configurado correctamente. "
                    f"Error: {e}"
                ) from e
            except Exception as e:
                logger.error(f"[StateManager] Error al inicializar Redis: {e}")
                raise

    def get_state(self, session_id: str) -> ConversationState:
        """
        Recupera el estado de la conversación desde Redis.
        """
        # Asegurar que Redis esté inicializado
        self._ensure_redis_initialized()

        key = f"session:{session_id}"

        try:
            data = self.client.get(key)

            if data:
                # Deserializar JSON desde Redis
                state = ConversationState.model_validate_json(data)
                logger.debug(f"[StateManager] Estado recuperado para session_id={session_id}")
                return state
            else:
                # Sesión no existe, crear nuevo estado
                new_state = ConversationState(session_id=session_id)
                logger.info(f"[StateManager] Nueva sesión creada: {session_id}")
                return new_state

        except redis.RedisError as e:
            logger.error(f"[StateManager] Error de Redis al obtener estado: {e}")
            raise
        except Exception as e:
            logger.error(f"[StateManager] Error al deserializar estado: {e}")
            raise

    def update_state(self, state: ConversationState):
        """
        Persiste el estado de la conversación en Redis con expiración automática.
        """
        # Asegurar que Redis esté inicializado
        self._ensure_redis_initialized()

        key = f"session:{state.session_id}"

        try:
            # Serializar a JSON usando Pydantic
            json_data = state.model_dump_json()

            # Guardar en Redis con TTL (Time-To-Live)
            self.client.set(key, json_data, ex=self.session_ttl)
            logger.debug(f"[StateManager] Estado persistido para session_id={state.session_id} (TTL={self.session_ttl}s)")

        except redis.RedisError as e:
            logger.error(f"[StateManager] Error de Redis al persistir estado: {e}")
            raise
        except Exception as e:
            logger.error(f"[StateManager] Error al serializar estado: {e}")
            raise