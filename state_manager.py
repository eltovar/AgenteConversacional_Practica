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
    AWAITING_LEAD_NAME = "AWAITING_LEAD_NAME"
    TRANSFERRED_INFO = "TRANSFERRED_INFO"
    TRANSFERRED_CRM = "TRANSFERRED_CRM"
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

        La conexión a Redis se realiza bajo demanda cuando se accede
        por primera vez a get_state() o update_state().

        Variables de entorno requeridas:
        - REDIS_URL: URL de conexión (ej: redis://localhost:6379/0)
        - SESSION_TTL: Tiempo de vida en segundos (default: 86400)
        """
        # Cargar configuración (no conecta aún)
        self.redis_url = os.getenv("REDIS_URL")
        self.session_ttl = int(os.getenv("SESSION_TTL", "86400"))

        # Cliente Redis (se inicializará bajo demanda)
        self.client = None
        self._redis_initialized = False

        logger.info("[StateManager] StateManager creado (lazy initialization habilitada)")

    def _ensure_redis_initialized(self):
        """
        Inicializa la conexión a Redis bajo demanda (lazy initialization).

        Este método se llama automáticamente antes de cualquier operación
        que requiera acceso a Redis.

        Raises:
            ValueError: Si REDIS_URL no está configurada
            ConnectionError: Si la conexión a Redis falla
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
                logger.info(f"[StateManager] Cliente Redis creado desde {self.redis_url}")

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

        Args:
            session_id: Identificador único de la sesión

        Returns:
            ConversationState: Estado deserializado desde Redis o nuevo estado si no existe

        Raises:
            redis.RedisError: Si hay error de comunicación con Redis
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

        Args:
            state: Objeto ConversationState a persistir

        Raises:
            redis.RedisError: Si hay error de comunicación con Redis
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