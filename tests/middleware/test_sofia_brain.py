# tests/middleware/test_sofia_brain.py
"""
Tests para el cerebro de Sofía (memoria y procesamiento).

Test crítico de memoria:
- Mensaje 1: "Hola, soy Juan"
- Mensaje 2: "¿Cómo me llamo?"
- Resultado esperado: Sofía debe responder "Te llamas Juan" o similar

Ejecutar con: pytest tests/middleware/test_sofia_brain.py -v -s
"""

import pytest
import sys
import os

# Agregar el directorio raíz al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv
load_dotenv()

# Configuración
REDIS_URL = os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

skip_no_redis = pytest.mark.skipif(
    not REDIS_URL,
    reason="REDIS_URL/REDIS_PUBLIC_URL no configurada"
)

skip_no_openai = pytest.mark.skipif(
    not OPENAI_API_KEY,
    reason="OPENAI_API_KEY no configurada"
)


class TestHandoffDetection:
    """Tests para detección de intención de handoff (sin conexiones externas)."""

    @pytest.fixture
    def handoff_keywords(self):
        """Keywords de handoff desde prompts."""
        from prompts.middleware_prompts import HANDOFF_KEYWORDS
        return HANDOFF_KEYWORDS

    def test_detects_asesor_request(self, handoff_keywords):
        """Detecta cuando el usuario pide hablar con asesor."""
        def detect(msg):
            return any(kw in msg.lower() for kw in handoff_keywords)

        assert detect("Quiero hablar con un asesor")
        assert detect("¿Puedo hablar con una persona real?")
        assert detect("Necesito un asesor comercial")

    def test_detects_appointment_request(self, handoff_keywords):
        """Detecta cuando el usuario quiere agendar cita."""
        def detect(msg):
            return any(kw in msg.lower() for kw in handoff_keywords)

        assert detect("Quiero agendar una cita")
        assert detect("Me gustaría agendar visita")

    def test_detects_visit_request(self, handoff_keywords):
        """Detecta cuando el usuario quiere visitar inmueble."""
        def detect(msg):
            return any(kw in msg.lower() for kw in handoff_keywords)

        assert detect("¿Puedo ver el inmueble?")
        assert detect("Quiero visitar el apartamento")
        assert detect("Me gustaría visitar la casa")

    def test_no_handoff_for_normal_messages(self, handoff_keywords):
        """No detecta handoff en mensajes normales."""
        def detect(msg):
            return any(kw in msg.lower() for kw in handoff_keywords)

        assert not detect("Hola, buenos días")
        assert not detect("Busco un apartamento")
        assert not detect("¿Tienen propiedades en El Poblado?")
        assert not detect("Mi presupuesto es de 300 millones")


class TestSofiaBrainMemory:
    """Tests para la memoria de Sofía (requiere Redis y OpenAI)."""

    @pytest.fixture
    def test_session_id(self):
        """ID de sesión para tests."""
        return "+573001234567_test_memory"

    @pytest.fixture
    def sofia_brain(self):
        """Instancia del cerebro de Sofía para tests."""
        from middleware.sofia_brain import SofiaBrain
        return SofiaBrain(redis_url=REDIS_URL)

    @skip_no_redis
    @skip_no_openai
    @pytest.mark.asyncio
    async def test_sofia_remembers_name(self, sofia_brain, test_session_id):
        """
        Test crítico: Sofía debe recordar el nombre del usuario.

        Flujo:
        1. Usuario dice "Hola, soy Juan"
        2. Usuario pregunta "¿Cómo me llamo?"
        3. Sofía debe responder mencionando "Juan"
        """
        # Limpiar historial previo
        await sofia_brain.clear_history(test_session_id)

        # Mensaje 1: Presentación
        response1 = await sofia_brain.process_message(
            session_id=test_session_id,
            user_message="Hola, soy Juan"
        )

        print(f"\n[Test] Usuario: Hola, soy Juan")
        print(f"[Test] Sofía: {response1}")

        assert response1, "Sofía debe responder al saludo"

        # Mensaje 2: Pregunta sobre el nombre
        response2 = await sofia_brain.process_message(
            session_id=test_session_id,
            user_message="¿Cómo me llamo?"
        )

        print(f"\n[Test] Usuario: ¿Cómo me llamo?")
        print(f"[Test] Sofía: {response2}")

        # Verificar que menciona "Juan"
        assert "juan" in response2.lower(), (
            f"Sofía debe recordar que el usuario se llama Juan. "
            f"Respuesta recibida: {response2}"
        )

        # Limpiar después del test
        await sofia_brain.clear_history(test_session_id)

    @skip_no_redis
    @skip_no_openai
    @pytest.mark.asyncio
    async def test_sofia_remembers_preference(self, sofia_brain, test_session_id):
        """
        Test: Sofía debe recordar las preferencias del usuario.

        Flujo:
        1. Usuario dice "Busco un apartamento en El Poblado"
        2. Usuario pregunta "¿Qué zona te dije?"
        3. Sofía debe mencionar "El Poblado"
        """
        # Limpiar historial previo
        await sofia_brain.clear_history(test_session_id)

        # Mensaje 1: Preferencia
        response1 = await sofia_brain.process_message(
            session_id=test_session_id,
            user_message="Busco un apartamento en El Poblado"
        )

        print(f"\n[Test] Usuario: Busco un apartamento en El Poblado")
        print(f"[Test] Sofía: {response1}")

        # Mensaje 2: Verificación
        response2 = await sofia_brain.process_message(
            session_id=test_session_id,
            user_message="¿Qué zona te dije que me interesaba?"
        )

        print(f"\n[Test] Usuario: ¿Qué zona te dije que me interesaba?")
        print(f"[Test] Sofía: {response2}")

        # Verificar que menciona "Poblado"
        assert "poblado" in response2.lower(), (
            f"Sofía debe recordar la zona El Poblado. "
            f"Respuesta recibida: {response2}"
        )

        # Limpiar después del test
        await sofia_brain.clear_history(test_session_id)


# Ejecutar tests si se ejecuta directamente
if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])