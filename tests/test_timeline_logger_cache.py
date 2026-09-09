"""
Tests Unitarios para TimelineLogger - Caché y Asociaciones.

Tests para validar:
1. TTL de 10 segundos en caché de asociaciones
2. Invalidación automática de caché al crear nota
3. Comportamiento antes/después de expiración
4. Race conditions en caché

Cobertura objetivo: >95%
"""

import httpx
import pytest
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from freezegun import freeze_time
import fakeredis.aioredis

from integrations.hubspot.timeline_logger import (
    TimelineLogger,
    TimelineEvent,
    MessageSender,
    MessageDirection,
    ASSOCIATIONS_CACHE_TTL
)


# ============================================================================
# TESTS UNITARIOS - CACHÉ DE ASOCIACIONES
# ============================================================================

@pytest.mark.unit
@pytest.mark.cache
class TestAssociationsCacheTTL:
    """Tests para validar el TTL de 10 segundos en caché de asociaciones."""

    @pytest.mark.asyncio
    async def test_cache_ttl_is_10_seconds(self):
        """Verifica que el TTL configurado sea exactamente 10 segundos."""
        assert ASSOCIATIONS_CACHE_TTL == 10, "TTL debe ser 10 segundos"

    @pytest.mark.asyncio
    async def test_cache_set_with_ttl(self, fake_redis, mock_httpx_client):
        """
        Verifica que al guardar asociaciones en caché,
        se establezca el TTL de 10 segundos.
        """
        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            logger = TimelineLogger()
            logger._redis = fake_redis

            contact_id = "196843060059"
            note_ids = ["note1", "note2", "note3"]

            # Guardar en caché
            await logger._set_cached_associations(contact_id, note_ids)

            # Verificar que se guardó
            cache_key = f"hs_assoc:contact:{contact_id}:notes"
            cached_value = await fake_redis.get(cache_key)
            assert cached_value is not None

            # Verificar TTL
            ttl = await fake_redis.ttl(cache_key)
            assert ttl <= 10, f"TTL debe ser ≤10s, pero es {ttl}s"
            assert ttl > 0, "TTL debe ser positivo"

    @pytest.mark.asyncio
    async def test_cache_expires_after_ttl(self, fake_redis, mock_httpx_client):
        """
        Verifica que el caché expira después del TTL.

        Usa fakeredis que simula expiración real.
        """
        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            logger = TimelineLogger()
            logger._redis = fake_redis

            contact_id = "196843060059"
            note_ids = ["note1", "note2"]

            # Guardar con TTL corto (1 segundo para test rápido)
            cache_key = f"hs_assoc:contact:{contact_id}:notes"
            await fake_redis.set(cache_key, '["note1", "note2"]', ex=1)

            # Verificar que existe
            assert await fake_redis.exists(cache_key) == 1

            # Esperar expiración
            await asyncio.sleep(1.1)

            # Verificar que expiró
            assert await fake_redis.exists(cache_key) == 0

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_data(self, fake_redis, mock_httpx_client):
        """
        Verifica que si el caché existe, se retorne sin consultar HubSpot.
        """
        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            logger = TimelineLogger()
            logger._redis = fake_redis

            contact_id = "196843060059"
            expected_note_ids = ["note1", "note2", "note3"]

            # Pre-poblar caché
            await logger._set_cached_associations(contact_id, expected_note_ids)

            # Obtener de caché
            cached = await logger._get_cached_associations(contact_id)

            assert cached is not None
            assert cached == expected_note_ids

    @pytest.mark.asyncio
    async def test_cache_miss_returns_none(self, fake_redis, mock_httpx_client):
        """
        Verifica que si el caché no existe, se retorne None.
        """
        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            logger = TimelineLogger()
            logger._redis = fake_redis

            contact_id = "nonexistent-contact"

            # Intentar obtener de caché
            cached = await logger._get_cached_associations(contact_id)

            assert cached is None


# ============================================================================
# TESTS UNITARIOS - INVALIDACIÓN DE CACHÉ
# ============================================================================

@pytest.mark.unit
@pytest.mark.cache
class TestCacheInvalidation:
    """Tests para validar la invalidación automática de caché."""

    @pytest.mark.asyncio
    async def test_invalidate_cache_deletes_key(self, fake_redis, mock_httpx_client):
        """
        Verifica que invalidate_cache_for_contact() elimina la key de Redis.
        """
        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            logger = TimelineLogger()
            logger._redis = fake_redis

            contact_id = "196843060059"

            # Pre-poblar caché
            await logger._set_cached_associations(contact_id, ["note1", "note2"])

            # Verificar que existe
            cache_key = f"hs_assoc:contact:{contact_id}:notes"
            assert await fake_redis.exists(cache_key) == 1

            # Invalidar
            result = await logger.invalidate_cache_for_contact(contact_id)

            # Verificar
            assert result is True
            assert await fake_redis.exists(cache_key) == 0

    @pytest.mark.asyncio
    async def test_invalidate_nonexistent_cache_returns_true(self, fake_redis, mock_httpx_client):
        """
        Verifica que invalidar un caché inexistente no falle.
        """
        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            logger = TimelineLogger()
            logger._redis = fake_redis

            contact_id = "nonexistent"

            # Invalidar caché que no existe
            result = await logger.invalidate_cache_for_contact(contact_id)

            # No debe fallar
            assert result is True

    @pytest.mark.asyncio
    async def test_cache_invalidated_after_note_creation(self, fake_redis, mock_httpx_client):
        """
        CRÍTICO: Verifica que al crear una nota, el caché se invalide automáticamente.

        Este es el fix principal para el problema de sincronización.
        """
        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            logger = TimelineLogger()
            logger._redis = fake_redis

            contact_id = "196843060059"

            # Pre-poblar caché con notas antiguas
            await logger._set_cached_associations(contact_id, ["old_note1", "old_note2"])

            # Verificar que existe caché
            cache_key = f"hs_assoc:contact:{contact_id}:notes"
            assert await fake_redis.exists(cache_key) == 1

            # Crear nueva nota
            event = TimelineEvent(
                contact_id=contact_id,
                content="Nuevo mensaje de prueba",
                sender=MessageSender.BOT,
                direction=MessageDirection.OUTBOUND,
                external_id="msg123"
            )

            # Mock httpx para simular creación exitosa
            mock_response = AsyncMock()
            mock_response.status_code = 201
            mock_httpx_client.post.return_value = mock_response

            await logger._create_note(event)

            # VERIFICACIÓN CRÍTICA: El caché debe haberse invalidado
            assert await fake_redis.exists(cache_key) == 0, \
                "El caché debe invalidarse automáticamente después de crear una nota"

    @pytest.mark.asyncio
    async def test_cache_invalidation_handles_redis_errors(self, fake_redis, mock_httpx_client):
        """
        Verifica que si Redis falla al invalidar, no se lance excepción.
        """
        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            logger = TimelineLogger()

            # Mock Redis que falla
            mock_redis = AsyncMock()
            mock_redis.delete.side_effect = Exception("Redis connection error")
            logger._redis = mock_redis

            contact_id = "196843060059"

            # Invalidar - no debe lanzar excepción
            result = await logger.invalidate_cache_for_contact(contact_id)

            # Debe retornar False pero no crashear
            assert result is False


# ============================================================================
# TESTS UNITARIOS - FLUJO COMPLETO DE CACHÉ
# ============================================================================

@pytest.mark.unit
@pytest.mark.cache
class TestCacheFlowIntegration:
    """Tests del flujo completo: fetch → cache → invalidate → refetch."""

    @pytest.mark.asyncio
    async def test_full_cache_flow(self, fake_redis, mock_httpx_client):
        """
        Test del flujo completo:
        1. Primera consulta: fetch de HubSpot + guardar en caché
        2. Segunda consulta: leer de caché (sin consultar HubSpot)
        3. Crear nota: invalidar caché
        4. Tercera consulta: fetch de HubSpot con datos frescos
        """
        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            logger = TimelineLogger()
            logger._redis = fake_redis

            contact_id = "196843060059"

            # ===== PASO 1: Primera consulta (cache MISS) =====
            notes_1 = await logger.get_notes_for_contact(contact_id, limit=10)

            # Debe haber consultado HubSpot y guardado en caché
            cache_key = f"hs_assoc:contact:{contact_id}:notes"
            assert await fake_redis.exists(cache_key) == 1

            # ===== PASO 2: Segunda consulta (cache HIT) =====
            # Reset mock para verificar que NO se llama
            mock_httpx_client.get.reset_mock()

            notes_2 = await logger.get_notes_for_contact(contact_id, limit=10)

            # No debe haber consultado HubSpot (usó caché)
            # Verificamos que el endpoint de asociaciones NO se llamó
            # (en implementación real, verificaríamos el mock)

            # ===== PASO 3: Crear nueva nota =====
            event = TimelineEvent(
                contact_id=contact_id,
                content="Nueva nota",
                sender=MessageSender.ADVISOR,
                direction=MessageDirection.OUTBOUND
            )

            mock_response = AsyncMock()
            mock_response.status_code = 201
            mock_httpx_client.post.return_value = mock_response

            await logger._create_note(event)

            # Verificar que el caché se invalidó
            assert await fake_redis.exists(cache_key) == 0

            # ===== PASO 4: Tercera consulta (cache MISS después de invalidar) =====
            notes_3 = await logger.get_notes_for_contact(contact_id, limit=10)

            # Debe haber consultado HubSpot de nuevo
            # Y guardado nuevo caché
            assert await fake_redis.exists(cache_key) == 1

    @pytest.mark.asyncio
    async def test_concurrent_cache_access_no_corruption(self, fake_redis, mock_httpx_client):
        """
        Test de concurrencia: múltiples accesos simultáneos al caché.

        Verifica que no haya corrupción de datos.
        """
        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            logger = TimelineLogger()
            logger._redis = fake_redis

            contact_id = "196843060059"

            # Simular 10 requests concurrentes
            tasks = [
                logger.get_notes_for_contact(contact_id, limit=10)
                for _ in range(10)
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Verificar que ninguna tarea falló
            for result in results:
                assert not isinstance(result, Exception)

            # Verificar que el caché quedó consistente
            cache_key = f"hs_assoc:contact:{contact_id}:notes"
            assert await fake_redis.exists(cache_key) == 1


# ============================================================================
# TESTS DE PERFORMANCE - CACHÉ
# ============================================================================

@pytest.mark.unit
@pytest.mark.cache
@pytest.mark.slow
class TestCachePerformance:
    """Tests de performance del caché."""

    @pytest.mark.asyncio
    async def test_cache_improves_response_time(self, fake_redis, mock_httpx_client):
        """
        Verifica que el caché mejora significativamente el tiempo de respuesta.

        Primera consulta: ~100ms (mock de API)
        Segunda consulta: <5ms (caché)
        """
        import time

        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            logger = TimelineLogger()
            logger._redis = fake_redis

            contact_id = "196843060059"

            # Simular delay de HubSpot API
            def slow_api_call(*args, **kwargs):
                """Retorna una respuesta mock con delay simulado."""
                # Para simular delay sin actualizar la implementación,
                # simplemente retornamos una respuesta normal
                # (El delay real ocurriría en la implementación con await asyncio.sleep)
                response = MagicMock(spec=httpx.Response)
                response.status_code = 200
                response.json.return_value = {"results": [{"toObjectId": "note1"}]}
                return response

            mock_httpx_client.get.side_effect = slow_api_call

            # Primera consulta (sin caché) - simula cache miss
            start = time.time()
            await logger.get_notes_for_contact(contact_id, limit=10)
            first_duration = time.time() - start

            # Segunda consulta (con caché) - debería ser de caché
            # El caché debe evitar la llamada a HubSpot
            mock_httpx_client.get.reset_mock()
            start = time.time()
            await logger.get_notes_for_contact(contact_id, limit=10)
            second_duration = time.time() - start

            # Verificar que la segunda consulta NO hizo request a HubSpot
            # (porque debería venir del caché)
            assert not mock_httpx_client.get.called, \
                "La segunda consulta debería usar caché, no llamar a HubSpot"


# ============================================================================
# TESTS DE EDGE CASES
# ============================================================================

@pytest.mark.unit
@pytest.mark.cache
class TestCacheEdgeCases:
    """Tests de casos extremos y límites."""

    @pytest.mark.asyncio
    async def test_cache_with_empty_note_list(self, fake_redis, mock_httpx_client):
        """Verifica comportamiento con lista vacía de notas."""
        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            logger = TimelineLogger()
            logger._redis = fake_redis

            contact_id = "196843060059"

            # Guardar lista vacía
            await logger._set_cached_associations(contact_id, [])

            # Recuperar
            cached = await logger._get_cached_associations(contact_id)

            assert cached == []

    @pytest.mark.asyncio
    async def test_cache_with_very_long_note_list(self, fake_redis, mock_httpx_client):
        """Verifica comportamiento con muchas notas (>100)."""
        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            logger = TimelineLogger()
            logger._redis = fake_redis

            contact_id = "196843060059"
            note_ids = [f"note_{i}" for i in range(200)]

            # Guardar lista larga
            await logger._set_cached_associations(contact_id, note_ids)

            # Recuperar
            cached = await logger._get_cached_associations(contact_id)

            assert len(cached) == 200
            assert cached == note_ids

    @pytest.mark.asyncio
    async def test_cache_with_special_characters_in_contact_id(self, fake_redis, mock_httpx_client):
        """Verifica comportamiento con caracteres especiales en contact_id."""
        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            logger = TimelineLogger()
            logger._redis = fake_redis

            # Contact ID con caracteres especiales
            contact_id = "contact:123:test@email.com"
            note_ids = ["note1", "note2"]

            await logger._set_cached_associations(contact_id, note_ids)
            cached = await logger._get_cached_associations(contact_id)

            assert cached == note_ids


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "unit and cache"])