# reception_agent.py
from llm_client import llama_client
from agents.ReceptionAgent.reception_tool import RECEPTION_TOOLS, classify_intent_func
from prompts.reception_prompts import (
    RECEPTION_SYSTEM_PROMPT,
    CLARIFICATION_PROMPTS,
    SOFIA_PERSONALITY
)
from prompts.crm_prompts import PROPERTY_EXTRACTION_PROMPT
from state_manager import ConversationState, ConversationStatus
from langchain_core.messages import SystemMessage, HumanMessage
from logging_config import logger
import random
import json
from typing import Dict, Any


class ReceptionAgent:
    """ Agente de Recepción que clasifica intenciones y enruta al agente correcto. """

    def __init__(self):
        self.tools = {tool.name: tool for tool in RECEPTION_TOOLS}

    def process_message(self, message: str, state: ConversationState) -> Dict[str, Any]:
        """
        Procesa un mensaje del usuario según el estado actual de la conversación.
        """
        logger.info(f"[ReceptionAgent] Estado: {state.status}, Mensaje: '{message[:50]}...'")

        # Router basado en el estado
        if state.status == ConversationStatus.RECEPTION_START:
            return self._handle_reception_start(message, state)

        elif state.status == ConversationStatus.AWAITING_CLARIFICATION:
            return self._handle_awaiting_clarification(message, state)

        else:
            # Estado no manejado por ReceptionAgent
            logger.warning(f"[ReceptionAgent] Estado no manejado: {state.status}")
            return {
                "response": "Lo siento, hubo un error. ¿Podrías reformular tu consulta?",
                "new_state": state
            }

    def _handle_reception_start(self, message: str, state: ConversationState) -> Dict[str, Any]:
        """
        Maneja el estado inicial: clasifica la intención del usuario con retry logic.
        """
        logger.info("[ReceptionAgent] Clasificando intención del usuario...")

        lead_name = state.lead_data.get('name')
        system_prompt = RECEPTION_SYSTEM_PROMPT

        if lead_name:
            system_prompt = f"Dirígete al usuario como '{lead_name}' en tu respuesta. " + system_prompt

        # Invocar LLM con tool classify_intent (forzada)
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=message)
        ]

        llm_with_tools = llama_client.client.bind_tools(
            [classify_intent_func],
            tool_choice="classify_intent"
        )

        # Intentamos hasta 3 veces (1 intento inicial + 2 reintentos)
        MAX_RETRIES = 2

        for attempt in range(MAX_RETRIES + 1):
            if attempt > 0:
                logger.warning(f"[ReceptionAgent] Fallo en intento {attempt}. Reintentando clasificación...")

            try:
                response = llm_with_tools.invoke(messages)

                # Extraer tool call del LLM
                if hasattr(response, 'tool_calls') and response.tool_calls:
                    tool_call = response.tool_calls[0]
                    intent = tool_call['args']['intent']
                    reason = tool_call['args']['reason']

                    logger.info(f"[ReceptionAgent] Intención clasificada con éxito en intento {attempt+1}: '{intent}'")

                    # Lógica de Transición
                    if intent == "info":
                        state.status = ConversationStatus.TRANSFERRED_INFO
                        logger.info("[ReceptionAgent] Estado: RECEPTION_START → TRANSFERRED_INFO")
                        response_text = "Entendido, déjame buscar esa información para ti..."

                    elif intent == "crm":
                        # Extraer entidades iniciales del mensaje
                        property_data = self._extract_property_entities(message)
                        if property_data:
                            state.lead_data['metadata'] = property_data
                            logger.info(f"[ReceptionAgent] Metadata inicial extraída: {property_data}")

                        # Transferir al CRM Agent conversacional
                        state.status = ConversationStatus.CRM_CONVERSATION
                        logger.info("[ReceptionAgent] Estado: RECEPTION_START → CRM_CONVERSATION")
                        response_text = ""  # El CRM Agent generará la respuesta

                    elif intent == "ambiguous":
                        state.status = ConversationStatus.AWAITING_CLARIFICATION
                        logger.info("[ReceptionAgent] Estado: RECEPTION_START → AWAITING_CLARIFICATION")
                        response_text = random.choice(CLARIFICATION_PROMPTS)

                    else:
                        # Fallback si intent desconocido
                        logger.warning(f"[ReceptionAgent] Intent desconocido: '{intent}'. Usando fallback.")
                        state.status = ConversationStatus.AWAITING_CLARIFICATION
                        response_text = CLARIFICATION_PROMPTS[0]

                    return {"response": response_text, "new_state": state}

                # Si no hay tool_calls, continuar al siguiente intento
                logger.warning(f"[ReceptionAgent] No se recibió tool_call en intento {attempt+1}")

            except Exception as e:
                logger.error(f"[ReceptionAgent] Error en la invocación del LLM (Intento {attempt+1}): {e}")

        # Fallback si se agotan todos los reintentos
        logger.error(f"[ReceptionAgent] Clasificación fallida después de {MAX_RETRIES + 1} intentos.")
        state.status = ConversationStatus.AWAITING_CLARIFICATION

        return {
            "response": CLARIFICATION_PROMPTS[0],
            "new_state": state
        }

    def _handle_awaiting_clarification(self, message: str, state: ConversationState) -> Dict[str, Any]:
        """
        Maneja el estado de espera de aclaración: re-clasifica la intención.
        """
        logger.info("[ReceptionAgent] Re-clasificando después de aclaración...")
        state.status = ConversationStatus.RECEPTION_START
        return self._handle_reception_start(message, state)

    def _extract_property_entities(self, message: str) -> Dict[str, Any]:
        """
        Extrae entidades de propiedad del mensaje del usuario usando LLM.
        """
        logger.info("[ReceptionAgent] Extrayendo entidades de propiedad...")

        try:
            extraction_prompt = PROPERTY_EXTRACTION_PROMPT.format(user_message=message)
            messages = [SystemMessage(content=extraction_prompt)]

            response = llama_client.invoke(messages)
            response_text = response.content.strip()

            start_idx = response_text.find('{')
            end_idx = response_text.rfind('}') + 1

            if start_idx != -1 and end_idx > start_idx:
                json_str = response_text[start_idx:end_idx]
                property_data = json.loads(json_str)
                logger.info(f"[ReceptionAgent] Entidades extraídas: {property_data}")
                return property_data
            else:
                logger.warning("[ReceptionAgent] No se encontró JSON en la respuesta de extracción")
                return {}

        except json.JSONDecodeError as e:
            logger.error(f"[ReceptionAgent] Error parseando JSON de entidades: {e}")
            return {}
        except Exception as e:
            logger.error(f"[ReceptionAgent] Error extrayendo entidades: {e}")
            return {}


# Instancia global
reception_agent = ReceptionAgent()