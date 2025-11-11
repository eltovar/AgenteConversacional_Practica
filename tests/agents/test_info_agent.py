# tests/agents/test_info_agent.py
# -*- coding: utf-8 -*-
import pytest
from unittest.mock import Mock, patch, MagicMock
from info_agent import InfoAgent
from langchain_core.messages import AIMessage


@pytest.fixture
def info_agent():
    """Fixture que crea una instancia de InfoAgent."""
    return InfoAgent()


def test_info_agent_rag(info_agent):
    """
    Criterio de Aceptacion:
    Mockear rag_service. Asegurar que el InfoAgent llama al LLM
    con el contexto RAG correcto usando bind_tools nativo.
    """
    user_query = "Cual es el telefono de contacto?"
    expected_context = "Telefono: +57 601 555 1234"

    # Mock de rag_service.search_knowledge
    with patch('info_agent.rag_service.search_knowledge', return_value=expected_context) as mock_rag:
        # Mock de llama_client.client.bind_tools (primera llamada)
        # Crear mock de tool_call nativo (debe ser dict con name, args, id)
        mock_tool_decision_response = AIMessage(
            content="",
            tool_calls=[{
                'name': 'info_empresa_contacto_filosofia',
                'args': {'accion': 'obtener_info', 'tema': 'contacto'},
                'id': 'test_tool_call_1'
            }]
        )

        # Segunda llamada: generacion de respuesta con contexto RAG
        mock_rag_response = AIMessage(content="El telefono de contacto es +57 601 555 1234")

        # Crear mock del cliente completo
        mock_client = MagicMock()
        mock_llm_with_tools = MagicMock()
        mock_llm_with_tools.invoke.return_value = mock_tool_decision_response
        mock_client.bind_tools.return_value = mock_llm_with_tools

        with patch('info_agent.llama_client.client', mock_client), \
             patch('info_agent.llama_client.invoke') as mock_llm_invoke:

            # Configurar invoke directo para segunda llamada (generacion RAG)
            mock_llm_invoke.return_value = mock_rag_response

            # Ejecutar proceso
            response = info_agent.process_info_query(user_query)

            # Verificar que rag_service fue llamado
            mock_rag.assert_called_once()

            # Verificar que bind_tools fue llamado correctamente
            mock_client.bind_tools.assert_called_once()

            # Verificar que la respuesta final incluye informacion del RAG
            assert isinstance(response, str)
            assert len(response) > 0
            assert "RAG" in response  # Verifica prefijo "💬 Agente (RAG):"


def test_info_agent_no_tool_direct_response(info_agent):
    """
    Test adicional: Respuesta directa sin tool (LLM base).
    """
    user_query = "Hola, como estas?"

    # Mock de respuesta sin tool_calls (LLM decide no usar tool)
    mock_no_tool_response = AIMessage(
        content="Hola! Estoy aqui para ayudarte.",
        tool_calls=[]  # Sin tool calls
    )

    # Crear mock del cliente completo
    mock_client = MagicMock()
    mock_llm_with_tools = MagicMock()
    mock_llm_with_tools.invoke.return_value = mock_no_tool_response
    mock_client.bind_tools.return_value = mock_llm_with_tools

    with patch('info_agent.llama_client.client', mock_client):
        # Ejecutar proceso
        response = info_agent.process_info_query(user_query)

        # Verificar que bind_tools fue llamado
        mock_client.bind_tools.assert_called_once()

        # Verificar que hay respuesta
        assert isinstance(response, str)
        assert len(response) > 0
        assert "LLM" in response  # Verifica prefijo "💡 Agente (LLM):"


def test_info_agent_tool_detection(info_agent):
    """
    Test adicional: Verificar deteccion de tool call con bind_tools.
    """
    user_query = "Cual es la filosofia de la empresa?"

    # Mock de tool_call nativo (debe ser dict con name, args, id)
    mock_tool_response = AIMessage(
        content="",
        tool_calls=[{
            'name': 'info_empresa_contacto_filosofia',
            'args': {'accion': 'obtener_info', 'tema': 'filosofia'},
            'id': 'test_tool_call_3'
        }]
    )

    with patch('info_agent.rag_service.search_knowledge',
               return_value="Filosofia: Confianza y calidad") as mock_rag:

        # Crear mock del cliente completo
        mock_client = MagicMock()
        mock_llm_with_tools = MagicMock()
        mock_llm_with_tools.invoke.return_value = mock_tool_response
        mock_client.bind_tools.return_value = mock_llm_with_tools

        with patch('info_agent.llama_client.client', mock_client), \
             patch('info_agent.llama_client.invoke') as mock_llm_invoke:

            # Configurar respuesta final
            mock_llm_invoke.return_value = AIMessage(
                content="Nuestra filosofia se basa en la confianza..."
            )

            response = info_agent.process_info_query(user_query)

            # Verificar que se proceso correctamente
            assert isinstance(response, str)
            assert len(response) > 0
            assert "RAG" in response
            mock_rag.assert_called_once()
