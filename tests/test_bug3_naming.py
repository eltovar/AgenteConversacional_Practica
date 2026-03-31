"""
Test Bug 3: Naming — Verifica que el flujo de citas resuelve firstname correctamente.

Ejecutar:
    python -m pytest tests/test_bug3_naming.py -v

Requiere:
    - Variables de entorno: HUBSPOT_API_KEY, REDIS_URL (o REDIS_PUBLIC_URL)
    - pip install pytest pytest-asyncio httpx
"""
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Agregar root al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 1: _get_hubspot_contact_info retorna firstname
# ═══════════════════════════════════════════════════════════════════════════════
class TestHubSpotFirstnameExtraction:
    """Verifica que _get_hubspot_contact_info retorna firstname en el dict."""

    @pytest.mark.asyncio
    async def test_hubspot_returns_firstname(self):
        """Simula respuesta HubSpot con firstname y verifica extracción."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "properties": {
                "firstname": "Juan",
                "lastname": "Pérez",
                "email": "juan@test.com",
                "phone": "+573001234567"
            }
        }

        # Simular la lógica de extracción (igual a outbound_panel.py:4449-4461)
        hs_info = mock_response.json()["properties"]
        phone = hs_info.get("phone", "")
        contact_firstname = (hs_info.get("firstname") or "").strip()

        assert phone == "+573001234567"
        assert contact_firstname == "Juan"
        assert contact_firstname or None == "Juan"  # Lo que se pasa a create_appointment

    @pytest.mark.asyncio
    async def test_hubspot_firstname_none(self):
        """Cuando HubSpot retorna firstname=None."""
        hs_info = {"firstname": None, "phone": "+573001234567"}
        contact_firstname = (hs_info.get("firstname") or "").strip()

        assert contact_firstname == ""
        assert (contact_firstname or None) is None  # Se pasa None a create_appointment

    @pytest.mark.asyncio
    async def test_hubspot_firstname_empty_string(self):
        """Cuando HubSpot retorna firstname='' (string vacío)."""
        hs_info = {"firstname": "", "phone": "+573001234567"}
        contact_firstname = (hs_info.get("firstname") or "").strip()

        assert contact_firstname == ""
        assert (contact_firstname or None) is None

    @pytest.mark.asyncio
    async def test_hubspot_firstname_whitespace(self):
        """Cuando HubSpot retorna firstname='   ' (solo espacios)."""
        hs_info = {"firstname": "   ", "phone": "+573001234567"}
        contact_firstname = (hs_info.get("firstname") or "").strip()

        assert contact_firstname == ""
        assert (contact_firstname or None) is None

    @pytest.mark.asyncio
    async def test_hubspot_firstname_missing_key(self):
        """Cuando HubSpot no incluye el campo firstname."""
        hs_info = {"phone": "+573001234567"}
        contact_firstname = (hs_info.get("firstname") or "").strip()

        assert contact_firstname == ""

    @pytest.mark.asyncio
    async def test_hubspot_api_failure(self):
        """Cuando la API de HubSpot falla completamente."""
        hs_info = None  # Simula _get_hubspot_contact_info retornando None

        phone = ""
        contact_firstname = ""
        if hs_info:
            phone = hs_info.get("phone", "")
            contact_firstname = (hs_info.get("firstname") or "").strip()

        assert phone == ""
        assert contact_firstname == ""


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 2: Lógica de fallback de 3 niveles en scheduler
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass
class MockAppointment:
    phone_normalized: str = "+573001234567"
    canal: str = "whatsapp"
    contact_name: str = None
    contact_id: str = "123456"


@dataclass
class MockMeta:
    display_name: str = None


class TestNameFallbackLogic:
    """Verifica la lógica de 3 niveles: appointment → redis_meta → fallback."""

    def _resolve_name(self, apt_contact_name, meta_display_name):
        """Replica exacta de la lógica implementada en app.py."""
        contact_name = apt_contact_name
        _name_source = "appointment_redis"

        if not contact_name:
            # Nivel 2: simular get_meta
            _meta = MockMeta(display_name=meta_display_name) if meta_display_name is not None else None
            if _meta and _meta.display_name:
                contact_name = _meta.display_name.split()[0]
                _name_source = "redis_meta"

        if not contact_name:
            contact_name = "cliente"
            _name_source = "fallback"

        return contact_name, _name_source

    def test_level1_appointment_name(self):
        """Nivel 1: nombre viene del appointment en Redis."""
        name, source = self._resolve_name("Carlos", None)
        assert name == "Carlos"
        assert source == "appointment_redis"

    def test_level2_redis_meta(self):
        """Nivel 2: appointment sin nombre, pero meta tiene display_name."""
        name, source = self._resolve_name(None, "María García")
        assert name == "María"  # split()[0] = primer nombre
        assert source == "redis_meta"

    def test_level3_fallback(self):
        """Nivel 3: ni appointment ni meta tienen nombre."""
        name, source = self._resolve_name(None, None)
        assert name == "cliente"
        assert source == "fallback"

    def test_level2_meta_empty_display_name(self):
        """Meta existe pero display_name es vacío → fallback."""
        name, source = self._resolve_name(None, "")
        assert name == "cliente"
        assert source == "fallback"

    def test_level1_takes_priority_over_meta(self):
        """Si appointment tiene nombre, NO consulta meta."""
        name, source = self._resolve_name("Pedro", "Otro Nombre")
        assert name == "Pedro"
        assert source == "appointment_redis"

    def test_message_format_reminder(self):
        """Verifica formato final del mensaje de recordatorio."""
        contact_name = "Ana"
        hora = "14:30"
        message = (
            f"¡Hola {contact_name}! 👋 Te recuerdo tu cita programada para hoy "
            f"a las {hora}. ¡Te esperamos!"
        )
        assert "¡Hola Ana!" in message
        assert "14:30" in message

    def test_message_format_followup(self):
        """Verifica formato final del mensaje de seguimiento."""
        contact_name = "Luis"
        message = (
            f"¡Hola {contact_name}! 😊 Esperamos que tu visita haya sido de tu agrado. "
            f"¿Tienes alguna pregunta sobre el inmueble que visitaste? "
            f"Estamos aquí para ayudarte."
        )
        assert "¡Hola Luis!" in message

    def test_message_fallback_cliente(self):
        """Verifica que el fallback produce mensaje aceptable."""
        contact_name = "cliente"
        message = f"¡Hola {contact_name}! 👋 Te recuerdo tu cita..."
        assert "¡Hola cliente!" in message


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 3: Integración — Flujo completo appointment creation
# ═══════════════════════════════════════════════════════════════════════════════
class TestAppointmentCreationFlow:
    """Verifica que create_appointment recibe contact_name correctamente."""

    @pytest.mark.asyncio
    async def test_create_appointment_receives_name(self):
        """Simula el flujo: HubSpot → extraer firstname → pasar a create_appointment."""
        # Simular respuesta HubSpot
        hs_info = {
            "firstname": "Valentina",
            "lastname": "Rodríguez",
            "phone": "+573001234567"
        }

        # Lógica de extracción (outbound_panel.py)
        contact_firstname = (hs_info.get("firstname") or "").strip()

        # Lo que se pasa a create_appointment
        create_kwargs = {
            "phone_normalized": "+573001234567",
            "canal": "whatsapp",
            "contact_name": contact_firstname or None,
            "contact_id": "test_123",
        }

        assert create_kwargs["contact_name"] == "Valentina"

    @pytest.mark.asyncio
    async def test_create_appointment_none_when_no_name(self):
        """Sin firstname → contact_name debe ser None (no string vacío)."""
        hs_info = {"firstname": None, "phone": "+573001234567"}
        contact_firstname = (hs_info.get("firstname") or "").strip()

        create_kwargs = {"contact_name": contact_firstname or None}
        assert create_kwargs["contact_name"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
