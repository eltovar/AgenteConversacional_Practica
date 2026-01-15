# main.py (CLI WRAPPER - VERSIÓN REFACTORIZADA)
"""
Interfaz CLI para el sistema multi-agente.
Delega toda la lógica de negocio a orchestrator.py.
"""

from agents.orchestrator import process_message
from agents.InfoAgent.info_agent import agent  # Para comando /reload
from logging_config import logger
import signal
import sys


# ===== MANEJO DE SEÑALES =====

def signal_handler(sig, frame):
    """Manejo de Ctrl+C para salir limpiamente."""
    print("\n👋 ¡Adiós! Agente detenido.")
    logger.info("Sistema detenido por usuario (Ctrl+C)")
    sys.exit(0)


# ===== BUCLE CLI =====

def main_loop(session_id: str = "default"):
    """
    Bucle CLI para interacción local.
    Toda la lógica de negocio está en orchestrator.py.
    """
    print("=" * 60)
    print("🏢 INMOBILIARIA PROTEGER - Asistente Virtual")
    print("=" * 60)
    print("Bienvenido. Escribe 'salir' o presiona Ctrl+C para terminar.")
    print("Comandos especiales: /reload (recargar base de conocimiento)")
    print("-" * 60)

    logger.info(f"Sistema iniciado. Session ID: {session_id}")

    while True:
        try:
            user_input = input("\nTú: ").strip()

            if user_input.lower() == "salir":
                print("👋 ¡Adiós! Agente detenido.")
                logger.info("Sistema detenido por usuario (comando 'salir')")
                break

            # COMANDO ESPECIAL: Recarga de base de conocimiento
            if user_input.lower() == "/reload":
                print("\n🔄 Recargando base de conocimiento...")
                result = agent.reload_knowledge_base()

                if result.get("status") == "success":
                    print(f"✅ Recarga exitosa: {result.get('files_loaded')} archivos cargados")
                    logger.info(f"[CLI] Recarga manual exitosa: {result.get('files_loaded')} archivos")
                else:
                    print(f"❌ Error en recarga: {result.get('message')}")
                    logger.error(f"[CLI] Error en recarga manual: {result.get('message')}")
                continue

            # DELEGAR A ORCHESTRATOR
            result = process_message(session_id, user_input)

            # Mostrar respuesta
            print(f"\n🤖 Sofía: {result['response']}")

        except KeyboardInterrupt:
            print("\n👋 ¡Hasta pronto!")
            break

        except Exception as e:
            logger.error(f"[CLI] Error inesperado: {e}", exc_info=True)
            print(f"\n❌ Error: Lo siento, ocurrió un error inesperado.")

    logger.info("Sistema finalizado correctamente")


# ===== ENTRYPOINT =====

if __name__ == "__main__":
    # Configurar manejador de señales
    signal.signal(signal.SIGINT, signal_handler)

    # Session ID para CLI (siempre "default")
    session_id = "default"

    # Iniciar bucle principal
    main_loop(session_id)