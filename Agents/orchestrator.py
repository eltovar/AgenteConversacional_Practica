# orchestrator.py
"""
Orquestador Central del Sistema Multi-Agente (REFACTORIZADO - ASYNC).
Proporciona una función asíncrona stateless para procesar mensajes de usuario.
Funciona tanto para CLI como para webhooks (FastAPI).

Comandos:
    - Ejecutar servidor: uvicorn app:app --reload
    - Probar con: Invoke-WebRequest -Uri "http://localhost:8000/webhook" -Method POST -ContentType "application/json" -Body '{"session_id":"test","message":"Hola"}'
"""

import os
from dotenv import load_dotenv
from state_manager import StateManager, ConversationState, ConversationStatus

# IMPORTANTE: Asegúrate de que tus agentes expongan métodos async
# Si Reception e Info siguen siendo síncronos, funcionarán, pero lo ideal es migrarlos a async.
from Agents.ReceptionAgent.reception_agent import reception_agent
from Agents.InfoAgent.info_agent import agent as info_agent
from Agents.CRMAgent.crm_agent import crm_agent

from prompts.sofia_personality import SOFIA_WELCOME_MESSAGE
from logging_config import logger
from typing import Dict, Any
from datetime import datetime, timedelta

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
            _handle_welcome(state, now, is_new_session)
            return {"response": SOFIA_WELCOME_MESSAGE, "status": state.status}

        # 3. CORE ROUTING LOGIC
        response_text = ""

        # A. Si ya estamos en flujo de CRM (HubSpot)
        if state.status == ConversationStatus.TRANSFERRED_CRM:
            logger.info("[ORCHESTRATOR] Enrutando a CRMAgent (Async)...")
            # AWAIT: Llamada asíncrona a HubSpot
            result = await crm_agent.process_lead_handoff(user_message, state)
            response_text = result["response"]
            # El CRM podría devolver un estado nuevo si termina el flujo
            if "new_state" in result:
                state = result["new_state"]

            # Resetear a Reception si el CRM terminó su trabajo
            state.status = ConversationStatus.RECEPTION_START

        # B. Si estamos en flujo de Info (RAG)
        elif state.status == ConversationStatus.TRANSFERRED_INFO:
            logger.info("[ORCHESTRATOR] Enrutando a InfoAgent...")
            # Nota: Si info_agent es síncrono, esto bloquea un poco. Idealmente hazlo async también.
            response_text = info_agent.process_info_query(user_message, state)
            state.status = ConversationStatus.RECEPTION_START

        # C. Flujo Normal (Reception Agent)
        else:
            if state.status == ConversationStatus.WELCOME_SENT:
                state.status = ConversationStatus.RECEPTION_START

            logger.info("[ORCHESTRATOR] Enrutando a ReceptionAgent...")
            # Llamada a Reception (Asumimos síncrona por ahora, si la haces async ponle await)
            result = reception_agent.process_message(user_message, state)

            initial_response = result["response"]
            state = result["new_state"]  # Actualizamos el estado con lo que decidió Reception

            # === AUTO-ENRUTAMIENTO (Fast-Track) ===
            # Si Reception decide transferir AHORA MISMO, ejecutamos el siguiente agente
            # en la misma llamada para no hacer esperar al usuario.

            if state.status == ConversationStatus.TRANSFERRED_INFO:
                logger.info("[ORCHESTRATOR] Auto-enrutando a InfoAgent...")
                rag_response = info_agent.process_info_query(user_message, state)
                response_text = f"{initial_response}\n\n{rag_response}"
                state.status = ConversationStatus.RECEPTION_START

            elif state.status == ConversationStatus.TRANSFERRED_CRM:
                logger.info("[ORCHESTRATOR] Auto-enrutando a CRMAgent...")
                # AWAIT: Llamada asíncrona crucial aquí también
                crm_result = await crm_agent.process_lead_handoff(user_message, state)

                # Usamos la respuesta del CRM directamente (o concatenamos según tu preferencia UX)
                response_text = crm_result['response']
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

def _handle_welcome(state: ConversationState, now: datetime, is_new: bool):
    """Maneja la actualización de estado para sesiones nuevas o inactivas."""
    reason = "nueva" if is_new else "inactiva >24h"
    logger.info(f"[ORCHESTRATOR] Sesión {reason} detectada.")
    state.status = ConversationStatus.WELCOME_SENT
    state.last_interaction_timestamp = now
    state_manager.update_state(state)


def _update_history_and_state(state: ConversationState, user_msg: str, agent_msg: str, now: datetime):
    """
    Centraliza la lógica de guardado de historial y persistencia de estado.
    Evita duplicación de código (DRY principle).
    """
    if user_msg:
        state.history.append(f"User: {user_msg}")
    if agent_msg:
        state.history.append(f"Agent: {agent_msg}")

    state.last_interaction_timestamp = now
    state_manager.update_state(state)