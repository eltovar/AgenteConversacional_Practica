# tests/test_4_requirements.py
"""
Tests para los 4 requerimientos del área de Redes Sociales:
1. Pipeline para Redes Sociales
2. Score por canal de origen
3. Detección de códigos de inmuebles
4. Manejo de horarios laborales
"""

import pytest
from datetime import datetime, time
from unittest.mock import patch, MagicMock
import os
import sys


# ═══════════════════════════════════════════════════════════════════════════
# REQUERIMIENTO 1: Pipeline Router
# ═══════════════════════════════════════════════════════════════════════════

class TestPipelineRouter:
    """Tests para el router de pipelines por canal de origen."""

    @pytest.fixture(autouse=True)
    def setup_mock_env(self, monkeypatch):
        """Setup environment variables for tests."""
        monkeypatch.setenv("HUBSPOT_API_KEY", "test_key")
        monkeypatch.setenv("HUBSPOT_PIPELINE_ID", "test_pipeline")
        monkeypatch.setenv("HUBSPOT_DEAL_STAGE", "test_stage")
        monkeypatch.setenv("HUBSPOT_PIPELINE_REDES_ID", "redes_123")
        monkeypatch.setenv("HUBSPOT_STAGE_NUEVO_RS", "stage_rs_1")

    def test_get_target_pipeline_instagram(self):
        """Instagram debe ir a pipeline de Redes Sociales."""
        # Import directly to avoid __init__.py loading HubSpotClient
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pipeline_router",
            "integrations/hubspot/pipeline_router.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        result = module.get_target_pipeline("instagram")
        assert result["is_social_media"] is True
        assert result["analytics_source"] == "SOCIAL_MEDIA"

    def test_get_target_pipeline_facebook(self):
        """Facebook debe ir a pipeline de Redes Sociales."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pipeline_router",
            "integrations/hubspot/pipeline_router.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        result = module.get_target_pipeline("facebook")
        assert result["is_social_media"] is True
        assert result["analytics_source"] == "SOCIAL_MEDIA"

    def test_get_target_pipeline_tiktok(self):
        """TikTok debe ir a pipeline de Redes Sociales."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pipeline_router",
            "integrations/hubspot/pipeline_router.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        result = module.get_target_pipeline("tiktok")
        assert result["is_social_media"] is True
        assert result["analytics_source"] == "SOCIAL_MEDIA"

    def test_get_target_pipeline_linkedin(self):
        """LinkedIn debe ir a pipeline de Redes Sociales."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pipeline_router",
            "integrations/hubspot/pipeline_router.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        result = module.get_target_pipeline("linkedin")
        assert result["is_social_media"] is True

    def test_get_target_pipeline_youtube(self):
        """YouTube debe ir a pipeline de Redes Sociales."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pipeline_router",
            "integrations/hubspot/pipeline_router.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        result = module.get_target_pipeline("youtube")
        assert result["is_social_media"] is True

    def test_get_target_pipeline_finca_raiz(self):
        """Finca Raíz debe ir a pipeline General (no redes sociales)."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pipeline_router",
            "integrations/hubspot/pipeline_router.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        result = module.get_target_pipeline("finca_raiz")
        assert result["is_social_media"] is False
        assert result["analytics_source"] == "ORGANIC_SEARCH"

    def test_get_target_pipeline_metrocuadrado(self):
        """Metrocuadrado debe ir a pipeline General."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pipeline_router",
            "integrations/hubspot/pipeline_router.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        result = module.get_target_pipeline("metrocuadrado")
        assert result["is_social_media"] is False

    def test_get_target_pipeline_whatsapp_directo(self):
        """WhatsApp directo debe ir a pipeline General."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pipeline_router",
            "integrations/hubspot/pipeline_router.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        result = module.get_target_pipeline("whatsapp_directo")
        assert result["is_social_media"] is False
        assert result["analytics_source"] == "DIRECT_TRAFFIC"

    def test_get_analytics_source(self):
        """Verificar categorías de analytics por canal."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pipeline_router",
            "integrations/hubspot/pipeline_router.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert module.get_analytics_source("instagram") == "SOCIAL_MEDIA"
        assert module.get_analytics_source("facebook") == "SOCIAL_MEDIA"
        assert module.get_analytics_source("finca_raiz") == "ORGANIC_SEARCH"
        assert module.get_analytics_source("google_ads") == "PAID_SEARCH"
        assert module.get_analytics_source("referido") == "REFERRALS"


# ═══════════════════════════════════════════════════════════════════════════
# REQUERIMIENTO 2: Score por Canal de Origen
# ═══════════════════════════════════════════════════════════════════════════

class TestLeadScore:
    """Tests para el sistema de scoring con bonus por canal."""

    def _load_module(self):
        """Helper para cargar el módulo directamente sin pasar por __init__.py."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "hubspot_utils",
            "integrations/hubspot/hubspot_utils.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_score_finca_raiz_bonus(self):
        """Lead de Finca Raíz debe tener +25 bonus."""
        module = self._load_module()

        lead_data = {
            "firstname": "Juan",
            "phone": "+573001234567",
            "canal_origen": "finca_raiz",
            "metadata": {}
        }
        score = module.calculate_lead_score(lead_data)
        # 10 (nombre) + 20 (teléfono) + 25 (bonus finca_raiz) = 55
        assert score >= 55

    def test_score_metrocuadrado_bonus(self):
        """Lead de Metrocuadrado debe tener +25 bonus."""
        module = self._load_module()

        lead_data = {
            "firstname": "María",
            "phone": "+573001234567",
            "canal_origen": "metrocuadrado",
            "metadata": {}
        }
        score = module.calculate_lead_score(lead_data)
        assert score >= 55

    def test_score_instagram_bonus(self):
        """Lead de Instagram debe tener +10 bonus."""
        module = self._load_module()

        lead_data = {
            "firstname": "Carlos",
            "phone": "+573001234567",
            "canal_origen": "instagram",
            "metadata": {}
        }
        score = module.calculate_lead_score(lead_data)
        # 10 (nombre) + 20 (teléfono) + 10 (bonus instagram) = 40
        assert score >= 40

    def test_score_whatsapp_no_bonus(self):
        """Lead de WhatsApp directo no debe tener bonus."""
        module = self._load_module()

        lead_data = {
            "firstname": "Pedro",
            "phone": "+573001234567",
            "canal_origen": "whatsapp_directo",
            "metadata": {}
        }
        score = module.calculate_lead_score(lead_data)
        # 10 (nombre) + 20 (teléfono) + 0 (bonus) = 30
        assert score == 30

    def test_score_property_code_bonus(self):
        """Lead con código de inmueble debe tener +20 bonus."""
        module = self._load_module()

        lead_data = {
            "firstname": "Ana",
            "phone": "+573001234567",
            "canal_origen": "whatsapp_directo",
            "property_code": "12345",
            "metadata": {}
        }
        score = module.calculate_lead_score(lead_data)
        # 10 + 20 + 0 + 20 (código) = 50
        assert score >= 50

    def test_score_link_inmueble_bonus(self):
        """Lead con link de inmueble debe tener +15 bonus."""
        module = self._load_module()

        lead_data = {
            "firstname": "Luis",
            "phone": "+573001234567",
            "canal_origen": "finca_raiz",
            "llegada_por_link": True,
            "es_inmueble": True,
            "metadata": {}
        }
        score = module.calculate_lead_score(lead_data)
        # 10 + 20 + 25 (finca_raiz) + 15 (link) = 70
        assert score >= 70

    def test_score_max_100(self):
        """Score nunca debe superar 100."""
        module = self._load_module()

        lead_data = {
            "firstname": "Test",
            "lastname": "User",
            "phone": "+573001234567",
            "canal_origen": "finca_raiz",
            "llegada_por_link": True,
            "es_inmueble": True,
            "property_code": "12345",
            "metadata": {
                "tipo_propiedad": "apartamento",
                "ubicacion": "El Poblado",
                "presupuesto": "500 millones",
                "caracteristicas": "3 habitaciones"
            }
        }
        score = module.calculate_lead_score(lead_data)
        assert score == 100


# ═══════════════════════════════════════════════════════════════════════════
# REQUERIMIENTO 3: Detección de Códigos de Inmuebles
# ═══════════════════════════════════════════════════════════════════════════

class TestPropertyCodeDetector:
    """Tests para el detector de códigos de inmuebles."""

    def test_detect_codigo_numero(self):
        """Detectar 'código 12345'."""
        from utils.property_code_detector import detect_property_code

        result = detect_property_code("Hola, me interesa el código 12345")
        assert result.has_code is True
        assert "12345" in result.code

    def test_detect_codigo_con_dos_puntos(self):
        """Detectar 'código: 54321'."""
        from utils.property_code_detector import detect_property_code

        result = detect_property_code("Quiero información del código: 54321")
        assert result.has_code is True
        assert "54321" in result.code

    def test_detect_referencia(self):
        """Detectar 'referencia 78901'."""
        from utils.property_code_detector import detect_property_code

        result = detect_property_code("Vi la referencia 78901 en Instagram")
        assert result.has_code is True
        assert "78901" in result.code

    def test_detect_hashtag(self):
        """Detectar '#12345'."""
        from utils.property_code_detector import detect_property_code

        result = detect_property_code("Me interesa el #12345")
        assert result.has_code is True
        assert "12345" in result.code

    def test_detect_inmueble(self):
        """Detectar 'inmueble 99999'."""
        from utils.property_code_detector import detect_property_code

        result = detect_property_code("Quiero ver el inmueble 99999")
        assert result.has_code is True
        assert "99999" in result.code

    def test_no_detect_telefono(self):
        """No detectar números de teléfono."""
        from utils.property_code_detector import detect_property_code

        result = detect_property_code("Mi teléfono es 3001234567")
        assert result.has_code is False

    def test_no_detect_precio(self):
        """No detectar precios."""
        from utils.property_code_detector import detect_property_code

        result = detect_property_code("El precio es $500000000")
        assert result.has_code is False

    def test_has_code_intent(self):
        """Detectar intención de preguntar por código."""
        from utils.property_code_detector import PropertyCodeDetector

        detector = PropertyCodeDetector()
        assert detector._has_code_intent("me interesa el inmueble") is True
        assert detector._has_code_intent("vi este apartamento") is True
        assert detector._has_code_intent("hola buenos días") is False

    def test_extract_all_codes(self):
        """Extraer múltiples códigos de un mensaje."""
        from utils.property_code_detector import PropertyCodeDetector

        detector = PropertyCodeDetector()
        codes = detector.extract_all_codes(
            "Me interesan el código 12345 y también el #67890"
        )
        assert len(codes) == 2
        assert "12345" in codes
        assert "67890" in codes


# ═══════════════════════════════════════════════════════════════════════════
# REQUERIMIENTO 4: Horarios Laborales
# ═══════════════════════════════════════════════════════════════════════════

class TestBusinessHours:
    """Tests para el módulo de horarios laborales."""

    def _load_module(self):
        """Helper para cargar el módulo directamente."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "business_hours",
            "utils/business_hours.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_is_business_hours_weekday_morning(self):
        """Lunes 10 AM debe estar en horario."""
        module = self._load_module()

        # Lunes 10:00 AM
        test_time = datetime(2024, 1, 15, 10, 0, tzinfo=module.TIMEZONE)  # Lunes
        assert module.is_business_hours(test_time) is True

    def test_is_business_hours_weekday_afternoon(self):
        """Miércoles 3 PM debe estar en horario."""
        module = self._load_module()

        # Miércoles 15:00
        test_time = datetime(2024, 1, 17, 15, 0, tzinfo=module.TIMEZONE)  # Miércoles
        assert module.is_business_hours(test_time) is True

    def test_is_business_hours_saturday_morning(self):
        """Sábado 10 AM debe estar en horario."""
        module = self._load_module()

        # Sábado 10:00 AM
        test_time = datetime(2024, 1, 20, 10, 0, tzinfo=module.TIMEZONE)  # Sábado
        assert module.is_business_hours(test_time) is True

    def test_is_not_business_hours_saturday_afternoon(self):
        """Sábado 3 PM NO debe estar en horario."""
        module = self._load_module()

        # Sábado 15:00
        test_time = datetime(2024, 1, 20, 15, 0, tzinfo=module.TIMEZONE)  # Sábado
        assert module.is_business_hours(test_time) is False

    def test_is_not_business_hours_sunday(self):
        """Domingo no debe estar en horario."""
        module = self._load_module()

        # Domingo 10:00 AM
        test_time = datetime(2024, 1, 21, 10, 0, tzinfo=module.TIMEZONE)  # Domingo
        assert module.is_business_hours(test_time) is False

    def test_is_not_business_hours_late_night(self):
        """10 PM no debe estar en horario."""
        module = self._load_module()

        # Lunes 22:00
        test_time = datetime(2024, 1, 15, 22, 0, tzinfo=module.TIMEZONE)
        assert module.is_business_hours(test_time) is False

    def test_is_not_business_hours_early_morning(self):
        """6 AM no debe estar en horario."""
        module = self._load_module()

        # Martes 6:00 AM
        test_time = datetime(2024, 1, 16, 6, 0, tzinfo=module.TIMEZONE)
        assert module.is_business_hours(test_time) is False

    def test_get_out_of_hours_message(self):
        """Mensaje de fuera de horario debe contener información útil."""
        module = self._load_module()

        message = module.get_out_of_hours_message()
        assert "fuera de nuestro horario" in message.lower()
        assert "asesor" in message.lower()
        assert "registrada" in message.lower()

    def test_should_add_message_high_priority_outside(self):
        """Debe agregar mensaje si es handoff high y fuera de horario."""
        module = self._load_module()

        # Simular fuera de horario (domingo 10PM)
        test_time = datetime(2024, 1, 21, 22, 0, tzinfo=module.TIMEZONE)
        with patch.object(module, 'get_current_time', return_value=test_time):
            result = module.should_add_out_of_hours_message("high")
            assert result is True

    def test_should_not_add_message_low_priority(self):
        """No debe agregar mensaje si es handoff low."""
        module = self._load_module()

        result = module.should_add_out_of_hours_message("low")
        assert result is False

    def test_should_not_add_message_none_priority(self):
        """No debe agregar mensaje si no hay handoff."""
        module = self._load_module()

        result = module.should_add_out_of_hours_message("none")
        assert result is False

    def test_get_business_hours_info(self):
        """Info de horarios debe contener días y horas."""
        module = self._load_module()

        info = module.get_business_hours_info()
        assert "Lunes" in info
        assert "Viernes" in info
        assert "Sábado" in info or "Sábados" in info
        assert "8:30" in info


# ═══════════════════════════════════════════════════════════════════════════
# TESTS DE LINK DETECTOR (Nuevos canales)
# ═══════════════════════════════════════════════════════════════════════════

class TestLinkDetector:
    """Tests para el detector de links con nuevos canales."""

    def test_detect_linkedin_link(self):
        """Detectar link de LinkedIn."""
        from utils.link_detector import LinkDetector, PortalOrigen

        detector = LinkDetector()
        result = detector.analizar_mensaje(
            "Vi este post https://linkedin.com/posts/inmobiliaria/12345"
        )
        assert result.tiene_link is True
        assert result.portal == PortalOrigen.LINKEDIN

    def test_detect_youtube_link(self):
        """Detectar link de YouTube."""
        from utils.link_detector import LinkDetector, PortalOrigen

        detector = LinkDetector()
        result = detector.analizar_mensaje(
            "Vi este video https://youtube.com/watch?v=abc123"
        )
        assert result.tiene_link is True
        assert result.portal == PortalOrigen.YOUTUBE

    def test_detect_youtube_short_link(self):
        """Detectar link corto de YouTube."""
        from utils.link_detector import LinkDetector, PortalOrigen

        detector = LinkDetector()
        result = detector.analizar_mensaje(
            "Me interesa este https://youtu.be/abc123"
        )
        assert result.tiene_link is True
        assert result.portal == PortalOrigen.YOUTUBE

    def test_detect_tiktok_link(self):
        """Detectar link de TikTok."""
        from utils.link_detector import LinkDetector, PortalOrigen

        detector = LinkDetector()
        result = detector.analizar_mensaje(
            "Vi en TikTok https://tiktok.com/@inmobiliaria/video/123"
        )
        assert result.tiene_link is True
        assert result.portal == PortalOrigen.TIKTOK

    def test_detect_tiktok_short_link(self):
        """Detectar link corto de TikTok."""
        from utils.link_detector import LinkDetector, PortalOrigen

        detector = LinkDetector()
        result = detector.analizar_mensaje(
            "Me interesa https://vm.tiktok.com/abc123"
        )
        assert result.tiene_link is True
        assert result.portal == PortalOrigen.TIKTOK

    def test_detect_instagram_reel(self):
        """Detectar reel de Instagram."""
        from utils.link_detector import LinkDetector, PortalOrigen

        detector = LinkDetector()
        result = detector.analizar_mensaje(
            "Vi este reel https://instagram.com/reel/abc123"
        )
        assert result.tiene_link is True
        assert result.portal == PortalOrigen.INSTAGRAM
        assert result.es_inmueble is True

    def test_obtener_nombre_nuevos_portales(self):
        """Nombres amigables de nuevos portales."""
        from utils.link_detector import LinkDetector, PortalOrigen

        detector = LinkDetector()
        assert detector.obtener_nombre_portal(PortalOrigen.LINKEDIN) == "LinkedIn"
        assert detector.obtener_nombre_portal(PortalOrigen.YOUTUBE) == "YouTube"
        assert detector.obtener_nombre_portal(PortalOrigen.TIKTOK) == "TikTok"


# ═══════════════════════════════════════════════════════════════════════════
# TESTS DE LEAD ASSIGNER (Nuevos equipos)
# ═══════════════════════════════════════════════════════════════════════════

class TestLeadAssigner:
    """Tests para la asignación de leads por canal."""

    def _load_module(self):
        """Helper para cargar el módulo directamente sin pasar por __init__.py."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "lead_assigner",
            "integrations/hubspot/lead_assigner.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_instagram_goes_to_redes_team(self):
        """Instagram debe ir al equipo de redes sociales."""
        module = self._load_module()

        team = module.LeadAssigner.CHANNEL_TO_TEAM.get("instagram")
        assert team == "equipo_redes_sociales"

    def test_facebook_goes_to_redes_team(self):
        """Facebook debe ir al equipo de redes sociales."""
        module = self._load_module()

        team = module.LeadAssigner.CHANNEL_TO_TEAM.get("facebook")
        assert team == "equipo_redes_sociales"

    def test_linkedin_goes_to_redes_team(self):
        """LinkedIn debe ir al equipo de redes sociales."""
        module = self._load_module()

        team = module.LeadAssigner.CHANNEL_TO_TEAM.get("linkedin")
        assert team == "equipo_redes_sociales"

    def test_youtube_goes_to_redes_team(self):
        """YouTube debe ir al equipo de redes sociales."""
        module = self._load_module()

        team = module.LeadAssigner.CHANNEL_TO_TEAM.get("youtube")
        assert team == "equipo_redes_sociales"

    def test_tiktok_goes_to_redes_team(self):
        """TikTok debe ir al equipo de redes sociales."""
        module = self._load_module()

        team = module.LeadAssigner.CHANNEL_TO_TEAM.get("tiktok")
        assert team == "equipo_redes_sociales"

    def test_finca_raiz_goes_to_portales_team(self):
        """Finca Raíz debe ir al equipo de portales."""
        module = self._load_module()

        team = module.LeadAssigner.CHANNEL_TO_TEAM.get("finca_raiz")
        assert team == "equipo_portales"

    def test_whatsapp_goes_to_directo_team(self):
        """WhatsApp directo debe ir al equipo directo."""
        module = self._load_module()

        team = module.LeadAssigner.CHANNEL_TO_TEAM.get("whatsapp_directo")
        assert team == "equipo_directo"


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])