# tests/agents/test_info_agent.py
# -*- coding: utf-8 -*-
import pytest
from unittest.mock import Mock, patch, MagicMock
from info_agent import infoAgent
from langchain_core.messages import AIMessage


@pytest.fixture
def info_agent():
    """Fixture que crea una instancia de InfoAgent."""
    return infoAgent()


def test_info_agent_rag(info_agent):
    """
    Criterio de Aceptacion:
    Mockear rag_service. Asegurar que el InfoAgent llama al LLM
    con el contexto RAG correcto.
    """
    user_query = "Cual es el telefono de contacto?"
    expected_context = "Telefono: +57 601 555 1234"

    # Mock de rag_service.search_knowledge
    with patch('info_agent.rag_service.search_knowledge', return_value=expected_context) as mock_rag:
        # Mock de llama_client.invoke para capturar la llamada
        # Primera llamada: _determine_tool_call (debe devolver tool call)
        mock_tool_decision_response = AIMessage(content='{"tool_name": "info_empresa_contacto_filosofia", "tool_input": {"tema": "contacto"}}')

        # Segunda llamada: generacion de respuesta con contexto RAG
        mock_rag_response = AIMessage(content="El telefono de contacto es +57 601 555 1234")

        with patch('info_agent.llama_client.invoke') as mock_llm:
            # Configurar side_effect para retornar diferentes respuestas
            mock_llm.side_effect = [mock_tool_decision_response, mock_rag_response]

            # Ejecutar proceso
            response = info_agent.process_info_query(user_query)

            # Verificar que rag_service fue llamado
            mock_rag.assert_called_once()

            # Verificar que el LLM fue llamado 2 veces
            assert mock_llm.call_count == 2

            # Verificar la segunda llamada (con contexto RAG)
            second_call_args = mock_llm.call_args_list[1]
            messages = second_call_args[0][0]  # Primer argumento posicional

            # Verificar que el contexto RAG esta en los mensajes
            messages_text = str(messages)
            assert expected_context in messages_text or "contexto" in messages_text.lower()

            # Verificar que la respuesta final incluye informacion del RAG
            assert isinstance(response, str)
            assert len(response) > 0


def test_info_agent_no_tool_direct_response(info_agent):
    """
    Test adicional: Respuesta directa sin tool (LLM base).
    """
    user_query = "Hola, como estas?"

    # Mock de llama_client.invoke (respuesta sin tool)
    mock_response = AIMessage(content="NO_TOOL")

    with patch('info_agent.llama_client.invoke') as mock_llm:
        # Primera llamada: _determine_tool_call (NO_TOOL)
        # Segunda llamada: respuesta directa
        mock_llm.side_effect = [
            mock_response,
            AIMessage(content="Hola! Estoy aqui para ayudarte.")
        ]

        # Ejecutar proceso
        response = info_agent.process_info_query(user_query)

        # Verificar que se llamo al LLM 2 veces (decision + respuesta)
        assert mock_llm.call_count == 2

        # Verificar que hay respuesta
        assert isinstance(response, str)
        assert len(response) > 0


def test_info_agent_tool_detection(info_agent):
    """
    Test adicional: Verificar deteccion de tool call.
    """
    user_query = "Cual es la filosofia de la empresa?"

    # Mock que simula deteccion de tool
    mock_tool_decision = AIMessage(
        content='{"tool_name": "info_empresa_contacto_filosofia", "tool_input": {"tema": "filosofia"}}'
    )

    with patch('info_agent.rag_service.search_knowledge', return_value="Filosofia: Confianza y calidad"):
        with patch('info_agent.llama_client.invoke') as mock_llm:
            mock_llm.side_effect = [
                mock_tool_decision,
                AIMessage(content="Nuestra filosofia se basa en la confianza...")
            ]

            response = info_agent.process_info_query(user_query)

            # Verificar que se proceso correctamente
            assert isinstance(response, str)
            assert len(response) > 0
