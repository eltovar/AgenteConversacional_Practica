# info_agent.py (Refactorizado con bind_tools y tool_choice="auto")

from llm_client import llama_client
from rag.rag_service import rag_service
from agents.InfoAgent.info_tool import ALL_TOOLS
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from prompts.info_prompts import (
    SYSTEM_AGENT_PROMPT_BASE,
    SYSTEM_AGENT_PROMPT_WITH_USER,
    RAG_GENERATION_INSTRUCTIONS,
    FIRST_MESSAGE_INSTRUCTIONS
)
from state_manager import ConversationState
from typing import Dict, Any, List, Optional
from logging_config import logger

# Mapeo de tools a documentos específicos
TOOL_DOCUMENT_MAP = {
    "info_institucional": [
        "knowledge_base/informacion_institucional.txt",
        "knowledge_base/info_cobertura_propiedades.txt",
        "knowledge_base/info_pagos_online.txt"
    ],
    "soporte_contacto": [
        "knowledge_base/soporte_administraciones_multas.txt",
        "knowledge_base/soporte_caja_pagos.txt",
        "knowledge_base/soporte_contabilidad_facturas.txt",
        "knowledge_base/soporte_contratos_terminacion.txt",
        "knowledge_base/soporte_juridico_legal.txt",
        "knowledge_base/soporte_servicios_publicos.txt",
        "knowledge_base/soporte_reparaciones.txt",
        "knowledge_base/info_estudios_libertador.txt"
    ],
    "asesoria_legal_blog": [
        "knowledge_base/blog_arriendo_claves_riesgos.txt",
        "knowledge_base/blog_arriendo_contrato_legalidad.txt",
        "knowledge_base/blog_arriendo_estudios_fraude.txt",
        "knowledge_base/blog_arriendo_gastos_administracion.txt",
        "knowledge_base/blog_arriendo_incrementos_ley.txt"
    ]
}

class InfoAgent: # Renombrado de 'infoAgent' a 'InfoAgent' por convención
    """
    Agente de Información que maneja consultas RAG.
    Utiliza LangChain bind_tools(..., tool_choice='auto') para la toma de decisiones.
    """

    def __init__(self, tools: List[Any] = ALL_TOOLS):
        self.tools = {tool.name: tool for tool in tools}

    def _run_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        """
        Ejecuta la herramienta RAG buscando en los documentos específicos de cada tool.
        """
        logger.info(f"[InfoAgent] Ejecutando Tool '{tool_name}'...")

        # Validar que la tool existe en el mapeo
        if tool_name not in TOOL_DOCUMENT_MAP:
            logger.error(f"[InfoAgent] Tool '{tool_name}' no encontrada en TOOL_DOCUMENT_MAP")
            return f"Error: Tool '{tool_name}' no encontrada o no implementada."

        # Obtener el tema/query desde tool_input
        query = tool_input.get('tema', 'información general')
        logger.info(f"[InfoAgent] Búsqueda RAG con query: '{query}'")

        # Obtener lista de documentos asociados a esta tool
        document_paths = TOOL_DOCUMENT_MAP[tool_name]
        logger.info(f"[InfoAgent] Buscando en {len(document_paths)} documentos de '{tool_name}'")

        # Buscar en cada documento y agregar resultados
        all_contexts = []
        for doc_path in document_paths:
            logger.debug(f"[InfoAgent] Buscando en: {doc_path}")
            context = rag_service.search_knowledge(doc_path, query)

            # Solo agregar si no es un error
            if not context.startswith("[ERROR]"):
                all_contexts.append(f"--- Fuente: {doc_path} ---\n{context}")

        # Combinar todos los contextos encontrados
        if all_contexts:
            combined_context = "\n\n".join(all_contexts)
            logger.info(f"[InfoAgent] Contexto combinado generado ({len(combined_context)} caracteres)")
            return combined_context
        else:
            logger.warning(f"[InfoAgent] No se encontró contexto relevante para query: '{query}'")
            return f"No se encontró información específica sobre '{query}' en los documentos disponibles."
        
    def process_info_query(self, user_input: str, state: Optional[ConversationState] = None) -> str:
        """
        Procesa la consulta del usuario usando el flujo Tool Call (RAG) o LLM Base.
        Este método reemplaza la lógica de _determine_tool_call().
        """
        # Detectar si es primer mensaje para incluir presentación
        is_first_message = state and state.metadata.get("is_first_message", False)
        if is_first_message:
            logger.info("[InfoAgent] Primer mensaje detectado - incluirá presentación")
            # Limpiar el flag para que no se repita
            state.metadata["is_first_message"] = False

        # Construir prompt con o sin contexto de usuario
        if state and state.lead_data.get('name'):
            user_name = state.lead_data['name']
            system_prompt = SYSTEM_AGENT_PROMPT_WITH_USER.format(user_name=user_name)
            logger.info(f"[InfoAgent] Usando contexto de usuario: {user_name}")
        else:
            system_prompt = SYSTEM_AGENT_PROMPT_BASE

        # Añadir instrucciones de primer mensaje si aplica
        if is_first_message:
            system_prompt = system_prompt + "\n\n" + FIRST_MESSAGE_INSTRUCTIONS

        # Construir mensajes con historial completo de la conversación
        messages = [SystemMessage(content=system_prompt)]

        # Inyectar historial desde state.history (si existe)
        if state and state.history:
            logger.info(f"[InfoAgent] Inyectando {len(state.history)} mensajes del historial")
            for msg in state.history:
                if msg.startswith("User: "):
                    messages.append(HumanMessage(content=msg[6:]))  # Remover prefijo "User: "
                elif msg.startswith("Agent: "):
                    messages.append(AIMessage(content=msg[7:]))  # Remover prefijo "Agent: "

        # Añadir el mensaje actual del usuario como último HumanMessage
        messages.append(HumanMessage(content=user_input))
        
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
                
                # Construir mensajes RAG con historial completo
                messages_rag = [SystemMessage(content=final_system_prompt)]

                # Inyectar historial (igual que en flujo principal)
                if state and state.history:
                    for msg in state.history:
                        if msg.startswith("User: "):
                            messages_rag.append(HumanMessage(content=msg[6:]))
                        elif msg.startswith("Agent: "):
                            messages_rag.append(AIMessage(content=msg[7:]))

                # La pregunta original del usuario va como último HumanMessage
                messages_rag.append(HumanMessage(content=user_input))
                
                final_response = llama_client.invoke(messages_rag).content
                return final_response

            # 4. LLM Base (Respuesta Conversacional)
            else:
                # El LLM decidió no usar la Tool (NO_TOOL)
                logger.info("[InfoAgent] LLM decidió NO usar Tool. Respondiendo directamente...")

                # La respuesta ya está en response_llm.content (la respuesta LLM al prompt inicial)
                return response_llm.content

        except Exception as e:
            logger.error(f"[InfoAgent] Error crítico en el flujo RAG/LLM: {e}", exc_info=True)
            return "❌ Lo siento, no puedo procesar tu consulta en este momento. Inténtalo de nuevo más tarde."

    def reload_knowledge_base(self) -> Dict[str, Any]:
        """
        Método pasarela para recargar la base de conocimiento RAG.
        Delega la operación a rag_service.reload_knowledge_base().
        """
        logger.info("[InfoAgent] Solicitando recarga de base de conocimiento...")
        result = rag_service.reload_knowledge_base()
        logger.info(f"[InfoAgent] Recarga completada: {result.get('message')}")
        return result

# Instancia global (Singleton)
agent = InfoAgent()