# test_crm_prompt_local.py
"""
Script de prueba local para verificar que el CRM prompt
hace todas las preguntas en UN SOLO mensaje.

Ejecutar: python test_crm_prompt_local.py
"""

from dotenv import load_dotenv
load_dotenv()

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from prompts.crm_prompts import CRM_SYSTEM_PROMPT

def test_crm_single_message_questions():
    """
    Simula una conversación donde el usuario expresa interés en arriendo.
    Verifica que Sofía haga todas las preguntas en un solo mensaje.
    """
    print("=" * 60)
    print("TEST: CRM Prompt - Preguntas en UN SOLO mensaje")
    print("=" * 60)

    # Crear cliente LLM
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.1)

    # Escenario 1: Usuario solo dice que quiere arrendar
    print("\n--- ESCENARIO 1: Usuario solo expresa interés en arriendo ---")
    messages = [
        SystemMessage(content=CRM_SYSTEM_PROMPT),
        HumanMessage(content="Hola, estoy interesado en arrendar un apartamento")
    ]

    response = llm.invoke(messages)
    print(f"\nUsuario: Hola, estoy interesado en arrendar un apartamento")
    print(f"\nSofía responde:\n{response.content}")

    # Análisis del resultado
    print("\n--- ANÁLISIS ---")
    response_text = response.content.lower()

    checks = {
        "Pregunta tipo inmueble": any(x in response_text for x in ["tipo", "casa", "apartamento", "local"]),
        "Pregunta zona": any(x in response_text for x in ["zona", "barrio", "ubicacion", "donde"]),
        "Pregunta presupuesto": any(x in response_text for x in ["presupuesto", "precio", "rango"]),
        "Pregunta nombre": any(x in response_text for x in ["nombre", "como te llamas"]),
    }

    print("Verificacion de preguntas incluidas:")
    for check, passed in checks.items():
        status = "[OK]" if passed else "[X]"
        print(f"  {status} {check}")

    all_passed = all(checks.values())
    print(f"\n{'[OK] EXITO' if all_passed else '[X] FALLO'}: {'Todas las preguntas en un mensaje' if all_passed else 'Faltan preguntas'}")

    # Escenario 2: Usuario ya mencionó que busca apartamento
    print("\n" + "=" * 60)
    print("--- ESCENARIO 2: Usuario ya dio información parcial ---")
    messages2 = [
        SystemMessage(content=CRM_SYSTEM_PROMPT),
        HumanMessage(content="Quiero arrendar un apartamento de 3 habitaciones en el Poblado")
    ]

    response2 = llm.invoke(messages2)
    print(f"\nUsuario: Quiero arrendar un apartamento de 3 habitaciones en el Poblado")
    print(f"\nSofía responde:\n{response2.content}")

    print("\n--- ANÁLISIS ---")
    response_text2 = response2.content.lower()

    # Verificar que NO repite lo que ya dijo el usuario
    no_repeat_checks = {
        "NO pregunta tipo (ya dijo apartamento)": "que tipo" not in response_text2,
        "NO pregunta habitaciones (ya dijo 3)": "cuantas habitaciones" not in response_text2,
        "NO pregunta zona (ya dijo Poblado)": "que zona" not in response_text2 and "en donde" not in response_text2,
    }

    print("Verificacion de NO repetir preguntas:")
    for check, passed in no_repeat_checks.items():
        status = "[OK]" if passed else "[!]"
        print(f"  {status} {check}")

    # Escenario 3: Usuario quiere hablar directo con asesor
    print("\n" + "=" * 60)
    print("--- ESCENARIO 3: Usuario solo quiere hablar con asesor ---")
    messages3 = [
        SystemMessage(content=CRM_SYSTEM_PROMPT),
        HumanMessage(content="Solo quiero que me contacte un asesor, no quiero dar más información")
    ]

    response3 = llm.invoke(messages3)
    print(f"\nUsuario: Solo quiero que me contacte un asesor, no quiero dar más información")
    print(f"\nSofía responde:\n{response3.content}")

    print("\n--- ANÁLISIS ---")
    response_text3 = response3.content.lower()

    # Verificar que solo pide el nombre
    minimal_check = "nombre" in response_text3
    print(f"{'[OK]' if minimal_check else '[X]'} Pide solo el nombre (respeta preferencia del usuario)")

    print("\n" + "=" * 60)
    print("TEST COMPLETADO")
    print("=" * 60)

if __name__ == "__main__":
    test_crm_single_message_questions()