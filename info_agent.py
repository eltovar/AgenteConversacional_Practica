# info_agent.py (Refactorizado con bind_tools y tool_choice="auto")

from llm_client import llama_client
from rag import rag_service
from info_tool import ALL_TOOLS, informacion_empresa_func
from langchain_core.messages import SystemMessage, HumanMessage
from prompts.info_prompts import (SYSTEM_AGENT_PROMPT_BASE, SYSTEM_AGENT_PROMPT_WITH_USER, RAG_GENERATION_INSTRUCTIONS)
from state_manager import ConversationState
from typing import Dict, Any, List, Optional
from logging_config import logger

INFO_EMPRESA_TOOL_NAME = "info_empresa_contacto_filosofia"

class InfoAgent: # Renombrado de 'infoAgent' a 'InfoAgent' por convención
    """
    Agente de Información que maneja consultas RAG.
    Utiliza LangChain bind_tools(..., tool_choice='auto') para la toma de decisiones.
    """

    def __init__(self, tools: List[Any] = ALL_TOOLS):
        self.tools = {tool.name: tool for tool in tools}

    def _run_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        """
        Ejecuta la herramienta RAG. El único propósito de esta tool es generar la query para RAG.
        """
        logger.info(f"[InfoAgent] Ejecutando Tool '{tool_name}'...")
        
        # Solo manejamos la tool de información de la empresa, que es un alias para RAG.
        if tool_name == INFO_EMPRESA_TOOL_NAME:
            # Obtener el tema/query desde la tool_input (usando 'tema')
            query = tool_input.get('tema', 'información general de la empresa')
            
            logger.info(f"[InfoAgent] Búsqueda RAG con query: {query}")
            
            # Llamada al servicio RAG (rag.py)
            context = rag_service.search_knowledge(query)
            
            # Devolvemos el contexto encontrado para que el LLM genere la respuesta final.
            return context

        return f"Error: Tool '{tool_name}' no encontrada o no implementada."

    def process_info_query(self, user_input: str, state: Optional[ConversationState] = None) -> str:
        """
        Procesa la consulta del usuario usando el flujo Tool Call (RAG) o LLM Base.
        Este método reemplaza la lógica de _determine_tool_call().

        Args:
            user_input: Mensaje del usuario
            state: Estado de la conversación (opcional, para inyectar contexto de usuario)
        """
        # Construir prompt con o sin contexto de usuario
        if state and state.lead_data.get('name'):
            user_name = state.lead_data['name']
            system_prompt = SYSTEM_AGENT_PROMPT_WITH_USER.format(user_name=user_name)
            logger.info(f"[InfoAgent] Usando contexto de usuario: {user_name}")
        else:
            system_prompt = SYSTEM_AGENT_PROMPT_BASE

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_input)
        ]
        
        # 1. Detección de Tools con tool_choice="auto" (LangChain nativo)
        try:
            # Configurar LLM para que DECIDA si usar la tool o no
            llm_with_tools = llama_client.client.bind_tools(
                ALL_TOOLS, 
                tool_choice="auto"
            )
            response_llm = llm_with_tools.invoke(messages)
            
            # 2. Análisis del resultado de la decisión (RAG Flow)
            if hasattr(response_llm, 'tool_calls') and response_llm.tool_calls:
                
                # Ejecutar el flujo RAG
                tool_call = response_llm.tool_calls[0]
                tool_name = tool_call['name']
                tool_input = tool_call['args']

                logger.info(f"[InfoAgent] Tool '{tool_name}' invocada. Ejecutando RAG...")

                # Ejecutar RAG y obtener el contexto (Resultado de la Tool)
                context = self._run_tool(tool_name, tool_input) 

                # 3. Generación de Respuesta Final con Contexto
                # Crear las instrucciones RAG con el contexto recuperado
                rag_instructions = RAG_GENERATION_INSTRUCTIONS.format(context=context)

                # Combinar el prompt base (con o sin nombre) con las instrucciones RAG
                final_system_prompt = system_prompt + "\n\n" + rag_instructions
                
                messages_rag = [
                    # Inyectar la instrucción RAG como el nuevo SystemMessage
                    SystemMessage(content=final_system_prompt),
                    # La pregunta original del usuario va sola como HumanMessage
                    HumanMessage(content=user_input)
                ]
                
                final_response = llama_client.invoke(messages_rag).content
                return f"💬 Agente (RAG): {final_response}"
            
            # 4. LLM Base (Respuesta Conversacional)
            else:
                # El LLM decidió no usar la Tool (NO_TOOL)
                logger.info("[InfoAgent] LLM decidió NO usar Tool. Respondiendo directamente...")
                
                # La respuesta ya está en response_llm.content (la respuesta LLM al prompt inicial)
                return f"💡 Agente (LLM): {response_llm.content}"

        except Exception as e:
            logger.error(f"[InfoAgent] Error crítico en el flujo RAG/LLM: {e}", exc_info=True)
            return "❌ Lo siento, no puedo procesar tu consulta en este momento. Inténtalo de nuevo más tarde."

# Instancia global (Singleton)
agent = InfoAgent()