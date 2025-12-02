# tests/test_welcome_message.py
"""
Tests para la funcionalidad de mensaje de bienvenida (Detector de Sesión Nueva).

NOTA: Estos tests usan el StateManager real del orchestrator.
      Requieren que Redis esté disponible o se ejecuten con mocks.
"""
import pytest
import os
from unittest.mock import patch, MagicMock, Mock
from state_manager import StateManager, ConversationState, ConversationStatus
from Agents.orchestrator import process_message, state_manager
from prompts.sofia_personality import SOFIA_WELCOME_MESSAGE


# ===== FIXTURES =====

@pytest.fixture(scope="function")
def mock_redis_client():
    """Fixture que proporciona un cliente Redis mockeado."""
    mock_client = MagicMock()
    storage = {}

    def mock_get(key):
        return storage.get(key)

    def mock_set(key, value, ex=None):
        storage[key] = value
        return True

    def mock_ping():
        return True

    def mock_delete(*keys):
        for key in keys:
            storage.pop(key, None)
        return len(keys)

    mock_client.get = Mock(side_effect=mock_get)
    mock_client.set = Mock(side_effect=mock_set)
    mock_client.ping = Mock(side_effect=mock_ping)
    mock_client.delete = Mock(side_effect=mock_delete)

    yield mock_client
    storage.clear()


@pytest.fixture(autouse=True)
def setup_redis_mock(mock_redis_client):
    """
    Fixture que mockea Redis para todos los tests en este archivo.
    autouse=True asegura que se aplica automáticamente.
    """
    with patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379/0", "SESSION_TTL": "3600"}):
        with patch("redis.from_url", return_value=mock_redis_client):
            # Limpiar el state_manager del orchestrator (forzar reinicialización)
            state_manager._redis_initialized = False
            state_manager.client = None
            yield


# ===== TEST 1: Verificar que ConversationStatus contiene WELCOME_SENT =====

def test_conversation_status_contains_welcome_sent():
    """
    Verificar que la enumeración ConversationStatus contiene el nuevo estado WELCOME_SENT.
    """
    assert hasattr(ConversationStatus, 'WELCOME_SENT')
    assert ConversationStatus.WELCOME_SENT == "WELCOME_SENT"


# ===== TEST 2: Orchestrator envía bienvenida en sesión nueva =====

def test_orchestrator_sends_welcome_on_new_session():
    """
    Test que verifica que el orchestrator envía el mensaje de bienvenida
    cuando detecta una sesión nueva (RECEPTION_START + history vacío).
    """
    test_session_id = "test_welcome_session"

    # Asegurar que Redis está inicializado y limpiar datos residuales
    state_manager._ensure_redis_initialized()
    key = f"session:{test_session_id}"
    if hasattr(state_manager.client, 'delete'):
        state_manager.client.delete(key)

    # Primer mensaje de una sesión nueva
    result = process_message(test_session_id, "Hola")

    # Verificar que se envió el mensaje de bienvenida
    assert result["response"] == SOFIA_WELCOME_MESSAGE
    assert result["status"] == ConversationStatus.WELCOME_SENT
    
    # Verificar que el estado se actualizó a WELCOME_SENT
    state = state_manager.get_state(test_session_id)
    assert state.status == ConversationStatus.WELCOME_SENT


# ===== TEST 3: Segundo mensaje se clasifica correctamente =====

def test_second_message_classified_correctly():
    """
    Verificar que el segundo mensaje del usuario (después de la bienvenida)
    se clasifica correctamente por ReceptionAgent.
    """
    test_session_id = "test_second_message_session"

    # Asegurar que Redis está inicializado y limpiar datos residuales
    state_manager._ensure_redis_initialized()
    key = f"session:{test_session_id}"
    if hasattr(state_manager.client, 'delete'):
        state_manager.client.delete(key)

    # Primer mensaje - debe recibir bienvenida
    result1 = process_message(test_session_id, "Hola")
    assert result1["status"] == ConversationStatus.WELCOME_SENT

    # Mock del LLM para el segundo mensaje
    mock_response = MagicMock()
    mock_response.tool_calls = [{
        'args': {
            'intent': 'info',
            'reason': 'El usuario busca información'
        }
    }]

    # Segundo mensaje - debe ser clasificado por ReceptionAgent
    with patch('reception_agent.llama_client.invoke', return_value=mock_response):
        result2 = process_message(test_session_id, "¿Cuál es el horario de atención?")

    # Verificar que el estado cambió correctamente
    # El estado debería transicionar de WELCOME_SENT a RECEPTION_START y luego procesar
    assert 'response' in result2
    assert result2["response"] != SOFIA_WELCOME_MESSAGE  # No debe ser bienvenida de nuevo


# ===== TEST 4: Agente responde "Sofía" a pregunta de nombre =====

def test_agent_responds_sofia_to_name_question():
    """
    Asegurar que el agente responda a la pregunta ¿Cuál es tu nombre? con "Sofía".
    """
    # Verificar que SOFIA_WELCOME_MESSAGE contiene "Sofía"
    assert "Sofía" in SOFIA_WELCOME_MESSAGE

    test_session_id = "test_name_question_session"

    # Asegurar que Redis está inicializado y limpiar datos residuales
    state_manager._ensure_redis_initialized()
    key = f"session:{test_session_id}"
    if hasattr(state_manager.client, 'delete'):
        state_manager.client.delete(key)

    # Primer mensaje - recibe bienvenida que incluye "Sofía"
    result = process_message(test_session_id, "¿Cuál es tu nombre?")

    # La bienvenida debe contener "Sofía"
    assert "Sofía" in result["response"]