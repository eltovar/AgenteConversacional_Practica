"""
Tests para _update_contact_to_visita_realizada.

Usa mocks puros para evitar importar outbound_panel (que depende de slowapi
en el entorno local). Valida la lógica de guards, PATCH y cache invalidation.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def httpx_client_mock():
    """Mock del singleton get_httpx_client()."""
    client = MagicMock()
    client.patch = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_transition_skips_if_contact_id_none():
    """contact_id vacío → early return, no hace PATCH."""
    from middleware.outbound_panel import _update_contact_to_visita_realizada
    with patch('middleware.outbound_panel.get_httpx_client') as mock_client:
        await _update_contact_to_visita_realizada(None)
        mock_client.assert_not_called()
        await _update_contact_to_visita_realizada("")
        mock_client.assert_not_called()


@pytest.mark.asyncio
async def test_transition_skips_if_stage_customer(httpx_client_mock):
    """Contacto ya en 'customer' → no revertir progreso comercial."""
    from middleware.outbound_panel import _update_contact_to_visita_realizada
    with patch('middleware.outbound_panel._get_contact_lifecyclestage',
               new=AsyncMock(return_value='customer')), \
         patch('middleware.outbound_panel.HUBSPOT_API_KEY', 'fake-key'), \
         patch('middleware.outbound_panel.get_httpx_client',
               return_value=httpx_client_mock):
        await _update_contact_to_visita_realizada('123456')
        httpx_client_mock.patch.assert_not_called()


@pytest.mark.asyncio
async def test_transition_skips_if_stage_opportunity(httpx_client_mock):
    """Contacto en 'opportunity' → no revertir."""
    from middleware.outbound_panel import _update_contact_to_visita_realizada
    with patch('middleware.outbound_panel._get_contact_lifecyclestage',
               new=AsyncMock(return_value='opportunity')), \
         patch('middleware.outbound_panel.HUBSPOT_API_KEY', 'fake-key'), \
         patch('middleware.outbound_panel.get_httpx_client',
               return_value=httpx_client_mock):
        await _update_contact_to_visita_realizada('123456')
        httpx_client_mock.patch.assert_not_called()


@pytest.mark.asyncio
async def test_transition_skips_if_already_salesqualifiedlead(httpx_client_mock):
    """Idempotencia: si ya está en salesqualifiedlead, skip."""
    from middleware.outbound_panel import _update_contact_to_visita_realizada
    with patch('middleware.outbound_panel._get_contact_lifecyclestage',
               new=AsyncMock(return_value='salesqualifiedlead')), \
         patch('middleware.outbound_panel.HUBSPOT_API_KEY', 'fake-key'), \
         patch('middleware.outbound_panel.get_httpx_client',
               return_value=httpx_client_mock):
        await _update_contact_to_visita_realizada('123456')
        httpx_client_mock.patch.assert_not_called()


@pytest.mark.asyncio
async def test_transition_skips_if_not_marketingqualifiedlead(httpx_client_mock):
    """Contacto en 'lead' (no es visita agendada) → no saltar stages."""
    from middleware.outbound_panel import _update_contact_to_visita_realizada
    with patch('middleware.outbound_panel._get_contact_lifecyclestage',
               new=AsyncMock(return_value='lead')), \
         patch('middleware.outbound_panel.HUBSPOT_API_KEY', 'fake-key'), \
         patch('middleware.outbound_panel.get_httpx_client',
               return_value=httpx_client_mock):
        await _update_contact_to_visita_realizada('123456')
        httpx_client_mock.patch.assert_not_called()


@pytest.mark.asyncio
async def test_transition_patches_hubspot_on_happy_path(httpx_client_mock):
    """marketingqualifiedlead → PATCH con salesqualifiedlead + invalida caché."""
    from middleware.outbound_panel import _update_contact_to_visita_realizada

    response_ok = MagicMock()
    response_ok.status_code = 200
    httpx_client_mock.patch.return_value = response_ok

    cache_invalidate = AsyncMock()

    with patch('middleware.outbound_panel._get_contact_lifecyclestage',
               new=AsyncMock(return_value='marketingqualifiedlead')), \
         patch('middleware.outbound_panel.HUBSPOT_API_KEY', 'fake-key'), \
         patch('middleware.outbound_panel.get_httpx_client',
               return_value=httpx_client_mock), \
         patch('middleware.outbound_panel._invalidate_contact_stage_cache',
               new=cache_invalidate):
        await _update_contact_to_visita_realizada('123456')

    # Verificar PATCH
    httpx_client_mock.patch.assert_called_once()
    call_kwargs = httpx_client_mock.patch.call_args.kwargs
    assert call_kwargs['json']['properties']['lifecyclestage'] == 'salesqualifiedlead'
    # Verificar cache invalidation
    cache_invalidate.assert_awaited_once_with('123456')


@pytest.mark.asyncio
async def test_transition_handles_httpx_error(httpx_client_mock):
    """4xx/5xx: no lanza excepción, no invalida caché."""
    from middleware.outbound_panel import _update_contact_to_visita_realizada

    response_err = MagicMock()
    response_err.status_code = 500
    response_err.text = "Internal Error"
    httpx_client_mock.patch.return_value = response_err

    cache_invalidate = AsyncMock()

    with patch('middleware.outbound_panel._get_contact_lifecyclestage',
               new=AsyncMock(return_value='marketingqualifiedlead')), \
         patch('middleware.outbound_panel.HUBSPOT_API_KEY', 'fake-key'), \
         patch('middleware.outbound_panel.get_httpx_client',
               return_value=httpx_client_mock), \
         patch('middleware.outbound_panel._invalidate_contact_stage_cache',
               new=cache_invalidate):
        # No debe lanzar excepción
        await _update_contact_to_visita_realizada('123456')

    cache_invalidate.assert_not_called()


@pytest.mark.asyncio
async def test_transition_swallows_exceptions(httpx_client_mock):
    """Excepciones imprevistas no propagan (fire-and-forget)."""
    from middleware.outbound_panel import _update_contact_to_visita_realizada

    with patch('middleware.outbound_panel._get_contact_lifecyclestage',
               new=AsyncMock(side_effect=RuntimeError("boom"))), \
         patch('middleware.outbound_panel.HUBSPOT_API_KEY', 'fake-key'):
        # No debe levantar
        await _update_contact_to_visita_realizada('123456')


def test_advisor_map_builds_from_owners_config():
    """_build_advisor_id_to_name_map retorna dict sin explotar."""
    from middleware.outbound_panel import _build_advisor_id_to_name_map
    # Debe retornar dict aunque OWNERS_CONFIG no cargue (env sin HUBSPOT_API_KEY)
    mapping = _build_advisor_id_to_name_map()
    assert isinstance(mapping, dict)
    # Valores (si los hay) son strings
    for k, v in mapping.items():
        assert isinstance(k, str)
        assert isinstance(v, str)


def test_protected_stages_includes_visita_realizada():
    """La constante protege salesqualifiedlead (evita doble transición)."""
    from middleware.outbound_panel import PROTECTED_STAGES_POST_VISITA
    assert 'salesqualifiedlead' in PROTECTED_STAGES_POST_VISITA
    assert 'opportunity' in PROTECTED_STAGES_POST_VISITA
    assert 'customer' in PROTECTED_STAGES_POST_VISITA
    assert 'evangelist' in PROTECTED_STAGES_POST_VISITA
