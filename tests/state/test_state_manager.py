# tests/test_state_manager.py
# -*- coding: utf-8 -*-
"""
Tests básicos de StateManager con Redis (refactorizado para lazy initialization).

NOTA: Estos tests usan mocks de Redis para funcionar localmente.
      Para tests con conexión real a Redis, ver test_redis_metrics.py
"""
import pytest
import os
from unittest.mock import Mock, patch, MagicMock
from state_manager import StateManager, ConversationState, ConversationStatus


# ===== FIXTURES =====

@pytest.fixture(scope="function")
def mock_redis_client():
    """Fixture que proporciona un cliente Redis mockeado."""
    mock_client = MagicMock()
    storage = {}  # Simula almacenamiento en memoria

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

    # Cleanup
    storage.clear()


@pytest.fixture(scope="function")
def state_manager(mock_redis_client):
    """Fixture que proporciona un StateManager con Redis mockeado."""
    with patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379/0", "SESSION_TTL": "3600"}):
        with patch("redis.from_url", return_value=mock_redis_client):
            manager = StateManager()
            return manager


# ===== TESTS =====

def test_get_state_creates_new_session(state_manager):
    """
    Criterio de Aceptacion:
    get_state debe crear un estado RECEPTION_START si la sesion es nueva.
    """
    session_id = "test_session_123"

    # Obtener estado (deberia crear uno nuevo)
    state = state_manager.get_state(session_id)

    # Verificar que el estado fue creado
    assert state is not None
    assert isinstance(state, ConversationState)
    assert state.session_id == session_id
    assert state.status == ConversationStatus.RECEPTION_START
    assert state.lead_data == {}
    assert state.history == []


def test_get_state_returns_existing_session(state_manager):
    """
    Test adicional: get_state retorna sesion existente sin modificarla.
    """
    session_id = "test_session_456"

    # Crear estado inicial
    initial_state = state_manager.get_state(session_id)
    initial_state.status = ConversationStatus.AWAITING_LEAD_NAME
    initial_state.lead_data['test'] = 'value'
    state_manager.update_state(initial_state)

    # Obtener el mismo estado
    retrieved_state = state_manager.get_state(session_id)

    # Verificar que es el mismo estado (con modificaciones)
    assert retrieved_state.session_id == session_id
    assert retrieved_state.status == ConversationStatus.AWAITING_LEAD_NAME
    assert retrieved_state.lead_data['test'] == 'value'


def test_update_state(state_manager):
    """
    Test adicional: update_state actualiza correctamente.
    """
    session_id = "test_session_789"

    # Asegurar que Redis está inicializado
    state_manager._ensure_redis_initialized()

    # Limpiar cualquier dato residual explícitamente
    key = f"session:{session_id}"
    if hasattr(state_manager.client, 'delete'):
        state_manager.client.delete(key)

    # Crear estado inicial
    state = state_manager.get_state(session_id)
    assert state.status == ConversationStatus.RECEPTION_START

    # Modificar estado
    state.status = ConversationStatus.TRANSFERRED_INFO
    state.lead_data['name'] = 'Juan Perez'
    state_manager.update_state(state)

    # Verificar que los cambios persisten
    retrieved_state = state_manager.get_state(session_id)
    assert retrieved_state.status == ConversationStatus.TRANSFERRED_INFO
    assert retrieved_state.lead_data['name'] == 'Juan Perez'


def test_multiple_sessions_isolated(state_manager):
    """
    Test adicional: Multiples sesiones estan aisladas.
    """
    # Crear dos sesiones
    state1 = state_manager.get_state("session_1")
    state_manager.get_state("session_2")  # Crear segunda sesión

    # Modificar la primera
    state1.status = ConversationStatus.AWAITING_LEAD_NAME
    state_manager.update_state(state1)

    # Verificar que la segunda no fue afectada
    retrieved_state2 = state_manager.get_state("session_2")
    assert retrieved_state2.status == ConversationStatus.RECEPTION_START
