"""
Tests para el endpoint /metrics/appointments y su export Excel.

Valida contratos de request/response, validaciones de rango,
agregaciones y estructura del Excel. Usa mocks de MongoDB.
"""
import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


TZ_BOG = ZoneInfo("America/Bogota")


def _make_appointment(
    *,
    contact_id="C1",
    phone="+573001234567",
    advisor_id="89096378",
    worker_id="W1",
    worker_name="Carlos Trabajador",
    contact_name="Juan Pérez",
    canal="whatsapp",
    visit_completed_at=None,
    appointment_dt=None,
):
    """Helper para construir doc MongoDB mock."""
    now_bog = datetime.now(TZ_BOG)
    return {
        "contact_id": contact_id,
        "phone": phone,
        "advisor_id": advisor_id,
        "worker_id": worker_id,
        "worker_name": worker_name,
        "contact_name": contact_name,
        "canal": canal,
        "status": "completed",
        "visit_completed_at": visit_completed_at or now_bog,
        "appointment_dt": appointment_dt or (now_bog - timedelta(hours=2)),
    }


@pytest.fixture
def mock_mongo():
    """Mock del mongo_manager con métodos async."""
    m = AsyncMock()
    m.get_completed_appointments = AsyncMock(return_value=[])
    m.count_completed_appointments = AsyncMock(return_value=0)
    return m


@pytest.mark.asyncio
async def test_endpoint_requires_valid_api_key():
    """API Key inválida → 401."""
    from middleware.outbound_panel import get_appointments_metrics
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await get_appointments_metrics(
            date_from="2026-04-01",
            date_to="2026-04-22",
            x_api_key="invalid_key",
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_endpoint_validates_range_over_90_days(mock_mongo):
    """Rango >90 días → 400."""
    from middleware.outbound_panel import get_appointments_metrics
    from fastapi import HTTPException

    with patch('middleware.outbound_panel._validate_api_key', return_value=True), \
         patch('middleware.outbound_panel.get_mongo_manager', return_value=mock_mongo):
        with pytest.raises(HTTPException) as exc:
            await get_appointments_metrics(
                date_from="2026-01-01",
                date_to="2026-05-01",  # 120 días
                x_api_key="valid",
            )
        assert exc.value.status_code == 400
        assert "90" in exc.value.detail


@pytest.mark.asyncio
async def test_endpoint_rejects_invalid_date_format(mock_mongo):
    """Fecha mal formada → 400."""
    from middleware.outbound_panel import get_appointments_metrics
    from fastapi import HTTPException

    with patch('middleware.outbound_panel._validate_api_key', return_value=True), \
         patch('middleware.outbound_panel.get_mongo_manager', return_value=mock_mongo):
        with pytest.raises(HTTPException) as exc:
            await get_appointments_metrics(
                date_from="not-a-date",
                date_to="2026-04-22",
                x_api_key="valid",
            )
        assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_endpoint_rejects_inverted_range(mock_mongo):
    """date_from >= date_to → 400."""
    from middleware.outbound_panel import get_appointments_metrics
    from fastapi import HTTPException

    with patch('middleware.outbound_panel._validate_api_key', return_value=True), \
         patch('middleware.outbound_panel.get_mongo_manager', return_value=mock_mongo):
        with pytest.raises(HTTPException) as exc:
            await get_appointments_metrics(
                date_from="2026-04-22",
                date_to="2026-04-01",
                x_api_key="valid",
            )
        assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_endpoint_returns_empty_on_no_data(mock_mongo):
    """Sin citas → total=0, delta_pct=None."""
    from middleware.outbound_panel import get_appointments_metrics

    with patch('middleware.outbound_panel._validate_api_key', return_value=True), \
         patch('middleware.outbound_panel.get_mongo_manager', return_value=mock_mongo):
        result = await get_appointments_metrics(
            date_from="2026-04-01",
            date_to="2026-04-22",
            x_api_key="valid",
        )
    assert result["total"] == 0
    assert result["delta_pct"] is None
    assert result["by_day"] == {}
    assert result["_details"] == []


@pytest.mark.asyncio
async def test_endpoint_aggregates_correctly(mock_mongo):
    """3 citas → total=3, by_worker cuenta correcto, _details poblado."""
    from middleware.outbound_panel import get_appointments_metrics

    now = datetime.now(TZ_BOG)
    mock_mongo.get_completed_appointments = AsyncMock(return_value=[
        _make_appointment(contact_name="Cliente A", worker_id="W1",
                          worker_name="Carlos", visit_completed_at=now),
        _make_appointment(contact_name="Cliente B", worker_id="W1",
                          worker_name="Carlos",
                          visit_completed_at=now - timedelta(days=1)),
        _make_appointment(contact_name="Cliente C", worker_id="W2",
                          worker_name="Maria",
                          visit_completed_at=now - timedelta(days=2)),
    ])
    mock_mongo.count_completed_appointments = AsyncMock(return_value=2)

    with patch('middleware.outbound_panel._validate_api_key', return_value=True), \
         patch('middleware.outbound_panel.get_mongo_manager', return_value=mock_mongo):
        result = await get_appointments_metrics(
            date_from="2026-04-01",
            date_to="2026-04-22",
            x_api_key="valid",
        )

    assert result["total"] == 3
    assert len(result["_details"]) == 3
    assert result["delta_pct"] == 50.0
    assert result["avg_per_day"] > 0
    assert len(result["by_day"]) >= 1
    # by_worker debe contar: Carlos=2, Maria=1
    assert result["by_worker"]["Carlos"] == 2
    assert result["by_worker"]["Maria"] == 1
    # top_advisor fue eliminado del contrato
    assert "top_advisor" not in result


@pytest.mark.asyncio
async def test_endpoint_filters_by_worker(mock_mongo):
    """Filtro worker_id se propaga a mongo query."""
    from middleware.outbound_panel import get_appointments_metrics

    mock_mongo.get_completed_appointments = AsyncMock(return_value=[])
    mock_mongo.count_completed_appointments = AsyncMock(return_value=0)

    with patch('middleware.outbound_panel._validate_api_key', return_value=True), \
         patch('middleware.outbound_panel.get_mongo_manager', return_value=mock_mongo):
        await get_appointments_metrics(
            date_from="2026-04-01",
            date_to="2026-04-22",
            worker_id="W123",
            x_api_key="valid",
        )

    call_args = mock_mongo.get_completed_appointments.await_args
    assert call_args.args[2] == "W123"
    count_args = mock_mongo.count_completed_appointments.await_args
    assert count_args.args[2] == "W123"


@pytest.mark.asyncio
async def test_endpoint_handles_missing_visit_completed_at(mock_mongo):
    """Cita sin visit_completed_at no rompe la agregación."""
    from middleware.outbound_panel import get_appointments_metrics

    bad_apt = _make_appointment()
    bad_apt["visit_completed_at"] = None
    good_apt = _make_appointment(contact_name="Valid")

    mock_mongo.get_completed_appointments = AsyncMock(return_value=[bad_apt, good_apt])
    mock_mongo.count_completed_appointments = AsyncMock(return_value=0)

    with patch('middleware.outbound_panel._validate_api_key', return_value=True), \
         patch('middleware.outbound_panel.get_mongo_manager', return_value=mock_mongo):
        result = await get_appointments_metrics(
            date_from="2026-04-01",
            date_to="2026-04-22",
            x_api_key="valid",
        )

    # total=2 (raw) pero by_day/_details solo cuentan 1 válida
    assert result["total"] == 2
    assert len(result["_details"]) == 1


@pytest.mark.asyncio
async def test_export_excel_requires_api_key():
    """Export Excel → 401 sin key."""
    from middleware.outbound_panel import export_appointments_excel
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await export_appointments_excel(
            date_from="2026-04-01",
            date_to="2026-04-22",
            x_api_key="invalid",
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_export_excel_returns_xlsx_bytes(mock_mongo):
    """Export Excel genera bytes válidos con Content-Disposition RFC 5987."""
    from middleware.outbound_panel import export_appointments_excel

    mock_mongo.get_completed_appointments = AsyncMock(return_value=[
        _make_appointment(contact_name="Test Client"),
    ])
    mock_mongo.count_completed_appointments = AsyncMock(return_value=0)

    with patch('middleware.outbound_panel._validate_api_key', return_value=True), \
         patch('middleware.outbound_panel.get_mongo_manager', return_value=mock_mongo):
        response = await export_appointments_excel(
            date_from="2026-04-01",
            date_to="2026-04-22",
            x_api_key="valid",
        )

    # Response debe tener bytes y content-disposition RFC 5987
    assert response.media_type == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "filename*=UTF-8''" in response.headers["Content-Disposition"]
    assert len(response.body) > 100  # archivo Excel no vacío


def test_format_datetime_bogota():
    """_format_datetime_bogota convierte ISO a DD/MM/YYYY HH:mm."""
    from middleware.outbound_panel import _format_datetime_bogota
    # UTC ISO → Bogotá (UTC-5)
    result = _format_datetime_bogota("2026-04-22T15:30:00+00:00")
    # 15:30 UTC = 10:30 Bogotá
    assert "10:30" in result
    assert "22/04/2026" in result

    # Input vacío
    assert _format_datetime_bogota("") == ""
    assert _format_datetime_bogota(None) == ""
