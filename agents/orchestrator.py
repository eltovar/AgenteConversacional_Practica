# orchestrator.py
"""
Orquestador Central del Sistema Multi-Agente (REFACTORIZADO - ASYNC).
Proporciona una función asíncrona stateless para procesar mensajes de usuario.
Funciona tanto para CLI como para webhooks (FastAPI).

Comandos:
    - Ejecutar servidor: uvicorn app:app --reload
    - Probar con: Invoke-WebRequest -Uri "http://localhost:8001/webhook" -Method POST -ContentType "application/json" -Body '{"session_id":"test","message":"Hola"}'
"""

import os
from dotenv import load_dotenv
from state_manager import StateManager, ConversationState, ConversationStatus

# Todos los agentes son ahora async: Reception, Info y CRM.
from agents.ReceptionAgent.reception_agent import reception_agent
from agents.InfoAgent.info_agent import agent as info_agent
from agents.CRMAgent.crm_agent import crm_agent

from logging_config import logger
from typing import Dict, Any
from datetime import datetime, timedelta
from utils.link_detector import LinkDetector

# Instancia del detector de links para el flujo de bienvenida
link_detector = LinkDetector()

# ===== VALIDACIÓN DE SECRETS =====
load_dotenv()

REQUIRED_SECRETS = ["OPENAI_API_KEY"]
missing = [key for key in REQUIRED_SECRETS if not os.getenv(key)]
if missing:
    raise EnvironmentError(
        f"❌ Missing required secrets: {', '.join(missing)}\n"
        f"💡 Copy .env.example to .env and add your API keys"
    )

# Instancia global del state manager
state_manager = StateManager()


async def process_message(session_id: str, user_message: str) -> Dict[str, Any]:
    """
    Función central ASÍNCRONA que procesa un mensaje del usuario.
    Funciona tanto para CLI como para webhooks (FastAPI).
    """
    try:
        # 1. OBTENER ESTADO ACTUAL
        state = state_manager.get_state(session_id)
        now = datetime.now()
        logger.info(f"[ORCHESTRATOR] Estado actual: {state.status}")

        # 2. LOGICA DE SESIÓN (BIENVENIDA)
        is_new_session = state.last_interaction_timestamp is None
        is_stale_session = (
            state.last_interaction_timestamp is not None and
            (now - state.last_interaction_timestamp) > timedelta(hours=24)
        )

        if is_new_session or is_stale_session:
            # Marcar como primer mensaje para que los agentes incluyan presentación
            state.metadata["is_first_message"] = True
            reason = "nueva" if is_new_session else "inactiva >24h"
            logger.info(f"[ORCHESTRATOR] Sesión {reason} detectada. Marcando is_first_message=True")

            # Inicializar estado y timestamp
            state.status = ConversationStatus.RECEPTION_START
            state.last_interaction_timestamp = now
            state_manager.update_state(state)
            # NOTA: El flujo continúa al CORE ROUTING LOGIC (no retorna aquí)

        # 3. CORE ROUTING LOGIC
        response_text = ""

        # A. Si estamos en conversación CRM (recopilando datos)
        if state.status == ConversationStatus.CRM_CONVERSATION:
            logger.info("[ORCHESTRATOR] Enrutando a CRMAgent conversacional...")
            result = await crm_agent.process_conversation(user_message, state)
            response_text = result["response"]
            if "new_state" in result:
                state = result["new_state"]

            # Si el CRM completó el handoff, resetear a Reception
            if result.get("ready_for_handoff"):
                state.status = ConversationStatus.RECEPTION_START

        # A2. Si ya se completó la transferencia CRM (post-handoff)
        elif state.status == ConversationStatus.TRANSFERRED_CRM:
            logger.info("[ORCHESTRATOR] CRM ya transferido. Reseteando a Reception...")
            state.status = ConversationStatus.RECEPTION_START
            response_text = "¿Hay algo más en lo que pueda ayudarte?"

        # B. Si estamos en flujo de Info (RAG)
        elif state.status == ConversationStatus.TRANSFERRED_INFO:
            logger.info("[ORCHESTRATOR] Enrutando a InfoAgent...")
            response_text = await info_agent.process_info_query(user_message, state)
            state.status = ConversationStatus.RECEPTION_START

        # C. Flujo Normal (Reception Agent)
        else:
            if state.status == ConversationStatus.WELCOME_SENT:
                state.status = ConversationStatus.RECEPTION_START

            logger.info("[ORCHESTRATOR] Enrutando a ReceptionAgent...")
            result = await reception_agent.process_message(user_message, state)

            initial_response = result["response"]
            state = result["new_state"]  # Actualizamos el estado con lo que decidió Reception

            # === AUTO-ENRUTAMIENTO (Fast-Track) ===
            # Si Reception decide transferir AHORA MISMO, ejecutamos el siguiente agente
            # en la misma llamada para no hacer esperar al usuario.

            if state.status == ConversationStatus.TRANSFERRED_INFO:
                logger.info("[ORCHESTRATOR] Auto-enrutando a InfoAgent...")
                rag_response = await info_agent.process_info_query(user_message, state)
                response_text = rag_response
                state.status = ConversationStatus.RECEPTION_START

            elif state.status == ConversationStatus.CRM_CONVERSATION:
                logger.info("[ORCHESTRATOR] Auto-enrutando a CRMAgent conversacional...")
                crm_result = await crm_agent.process_conversation(user_message, state)
                response_text = crm_result['response']
                if "new_state" in crm_result:
                    state = crm_result["new_state"]
                # Si el CRM completó el handoff en este mismo turno, resetear
                if crm_result.get("ready_for_handoff"):
                    state.status = ConversationStatus.RECEPTION_START

            else:
                # No hubo transferencia, respuesta normal
                response_text = initial_response

        # 4. CENTRALIZACIÓN DE PERSISTENCIA (DRY)
        # Guardamos historial y estado una sola vez al final
        _update_history_and_state(state, user_message, response_text, now)

        return {"response": response_text, "status": state.status}

    except Exception as e:
        logger.error(f"[ORCHESTRATOR] Error crítico: {e}", exc_info=True)
        return {
            "response": "Lo siento, tuve un problema técnico momentáneo. ¿Podrías repetirme eso?",
            "status": "error"
        }


# ===== FUNCIONES AUXILIARES (Private Helpers) =====


# Máximo de entries en state.history (30 turnos × 2 = 60).
# Sin límite, conversaciones largas crecen indefinidamente en Redis (TTL 30 días)
# y se deserializan completas en RAM en cada request del mismo usuario.
_MAX_HISTORY_ENTRIES = 60


def _update_history_and_state(state: ConversationState, user_msg: str, agent_msg: str, now: datetime):
    """
    Centraliza la lógica de guardado de historial y persistencia de estado.
    Evita duplicación de código (DRY principle).
    """
    if user_msg:
        state.history.append(f"User: {user_msg}")
    if agent_msg:
        state.history.append(f"Agent: {agent_msg}")

    # Trim para evitar que el historial crezca sin límite en Redis y en RAM
    if len(state.history) > _MAX_HISTORY_ENTRIES:
        state.history = state.history[-_MAX_HISTORY_ENTRIES:]

    state.last_interaction_timestamp = now

    try:
        state_manager.update_state(state)
        logger.debug(f"[ORCHESTRATOR] Estado guardado exitosamente para {state.session_id}")
    except Exception as e:
        logger.error(f"[ORCHESTRATOR] ERROR guardando estado para {state.session_id}: {e}", exc_info=True)