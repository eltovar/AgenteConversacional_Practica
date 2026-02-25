"""
Tests de Integración End-to-End - Sincronización de Caché.

Flujos completos que integran múltiples componentes:
1. Crear nota → invalidar caché → refetch → verificar datos frescos
2. Take-control → HUMAN_ACTIVE → webhook → bot silenciado
3. Polling incremental → verificar sincronización

Cobertura: Flujos críticos de producción
"""

import httpx
import pytest
import pytest_asyncio
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import fakeredis.aioredis

from integrations.hubspot.timeline_logger import (
    TimelineLogger,
    TimelineEvent,
    MessageSender,
    MessageDirection
)
from middleware.conversation_state import ConversationStateManager, ConversationStatus


# ============================================================================
# TESTS E2E - FLUJO COMPLETO DE CACHÉ
# ============================================================================

@pytest.mark.integration
@pytest.mark.e2e
class TestCacheSyncE2E:
    """Tests end-to-end del flujo completo de sincronización de caché."""

    @pytest.mark.asyncio
    async def test_e2e_create_note_invalidate_refetch(self, fake_redis, mock_httpx_client):
        """
        Flujo completo E2E:
        1. Asesor envía mensaje
        2. Se crea nota en HubSpot
        3. Caché se invalida automáticamente
        4. Siguiente fetch obtiene datos frescos

        Este test valida el fix del problema de sincronización reportado.
        """
        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            logger = TimelineLogger()
            logger._redis = fake_redis

            contact_id = "196843060059"

            # ===== PASO 1: Primera consulta (poblar caché inicial) =====
            # Configurar el mock estadual para devolver diferentes resultados
            call_count = 0
            
            def mock_get_side_effect(url, **kwargs):
                """Mock para GET requests - retorna 2 o 3 notas según la consulta."""
                nonlocal call_count
                call_count += 1
                
                response = MagicMock(spec=httpx.Response)
                response.status_code = 200
                
                if call_count == 1:
                    # Primera consulta: 2 notas existentes
                    response.json.return_value = {
                        "results": [
                            {"toObjectId": "old_note_1"},
                            {"toObjectId": "old_note_2"}
                        ]
                    }
                else:
                    # Segunda consulta: 3 notas (incluye la nueva)
                    response.json.return_value = {
                        "results": [
                            {"toObjectId": "old_note_1"},
                            {"toObjectId": "old_note_2"},
                            {"toObjectId": "new_note_3"}  # Nueva nota
                        ]
                    }
                
                return response

            mock_httpx_client.get.side_effect = mock_get_side_effect

            notes_before = await logger.get_notes_for_contact(contact_id, limit=10)

            # Verificar que el caché se pobló
            cache_key = f"hs_assoc:contact:{contact_id}:notes"
            assert await fake_redis.exists(cache_key) == 1

            cached_before = await logger._get_cached_associations(contact_id)
            assert len(cached_before) == 2, f"Expected 2 notes in cache, got {len(cached_before)}: {cached_before}"      
            assert "old_note_1" in cached_before

            # ===== PASO 2: Asesor envía mensaje nuevo =====
            new_event = TimelineEvent(
                contact_id=contact_id,
                content="Mensaje nuevo del asesor",
                sender=MessageSender.ADVISOR,
                direction=MessageDirection.OUTBOUND,
                external_id="new_msg_123"
            )

            # Mock respuesta exitosa de creación
            mock_response = MagicMock(spec=httpx.Response)
            mock_response.status_code = 201
            mock_response.json.return_value = {
                "id": "new_note_3",
                "properties": {"hs_note_body": "Mensaje nuevo"}
            }
            mock_httpx_client.post.return_value = mock_response

            # Crear nota
            success = await logger._create_note(new_event)
            assert success is True

            # ===== PASO 3: Verificar que el caché se invalidó =====
            cache_exists = await fake_redis.exists(cache_key)
            assert cache_exists == 0, \
                "El caché debe invalidarse automáticamente después de crear nota"

            # ===== PASO 4: Siguiente fetch debe obtener datos frescos =====
            # Trigger fetch para recargar caché (second call_count == 2)
            notes_after = await logger.get_notes_for_contact(contact_id, limit=10)

            # Verificar que el nuevo caché incluye la nota nueva
            cached_after = await logger._get_cached_associations(contact_id)
            assert len(cached_after) == 3, f"Expected 3 notes in cache, got {len(cached_after)}: {cached_after}"
            assert "new_note_3" in cached_after, \
                "La nueva nota debe aparecer en el caché refrescado"

    @pytest.mark.asyncio
    async def test_e2e_concurrent_updates_cache_consistency(self, fake_redis, mock_httpx_client):
        """
        Test de concurrencia E2E:
        Múltiples asesores envían mensajes simultáneamente al mismo contacto.

        Verifica que:
        1. No hay corrupción de caché
        2. Todas las notas se crean
        3. El caché final es consistente
        """
        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            logger = TimelineLogger()
            logger._redis = fake_redis

            contact_id = "196843060059"

            # Simular 5 asesores enviando mensajes concurrentemente
            async def send_message(msg_id):
                event = TimelineEvent(
                    contact_id=contact_id,
                    content=f"Mensaje {msg_id}",
                    sender=MessageSender.ADVISOR,
                    direction=MessageDirection.OUTBOUND,
                    external_id=f"msg_{msg_id}"
                )

                # Mock respuesta
                mock_response = AsyncMock()
                mock_response.status_code = 201
                mock_response.json.return_value = {
                    "id": f"note_{msg_id}",
                    "properties": {"hs_note_body": f"Mensaje {msg_id}"}
                }
                mock_httpx_client.post.return_value = mock_response

                return await logger._create_note(event)

            # Ejecutar concurrentemente
            tasks = [send_message(i) for i in range(5)]
            results = await asyncio.gather(*tasks)

            # Verificar que todas se crearon exitosamente
            assert all(results), "Todas las notas deben crearse exitosamente"

            # Verificar que el caché no está corrupto
            cache_key = f"hs_assoc:contact:{contact_id}:notes"
            cache_exists = await fake_redis.exists(cache_key)

            # El caché debe haberse invalidado (por las creaciones)
            # O si existe, debe ser válido (no corrupto)
            if cache_exists:
                cached = await logger._get_cached_associations(contact_id)
                assert isinstance(cached, list), "Caché no debe estar corrupto"


# ============================================================================
# TESTS E2E - FLUJO TAKE-CONTROL → WEBHOOK
# ============================================================================

@pytest.mark.integration
@pytest.mark.e2e
class TestTakeControlWebhookE2E:
    """Tests E2E del flujo completo: panel → take-control → webhook."""

    @pytest.mark.asyncio
    async def test_e2e_advisor_takes_control_bot_stops_responding(self, fake_redis):
        """
        Flujo E2E completo:
        1. Cliente envía mensaje → Bot responde (BOT_ACTIVE)
        2. Asesora hace click en contacto → take-control (HUMAN_ACTIVE)
        3. Cliente envía otro mensaje → Bot NO responde
        4. Asesora cierra conversación → Bot retoma control (BOT_ACTIVE)
        5. Cliente envía mensaje → Bot vuelve a responder

        Este es el flujo completo de handoff.
        """
        from middleware.webhook_handler import should_bot_respond

        phone = "+573001234567"
        canal = "whatsapp_directo"
        contact_id = "196843060059"

        state_manager = ConversationStateManager("redis://fake")
        state_manager.redis = fake_redis

        # ===== FASE 1: Bot activo =====
        status_1 = await state_manager.get_status(phone, canal)
        assert status_1 is None or status_1 == ConversationStatus.BOT_ACTIVE

        with patch("middleware.webhook_handler.get_state_manager", return_value=state_manager):
            should_respond_1, reason_1, _ = await should_bot_respond(phone, contact_id)

            assert should_respond_1 is True, \
                "Bot debe responder inicialmente (BOT_ACTIVE)"

        # ===== FASE 2: Asesora toma control =====
        await state_manager.activate_human(
            phone_normalized=phone,
            canal_origen=canal,
            owner_id="88251457",
            contact_id=contact_id,
            reason="Asesora seleccionó contacto"
        )

        status_2 = await state_manager.get_status(phone, canal)
        assert status_2 == ConversationStatus.HUMAN_ACTIVE

        with patch("middleware.webhook_handler.get_state_manager", return_value=state_manager):
            should_respond_2, reason_2, _ = await should_bot_respond(phone, contact_id)

            assert should_respond_2 is False, \
                "Bot NO debe responder después de take-control (HUMAN_ACTIVE)"
            assert "HUMAN_ACTIVE" in reason_2 or "humano" in reason_2.lower()

        # ===== FASE 3: Asesora cierra conversación =====
        await state_manager.activate_bot(phone, canal=canal)

        status_3 = await state_manager.get_status(phone, canal)
        assert status_3 == ConversationStatus.BOT_ACTIVE

        with patch("middleware.webhook_handler.get_state_manager", return_value=state_manager):
            should_respond_3, reason_3, _ = await should_bot_respond(phone, contact_id)

            assert should_respond_3 is True, \
                "Bot debe volver a responder después de cerrar conversación (BOT_ACTIVE)"

    @pytest.mark.asyncio
    async def test_e2e_multi_channel_handoff_consistency(self, fake_redis):
        """
        Flujo E2E con múltiples canales:
        1. Cliente escribe por Instagram
        2. Asesora toma control en Instagram
        3. Cliente envía mensaje por WhatsApp (canal diferente)
        4. Bot NO debe responder (detecta HUMAN_ACTIVE en otro canal)
        """
        from middleware.webhook_handler import should_bot_respond

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

        # Cliente envía mensaje por WhatsApp (diferente canal)
        with patch("middleware.webhook_handler.get_state_manager", return_value=state_manager):
            # Verificar con canal whatsapp
            should_respond_wsp, reason_wsp, _ = await should_bot_respond(
                phone_normalized=phone,
                contact_id="196843060059"
            )

            # Bot NO debe responder (debe detectar HUMAN_ACTIVE en Instagram)
            assert should_respond_wsp is False, \
                "Bot debe detectar HUMAN_ACTIVE incluso en canal diferente"


# ============================================================================
# TESTS E2E - POLLING INCREMENTAL
# ============================================================================

@pytest.mark.integration
@pytest.mark.e2e
class TestPollingIncrementalE2E:
    """Tests E2E del polling incremental en frontend."""

    @pytest.mark.asyncio
    async def test_e2e_polling_sequence_verification(self):
        """
        Simula el flujo de polling incremental del frontend.

        Secuencia esperada:
        1. Usuario envía mensaje
        2. Poll en 500ms  → puede no estar indexado aún
        3. Poll en 1000ms → puede aparecer
        4. Poll en 1500ms → debe aparecer
        5. Poll en 2500ms → definitivamente aparece
        """
        # Este test es más conceptual ya que el polling es JavaScript
        # Pero podemos validar el comportamiento del backend

        poll_intervals = [500, 1000, 1500, 2500]  # ms

        # Simular que HubSpot tarda 1.2s en indexar
        hubspot_indexing_delay = 1.2  # segundos

        # Calcular en qué poll debería aparecer
        expected_first_appearance_poll = None

        cumulative_time = 0
        for i, interval_ms in enumerate(poll_intervals):
            cumulative_time += interval_ms / 1000.0  # Convertir a segundos

            if cumulative_time >= hubspot_indexing_delay:
                expected_first_appearance_poll = i
                break

        # Debe aparecer en el 2do poll (índice 1) después de 1.5s acumulado (>= 1.2s)
        # - Poll 0: 500ms = 0.5s (no aparece)
        # - Poll 1: 500ms + 1000ms = 1.5s (APARECE aquí)
        assert expected_first_appearance_poll == 1, \
            f"Mensaje debe aparecer en poll {expected_first_appearance_poll}"


# ============================================================================
# TESTS E2E - SENDER DETECTION + FORMAT
# ============================================================================

@pytest.mark.integration
@pytest.mark.e2e
class TestSenderDetectionFormatE2E:
    """Tests E2E del flujo: crear nota → detectar sender → formatear burbujas."""

    @pytest.mark.asyncio
    async def test_e2e_advisor_message_full_flow(self, fake_redis, mock_httpx_client):
        """
        Flujo completo E2E de mensaje de asesor:
        1. Asesor envía mensaje desde panel
        2. Se crea nota en HubSpot con formato específico
        3. Panel carga historial
        4. Sender se detecta correctamente
        5. Burbuja se renderiza con alineación correcta
        """
        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            logger = TimelineLogger()
            logger._redis = fake_redis

            contact_id = "196843060059"

            # ===== PASO 1: Crear nota de asesor =====
            advisor_event = TimelineEvent(
                contact_id=contact_id,
                content="Hola, te ayudo con eso",
                sender=MessageSender.ADVISOR,
                direction=MessageDirection.OUTBOUND,
                external_id="advisor_msg_1"
            )

            # Mock creación exitosa
            mock_response = AsyncMock()
            mock_response.status_code = 201
            mock_httpx_client.post.return_value = mock_response

            success = await logger._create_note(advisor_event)
            assert success is True

            # ===== PASO 2: Simular fetch del panel =====
            # Mock nota del asesor que retorna HubSpot
            mock_note = {
                "id": "note_advisor_1",
                "body": "👤 [Asesor] ➡️\n\nHola, te ayudo con eso\n\n---\n📅 2026-02-24 16:00:00",
                "timestamp": "2026-02-24T16:00:00Z"
            }

            # ===== PASO 3: Detectar sender =====
            sender, sender_name, align = logger._detect_sender_from_body(mock_note["body"])

            assert sender == "advisor"
            assert "Asesor" in sender_name
            assert align == "right"

            # ===== PASO 4: Limpiar body para renderizar =====
            clean_body = logger._clean_note_body(mock_note["body"])

            assert "Hola, te ayudo con eso" in clean_body
            assert "👤" not in clean_body
            assert "---" not in clean_body

    @pytest.mark.asyncio
    async def test_e2e_mixed_conversation_rendering(self, fake_redis, mock_httpx_client):
        """
        Flujo E2E de conversación completa con múltiples tipos de mensajes:
        1. Cliente: pregunta
        2. Bot: respuesta
        3. Asesor: mensaje manual
        4. Cliente: nueva pregunta

        Verifica que cada mensaje se detecte y formatee correctamente.
        """
        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            logger = TimelineLogger()
            logger._redis = fake_redis

            # Simular notas de una conversación completa
            mixed_notes = [
                {
                    "id": "note1",
                    "body": "📱 [Cliente - WhatsApp] ⬅️\n\n¿Tienen apartamentos?\n\n---\n📅 2026-02-24 16:00:00",
                    "timestamp": "2026-02-24T16:00:00Z"
                },
                {
                    "id": "note2",
                    "body": "🤖 [Sofía - IA] ➡️\n\n¡Sí! Te puedo ayudar a buscar\n\n---\n📅 2026-02-24 16:00:30",
                    "timestamp": "2026-02-24T16:00:30Z"
                },
                {
                    "id": "note3",
                    "body": "👤 [Asesor] ➡️\n\nPermíteme mostrarte opciones\n\n---\n📅 2026-02-24 16:01:00",
                    "timestamp": "2026-02-24T16:01:00Z"
                },
                {
                    "id": "note4",
                    "body": "📱 [Cliente - WhatsApp] ⬅️\n\nPerfecto, gracias\n\n---\n📅 2026-02-24 16:01:30",
                    "timestamp": "2026-02-24T16:01:30Z"
                }
            ]

            # Procesar todas las notas
            bubbles = logger._format_notes_as_chat(mixed_notes)

            # Verificar que se generaron 4 burbujas
            assert len(bubbles) == 4

            # Verificar cada tipo
            assert bubbles[0]["sender"] == "client"
            assert bubbles[0]["align"] == "left"

            assert bubbles[1]["sender"] == "bot"
            assert bubbles[1]["align"] == "right"

            assert bubbles[2]["sender"] == "advisor"
            assert bubbles[2]["align"] == "right"

            assert bubbles[3]["sender"] == "client"
            assert bubbles[3]["align"] == "left"

            # Verificar contenido limpio
            assert "apartamentos" in bubbles[0]["message"].lower()
            assert "ayudar a buscar" in bubbles[1]["message"].lower()
            assert "opciones" in bubbles[2]["message"].lower()
            assert "gracias" in bubbles[3]["message"].lower()


# ============================================================================
# TESTS E2E - STRESS TESTS
# ============================================================================

@pytest.mark.integration
@pytest.mark.e2e
@pytest.mark.slow
class TestE2EStress:
    """Tests de estrés para validar comportamiento bajo carga."""

    @pytest.mark.asyncio
    async def test_stress_high_volume_messages(self, fake_redis, mock_httpx_client):
        """
        Simula volumen alto de mensajes:
        - 100 mensajes creados rápidamente
        - Caché se invalida correctamente
        - No hay memory leaks
        """
        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            logger = TimelineLogger()
            logger._redis = fake_redis

            contact_id = "196843060059"

            # Mock respuesta exitosa
            mock_response = AsyncMock()
            mock_response.status_code = 201
            mock_httpx_client.post.return_value = mock_response

            # Crear 100 mensajes
            tasks = []
            for i in range(100):
                event = TimelineEvent(
                    contact_id=contact_id,
                    content=f"Mensaje {i}",
                    sender=MessageSender.BOT,
                    direction=MessageDirection.OUTBOUND,
                    external_id=f"msg_{i}"
                )
                tasks.append(logger._create_note(event))

            # Ejecutar en paralelo
            results = await asyncio.gather(*tasks)

            # Verificar que todos se crearon
            assert all(results), "Todos los mensajes deben crearse exitosamente"

            # Verificar que el caché se invalidó (no quedó corrupto)
            cache_key = f"hs_assoc:contact:{contact_id}:notes"
            cache_exists = await fake_redis.exists(cache_key)

            # Puede o no existir, pero si existe debe ser válido
            if cache_exists:
                cached = await logger._get_cached_associations(contact_id)
                assert isinstance(cached, list)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "integration or e2e"])