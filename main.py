# main.py
from agent import agent
import signal
import sys

def signal_handler(sig, frame):
    """Manejo de Ctrl+C para salir limpiamente."""
    print("\n👋 ¡Adiós! Agente detenido.")
    sys.exit(0)

def main_loop():
    """Bucle principal de interacción en terminal."""
    print("-----------------------------------------------------")
    print("Agente Conversational gent iniciado.")
    print("Escribe 'salir' o presiona Ctrl+C para terminar.")
    print("-----------------------------------------------------")

    while True:
        try:
            user_input = input("Tú: ")
            if user_input.lower() in ["salir", "exit", "quit"]:
                break
            
            # Procesar la consulta y obtener la respuesta
            response = agent.process_query(user_input)
            print(response)

        except Exception as e:
            print(f"Error inesperado: {e}")
            break

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler) # Captura Ctrl+C
    main_loop()