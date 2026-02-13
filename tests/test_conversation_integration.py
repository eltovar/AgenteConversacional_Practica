# tests/test_conversation_integration.py
"""
Tests de integración para verificar el comportamiento de Sofía
en conversaciones naturales con los 4 requerimientos:

1. Pipeline de Redes Sociales
2. Score por canal de origen
3. Detección de códigos de inmuebles
4. Manejo de horarios laborales
"""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock, AsyncMock


class TestConversationScenarios:
    """
    Tests de escenarios de conversación completos.
    Simula el flujo de webhook_handler sin necesidad del servidor.
    """

    def _load_modules(self):
        """Carga los módulos necesarios directamente."""
        import importlib.util
        import os

        # Configurar variables de entorno para tests
        os.environ.setdefault("HUBSPOT_PIPELINE_REDES_ID", "test_redes_pipeline")
        os.environ.setdefault("HUBSPOT_STAGE_NUEVO_RS", "test_stage_rs")
        os.environ.setdefault("HUBSPOT_PIPELINE_ID", "test_general_pipeline")
        os.environ.setdefault("HUBSPOT_DEAL_STAGE", "test_stage")
        os.environ.setdefault("HUBSPOT_API_KEY", "test_key")

        # Cargar link_detector
        spec = importlib.util.spec_from_file_location(
            "link_detector", "utils/link_detector.py"
        )
        link_detector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(link_detector)

        # Cargar property_code_detector
        spec = importlib.util.spec_from_file_location(
            "property_code_detector", "utils/property_code_detector.py"
        )
        code_detector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(code_detector)

        # Cargar business_hours
        spec = importlib.util.spec_from_file_location(
            "business_hours", "utils/business_hours.py"
        )
        business_hours = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(business_hours)

        # Cargar pipeline_router (fresh load con env vars)
        spec = importlib.util.spec_from_file_location(
            "pipeline_router", "integrations/hubspot/pipeline_router.py"
        )
        pipeline_router = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pipeline_router)

        # Cargar hubspot_utils
        spec = importlib.util.spec_from_file_location(
            "hubspot_utils", "integrations/hubspot/hubspot_utils.py"
        )
        hubspot_utils = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hubspot_utils)

        return {
            "link_detector": link_detector,
            "code_detector": code_detector,
            "business_hours": business_hours,
            "pipeline_router": pipeline_router,
            "hubspot_utils": hubspot_utils,
        }

    # ═══════════════════════════════════════════════════════════════════════
    # ESCENARIO 1: Cliente llega desde Instagram con link de reel
    # ═══════════════════════════════════════════════════════════════════════

    def test_scenario_instagram_reel_link(self):
        """
        Cliente envía un link de Instagram reel.
        - Debe detectar que es de Instagram
        - Debe clasificar como redes sociales
        - Score debe incluir bonus de Instagram (+10)
        """
        modules = self._load_modules()

        # Mensaje del cliente
        message = "Hola! Vi este apartamento en su reel https://instagram.com/reel/abc123"

        # 1. Detectar link
        detector = modules["link_detector"].LinkDetector()
        result = detector.analizar_mensaje(message)

        assert result.tiene_link is True
        assert result.portal == modules["link_detector"].PortalOrigen.INSTAGRAM
        assert result.es_inmueble is True

        # 2. Obtener configuración de pipeline
        canal_origen = result.portal.value  # "instagram"
        pipeline_config = modules["pipeline_router"].get_target_pipeline(canal_origen)

        assert pipeline_config["is_social_media"] is True
        assert pipeline_config["analytics_source"] == "SOCIAL_MEDIA"

        # 3. Calcular score
        lead_data = {
            "firstname": "Cliente Instagram",
            "phone": "+573001234567",
            "canal_origen": canal_origen,
            "llegada_por_link": True,
            "es_inmueble": True,
            "metadata": {}
        }
        score = modules["hubspot_utils"].calculate_lead_score(lead_data)

        # 10 (nombre) + 20 (teléfono) + 10 (instagram) + 15 (link inmueble) = 55
        assert score >= 55
        print(f"✓ Escenario Instagram Reel: Score = {score}, Pipeline = Redes Sociales")

    # ═══════════════════════════════════════════════════════════════════════
    # ESCENARIO 2: Cliente de TikTok menciona código de inmueble
    # ═══════════════════════════════════════════════════════════════════════

    def test_scenario_tiktok_with_property_code(self):
        """
        Cliente llega de TikTok y menciona un código de inmueble.
        - Debe detectar TikTok como canal
        - Debe detectar el código de inmueble
        - Score debe incluir bonus de TikTok (+10) y código (+20)
        """
        modules = self._load_modules()

        # Mensaje 1: Link de TikTok
        message1 = "Hola, vi este video https://vm.tiktok.com/abc123"

        link_detector = modules["link_detector"].LinkDetector()
        result1 = link_detector.analizar_mensaje(message1)

        assert result1.portal == modules["link_detector"].PortalOrigen.TIKTOK

        # Mensaje 2: Menciona código
        message2 = "Me interesa el código 12345 que vi en el video"

        code_result = modules["code_detector"].detect_property_code(message2)

        assert code_result.has_code is True
        assert code_result.code == "12345"

        # Calcular score con ambos datos
        lead_data = {
            "firstname": "Cliente TikTok",
            "phone": "+573001234567",
            "canal_origen": "tiktok",
            "llegada_por_link": True,
            "es_inmueble": True,
            "property_code": code_result.code,
            "metadata": {}
        }
        score = modules["hubspot_utils"].calculate_lead_score(lead_data)

        # 10 + 20 + 10 (tiktok) + 15 (link) + 20 (código) = 75
        assert score >= 75
        print(f"✓ Escenario TikTok + Código: Score = {score}")

    # ═══════════════════════════════════════════════════════════════════════
    # ESCENARIO 3: Cliente de Finca Raíz (portal inmobiliario)
    # ═══════════════════════════════════════════════════════════════════════

    def test_scenario_finca_raiz_portal(self):
        """
        Cliente llega desde Finca Raíz con link de propiedad.
        - Debe detectar que es de portal inmobiliario (no redes)
        - Score debe incluir bonus alto de Finca Raíz (+25)
        - Pipeline debe ser el general (no redes sociales)
        """
        modules = self._load_modules()

        # Mensaje con link de Finca Raíz
        message = "Vi este apartamento https://fincaraiz.com.co/apartamento/bogota/12345"

        link_detector = modules["link_detector"].LinkDetector()
        result = link_detector.analizar_mensaje(message)

        assert result.portal == modules["link_detector"].PortalOrigen.FINCA_RAIZ
        assert result.es_inmueble is True

        # Pipeline config
        canal_origen = result.portal.value
        pipeline_config = modules["pipeline_router"].get_target_pipeline(canal_origen)

        assert pipeline_config["is_social_media"] is False
        assert pipeline_config["analytics_source"] == "ORGANIC_SEARCH"

        # Score
        lead_data = {
            "firstname": "Cliente Finca Raíz",
            "phone": "+573001234567",
            "canal_origen": canal_origen,
            "llegada_por_link": True,
            "es_inmueble": True,
            "metadata": {
                "tipo_propiedad": "apartamento",
                "ubicacion": "Bogotá"
            }
        }
        score = modules["hubspot_utils"].calculate_lead_score(lead_data)

        # 10 + 20 + 25 (finca_raiz) + 15 (link) + 15 (tipo) + 15 (ubicación) = 100
        assert score >= 85
        print(f"✓ Escenario Finca Raíz: Score = {score}, Pipeline = General")

    # ═══════════════════════════════════════════════════════════════════════
    # ESCENARIO 4: Cliente fuera de horario pidiendo asesor
    # ═══════════════════════════════════════════════════════════════════════

    def test_scenario_out_of_hours_handoff(self):
        """
        Cliente escribe a las 10 PM (fuera de horario) y quiere hablar con asesor.
        - Sofía debe responder (24/7)
        - Si handoff_priority es high, debe agregar mensaje de fuera de horario
        """
        modules = self._load_modules()
        bh = modules["business_hours"]

        # Simular domingo a las 10 PM (fuera de horario)
        sunday_night = datetime(2024, 1, 21, 22, 0, tzinfo=bh.TIMEZONE)

        # Verificar que está fuera de horario
        assert bh.is_business_hours(sunday_night) is False

        # Simular handoff priority high
        with patch.object(bh, 'get_current_time', return_value=sunday_night):
            should_add = bh.should_add_out_of_hours_message("high")
            assert should_add is True

            message = bh.get_out_of_hours_message()
            assert "fuera de nuestro horario" in message.lower()
            assert "asesor" in message.lower()

        print(f"✓ Escenario Fuera de Horario: Mensaje agregado correctamente")

    def test_scenario_in_hours_no_extra_message(self):
        """
        Cliente escribe en horario laboral con handoff high.
        - NO debe agregar mensaje de fuera de horario.
        """
        modules = self._load_modules()
        bh = modules["business_hours"]

        # Lunes 10 AM (en horario)
        monday_morning = datetime(2024, 1, 15, 10, 0, tzinfo=bh.TIMEZONE)

        with patch.object(bh, 'get_current_time', return_value=monday_morning):
            # En horario laboral, no se debe agregar mensaje
            should_add = bh.should_add_out_of_hours_message("high")
            assert should_add is False

        print(f"✓ Escenario En Horario: Sin mensaje adicional")

    # ═══════════════════════════════════════════════════════════════════════
    # ESCENARIO 5: WhatsApp directo (sin link, sin código)
    # ═══════════════════════════════════════════════════════════════════════

    def test_scenario_whatsapp_directo(self):
        """
        Cliente escribe directamente sin link ni código.
        - Canal debe ser whatsapp_directo
        - Score base sin bonus adicionales
        """
        modules = self._load_modules()

        # Mensaje simple sin links
        message = "Hola, estoy buscando un apartamento en El Poblado"

        link_detector = modules["link_detector"].LinkDetector()
        result = link_detector.analizar_mensaje(message)

        # Sin link detectado
        assert result.tiene_link is False

        # Sin código detectado
        code_result = modules["code_detector"].detect_property_code(message)
        assert code_result.has_code is False

        # Canal es whatsapp_directo (default)
        canal_origen = "whatsapp_directo"
        pipeline_config = modules["pipeline_router"].get_target_pipeline(canal_origen)

        assert pipeline_config["is_social_media"] is False
        assert pipeline_config["analytics_source"] == "DIRECT_TRAFFIC"

        # Score base
        lead_data = {
            "firstname": "Cliente Directo",
            "phone": "+573001234567",
            "canal_origen": canal_origen,
            "metadata": {}
        }
        score = modules["hubspot_utils"].calculate_lead_score(lead_data)

        # 10 + 20 + 0 (sin bonus) = 30
        assert score == 30
        print(f"✓ Escenario WhatsApp Directo: Score = {score}")

    # ═══════════════════════════════════════════════════════════════════════
    # ESCENARIO 6: LinkedIn profesional
    # ═══════════════════════════════════════════════════════════════════════

    def test_scenario_linkedin_post(self):
        """
        Cliente llega desde un post de LinkedIn.
        - Debe detectar LinkedIn como canal
        - Debe ir al pipeline de Redes Sociales
        """
        modules = self._load_modules()

        message = "Vi su publicación en LinkedIn https://linkedin.com/posts/inmobiliaria/office-space"

        link_detector = modules["link_detector"].LinkDetector()
        result = link_detector.analizar_mensaje(message)

        assert result.portal == modules["link_detector"].PortalOrigen.LINKEDIN

        pipeline_config = modules["pipeline_router"].get_target_pipeline("linkedin")
        assert pipeline_config["is_social_media"] is True
        assert pipeline_config["analytics_source"] == "SOCIAL_MEDIA"

        print(f"✓ Escenario LinkedIn: Pipeline = Redes Sociales")

    # ═══════════════════════════════════════════════════════════════════════
    # ESCENARIO 7: YouTube video
    # ═══════════════════════════════════════════════════════════════════════

    def test_scenario_youtube_video(self):
        """
        Cliente llega desde un video de YouTube.
        - Debe detectar YouTube
        - Debe usar link corto youtu.be también
        """
        modules = self._load_modules()

        # Link completo
        message1 = "Vi este tour https://youtube.com/watch?v=tour123"
        link_detector = modules["link_detector"].LinkDetector()
        result1 = link_detector.analizar_mensaje(message1)
        assert result1.portal == modules["link_detector"].PortalOrigen.YOUTUBE

        # Link corto
        message2 = "Me interesa este https://youtu.be/tour123"
        result2 = link_detector.analizar_mensaje(message2)
        assert result2.portal == modules["link_detector"].PortalOrigen.YOUTUBE

        print(f"✓ Escenario YouTube: Detecta links completos y cortos")

    # ═══════════════════════════════════════════════════════════════════════
    # ESCENARIO 8: Múltiples códigos en mensaje
    # ═══════════════════════════════════════════════════════════════════════

    def test_scenario_multiple_codes(self):
        """
        Cliente menciona varios códigos de inmuebles.
        - Debe extraer todos los códigos
        """
        modules = self._load_modules()

        message = "Me interesan el código 12345, referencia 67890 y también el #11111"

        detector = modules["code_detector"].PropertyCodeDetector()
        codes = detector.extract_all_codes(message)

        assert len(codes) >= 2
        assert "12345" in codes
        assert "67890" in codes

        print(f"✓ Escenario Múltiples Códigos: {codes}")


# ═══════════════════════════════════════════════════════════════════════════
# RESUMEN
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v"])