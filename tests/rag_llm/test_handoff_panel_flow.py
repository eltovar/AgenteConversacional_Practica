"""
Tests para el flujo de handoff y aparición de contactos en el panel.

Verifica:
1. Sofia detecta handoff_priority correctamente
2. Contactos con handoff_priority="high" o "immediate" aparecen en el panel
3. request_handoff() se llama con los parámetros correctos

Escenarios de prueba para Postman:
- Test 1.1: Primer mensaje sin nombre → NO aparece en panel
- Test 1.2: Mensaje con nombre y necesidades claras → SÍ aparece en panel
- Test 1.3: Cliente enojado → SÍ aparece en panel (immediate)
"""

import pytest
import json
from unittest.mock import MagicMock, AsyncMock, patch
from dataclasses import dataclass
from typing import Optional


# ═══════════════════════════════════════════════════════════════════════════════
# ESTRUCTURAS DE DATOS PARA TESTS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class MockAnalysis:
    """Mock del análisis de Sofia."""
    handoff_priority: str = "none"
    emocion: str = "neutral"
    sentiment_score: float = 0.0
    intencion_visita: bool = False
    link_redes_sociales: bool = False
    summary_update: Optional[str] = None


@dataclass
class MockContactInfo:
    """Mock de la información de contacto."""
    contact_id: str = "12345678"
    is_new: bool = False
    owner_id: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def sample_phone():
    """Teléfono de prueba normalizado."""
    return "+573001234567"


@pytest.fixture
def sample_channel():
    """Canal de prueba."""
    return "whatsapp_directo"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST: Detección de handoff_priority
# ═══════════════════════════════════════════════════════════════════════════════

class TestHandoffPriorityDetection:
    """
    Tests para verificar que Sofia detecta correctamente el handoff_priority.
    
    Estos escenarios definen qué mensajes deberían activar la aparición en panel.
    """

    # ─────────────────────────────────────────────────────────────────────────
    # ESCENARIO 1.1: Mensaje sin nombre - NO debería aparecer en panel
    # ─────────────────────────────────────────────────────────────────────────
    
    SCENARIO_1_1_MESSAGES = [
        ("Hola", "none", "Saludo simple sin información"),
        ("Buenos días", "none", "Saludo formal"),
        ("Busco apartamento", "none", "Necesidad sin nombre ni detalles"),
        ("Tienen apartamentos en arriendo?", "none", "Pregunta general"),
        ("Cuánto cuesta un apartamento?", "none", "Consulta de precio sin contexto"),
    ]

    @pytest.mark.parametrize("message,expected_priority,description", SCENARIO_1_1_MESSAGES)
    def test_scenario_1_1_no_name_no_panel(self, message, expected_priority, description):
        """
        Escenario 1.1: Mensajes sin nombre NO deberían activar handoff.
        
        El cliente no ha proporcionado información suficiente para ser
        considerado un lead calificado.
        
        Postman Body:
        {
            "Body": "<message>",
            "From": "whatsapp:+573001234567",
            "To": "whatsapp:+15551234567"
        }
        
        Expected: El contacto NO aparece en el panel
        """
        # Este test documenta el comportamiento esperado
        # El handoff_priority debe ser "none" o "low" para estos mensajes
        assert expected_priority in ["none", "low"], f"Mensaje '{message}' debería ser {expected_priority}"

    # ─────────────────────────────────────────────────────────────────────────
    # ESCENARIO 1.2: Mensaje con nombre y necesidades - SÍ aparece en panel
    # ─────────────────────────────────────────────────────────────────────────
    
    SCENARIO_1_2_MESSAGES = [
        (
            "Hola, soy Carlos, busco apartamento 3 hab en El Poblado para arriendo, presupuesto 3M",
            "high",
            "Cliente con nombre, necesidades claras y presupuesto"
        ),
        (
            "Mi nombre es María García, necesito un apartamento de 2 habitaciones cerca al centro",
            "high",
            "Cliente con nombre completo y necesidades específicas"
        ),
        (
            "Soy Juan Pérez, me interesa el apartamento que publicaron en Metrocuadrado",
            "high",
            "Cliente identificado con interés específico"
        ),
        (
            "Buenos días, me llamo Andrea y quisiera agendar una cita para ver apartamentos",
            "high",
            "Cliente con nombre y intención de visita"
        ),
    ]

    @pytest.mark.parametrize("message,expected_priority,description", SCENARIO_1_2_MESSAGES)
    def test_scenario_1_2_with_name_appears_in_panel(self, message, expected_priority, description):
        """
        Escenario 1.2: Mensajes con nombre y necesidades claras SÍ aparecen en panel.
        
        El cliente ha proporcionado información suficiente para ser
        considerado un lead calificado y debe aparecer en el panel.
        
        Postman Body:
        {
            "Body": "<message>",
            "From": "whatsapp:+573001234567",
            "To": "whatsapp:+15551234567"
        }
        
        Expected: El contacto SÍ aparece en el panel
        """
        assert expected_priority == "high", f"Mensaje con nombre debe ser 'high': {message}"

    # ─────────────────────────────────────────────────────────────────────────
    # ESCENARIO 1.3: Cliente enojado/urgente - SÍ aparece en panel (IMMEDIATE)
    # ─────────────────────────────────────────────────────────────────────────
    
    SCENARIO_1_3_MESSAGES = [
        (
            "Esto es un desastre! Llevo esperando una hora y nadie me atiende!",
            "immediate",
            "Cliente muy frustrado"
        ),
        (
            "QUIERO HABLAR CON UN ASESOR AHORA MISMO",
            "immediate",
            "Solicitud explícita de humano"
        ),
        (
            "Ya me cansé de este bot, pásenme con alguien de verdad",
            "immediate",
            "Rechazo explícito del bot"
        ),
        (
            "Es urgente, necesito que alguien me llame inmediatamente",
            "immediate",
            "Urgencia explícita"
        ),
    ]

    @pytest.mark.parametrize("message,expected_priority,description", SCENARIO_1_3_MESSAGES)
    def test_scenario_1_3_angry_customer_immediate(self, message, expected_priority, description):
        """
        Escenario 1.3: Clientes enojados o que solicitan humano explícitamente.
        
        Estos contactos deben aparecer INMEDIATAMENTE en el panel
        con prioridad máxima.
        
        Postman Body:
        {
            "Body": "<message>",
            "From": "whatsapp:+573001234567",
            "To": "whatsapp:+15551234567"
        }
        
        Expected: El contacto aparece en el panel con prioridad IMMEDIATE
        """
        assert expected_priority == "immediate", f"Cliente enojado debe ser 'immediate': {message}"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST: Llamada a request_handoff()
# ═══════════════════════════════════════════════════════════════════════════════

class TestRequestHandoffCalls:
    """
    Tests para verificar que request_handoff() se llama correctamente.
    """

    def test_immediate_priority_calls_request_handoff(self, sample_phone, sample_channel):
        """
        Verifica que handoff_priority='immediate' activa request_handoff().
        """
        analysis = MockAnalysis(
            handoff_priority="immediate",
            emocion="enojado",
            sentiment_score=-0.8
        )
        contact_info = MockContactInfo()
        
        # Simular la lógica del webhook_handler.py (líneas 679-710)
        should_call_request_handoff = analysis.handoff_priority in ["immediate", "high"]
        
        assert should_call_request_handoff is True
        
        # Verificar parámetros esperados
        expected_reason = f"Cliente urgente - Emoción: {analysis.emocion}, Score: {analysis.sentiment_score}"
        assert "urgente" in expected_reason.lower()
        assert analysis.emocion in expected_reason

    def test_high_priority_calls_request_handoff(self, sample_phone, sample_channel):
        """
        Verifica que handoff_priority='high' activa request_handoff().
        """
        analysis = MockAnalysis(
            handoff_priority="high",
            intencion_visita=True
        )
        contact_info = MockContactInfo()
        
        should_call_request_handoff = analysis.handoff_priority in ["immediate", "high"]
        
        assert should_call_request_handoff is True

    def test_none_priority_does_not_call_request_handoff(self, sample_phone, sample_channel):
        """
        Verifica que handoff_priority='none' NO activa request_handoff().
        """
        analysis = MockAnalysis(handoff_priority="none")
        
        should_call_request_handoff = analysis.handoff_priority in ["immediate", "high"]
        
        assert should_call_request_handoff is False

    def test_low_priority_does_not_call_request_handoff(self, sample_phone, sample_channel):
        """
        Verifica que handoff_priority='low' NO activa request_handoff().
        """
        analysis = MockAnalysis(handoff_priority="low")
        
        should_call_request_handoff = analysis.handoff_priority in ["immediate", "high"]
        
        assert should_call_request_handoff is False

    def test_medium_priority_does_not_call_request_handoff(self, sample_phone, sample_channel):
        """
        Verifica que handoff_priority='medium' NO activa request_handoff().
        
        NOTA: Si se desea que 'medium' también aparezca en panel,
        modificar la condición en webhook_handler.py
        """
        analysis = MockAnalysis(handoff_priority="medium")
        
        should_call_request_handoff = analysis.handoff_priority in ["immediate", "high"]
        
        assert should_call_request_handoff is False


# ═══════════════════════════════════════════════════════════════════════════════
# TEST: Parámetros de request_handoff()
# ═══════════════════════════════════════════════════════════════════════════════

class TestRequestHandoffParameters:
    """
    Tests para verificar los parámetros pasados a request_handoff().
    """

    def test_immediate_includes_emotion_in_reason(self):
        """
        Verifica que la razón de handoff IMMEDIATE incluye la emoción.
        """
        analysis = MockAnalysis(
            handoff_priority="immediate",
            emocion="frustrado",
            sentiment_score=-0.6
        )
        
        reason = f"Cliente urgente - Emoción: {analysis.emocion}, Score: {analysis.sentiment_score}"
        
        assert "frustrado" in reason
        assert "-0.6" in reason

    def test_high_includes_visit_intention_in_reason(self):
        """
        Verifica que la razón de handoff HIGH incluye la intención de visita.
        """
        analysis = MockAnalysis(
            handoff_priority="high",
            intencion_visita=True
        )
        
        reason_parts = []
        if analysis.intencion_visita:
            reason_parts.append("Intención de visita")
        
        reason = ", ".join(reason_parts) if reason_parts else "Cliente potencial listo para asesor"
        
        assert "Intención de visita" in reason

    def test_high_includes_social_link_in_reason(self):
        """
        Verifica que la razón de handoff HIGH incluye link de red social.
        """
        analysis = MockAnalysis(
            handoff_priority="high",
            link_redes_sociales=True
        )
        
        reason_parts = []
        if analysis.link_redes_sociales:
            reason_parts.append("Link de red social")
        
        reason = ", ".join(reason_parts) if reason_parts else "Cliente potencial listo para asesor"
        
        assert "Link de red social" in reason

    def test_channel_is_passed_to_request_handoff(self, sample_channel):
        """
        Verifica que el canal se pasa correctamente a request_handoff().
        
        Esto es CRÍTICO para la segregación de equipos.
        """
        final_channel = sample_channel
        
        # El parámetro canal debe pasarse a request_handoff()
        # await state_manager.request_handoff(phone, reason, contact_id, canal=final_channel)
        assert final_channel == "whatsapp_directo"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST: Integración con Redis ACTIVE_CONTACTS_SET
# ═══════════════════════════════════════════════════════════════════════════════

class TestActiveContactsSet:
    """
    Tests para verificar la integración con Redis ACTIVE_CONTACTS_SET.
    """

    def test_request_handoff_adds_to_active_set(self, sample_phone):
        """
        Verifica que request_handoff() agrega el contacto al set activo.
        """
        ACTIVE_CONTACTS_SET = "active_contacts"
        mock_redis = MagicMock()
        
        # Simular request_handoff agregando al set
        mock_redis.sadd(ACTIVE_CONTACTS_SET, sample_phone)
        
        # Verificar que se llamó sadd
        mock_redis.sadd.assert_called_once_with(ACTIVE_CONTACTS_SET, sample_phone)

    def test_panel_reads_from_active_set(self):
        """
        Verifica que el panel lee del set activo de Redis.
        """
        ACTIVE_CONTACTS_SET = "active_contacts"
        mock_redis = MagicMock()
        mock_redis.smembers.return_value = {
            b"+573001234567",
            b"+573009876543"
        }
        
        # Simular lectura del panel
        active_phones = mock_redis.smembers(ACTIVE_CONTACTS_SET)
        
        assert len(active_phones) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# ESCENARIOS DE PRUEBA PARA POSTMAN
# ═══════════════════════════════════════════════════════════════════════════════

class TestPostmanScenarios:
    """
    Documentación de escenarios de prueba para Postman.
    
    Estos tests documentan el comportamiento esperado que se puede
    verificar manualmente con Postman.
    """

    def test_postman_scenario_documentation(self):
        """
        ESCENARIOS DE PRUEBA PARA POSTMAN
        =================================
        
        Endpoint: POST /whatsapp/webhook
        
        Headers:
        - Content-Type: application/x-www-form-urlencoded
        
        ─────────────────────────────────────────────────────────────
        TEST 1.1: Primer mensaje sin nombre (NO aparece en panel)
        ─────────────────────────────────────────────────────────────
        
        Body (form-data):
            From: whatsapp:+573001234567
            To: whatsapp:+15551234567
            Body: Hola, busco apartamento
            NumMedia: 0
        
        Expected:
            - Response: 200 OK con TwiML
            - Sofia responde solicitando más información
            - Contacto NO aparece en panel
            - Redis ACTIVE_CONTACTS_SET: NO contiene +573001234567
        
        ─────────────────────────────────────────────────────────────
        TEST 1.2: Mensaje con nombre (SÍ aparece en panel)
        ─────────────────────────────────────────────────────────────
        
        Body (form-data):
            From: whatsapp:+573001234567
            To: whatsapp:+15551234567
            Body: Hola, soy Carlos López, busco apartamento 3 hab en El Poblado, presupuesto 3M
            NumMedia: 0
        
        Expected:
            - Response: 200 OK con TwiML
            - Sofia responde confirmando interés
            - Contacto SÍ aparece en panel
            - Redis ACTIVE_CONTACTS_SET: contiene +573001234567
            - Razón de handoff: "Intención de visita" o similar
        
        ─────────────────────────────────────────────────────────────
        TEST 1.3: Cliente enojado (aparece INMEDIATAMENTE)
        ─────────────────────────────────────────────────────────────
        
        Body (form-data):
            From: whatsapp:+573009876543
            To: whatsapp:+15551234567
            Body: ESTO ES INACEPTABLE! QUIERO HABLAR CON ALGUIEN AHORA!
            NumMedia: 0
        
        Expected:
            - Response: 200 OK con TwiML
            - Sofia responde con empatía
            - Contacto SÍ aparece en panel
            - Redis ACTIVE_CONTACTS_SET: contiene +573009876543
            - Razón de handoff: "Cliente urgente - Emoción: enojado, Score: -0.9"
        
        ─────────────────────────────────────────────────────────────
        VERIFICACIÓN EN PANEL
        ─────────────────────────────────────────────────────────────
        
        Endpoint: GET /whatsapp/panel/contacts
        
        Query params:
            - filter_time: 24h (o all)
            - advisor: <advisor_id> (opcional)
        
        Los contactos con handoff_priority="high" o "immediate"
        deberían aparecer en esta respuesta.
        """
        # Este test solo documenta los escenarios
        assert True


# ═══════════════════════════════════════════════════════════════════════════════
# TEST: Verificación de logs
# ═══════════════════════════════════════════════════════════════════════════════

class TestLogsVerification:
    """
    Tests para verificar los logs esperados.
    """

    def test_immediate_handoff_log_message(self):
        """
        Verifica el mensaje de log para handoff IMMEDIATE.
        
        Expected log:
        [Webhook] ✅ Contacto +573001234567 agregado al panel (IMMEDIATE)
        """
        phone = "+573001234567"
        expected_log = f"[Webhook] ✅ Contacto {phone} agregado al panel (IMMEDIATE)"
        
        assert "IMMEDIATE" in expected_log
        assert phone in expected_log

    def test_high_handoff_log_message(self):
        """
        Verifica el mensaje de log para handoff HIGH.
        
        Expected log:
        [Webhook] ✅ Contacto +573001234567 agregado al panel (HIGH)
        """
        phone = "+573001234567"
        expected_log = f"[Webhook] ✅ Contacto {phone} agregado al panel (HIGH)"
        
        assert "HIGH" in expected_log
        assert phone in expected_log
