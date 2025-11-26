# tests/test_persona.py
"""
Test de validación tonal para verificar que todos los agentes usen la personalidad de Sofía.
Verifica el uso de tuteo ("tú") y tono profesional/cercano en las respuestas.
"""

import pytest
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from state_manager import StateManager, ConversationState, ConversationStatus
from reception_agent import reception_agent
from info_agent import agent as info_agent
from leadsales_agent import lead_sales_agent
import re


# ===== FIXTURES =====

@pytest.fixture
def state_manager():
    """
    Fixture para crear un StateManager que se conecta a Redis real.

    NOTA: Este test requiere conexión a Redis (Railway).
          Se skipea automáticamente si se ejecuta localmente.
    """
    if not os.getenv("REDIS_URL"):
        pytest.skip("REDIS_URL no configurada. Ejecutar tests en Railway o configurar Redis local")

    try:
        manager = StateManager()
        # Forzar inicialización para capturar errores de conexión en el fixture
        manager._ensure_redis_initialized()
        return manager
    except (ConnectionError, Exception) as e:
        pytest.skip(f"No se puede conectar a Redis: {e}")


@pytest.fixture
def session_id():
    """Fixture para el session_id de prueba."""
    return "test_persona_session"


@pytest.fixture
def clean_state(state_manager, session_id):
    """
    Fixture para obtener un estado limpio por test.

    NOTA: Limpia el estado antes y después del test para evitar contaminación.
    """
    # Crear un estado limpio
    state = state_manager.get_state(session_id)

    yield state

    # Cleanup: Eliminar la sesión de test después del test
    try:
        key = f"session:{session_id}"
        state_manager.client.delete(key)
    except Exception:
        pass  # Ignorar errores de cleanup


# ===== HELPERS DE VALIDACIÓN =====

def assert_tuteo(response: str) -> bool:
    """
    Verifica que la respuesta use tuteo ("tú", "te", "tu", "tienes").
    Acepta también respuestas que no usen pronombres directos pero mantengan tono cercano.
    """
    # Palabras clave de tuteo (case insensitive)
    tuteo_patterns = [
        r'\bt[uú]\b',           # "tú"
        r'\bte\b',              # "te"
        r'\btu\b',              # "tu" (posesivo)
        r'\btienes\b',          # "tienes"
        r'\bpuedes\b',          # "puedes"
        r'\bquieres\b',         # "quieres"
        r'\bnecesitas\b',       # "necesitas"
        r'\bpodr[ií]as\b',      # "podrías"
    ]

    # Buscar cualquier patrón de tuteo
    has_tuteo = any(re.search(pattern, response, re.IGNORECASE) for pattern in tuteo_patterns)

    # Verificar que no haya "usted" (fallo crítico)
    has_usted = re.search(r'\busted\b', response, re.IGNORECASE) is not None

    if has_usted:
        pytest.fail(f"FALLO: Se detectó 'usted' en la respuesta (debe usar tuteo)")

    # Si no hay tuteo ni usted, es neutral (warning pero pasa)
    if not has_tuteo:
        print(f"\n  ⚠️  Advertencia: Tono neutral (sin tuteo explícito)")

    return not has_usted  # Pasa si NO tiene "usted"


def assert_tono_profesional_cercano(response: str) -> bool:
    """
    Verifica que el tono sea profesional pero cercano.
    Características esperadas:
    - NO usa jerga coloquial excesiva
    - NO es excesivamente formal
    """
    # Verificar ausencia de jerga coloquial excesiva
    jerga_excesiva = ['brother', 'parce', 'loco', 'man', 'pana']
    for palabra in jerga_excesiva:
        if palabra in response.lower():
            pytest.fail(f"FALLO: Jerga coloquial excesiva detectada: '{palabra}'")

    # Verificar que no sea demasiado formal
    formalidad_excesiva = ['estimado', 'estimada', 'cordialmente', 'atentamente']
    for palabra in formalidad_excesiva:
        if palabra in response.lower():
            pytest.fail(f"FALLO: Tono excesivamente formal: '{palabra}'")

    return True


# ===== TESTS PARA RECEPTION AGENT =====

def test_reception_saludo(clean_state):
    """Test 1: ReceptionAgent - Saludo inicial (clasificación ambigua)"""
    clean_state.status = ConversationStatus.RECEPTION_START

    result = reception_agent.process_message("Hola", clean_state)
    response = result["response"]

    print(f"\n  📝 Respuesta: {response}")

    assert assert_tuteo(response), "Debe usar tuteo"
    assert assert_tono_profesional_cercano(response), "Debe tener tono profesional/cercano"


def test_reception_clasificacion_info(clean_state):
    """Test 2: ReceptionAgent - Clasificación de intención 'info'"""
    clean_state.status = ConversationStatus.RECEPTION_START

    result = reception_agent.process_message("¿Cuál es la misión de la empresa?", clean_state)
    response = result["response"]

    print(f"\n  📝 Respuesta: {response}")

    assert assert_tuteo(response), "Debe usar tuteo"
    assert assert_tono_profesional_cercano(response), "Debe tener tono profesional/cercano"


def test_reception_captura_nombre(clean_state):
    """Test 3: ReceptionAgent - Captura de nombre (estado AWAITING_LEAD_NAME)"""
    clean_state.status = ConversationStatus.AWAITING_LEAD_NAME

    result = reception_agent.process_message("Me llamo Carlos Martínez", clean_state)
    response = result["response"]

    print(f"\n  📝 Respuesta: {response}")

    # Verificar que use el nombre capturado
    assert "Carlos" in response, "Debe usar el nombre del usuario"
    assert assert_tuteo(response), "Debe usar tuteo"
    assert assert_tono_profesional_cercano(response), "Debe tener tono profesional/cercano"


# ===== TESTS PARA INFO AGENT =====

def test_info_rag_query(clean_state):
    """Test 4: InfoAgent - Consulta RAG (información institucional)"""
    clean_state.status = ConversationStatus.TRANSFERRED_INFO

    response = info_agent.process_info_query("¿Cuáles son los horarios de atención?", clean_state)

    print(f"\n  📝 Respuesta: {response}")

    assert assert_tuteo(response), "Debe usar tuteo"
    assert assert_tono_profesional_cercano(response), "Debe tener tono profesional/cercano"


def test_info_conversacional(clean_state):
    """Test 5: InfoAgent - Pregunta conversacional (LLM base, sin RAG)"""
    clean_state.status = ConversationStatus.TRANSFERRED_INFO

    response = info_agent.process_info_query("¿Cómo te llamas?", clean_state)

    print(f"\n  📝 Respuesta: {response}")

    assert assert_tuteo(response), "Debe usar tuteo"
    assert assert_tono_profesional_cercano(response), "Debe tener tono profesional/cercano"


def test_info_con_contexto_usuario(clean_state):
    """Test 6: InfoAgent - Consulta con contexto de usuario (nombre conocido)"""
    clean_state.status = ConversationStatus.TRANSFERRED_INFO
    clean_state.lead_data['name'] = "María González"

    response = info_agent.process_info_query("¿Qué comisiones cobran?", clean_state)

    print(f"\n  📝 Respuesta: {response}")

    # Verificar que use el nombre del usuario (opcional, warning si no)
    if "María" not in response and "maría" not in response:
        print("\n  ⚠️  Advertencia: No usa el nombre del usuario en la respuesta")

    assert assert_tuteo(response), "Debe usar tuteo"
    assert assert_tono_profesional_cercano(response), "Debe tener tono profesional/cercano"


# ===== TESTS PARA LEADSALES AGENT =====

def test_leadsales_handoff(clean_state):
    """Test 7: LeadSalesAgent - Confirmación de handoff (TRANSFERRED_LEADSALES)"""
    clean_state.status = ConversationStatus.TRANSFERRED_LEADSALES
    clean_state.lead_data['name'] = "Juan Pérez"

    result = lead_sales_agent.process_lead_handoff("Quiero vender mi apartamento", clean_state)
    response = result["response"]

    print(f"\n  📝 Respuesta: {response}")

    # Verificar que use el nombre del lead (crítico)
    assert "Juan" in response or "juan" in response, "Debe usar el nombre del lead"
    assert assert_tuteo(response), "Debe usar tuteo"
    assert assert_tono_profesional_cercano(response), "Debe tener tono profesional/cercano"


def test_leadsales_tono_ventas(clean_state):
    """Test 8: LeadSalesAgent - Verificar tono orientado a ventas pero cercano"""
    clean_state.status = ConversationStatus.TRANSFERRED_LEADSALES
    clean_state.lead_data['name'] = "Ana Rodríguez"

    result = lead_sales_agent.process_lead_handoff("Necesito arrendar urgente", clean_state)
    response = result["response"]

    print(f"\n  📝 Respuesta: {response}")

    # Verificar que mencione contacto de asesor (crítico para lead sales)
    menciona_asesor = any(word in response.lower() for word in ['asesor', 'contacto', 'pondrá en contacto'])
    assert menciona_asesor, "Debe mencionar contacto de asesor en respuesta de lead sales"

    assert assert_tuteo(response), "Debe usar tuteo"
    assert assert_tono_profesional_cercano(response), "Debe tener tono profesional/cercano"
