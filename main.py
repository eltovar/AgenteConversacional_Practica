# main.py (ORQUESTADOR MULTI-AGENTE)
from state_manager import StateManager, ConversationState, ConversationStatus
from reception_agent import reception_agent
from info_agent import agent as info_agent
from leadsales_agent import LeadSalesAgent
from logging_config import logger
import signal
import sys
import uuid


# ===== INSTANCIAS GLOBALES =====

state_manager = StateManager()
lead_sales_agent = LeadSalesAgent()

# ===== MANEJO DE SEÑALES =====

def signal_handler(sig, frame):
    """Manejo de Ctrl+C para salir limpiamente."""
    print("\n👋 ¡Adiós! Agente detenido.")
    logger.info("Sistema detenido por usuario (Ctrl+C)")
    sys.exit(0)

# ===== ORQUESTADOR PRINCIPAL =====

def main_loop(session_id: str = "default"):
    """
    Bucle principal de interacción en terminal.
    """
    print("=" * 60)
    print("🏢 INMOBILIARIA PROTEGER - Asistente Virtual")
    print("=" * 60)
    print("Bienvenido. Escribe 'salir' o presiona Ctrl+C para terminar.")
    print("-" * 60)

    logger.info(f"Sistema iniciado. Session ID: {session_id}")

    while True:
        try:
            user_input = input("\nTú: ").strip()
            if user_input.lower() == "salir":
                print("👋 ¡Adiós! Agente detenido.")
                logger.info("Sistema detenido por usuario (comando 'salir')")
                break

            # 1. OBTENER ESTADO ACTUAL
            state = state_manager.get_state(session_id)
            logger.info(f"[MAIN] Estado actual: {state.status}")

            # 2. ROUTER BASADO EN ESTADO
            
            if state.status == ConversationStatus.TRANSFERRED_INFO:
                # ... Lógica de InfoAgent con inyección de estado (para memoria de usuario) ...

                logger.info("[MAIN] Enrutando a InfoAgent...")
                response = info_agent.process_info_query(user_input, state)
                print(f"\n🤖 Agente: {response}")

                # Después de responder, volver a RECEPTION_START para siguiente consulta
                state.status = ConversationStatus.RECEPTION_START
                state_manager.update_state(state)

            elif state.status == ConversationStatus.TRANSFERRED_LEADSALES:
                # --- NUEVA LÓGICA DE ENRUTAMIENTO ---
                logger.info("[MAIN] Enrutando a LeadSalesAgent...")
                
                # Ejecutar el agente
                result = lead_sales_agent.process_lead_handoff(user_input, state)
                
                response = result["response"]
                new_state = result.get("new_state", state)
                # El LeadSalesAgent retorna el estado sin cambiar, main.py lo resetea:
                new_state.status = ConversationStatus.RECEPTION_START 
                state_manager.update_state(new_state)
                
                print(f"\n🤖 Agente: {response}")
                # --- FIN NUEVA LÓGICA ---

            else:
                # Estados manejados por ReceptionAgent: RECEPTION_START, AWAITING_CLARIFICATION, AWAITING_LEAD_NAME
                # ... Lógica de ReceptionAgent (sin cambios) ...
                logger.info("[MAIN] Enrutando a ReceptionAgent...")
                result = reception_agent.process_message(user_input, state)

                response = result["response"]
                new_state = result["new_state"]

                # Actualizar estado
                state_manager.update_state(new_state)

                print(f"\n🤖 Agente: {response}")

        except KeyboardInterrupt:
            # Capturado por signal_handler, pero por si acaso
            print("\n👋 ¡Hasta pronto!")
            break

        except Exception as e:
            logger.error(f"[MAIN] Error inesperado: {e}", exc_info=True)
            print(f"\n❌ Error: Lo siento, ocurrió un error inesperado. Por favor, intenta de nuevo.")

            # Resetear estado en caso de error crítico
            state = state_manager.get_state(session_id)
            state.status = ConversationStatus.RECEPTION_START
            state_manager.update_state(state)

    logger.info("Sistema finalizado correctamente")

# ===== ENTRYPOINT =====

if __name__ == "__main__":
    # Configurar manejador de señales
    signal.signal(signal.SIGINT, signal_handler)

    # Generar session_id único (o usar "default" para sesión única)
    # session_id = str(uuid.uuid4())  # Para sesiones múltiples
    session_id = "default"  # Para sesión única en CLI

    # Iniciar bucle principal
    main_loop(session_id)

