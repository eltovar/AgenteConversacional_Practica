"""
Tests del módulo middleware/stage_filter.py.

Verifica:
  - Cache helpers (read/write/error handling)
  - Paginación HubSpot con cursor 'after'
  - Caps (MAX_STAGE_CONTACTS, _MAX_PAGES)
  - Merge con Redis (skeleton vs meta completo)
  - Función pública get_contacts_by_stage_full (pipeline completo)
  - Contrato de respuesta con outbound_panel.py

Ejecutar:
    python -m pytest tests/test_stage_filter.py -v
"""
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Forzar valores predecibles antes de importar el módulo (lee env al cargar)
os.environ.setdefault("MAX_STAGE_CONTACTS", "500")
os.environ.setdefault("HS_STAGE_CACHE_TTL", "60")

from middleware import stage_filter as sf  # noqa: E402


# ═════════════════════════════════════════════════════════════════════════════
# Clase 1: Cache helpers
# ═════════════════════════════════════════════════════════════════════════════
class TestCacheHelpers:
    """Verifica lectura/escritura de cache Redis hs_stage:{stage}:{owner_id}."""

    @pytest.mark.asyncio
    async def test_cache_miss_returns_none(self):
        mock_rc = AsyncMock()
        mock_rc.get = AsyncMock(return_value=None)
        with patch("middleware.outbound_panel._get_redis_client",
                   AsyncMock(return_value=mock_rc)):
            result = await sf._get_cached_stage_results("customer", "owner1")
            assert result is None

    @pytest.mark.asyncio
    async def test_cache_hit_returns_data(self):
        cached_data = [{"contact_id": "c1"}, {"contact_id": "c2"}]
        mock_rc = AsyncMock()
        mock_rc.get = AsyncMock(return_value=json.dumps(cached_data))
        with patch("middleware.outbound_panel._get_redis_client",
                   AsyncMock(return_value=mock_rc)):
            result = await sf._get_cached_stage_results("customer", "owner1")
            assert result == cached_data

    @pytest.mark.asyncio
    async def test_cache_write_uses_correct_key_and_ttl(self):
        mock_rc = AsyncMock()
        with patch("middleware.outbound_panel._get_redis_client",
                   AsyncMock(return_value=mock_rc)):
            await sf._set_cached_stage_results("customer", "owner1", [{"id": "c1"}])
            mock_rc.setex.assert_called_once()
            call_args = mock_rc.setex.call_args
            assert call_args[0][0] == "hs_stage:customer:owner1"
            assert call_args[0][1] == sf.HS_STAGE_CACHE_TTL

    @pytest.mark.asyncio
    async def test_cache_read_handles_redis_error(self):
        """Si Redis está caído, _get_cached_* retorna None sin crash."""
        with patch("middleware.outbound_panel._get_redis_client",
                   AsyncMock(side_effect=Exception("Redis down"))):
            result = await sf._get_cached_stage_results("customer", "owner1")
            assert result is None

    @pytest.mark.asyncio
    async def test_cache_write_swallows_error(self):
        """Si Redis falla al escribir, no propaga."""
        with patch("middleware.outbound_panel._get_redis_client",
                   AsyncMock(side_effect=Exception("Redis down"))):
            # Si lanza, el test falla. Si retorna sin error, pasa.
            await sf._set_cached_stage_results("customer", "owner1", [])


# ═════════════════════════════════════════════════════════════════════════════
# Clase 2: Paginated HubSpot search
# ═════════════════════════════════════════════════════════════════════════════
class TestPaginatedSearch:
    """Verifica _paginated_search: cursor, cap MAX, cap pages, empty."""

    @pytest.mark.asyncio
    async def test_single_page_under_limit(self):
        mock_hs = MagicMock()
        mock_hs.search_contacts_by_lifecyclestage_and_owner = AsyncMock(
            return_value={"results": [{"id": f"c{i}"} for i in range(50)], "next_after": None}
        )
        with patch("integrations.hubspot.hubspot_client", mock_hs):
            results = await sf._paginated_search("customer", "owner1")
            assert len(results) == 50
            assert mock_hs.search_contacts_by_lifecyclestage_and_owner.call_count == 1

    @pytest.mark.asyncio
    async def test_pagination_with_cursor(self):
        mock_hs = MagicMock()
        mock_hs.search_contacts_by_lifecyclestage_and_owner = AsyncMock(side_effect=[
            {"results": [{"id": f"c{i}"} for i in range(100)], "next_after": "cursor_123"},
            {"results": [{"id": f"c{i}"} for i in range(100, 150)], "next_after": None},
        ])
        with patch("integrations.hubspot.hubspot_client", mock_hs):
            results = await sf._paginated_search("customer", "owner1")
            assert len(results) == 150
            second_call = mock_hs.search_contacts_by_lifecyclestage_and_owner.call_args_list[1]
            assert second_call.kwargs["after"] == "cursor_123"

    @pytest.mark.asyncio
    async def test_caps_at_max_stage_contacts(self):
        """Aunque HubSpot siga retornando cursor, el cap MAX_STAGE_CONTACTS gana."""
        mock_hs = MagicMock()

        async def _infinite_search(**kw):
            # Cada call devuelve un batch del tamaño pedido + cursor para seguir
            return {
                "results": [{"id": f"c_{kw.get('after','0')}_{i}"} for i in range(kw["limit"])],
                "next_after": "next_cursor",
            }
        mock_hs.search_contacts_by_lifecyclestage_and_owner = AsyncMock(side_effect=_infinite_search)
        with patch("integrations.hubspot.hubspot_client", mock_hs):
            results = await sf._paginated_search("customer", "owner1")
            assert len(results) == sf.MAX_STAGE_CONTACTS

    @pytest.mark.asyncio
    async def test_caps_at_max_pages_safety(self):
        """Si HubSpot devuelve páginas pequeñas pero siempre con cursor, para a 10 páginas."""
        mock_hs = MagicMock()
        counter = {"n": 0}

        async def _small_pages(**kw):
            counter["n"] += 1
            return {
                "results": [{"id": f"p{counter['n']}_{i}"} for i in range(10)],
                "next_after": "always_next",
            }
        mock_hs.search_contacts_by_lifecyclestage_and_owner = AsyncMock(side_effect=_small_pages)
        with patch("integrations.hubspot.hubspot_client", mock_hs):
            await sf._paginated_search("customer", "owner1")
            assert counter["n"] == sf._MAX_PAGES

    @pytest.mark.asyncio
    async def test_empty_results_returns_empty(self):
        mock_hs = MagicMock()
        mock_hs.search_contacts_by_lifecyclestage_and_owner = AsyncMock(
            return_value={"results": [], "next_after": None}
        )
        with patch("integrations.hubspot.hubspot_client", mock_hs):
            results = await sf._paginated_search("customer", "owner1")
            assert results == []


# ═════════════════════════════════════════════════════════════════════════════
# Clase 3: Batch enrichment
# ═════════════════════════════════════════════════════════════════════════════
class TestBatchEnrich:
    """Verifica que >100 IDs se procesan en chunks de 100."""

    @pytest.mark.asyncio
    async def test_under_100_single_call(self):
        ids = [f"c{i}" for i in range(50)]
        with patch("middleware.outbound_panel._hubspot_batch_get_contacts",
                   AsyncMock(return_value={cid: {"firstname": cid} for cid in ids})) as m:
            result = await sf._batch_enrich(ids)
            assert m.call_count == 1
            assert len(result) == 50

    @pytest.mark.asyncio
    async def test_over_100_multiple_chunks(self):
        ids = [f"c{i}" for i in range(250)]

        async def _per_chunk(chunk):
            return {cid: {"firstname": cid} for cid in chunk}

        with patch("middleware.outbound_panel._hubspot_batch_get_contacts",
                   AsyncMock(side_effect=_per_chunk)) as m:
            result = await sf._batch_enrich(ids)
            assert m.call_count == 3  # 100 + 100 + 50
            assert len(result) == 250

    @pytest.mark.asyncio
    async def test_empty_returns_empty(self):
        result = await sf._batch_enrich([])
        assert result == {}


# ═════════════════════════════════════════════════════════════════════════════
# Clase 4: Merge con Redis
# ═════════════════════════════════════════════════════════════════════════════
class TestMergeWithRedis:
    """Verifica merge: meta Redis vs skeleton HubSpot."""

    @pytest.fixture
    def mock_state_manager_with_one_active(self):
        sm = MagicMock()
        sm.get_all_human_active_contacts = AsyncMock(return_value=[
            {"contact_id": "c1", "phone": "+57300001", "display_name": "Ana",
             "has_unread": True, "current_stage": "stage_old"}
        ])
        return sm

    @pytest.fixture
    def mock_state_manager_empty(self):
        sm = MagicMock()
        sm.get_all_human_active_contacts = AsyncMock(return_value=[])
        return sm

    @pytest.mark.asyncio
    async def test_active_contact_uses_redis_meta(self, mock_state_manager_with_one_active):
        enriched = {"c1": {"firstname": "Otro"}}
        result = await sf._merge_with_redis(
            ["c1"], enriched, mock_state_manager_with_one_active, "owner1", "customer"
        )
        assert len(result) == 1
        assert result[0]["from_hubspot_stage"] is False
        assert result[0]["display_name"] == "Ana"  # Redis ganó

    @pytest.mark.asyncio
    async def test_active_contact_current_stage_overridden(self, mock_state_manager_with_one_active):
        """Redis meta puede tener stage stale — HubSpot es source of truth."""
        result = await sf._merge_with_redis(
            ["c1"], {}, mock_state_manager_with_one_active, "owner1", "customer"
        )
        assert result[0]["current_stage"] == "customer"  # no stage_old

    @pytest.mark.asyncio
    async def test_hubspot_only_creates_skeleton(self, mock_state_manager_empty):
        enriched = {"c2": {"firstname": "Bob", "lastname": "Smith", "phone": "+57300002"}}
        result = await sf._merge_with_redis(
            ["c2"], enriched, mock_state_manager_empty, "owner1", "customer"
        )
        assert result[0]["from_hubspot_stage"] is True
        assert result[0]["display_name"] == "Bob Smith"
        assert result[0]["phone"] == "+57300002"
        assert result[0]["current_stage"] == "customer"
        assert result[0]["assigned_owner_id"] == "owner1"

    @pytest.mark.asyncio
    async def test_skeleton_firstname_only(self, mock_state_manager_empty):
        enriched = {"c2": {"firstname": "Bob"}}
        result = await sf._merge_with_redis(
            ["c2"], enriched, mock_state_manager_empty, "owner1", "customer"
        )
        assert result[0]["display_name"] == "Bob"

    @pytest.mark.asyncio
    async def test_skeleton_fallback_email_or_sin_nombre(self, mock_state_manager_empty):
        enriched = {
            "c2": {"email": "bob@x.com"},
            "c3": {},
        }
        result = await sf._merge_with_redis(
            ["c2", "c3"], enriched, mock_state_manager_empty, "owner1", "customer"
        )
        names = {c["contact_id"]: c["display_name"] for c in result}
        assert names["c2"] == "bob@x.com"
        assert names["c3"] == "Sin nombre"

    @pytest.mark.asyncio
    async def test_sort_by_last_activity_desc(self, mock_state_manager_empty):
        # Skeleton tiene last_activity=None, así que ordena por display_name como tiebreak
        sm = MagicMock()
        sm.get_all_human_active_contacts = AsyncMock(return_value=[
            {"contact_id": "c2", "display_name": "B", "last_activity": "2026-05-01"},
            {"contact_id": "c1", "display_name": "A", "last_activity": "2026-01-01"},
            {"contact_id": "c3", "display_name": "C", "last_activity": "2026-03-01"},
        ])
        result = await sf._merge_with_redis(
            ["c1", "c2", "c3"], {}, sm, "owner1", "customer"
        )
        assert [c["contact_id"] for c in result] == ["c2", "c3", "c1"]

    @pytest.mark.asyncio
    async def test_redis_error_does_not_crash(self):
        """Si get_all_human_active_contacts falla, sigue con skeleton para todos."""
        sm = MagicMock()
        sm.get_all_human_active_contacts = AsyncMock(side_effect=Exception("Redis down"))
        enriched = {"c1": {"firstname": "Ana"}}
        result = await sf._merge_with_redis(
            ["c1"], enriched, sm, "owner1", "customer"
        )
        assert len(result) == 1
        assert result[0]["from_hubspot_stage"] is True  # cae a skeleton


# ═════════════════════════════════════════════════════════════════════════════
# Clase 5: Función pública get_contacts_by_stage_full
# ═════════════════════════════════════════════════════════════════════════════
class TestPublicFunction:
    """Verifica pipeline completo y contrato de respuesta."""

    @pytest.mark.asyncio
    async def test_returns_from_cache_when_hit(self):
        cached = [{"contact_id": "c1", "from_hubspot_stage": False}]
        with patch.object(sf, "_get_cached_stage_results", AsyncMock(return_value=cached)):
            result = await sf.get_contacts_by_stage_full("customer", "owner1", MagicMock())
            assert result["from_cache"] is True
            assert result["contacts"] == cached
            assert result["total"] == 1
            assert result["max_reached"] is False

    @pytest.mark.asyncio
    async def test_full_pipeline_when_cache_miss(self):
        sm = MagicMock()
        sm.get_all_human_active_contacts = AsyncMock(return_value=[])
        with patch.object(sf, "_get_cached_stage_results", AsyncMock(return_value=None)), \
             patch.object(sf, "_paginated_search",
                          AsyncMock(return_value=[{"id": "c1"}])), \
             patch.object(sf, "_batch_enrich",
                          AsyncMock(return_value={"c1": {"firstname": "Ana"}})), \
             patch.object(sf, "_set_cached_stage_results", AsyncMock()) as mock_write:
            result = await sf.get_contacts_by_stage_full("customer", "owner1", sm)
            assert result["total"] == 1
            assert result["from_cache"] is False
            assert result["contacts"][0]["display_name"] == "Ana"
            # Verifica que se escribió cache
            mock_write.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_max_reached_true_when_at_cap(self):
        cached = [{"contact_id": f"c{i}"} for i in range(sf.MAX_STAGE_CONTACTS)]
        with patch.object(sf, "_get_cached_stage_results", AsyncMock(return_value=cached)):
            result = await sf.get_contacts_by_stage_full("customer", "owner1", MagicMock())
            assert result["max_reached"] is True

    @pytest.mark.asyncio
    async def test_returns_max_reached_false_under_cap(self):
        sm = MagicMock()
        sm.get_all_human_active_contacts = AsyncMock(return_value=[])
        with patch.object(sf, "_get_cached_stage_results", AsyncMock(return_value=None)), \
             patch.object(sf, "_paginated_search",
                          AsyncMock(return_value=[{"id": "c1"}])), \
             patch.object(sf, "_batch_enrich",
                          AsyncMock(return_value={"c1": {}})), \
             patch.object(sf, "_set_cached_stage_results", AsyncMock()):
            result = await sf.get_contacts_by_stage_full("customer", "owner1", sm)
            assert result["max_reached"] is False

    @pytest.mark.asyncio
    async def test_empty_stage_returns_empty_contacts(self):
        sm = MagicMock()
        sm.get_all_human_active_contacts = AsyncMock(return_value=[])
        with patch.object(sf, "_get_cached_stage_results", AsyncMock(return_value=None)), \
             patch.object(sf, "_paginated_search", AsyncMock(return_value=[])), \
             patch.object(sf, "_batch_enrich", AsyncMock(return_value={})), \
             patch.object(sf, "_set_cached_stage_results", AsyncMock()):
            result = await sf.get_contacts_by_stage_full("customer", "owner1", sm)
            assert result["contacts"] == []
            assert result["total"] == 0
            assert result["max_reached"] is False


# ═════════════════════════════════════════════════════════════════════════════
# Clase 6: Contrato con outbound_panel.py
# ═════════════════════════════════════════════════════════════════════════════
class TestEndpointContract:
    """Verifica que la respuesta del módulo tiene las keys que GET /contacts espera."""

    @pytest.mark.asyncio
    async def test_response_has_required_keys(self):
        cached = [{"contact_id": "c1"}]
        with patch.object(sf, "_get_cached_stage_results", AsyncMock(return_value=cached)):
            result = await sf.get_contacts_by_stage_full("customer", "owner1", MagicMock())
            # outbound_panel.py:get_active_contacts espera estas 4 keys exactas
            assert "contacts" in result
            assert "total" in result
            assert "max_reached" in result
            assert "from_cache" in result

    @pytest.mark.asyncio
    async def test_resilient_to_cache_write_failure(self):
        """Si Redis falla al escribir cache, igual retorna resultados frescos."""
        sm = MagicMock()
        sm.get_all_human_active_contacts = AsyncMock(return_value=[])
        with patch.object(sf, "_get_cached_stage_results", AsyncMock(return_value=None)), \
             patch.object(sf, "_paginated_search", AsyncMock(return_value=[])), \
             patch.object(sf, "_batch_enrich", AsyncMock(return_value={})), \
             patch.object(sf, "_set_cached_stage_results",
                          AsyncMock(side_effect=Exception("Redis full"))):
            # _set_cached_stage_results internamente swallowea — pero por si acaso lo simulamos como
            # excepción y verificamos que get_contacts_by_stage_full no propaga.
            # NOTA: en el código real, _set_cached_stage_results ya tiene try/except interno,
            # así que esto es belt-and-suspenders.
            try:
                result = await sf.get_contacts_by_stage_full("customer", "owner1", sm)
                # Si llega aquí sin crash, el contrato se mantiene
                assert "contacts" in result
            except Exception:
                pytest.fail("get_contacts_by_stage_full no debe propagar errores de cache write")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
