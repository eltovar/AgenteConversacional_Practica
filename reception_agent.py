# reception_agent.py (NUEVO)
from llm_client import llama_client
from reception_tool import RECEPTION_TOOLS, classify_intent_func
from prompts.reception_prompts import (
    RECEPTION_SYSTEM_PROMPT,
    CLARIFICATION_PROMPTS,
    LEAD_NAME_REQUEST_PROMPT,
    LEAD_NAME_RETRY_PROMPT,
    LEAD_TRANSFER_SUCCESS_PROMPT
)
from utils.pii_validator import robust_extract_name
from state_manager import ConversationState, ConversationStatus
from langchain_core.messages import SystemMessage, HumanMessage
from logging_config import logger
import random
from typing import Dict, Any

class ReceptionAgent:
    """
    Agente de Recepción que maneja la clasificación de intenciones y captura de PII.

    Responsable de:
    - Clasificar la intención del usuario (info / leadsales / ambiguous)
    - Capturar datos del lead (nombre) usando PII validation
    - Actualizar el estado de la conversación
    - Enrutar al usuario al agente apropiado
    """

    def __init__(self):
        self.tools = {tool.name: tool for tool in RECEPTION_TOOLS}

    def process_message(self, message: str, state: ConversationState) -> Dict[str, Any]:
        """
        Procesa un mensaje del usuario según el estado actual de la conversación.

        Args:
            message: El mensaje del usuario
            state: Estado actual de la conversación

        Returns:
            Dict con 'response' (str) y 'new_state' (ConversationState)
        """
        logger.info(f"[ReceptionAgent] Estado: {state.status}, Mensaje: '{message[:50]}...'")

        # Router basado en el estado
        if state.status == ConversationStatus.RECEPTION_START:
            return self._handle_reception_start(message, state)

        elif state.status == ConversationStatus.AWAITING_CLARIFICATION:
            return self._handle_awaiting_clarification(message, state)

        elif state.status == ConversationStatus.AWAITING_LEAD_NAME:
            return self._handle_awaiting_lead_name(message, state)

        else:
            # Estado no manejado por ReceptionAgent
            logger.warning(f"[ReceptionAgent] Estado no manejado: {state.status}")
            return {
                "response": "Lo siento, hubo un error. ¿Podrías reformular tu consulta?",
                "new_state": state
            }

    def _handle_reception_start(self, message: str, state: ConversationState) -> Dict[str, Any]:
        """
        Maneja el estado inicial: clasifica la intención del usuario.
        """
        logger.info("[ReceptionAgent] Clasificando intención del usuario...")

        # Invocar LLM con tool classify_intent (forzada)
        messages = [
            SystemMessage(content=RECEPTION_SYSTEM_PROMPT),
            HumanMessage(content=message)
        ]

        try:
            # Crear LLM con tools bindeadas
            llm_with_tools = llama_client.client.bind_tools(
                [classify_intent_func],
                tool_choice="classify_intent"
            )
            response = llm_with_tools.invoke(messages)

            # Extraer tool call del LLM
            if hasattr(response, 'tool_calls') and response.tool_calls:
                tool_call = response.tool_calls[0]
                intent = tool_call['args']['intent']
                reason = tool_call['args']['reason']

                logger.info(f"[ReceptionAgent] Intención clasificada: '{intent}' - {reason}")

                # Actualizar estado según intención
                if intent == "info":
                    state.status = ConversationStatus.TRANSFERRED_INFO
                    return {
                        "response": "Entendido, déjame buscar esa información para ti...",
                        "new_state": state
                    }

                elif intent == "leadsales":
                    state.status = ConversationStatus.AWAITING_LEAD_NAME
                    return {
                        "response": LEAD_NAME_REQUEST_PROMPT,
                        "new_state": state
                    }

                elif intent == "ambiguous":
                    state.status = ConversationStatus.AWAITING_CLARIFICATION
                    clarification = random.choice(CLARIFICATION_PROMPTS)
                    return {
                        "response": clarification,
                        "new_state": state
                    }

            # Fallback si no hay tool call
            logger.warning("[ReceptionAgent] No se recibió tool call del LLM")
            state.status = ConversationStatus.AWAITING_CLARIFICATION
            return {
                "response": CLARIFICATION_PROMPTS[0],
                "new_state": state
            }

        except Exception as e:
            logger.error(f"[ReceptionAgent] Error en clasificación: {e}")
            return {
                "response": "Disculpa, ocurrió un error. ¿Podrías reformular tu consulta?",
                "new_state": state
            }

    def _handle_awaiting_clarification(self, message: str, state: ConversationState) -> Dict[str, Any]:
        """
        Maneja el estado de espera de aclaración: re-clasifica la intención.
        """
        logger.info("[ReceptionAgent] Re-clasificando después de aclaración...")

        # Usar la misma lógica que RECEPTION_START
        state.status = ConversationStatus.RECEPTION_START
        return self._handle_reception_start(message, state)

    def _handle_awaiting_lead_name(self, message: str, state: ConversationState) -> Dict[str, Any]:
        """
        Maneja el estado de captura de nombre: extrae PII y transfiere a Leadsales.
        """
        logger.info("[ReceptionAgent] Extrayendo nombre del lead...")

        # Extraer nombre con NER robusto
        extracted_name = robust_extract_name(message)

        if extracted_name:
            # PII extraído exitosamente
            state.lead_data['name'] = extracted_name
            state.status = ConversationStatus.TRANSFERRED_LEADSALES

            # Simular transferencia a CRM (Respuesta a Q2)
            logger.info(f"🚀 [LEADSALES] Simulando transferencia a CRM: {state.lead_data}")

            response_text = LEAD_TRANSFER_SUCCESS_PROMPT.format(name=extracted_name)
            return {
                "response": response_text,
                "new_state": state
            }

        else:
            # No se pudo extraer el nombre
            logger.warning("[ReceptionAgent] No se pudo extraer nombre del mensaje")
            return {
                "response": LEAD_NAME_RETRY_PROMPT,
                "new_state": state  # Mantener el mismo estado
            }

# Instancia global
reception_agent = ReceptionAgent()
