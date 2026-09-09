# tests/test_greeting_local.py
"""
Script para probar el saludo dinámico localmente sin servidor.
Ejecutar: python tests/test_greeting_local.py
"""
import sys
import os

# Agregar el directorio raíz al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from agents.orchestrator import _generate_dynamic_greeting


def test_greeting(message: str):
    """Prueba el saludo dinámico con un mensaje específico."""
    print(f"\n{'='*60}")
    print(f"📨 MENSAJE DEL CLIENTE: {message}")
    print(f"{'='*60}")

    response = _generate_dynamic_greeting(message)

    print(f"\n🤖 RESPUESTA DE SOFÍA:")
    print(f"   {response}")
    print()
    return response


if __name__ == "__main__":
    print("\n" + "═"*60)
    print(" TEST DE SALUDO DINÁMICO - SOFÍA")
    print("═"*60)

    # Lista de mensajes a probar
    test_messages = [
        # Saludos simples
        "Hola",
        "Buenos días",
        "Buenas noches",
        "Hey",

        # Links de portales inmobiliarios
        "https://www.fincaraiz.com.co/apartamento-en-arriendo/medellin/el-poblado/codigo-12345678",
        "https://www.metrocuadrado.com/inmueble/venta-casa-envigado-3-habitaciones",
        "https://www.instagram.com/p/ABC123xyz/ vi este apartamento en su Instagram!",

        # Saludos con intención
        "Hola, busco apartamento en arriendo",
        "Buenos días, quiero vender mi casa",
    ]

    # Si se pasa un argumento, usar ese mensaje
    if len(sys.argv) > 1:
        custom_message = " ".join(sys.argv[1:])
        test_greeting(custom_message)
    else:
        # Probar todos los mensajes predefinidos
        for msg in test_messages:
            test_greeting(msg)

    print("\n✅ Test completado")