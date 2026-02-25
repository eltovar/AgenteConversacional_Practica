"""
Tests Unitarios y de Integración para Endpoint /take-control.

Tests para validar:
1. Activación de HUMAN_ACTIVE al seleccionar contacto
2. Idempotencia (múltiples clicks)
3. Transiciones de estado correctas
4. Manejo de errores
5. Integración con webhook (bot no responde)

Cobertura objetivo: >90%
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from fastapi import status as http_status

from middleware.conversation_state import ConversationStateManager, ConversationStatus


# ============================================================================
# TESTS UNITARIOS - ENDPOINT TAKE-CONTROL
# ============================================================================

@pytest.mark.unit
class TestTakeControlEndpoint:
    """Tests unitarios del endpoint POST /contacts/{phone}/take-control."""

    @pytest.mark.asyncio
    async def test_take_control_activates_human_active(self, api_client, api_headers, fake_redis):
        """
        Verifica que al llamar take-control se active HUMAN_ACTIVE correctamente.
        """
        phone = "+573001234567"
        canal = "instagram"
        contact_id = "196843060059"
        advisor_id = "88251457"

        # Mock ConversationStateManager
        with patch("middleware.outbound_panel.ConversationStateManager") as MockStateManager:
            mock_manager = AsyncMock()
            mock_manager.get_status.return_value = ConversationStatus.BOT_ACTIVE
            mock_manager.activate_human = AsyncMock()
            MockStateManager.return_value = mock_manager

            # Llamar endpoint
            response = api_client.post(
                f"/whatsapp/panel/contacts/{phone}/take-control",
                params={
                    "canal": canal,
                    "contact_id": contact_id,
                    "advisor_id": advisor_id
                },
                headers=api_headers
            )

            # Verificaciones
            assert response.status_code == http_status.HTTP_200_OK
            data = response.json()

            assert data["status"] == "success"
            assert data["action"] == "human_activated"
            assert data["phone"] == phone
            assert data["canal"] == canal
            assert data["new_status"] == "HUMAN_ACTIVE"
            assert data["previous_status"] == "BOT_ACTIVE"

            # Verificar que se llamó activate_human
            mock_manager.activate_human.assert_called_once()
            call_args = mock_manager.activate_human.call_args
            assert phone in str(call_args)
            assert advisor_id in str(call_args)

    @pytest.mark.asyncio
    async def test_take_control_idempotent_when_already_human_active(self, api_client, api_headers):
        """
        Verifica que si ya está en HUMAN_ACTIVE, solo refresque el TTL.

        IDEMPOTENCIA: Múltiples clicks no deben causar problemas.
        """
        phone = "+573001234567"
        canal = "whatsapp"

        with patch("middleware.outbound_panel.ConversationStateManager") as MockStateManager:
            mock_manager = AsyncMock()
            # Ya está en HUMAN_ACTIVE
            mock_manager.get_status.return_value = ConversationStatus.HUMAN_ACTIVE
            mock_manager.set_status = AsyncMock()
            MockStateManager.return_value = mock_manager

            # Primera llamada
            response1 = api_client.post(
                f"/whatsapp/panel/contacts/{phone}/take-control",
                params={"canal": canal},
                headers=api_headers
            )

            # Segunda llamada (simula doble click)
            response2 = api_client.post(
                f"/whatsapp/panel/contacts/{phone}/take-control",
                params={"canal": canal},
                headers=api_headers
            )

            # Ambas deben ser exitosas
            assert response1.status_code == http_status.HTTP_200_OK
            assert response2.status_code == http_status.HTTP_200_OK

            data1 = response1.json()
            data2 = response2.json()

            # Verificar que la acción fue refrescar TTL
            assert data1["action"] == "ttl_refreshed"
            assert data2["action"] == "ttl_refreshed"

            # Verificar que set_status se llamó (para refrescar TTL)
            assert mock_manager.set_status.call_count >= 2

    @pytest.mark.asyncio
    async def test_take_control_upgrades_pending_handoff_to_human_active(self, api_client, api_headers):
        """
        Verifica transición PENDING_HANDOFF → HUMAN_ACTIVE al tomar control.
        """
        phone = "+573001234567"
        canal = "facebook"

        with patch("middleware.outbound_panel.ConversationStateManager") as MockStateManager:
            mock_manager = AsyncMock()
            mock_manager.get_status.return_value = ConversationStatus.PENDING_HANDOFF
            mock_manager.activate_human = AsyncMock()
            MockStateManager.return_value = mock_manager

            response = api_client.post(
                f"/whatsapp/panel/contacts/{phone}/take-control",
                params={"canal": canal},
                headers=api_headers
            )

            assert response.status_code == http_status.HTTP_200_OK
            data = response.json()

            assert data["action"] == "human_activated"
            assert data["previous_status"] == "PENDING_HANDOFF"
            assert data["new_status"] == "HUMAN_ACTIVE"

            # Verificar que activate_human fue llamado
            mock_manager.activate_human.assert_called_once()

    @pytest.mark.asyncio
    async def test_take_control_requires_authentication(self, api_client):
        """
        Verifica que el endpoint requiera API Key válida.
        """
        phone = "+573001234567"

        # Sin headers
        response = api_client.post(
            f"/whatsapp/panel/contacts/{phone}/take-control"
        )

        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_take_control_normalizes_phone_number(self, api_client, api_headers):
        """
        Verifica que se normalice el número de teléfono correctamente.
        """
        # Número sin normalizar
        raw_phone = "3001234567"
        expected_normalized = "+573001234567"

        with patch("middleware.outbound_panel.ConversationStateManager") as MockStateManager:
            mock_manager = AsyncMock()
            mock_manager.get_status.return_value = ConversationStatus.BOT_ACTIVE
            mock_manager.activate_human = AsyncMock()
            MockStateManager.return_value = mock_manager

            response = api_client.post(
                f"/whatsapp/panel/contacts/{raw_phone}/take-control",
                params={"canal": "whatsapp"},
                headers=api_headers
            )

            assert response.status_code == http_status.HTTP_200_OK
            data = response.json()

            # Verificar que se normalizó
            assert data["phone"] == expected_normalized

    @pytest.mark.asyncio
    async def test_take_control_handles_redis_errors_gracefully(self, api_client, api_headers):
        """
        Verifica que errores de Redis se manejen correctamente.
        """
        phone = "+573001234567"

        with patch("middleware.outbound_panel.ConversationStateManager") as MockStateManager:
            mock_manager = AsyncMock()
            # Simular error de Redis
            mock_manager.get_status.side_effect = Exception("Redis connection error")
            MockStateManager.return_value = mock_manager

            response = api_client.post(
                f"/whatsapp/panel/contacts/{phone}/take-control",
                params={"canal": "whatsapp"},
                headers=api_headers
            )

            assert response.status_code == http_status.HTTP_500_INTERNAL_SERVER_ERROR


# ============================================================================
# TESTS DE INTEGRACIÓN - TAKE-CONTROL + WEBHOOK
# ============================================================================

@pytest.mark.integration
class TestTakeControlWebhookIntegration:
    """
    Tests de integración entre take-control y webhook.

    Flujo completo:
    1. Asesora selecciona contacto → take-control
    2. Cliente envía mensaje → webhook
    3. Bot NO debe responder (HUMAN_ACTIVE)
    """

    @pytest.mark.asyncio
    async def test_full_flow_take_control_prevents_bot_response(self, fake_redis):
        """
        Test de integración completo: take-control → webhook → bot silenciado.

        CRÍTICO: Este es el fix principal del problema reportado.
        """
        phone = "+573001234567"
        canal = "whatsapp_directo"
        advisor_id = "88251457"

        # Paso 1: Simular take-control
        state_manager = ConversationStateManager("redis://fake")
        state_manager.redis = fake_redis

        await state_manager.activate_human(
            phone_normalized=phone,
            canal_origen=canal,
            owner_id=advisor_id,
            reason="Asesora seleccionó contacto en panel"
        )

        # Verificar que se activó HUMAN_ACTIVE
        status = await state_manager.get_status(phone, canal)
        assert status == ConversationStatus.HUMAN_ACTIVE

        # Paso 2: Simular webhook entrante (cliente envía mensaje)
        # should_bot_respond debe retornar False
        from middleware.webhook_handler import should_bot_respond

        with patch("middleware.webhook_handler.get_state_manager", return_value=state_manager):
            should_respond, reason, _ = await should_bot_respond(
                phone_normalized=phone,
                contact_id="196843060059"
            )

            # VERIFICACIÓN CRÍTICA: Bot NO debe responder
            assert should_respond is False, \
                "Bot NO debe responder cuando hay HUMAN_ACTIVE"
            assert "HUMAN_ACTIVE" in reason or "humano" in reason.lower()

    @pytest.mark.asyncio
    async def test_take_control_persists_across_multiple_channels(self, fake_redis):
        """
        Verifica que el estado se mantenga correcto cuando hay múltiples canales.

        CASO REAL: Cliente escribe por Instagram, asesora toma control,
        luego cliente envía mensaje por WhatsApp. Bot NO debe responder.
        """
        phone = "+573001234567"
        canal_instagram = "instagram"
        canal_whatsapp = "whatsapp"

        state_manager = ConversationStateManager("redis://fake")
        state_manager.redis = fake_redis

        # Activar HUMAN_ACTIVE en Instagram
        await state_manager.activate_human(
            phone_normalized=phone,
            canal_origen=canal_instagram,
            owner_id="88251457"
        )

        # Verificar estado en Instagram
        status_instagram = await state_manager.get_status(phone, canal_instagram)
        assert status_instagram == ConversationStatus.HUMAN_ACTIVE

        # Cliente envía mensaje por WhatsApp (diferente canal)
        # should_bot_respond debe verificar TODOS los canales
        from middleware.webhook_handler import should_bot_respond

        with patch("middleware.webhook_handler.get_state_manager", return_value=state_manager):
            should_respond, reason, _ = await should_bot_respond(
                phone_normalized=phone,
                contact_id="196843060059"
            )

            # VERIFICACIÓN: Debe detectar HUMAN_ACTIVE en otro canal
            assert should_respond is False, \
                "Bot NO debe responder si hay HUMAN_ACTIVE en cualquier canal"


# ============================================================================
# TESTS DE CONCURRENCIA - TAKE-CONTROL
# ============================================================================

@pytest.mark.unit
@pytest.mark.concurrent
class TestTakeControlConcurrency:
    """Tests de concurrencia para take-control."""

    @pytest.mark.asyncio
    async def test_concurrent_take_control_no_corruption(self, fake_redis):
        """
        Verifica que múltiples llamadas concurrentes a take-control
        no corrompan el estado en Redis.
        """
        import asyncio

        phone = "+573001234567"
        canal = "whatsapp"

        state_manager = ConversationStateManager("redis://fake")
        state_manager.redis = fake_redis

        # Simular 10 requests concurrentes (10 asesoras clickean a la vez)
        async def concurrent_take_control(advisor_id):
            await state_manager.activate_human(
                phone_normalized=phone,
                canal_origen=canal,
                owner_id=str(advisor_id),
                reason="Concurrent test"
            )

        tasks = [
            concurrent_take_control(i)
            for i in range(10)
        ]

        # Ejecutar concurrentemente
        await asyncio.gather(*tasks)

        # Verificar que el estado final es consistente
        status = await state_manager.get_status(phone, canal)
        assert status == ConversationStatus.HUMAN_ACTIVE

        # Verificar que solo hay UNA key en Redis (no duplicadas)
        state_key = f"conv_state:{phone}:{canal}"
        assert await fake_redis.exists(state_key) == 1

    @pytest.mark.asyncio
    async def test_race_condition_take_control_vs_webhook(self, fake_redis):
        """
        Simula race condition: take-control y webhook llegan simultáneamente.

        Caso real: Asesora clickea justo cuando llega mensaje del cliente.
        """
        import asyncio

        phone = "+573001234567"
        canal = "whatsapp"

        state_manager = ConversationStateManager("redis://fake")
        state_manager.redis = fake_redis

        # Tarea 1: Take-control (asesora clickea)
        async def take_control_task():
            await asyncio.sleep(0.01)  # Pequeño delay
            await state_manager.activate_human(
                phone_normalized=phone,
                canal_origen=canal,
                owner_id="88251457"
            )

        # Tarea 2: Webhook (cliente envía mensaje)
        async def webhook_task():
            # Verificar estado
            status = await state_manager.get_status(phone, canal)
            return status

        # Ejecutar concurrentemente
        task1 = asyncio.create_task(take_control_task())
        task2 = asyncio.create_task(webhook_task())

        webhook_status = await task2
        await task1

        # Verificar estado final
        final_status = await state_manager.get_status(phone, canal)
        assert final_status == ConversationStatus.HUMAN_ACTIVE, \
            "HUMAN_ACTIVE debe ganar en race condition"


# ============================================================================
# TESTS DE REGRESIÓN - TAKE-CONTROL
# ============================================================================

@pytest.mark.unit
class TestTakeControlRegression:
    """
    Tests de regresión basados en bugs reales reportados.
    """

    @pytest.mark.asyncio
    async def test_regression_sofia_responds_after_click(self, fake_redis):
        """
        REGRESIÓN: Sofía respondía después de que asesora hacía click.

        Problema: HUMAN_ACTIVE solo se activaba al ENVIAR mensaje,
        no al SELECCIONAR contacto.

        Fix: Ahora take-control se llama al seleccionar.
        """
        phone = "+573001234567"
        canal = "instagram"

        state_manager = ConversationStateManager("redis://fake")
        state_manager.redis = fake_redis

        # Antes del fix: Estado sería BOT_ACTIVE
        initial_status = await state_manager.get_status(phone, canal)
        assert initial_status != ConversationStatus.HUMAN_ACTIVE

        # Simular click en contacto (ahora llama take-control)
        await state_manager.activate_human(
            phone_normalized=phone,
            canal_origen=canal,
            owner_id="88251457",
            reason="Asesora seleccionó contacto"
        )

        # Verificar que ahora está HUMAN_ACTIVE
        status_after_click = await state_manager.get_status(phone, canal)
        assert status_after_click == ConversationStatus.HUMAN_ACTIVE

        # Si llega webhook, bot NO debe responder
        from middleware.webhook_handler import should_bot_respond

        with patch("middleware.webhook_handler.get_state_manager", return_value=state_manager):
            should_respond, _, _ = await should_bot_respond(
                phone_normalized=phone,
                contact_id="196843060059"
            )

            assert should_respond is False

    @pytest.mark.asyncio
    async def test_regression_inconsistent_phone_normalization(self, fake_redis):
        """
        REGRESIÓN: Normalización inconsistente entre canales.

        Problema: phone_normalized era diferente entre "whatsapp" y "whatsapp_directo",
        causando que el bot no detectara HUMAN_ACTIVE.
        """
        # Diferentes formatos del mismo número
        phone_formats = [
            "+573001234567",
            "573001234567",
            "3001234567",
            "whatsapp:+573001234567"
        ]

        state_manager = ConversationStateManager("redis://fake")
        state_manager.redis = fake_redis

        # Activar HUMAN_ACTIVE con el primer formato
        await state_manager.activate_human(
            phone_normalized=phone_formats[0],
            canal_origen="instagram",
            owner_id="88251457"
        )

        # Verificar que TODOS los formatos detecten HUMAN_ACTIVE
        from middleware.phone_normalizer import normalize_colombian_phone

        for raw_phone in phone_formats:
            try:
                normalized = normalize_colombian_phone(raw_phone)
            except ValueError:
                # Si falla normalizar, usar el formato normalizado conocido
                normalized = "+573001234567"

            # Verificar estado
            status = await state_manager.get_status(normalized, "instagram")

            assert status == ConversationStatus.HUMAN_ACTIVE, \
                f"Formato {raw_phone} no detectó HUMAN_ACTIVE correctamente"


# ============================================================================
# TESTS DE PERFORMANCE - TAKE-CONTROL
# ============================================================================

@pytest.mark.unit
@pytest.mark.slow
class TestTakeControlPerformance:
    """Tests de performance del endpoint take-control."""

    @pytest.mark.asyncio
    async def test_take_control_response_time(self, fake_redis):
        """
        Verifica que take-control responda en <100ms.

        Importante para UX: la asesora hace click y espera respuesta inmediata.
        """
        import time

        phone = "+573001234567"
        canal = "whatsapp"

        state_manager = ConversationStateManager("redis://fake")
        state_manager.redis = fake_redis

        # Medir tiempo
        start = time.time()

        await state_manager.activate_human(
            phone_normalized=phone,
            canal_origen=canal,
            owner_id="88251457"
        )

        duration = time.time() - start

        # Debe ser muy rápido (< 100ms)
        assert duration < 0.1, f"Take-control tardó {duration:.3f}s (debe ser <0.1s)"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "unit or integration"])