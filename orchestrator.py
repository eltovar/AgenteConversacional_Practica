# orchestrator.py
"""
Orquestador Central del Sistema Multi-Agente.
Proporciona una función stateless para procesar mensajes de usuario.
Funciona tanto para CLI como para webhooks (FastAPI).

comandos:
    -Ejecutar servidor: uvicorn app:app --reload
    -Probar con: Invoke-WebRequest -Uri "http://localhost:8000/webhook" `
  -Method POST `
  -ContentType "application/json" `
  -Body '{"session_id":"test","message":"Hola"}'

"""

from state_manager import StateManager, ConversationState, ConversationStatus
from reception_agent import reception_agent
from info_agent import agent as info_agent
from leadsales_agent import lead_sales_agent
from prompts.sofia_personality import SOFIA_WELCOME_MESSAGE
from logging_config import logger
from typing import Dict, Any
from datetime import datetime, timedelta

# Instancia global del state manager
state_manager = StateManager()


def process_message(session_id: str, user_message: str) -> Dict[str, Any]:
    """
    Función central que procesa un mensaje del usuario.
    Funciona tanto para CLI como para webhooks.
    """
    try:
        # 1. OBTENER ESTADO ACTUAL
        state = state_manager.get_state(session_id)
        now = datetime.now()
        logger.info(f"[ORCHESTRATOR] Estado actual: {state.status}")

        # 2. PRE-ROUTER: DETECCIÓN DE SESIÓN NUEVA O INACTIVA (24h)
        is_new_session = state.last_interaction_timestamp is None
        is_stale_session = (
            state.last_interaction_timestamp is not None and
            (now - state.last_interaction_timestamp) > timedelta(hours=24)
        )

        if is_new_session or is_stale_session:
            reason = "nueva" if is_new_session else "inactiva >24h"
            logger.info(f"[ORCHESTRATOR] Sesión {reason} detectada - Enviando mensaje de bienvenida")
            state.status = ConversationStatus.WELCOME_SENT
            state.last_interaction_timestamp = now
            state_manager.update_state(state)
            return {"response": SOFIA_WELCOME_MESSAGE, "status": state.status}

        # 3. ROUTER BASADO EN ESTADO
        if state.status == ConversationStatus.TRANSFERRED_INFO:
            logger.info("[ORCHESTRATOR] Enrutando a InfoAgent...")
            response = info_agent.process_info_query(user_message, state)

            # Resetear estado y actualizar timestamp
            state.status = ConversationStatus.RECEPTION_START
            state.last_interaction_timestamp = now
            state_manager.update_state(state)

            return {"response": response, "status": state.status}

        elif state.status == ConversationStatus.TRANSFERRED_LEADSALES:
            logger.info("[ORCHESTRATOR] Enrutando a LeadSalesAgent...")
            result = lead_sales_agent.process_lead_handoff(user_message, state)

            response = result["response"]
            new_state = result.get("new_state", state)
            new_state.status = ConversationStatus.RECEPTION_START
            new_state.last_interaction_timestamp = now
            state_manager.update_state(new_state)

            return {"response": response, "status": new_state.status}

        else:
            # Estados manejados por ReceptionAgent (RECEPTION_START, AWAITING_CLARIFICATION, WELCOME_SENT)

            # Transición post-bienvenida: cambiar WELCOME_SENT a RECEPTION_START
            if state.status == ConversationStatus.WELCOME_SENT:
                state.status = ConversationStatus.RECEPTION_START
                state_manager.update_state(state)

            logger.info("[ORCHESTRATOR] Enrutando a ReceptionAgent...")
            result = reception_agent.process_message(user_message, state)

            response = result["response"]
            new_state = result["new_state"]
            state_manager.update_state(new_state)

            # === AUTO-ENRUTAMIENTO INMEDIATO (FIX PR005) ===
            if new_state.status == ConversationStatus.TRANSFERRED_INFO:
                logger.info("[ORCHESTRATOR] Auto-enrutando a InfoAgent...")
                info_response = info_agent.process_info_query(user_message, new_state)
                response = f"{response}\n\n{info_response}"

                new_state.status = ConversationStatus.RECEPTION_START
                state_manager.update_state(new_state)

            elif new_state.status == ConversationStatus.TRANSFERRED_LEADSALES:
                logger.info("[ORCHESTRATOR] Auto-enrutando a LeadSalesAgent...")
                lead_result = lead_sales_agent.process_lead_handoff(user_message, new_state)
                response = f"{response}\n\n{lead_result['response']}"

                new_state.status = ConversationStatus.RECEPTION_START
                state_manager.update_state(new_state)

            # Actualizar timestamp en todas las rutas del bloque else
            new_state.last_interaction_timestamp = now
            state_manager.update_state(new_state)

            return {"response": response, "status": new_state.status}

    except Exception as e:
        logger.error(f"[ORCHESTRATOR] Error inesperado: {e}", exc_info=True)
        return {
            "response": "Lo siento, ocurrió un error inesperado. Por favor, intenta de nuevo.",
            "status": "error"
        }