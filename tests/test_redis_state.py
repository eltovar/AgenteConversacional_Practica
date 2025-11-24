# tests/test_redis_state.py
"""
Tests de integración para StateManager con Redis usando mocks.
Verifica que la refactorización mantiene compatibilidad con orchestrator.
"""

import pytest
import os
from unittest.mock import Mock, patch, MagicMock
from state_manager import StateManager, ConversationState, ConversationStatus

# ===== FIXTURES =====

@pytest.fixture
def mock_redis_client():
    """
    Fixture que proporciona un cliente Redis mockeado.
    Simula el comportamiento de redis.Redis con un dict en memoria.
    """
    mock_client = MagicMock()
    storage = {}  # Simula almacenamiento en memoria

    def mock_get(key):
        return storage.get(key)

    def mock_set(key, value, ex=None):
        storage[key] = value
        return True

    def mock_ping():
        return True

    mock_client.get = Mock(side_effect=mock_get)
    mock_client.set = Mock(side_effect=mock_set)
    mock_client.ping = Mock(side_effect=mock_ping)

    return mock_client


@pytest.fixture
def state_manager(mock_redis_client):
    """
    Fixture que proporciona un StateManager con Redis mockeado.
    """
    with patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379/0", "SESSION_TTL": "3600"}):
        with patch("redis.from_url", return_value=mock_redis_client):
            manager = StateManager()
            return manager


# ===== TESTS: Inicialización =====

def test_statemanager_init_without_redis_url():
    """
    Test que verifica que StateManager falla si REDIS_URL no está configurada.
    """
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="REDIS_URL no encontrada"):
            StateManager()


def test_statemanager_init_with_connection_error():
    """
    Test que verifica que StateManager falla si Redis no responde al ping.
    """
    mock_client = MagicMock()
    mock_client.ping.side_effect = Exception("Connection refused")

    with patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379/0"}):
        with patch("redis.from_url", return_value=mock_client):
            with pytest.raises(Exception, match="Connection refused"):
                StateManager()


def test_statemanager_init_success(state_manager):
    """
    Test que verifica inicialización exitosa con configuración válida.
    """
    assert state_manager.client is not None
    assert state_manager.session_ttl == 3600


# ===== TESTS: get_state =====

def test_get_state_new_session(state_manager):
    """
    Test que verifica que get_state crea una nueva sesión si no existe en Redis.
    """
    state = state_manager.get_state("test_session_new")

    assert state.session_id == "test_session_new"
    assert state.status == ConversationStatus.RECEPTION_START
    assert state.lead_data == {}
    assert state.history == []


def test_get_state_existing_session(state_manager):
    """
    Test que verifica que get_state recupera una sesión existente desde Redis.
    """
    # Preparar: Guardar un estado en Redis
    existing_state = ConversationState(
        session_id="test_session_existing",
        status=ConversationStatus.TRANSFERRED_INFO,
        lead_data={"name": "Juan Pérez"},
        history=["Hola", "¿Cuáles son sus horarios?"]
    )
    state_manager.update_state(existing_state)

    # Ejecutar: Recuperar estado
    retrieved_state = state_manager.get_state("test_session_existing")

    # Verificar
    assert retrieved_state.session_id == "test_session_existing"
    assert retrieved_state.status == ConversationStatus.TRANSFERRED_INFO
    assert retrieved_state.lead_data == {"name": "Juan Pérez"}
    assert retrieved_state.history == ["Hola", "¿Cuáles son sus horarios?"]


def test_get_state_with_redis_error(state_manager):
    """
    Test que verifica manejo de errores de Redis en get_state.
    """
    import redis as redis_module
    state_manager.client.get.side_effect = redis_module.RedisError("Connection lost")

    with pytest.raises(redis_module.RedisError, match="Connection lost"):
        state_manager.get_state("test_session_error")


# ===== TESTS: update_state =====

def test_update_state_new_session(state_manager):
    """
    Test que verifica que update_state persiste correctamente un nuevo estado.
    """
    new_state = ConversationState(
        session_id="test_session_update",
        status=ConversationStatus.AWAITING_LEAD_NAME,
        lead_data={"interest": "Arriendo"}
    )

    # Ejecutar
    state_manager.update_state(new_state)

    # Verificar que se llamó a Redis set con TTL
    state_manager.client.set.assert_called_once()
    call_args = state_manager.client.set.call_args

    assert call_args[0][0] == "session:test_session_update"  # Key
    assert "test_session_update" in call_args[0][1]  # JSON contiene session_id
    assert call_args[1]["ex"] == 3600  # TTL


def test_update_state_existing_session(state_manager):
    """
    Test que verifica actualización de estado existente.
    """
    # Preparar: Crear estado inicial
    initial_state = ConversationState(session_id="test_session_modify")
    state_manager.update_state(initial_state)

    # Ejecutar: Modificar estado
    modified_state = state_manager.get_state("test_session_modify")
    modified_state.status = ConversationStatus.WELCOME_SENT
    modified_state.lead_data["email"] = "test@example.com"
    state_manager.update_state(modified_state)

    # Verificar: Recuperar y validar
    final_state = state_manager.get_state("test_session_modify")
    assert final_state.status == ConversationStatus.WELCOME_SENT
    assert final_state.lead_data["email"] == "test@example.com"


def test_update_state_with_redis_error(state_manager):
    """
    Test que verifica manejo de errores de Redis en update_state.
    """
    import redis as redis_module
    state_manager.client.set.side_effect = redis_module.RedisError("Write failed")

    test_state = ConversationState(session_id="test_error")

    with pytest.raises(redis_module.RedisError, match="Write failed"):
        state_manager.update_state(test_state)


# ===== TESTS: Serialización JSON =====

def test_json_serialization_deserialization(state_manager):
    """
    Test que verifica ciclo completo de serialización/deserialización.
    """
    from datetime import datetime

    original_state = ConversationState(
        session_id="test_json",
        status=ConversationStatus.TRANSFERRED_LEADSALES,
        lead_data={"name": "Ana López", "phone": "3001234567"},
        history=["msg1", "msg2", "msg3"],
        last_interaction_timestamp=datetime(2025, 1, 15, 10, 30, 0)
    )

    # Guardar y recuperar
    state_manager.update_state(original_state)
    recovered_state = state_manager.get_state("test_json")

    # Verificar igualdad
    assert recovered_state.session_id == original_state.session_id
    assert recovered_state.status == original_state.status
    assert recovered_state.lead_data == original_state.lead_data
    assert recovered_state.history == original_state.history
    assert recovered_state.last_interaction_timestamp == original_state.last_interaction_timestamp


# ===== TESTS: TTL =====

def test_ttl_configuration_default(mock_redis_client):
    """
    Test que verifica que el TTL por defecto es 86400 segundos (24 horas).
    """
    with patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379/0"}, clear=True):
        with patch("redis.from_url", return_value=mock_redis_client):
            manager = StateManager()
            assert manager.session_ttl == 86400


def test_ttl_configuration_custom(mock_redis_client):
    """
    Test que verifica que el TTL personalizado se respeta.
    """
    with patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379/0", "SESSION_TTL": "7200"}):
        with patch("redis.from_url", return_value=mock_redis_client):
            manager = StateManager()
            assert manager.session_ttl == 7200


def test_ttl_applied_on_update(state_manager):
    """
    Test que verifica que el TTL se aplica correctamente en cada update.
    """
    test_state = ConversationState(session_id="test_ttl")
    state_manager.update_state(test_state)

    # Verificar que set fue llamado con parámetro ex (expire)
    call_args = state_manager.client.set.call_args
    assert call_args[1]["ex"] == 3600  # TTL configurado en fixture


# ===== TESTS: Integración con Orchestrator (Simulación) =====

def test_orchestrator_flow_simulation(state_manager):
    """
    Test que simula el flujo completo del orchestrator:
    1. Obtener estado inicial
    2. Modificar estado
    3. Persistir estado
    4. Recuperar estado en nueva llamada
    """
    session_id = "orchestrator_test"

    # Primera interacción: Usuario nuevo
    state1 = state_manager.get_state(session_id)
    assert state1.status == ConversationStatus.RECEPTION_START

    # Orchestrator actualiza estado después de clasificación
    state1.status = ConversationStatus.TRANSFERRED_INFO
    state1.history.append("User: ¿Cuáles son sus servicios?")
    state_manager.update_state(state1)

    # Segunda interacción: Usuario retorna
    state2 = state_manager.get_state(session_id)
    assert state2.status == ConversationStatus.TRANSFERRED_INFO
    assert len(state2.history) == 1
    assert state2.history[0] == "User: ¿Cuáles son sus servicios?"

    # Orchestrator actualiza nuevamente
    state2.history.append("Agent: Ofrecemos arriendo, ventas y avalúos")
    state_manager.update_state(state2)

    # Tercera interacción: Validar persistencia
    state3 = state_manager.get_state(session_id)
    assert len(state3.history) == 2
    assert state3.status == ConversationStatus.TRANSFERRED_INFO