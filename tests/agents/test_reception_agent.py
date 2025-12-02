# tests/agents/test_reception_agent.py
import pytest
from unittest.mock import Mock, patch, MagicMock
from Agents.ReceptionAgent.reception_agent import ReceptionAgent
from state_manager import ConversationState, ConversationStatus


@pytest.fixture
def reception_agent():
    """Fixture que crea una instancia de ReceptionAgent."""
    return ReceptionAgent()


@pytest.fixture
def initial_state():
    """Fixture que crea un estado inicial en RECEPTION_START."""
    return ConversationState(
        session_id="test_session",
        status=ConversationStatus.RECEPTION_START,
        lead_data={},
        history=[]
    )


def test_reception_agent_intent_leadsales(reception_agent, initial_state):
    """
    Criterio de Aceptación:
    Mockear LLM (para que devuelva classify_intent: leadsales).
    Asegurar que el estado de salida sea AWAITING_LEAD_NAME.
    """
    # Mock del LLM response con tool call
    mock_response = MagicMock()
    mock_response.tool_calls = [{
        'args': {
            'intent': 'leadsales',
            'reason': 'El usuario quiere vender su propiedad'
        }
    }]

    with patch('reception_agent.llama_client.invoke', return_value=mock_response):
        result = reception_agent.process_message(
            "Quiero vender mi casa",
            initial_state
        )

    # Verificar respuesta
    assert 'response' in result
    assert 'new_state' in result

    # Verificar que el estado cambió a AWAITING_LEAD_NAME
    new_state = result['new_state']
    assert new_state.status == ConversationStatus.AWAITING_LEAD_NAME

    # Verificar que la respuesta solicita el nombre
    assert 'nombre' in result['response'].lower()


def test_reception_agent_intent_info(reception_agent, initial_state):
    """
    Test adicional: Clasificación intent='info'.
    Asegurar que el estado de salida sea TRANSFERRED_INFO.
    """
    # Mock del LLM response con tool call
    mock_response = MagicMock()
    mock_response.tool_calls = [{
        'args': {
            'intent': 'info',
            'reason': 'El usuario busca información sobre servicios'
        }
    }]

    with patch('reception_agent.llama_client.invoke', return_value=mock_response):
        result = reception_agent.process_message(
            "¿Cuál es el teléfono de la empresa?",
            initial_state
        )

    # Verificar que el estado cambió a TRANSFERRED_INFO
    new_state = result['new_state']
    assert new_state.status == ConversationStatus.TRANSFERRED_INFO


def test_reception_agent_intent_ambiguous(reception_agent, initial_state):
    """
    Test adicional: Clasificación intent='ambiguous'.
    Asegurar que el estado de salida sea AWAITING_CLARIFICATION.
    """
    # Mock del LLM response con tool call
    mock_response = MagicMock()
    mock_response.tool_calls = [{
        'args': {
            'intent': 'ambiguous',
            'reason': 'El mensaje es demasiado vago'
        }
    }]

    with patch('reception_agent.llama_client.invoke', return_value=mock_response):
        result = reception_agent.process_message(
            "Hola",
            initial_state
        )

    # Verificar que el estado cambió a AWAITING_CLARIFICATION
    new_state = result['new_state']
    assert new_state.status == ConversationStatus.AWAITING_CLARIFICATION


def test_reception_agent_pii_capture(reception_agent):
    """
    Criterio de Aceptación:
    Mockear PII validator (para que devuelva "Juan").
    Asegurar que el estado de salida sea TRANSFERRED_LEADSALES.
    """
    # Estado inicial en AWAITING_LEAD_NAME
    state = ConversationState(
        session_id="test_session",
        status=ConversationStatus.AWAITING_LEAD_NAME,
        lead_data={},
        history=[]
    )

    # Mock del PII validator
    with patch('reception_agent.robust_extract_name', return_value="Juan"):
        result = reception_agent.process_message(
            "Me llamo Juan",
            state
        )

    # Verificar respuesta
    assert 'response' in result
    assert 'new_state' in result

    # Verificar que el estado cambió a TRANSFERRED_LEADSALES
    new_state = result['new_state']
    assert new_state.status == ConversationStatus.TRANSFERRED_LEADSALES

    # Verificar que el nombre fue guardado
    assert 'name' in new_state.lead_data
    assert new_state.lead_data['name'] == "Juan"

    # Verificar que la respuesta incluye el nombre
    assert 'Juan' in result['response']


def test_reception_agent_pii_capture_failure(reception_agent):
    """
    Test adicional: PII validator no detecta nombre.
    El estado debe permanecer en AWAITING_LEAD_NAME.
    """
    # Estado inicial en AWAITING_LEAD_NAME
    state = ConversationState(
        session_id="test_session",
        status=ConversationStatus.AWAITING_LEAD_NAME,
        lead_data={},
        history=[]
    )

    # Mock del PII validator (retorna None)
    with patch('reception_agent.robust_extract_name', return_value=None):
        result = reception_agent.process_message(
            "No entendí",
            state
        )

    # Verificar que el estado sigue siendo AWAITING_LEAD_NAME
    new_state = result['new_state']
    assert new_state.status == ConversationStatus.AWAITING_LEAD_NAME

    # Verificar que no se guardó ningún nombre
    assert 'name' not in new_state.lead_data

    # Verificar que se solicita repetir el nombre
    assert 'nombre' in result['response'].lower()
