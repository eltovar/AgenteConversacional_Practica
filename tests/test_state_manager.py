# tests/test_state_manager.py
# -*- coding: utf-8 -*-
import pytest
from state_manager import StateManager, ConversationState, ConversationStatus


def test_get_state_creates_new_session():
    """
    Criterio de Aceptacion:
    get_state debe crear un estado RECEPTION_START si la sesion es nueva.
    """
    manager = StateManager()
    session_id = "test_session_123"

    # La sesion no existe aun
    assert session_id not in manager.sessions

    # Obtener estado (deberia crear uno nuevo)
    state = manager.get_state(session_id)

    # Verificar que el estado fue creado
    assert state is not None
    assert isinstance(state, ConversationState)
    assert state.session_id == session_id
    assert state.status == ConversationStatus.RECEPTION_START
    assert state.lead_data == {}
    assert state.history == []

    # Verificar que esta en el diccionario de sesiones
    assert session_id in manager.sessions


def test_get_state_returns_existing_session():
    """
    Test adicional: get_state retorna sesion existente sin modificarla.
    """
    manager = StateManager()
    session_id = "test_session_456"

    # Crear estado inicial
    initial_state = manager.get_state(session_id)
    initial_state.status = ConversationStatus.AWAITING_LEAD_NAME
    initial_state.lead_data['test'] = 'value'
    manager.update_state(initial_state)

    # Obtener el mismo estado
    retrieved_state = manager.get_state(session_id)

    # Verificar que es el mismo estado (con modificaciones)
    assert retrieved_state.session_id == session_id
    assert retrieved_state.status == ConversationStatus.AWAITING_LEAD_NAME
    assert retrieved_state.lead_data['test'] == 'value'


def test_update_state():
    """
    Test adicional: update_state actualiza correctamente.
    """
    manager = StateManager()
    session_id = "test_session_789"

    # Crear estado inicial
    state = manager.get_state(session_id)
    assert state.status == ConversationStatus.RECEPTION_START

    # Modificar estado
    state.status = ConversationStatus.TRANSFERRED_INFO
    state.lead_data['name'] = 'Juan Perez'
    manager.update_state(state)

    # Verificar que los cambios persisten
    retrieved_state = manager.get_state(session_id)
    assert retrieved_state.status == ConversationStatus.TRANSFERRED_INFO
    assert retrieved_state.lead_data['name'] == 'Juan Perez'


def test_multiple_sessions_isolated():
    """
    Test adicional: Multiples sesiones estan aisladas.
    """
    manager = StateManager()

    # Crear dos sesiones
    state1 = manager.get_state("session_1")
    state2 = manager.get_state("session_2")

    # Modificar la primera
    state1.status = ConversationStatus.AWAITING_LEAD_NAME
    manager.update_state(state1)

    # Verificar que la segunda no fue afectada
    retrieved_state2 = manager.get_state("session_2")
    assert retrieved_state2.status == ConversationStatus.RECEPTION_START
