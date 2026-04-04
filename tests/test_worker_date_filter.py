"""
Test Worker Date Filter — 3 bug fixes
  Bug 1: get_contacts_by_worker() acepta date_from/date_to (antes hardcodeado $gte:now)
  Bug 2: #dateFrom/#dateTo disparan loadContacts() en 'change' (JS — no testeable aquí)
  Bug 3: loadContacts() siempre envía date_from/date_to; backend los parsea y reenvía

Run:
    python -m pytest tests/test_worker_date_filter.py -v
"""
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOGOTA = ZoneInfo("America/Bogota")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _build_mongo_manager():
    """
    Retorna MongoDBManager con Motor completamente mockeado.
    IMPORTANTE: se setean _connected=True Y client=MagicMock() porque
    connect() revisa AMBAS condiciones: `if self._connected and self.client`.
    """
    from database.mongodb_client import MongoDBManager

    manager = MongoDBManager()
    manager._connected = True
    manager.client = MagicMock()   # crítico: sin esto connect() intenta conectar

    mock_cursor = MagicMock()
    mock_cursor.to_list = AsyncMock(return_value=[])

    mock_find = MagicMock()
    mock_find.sort = MagicMock(return_value=mock_cursor)

    mock_appointments = MagicMock()
    mock_appointments.find = MagicMock(return_value=mock_find)

    mock_db = MagicMock()
    mock_db.appointments = mock_appointments
    manager.db = mock_db

    return manager, mock_appointments, mock_find


def _dt(year=2026, month=4, day=1, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=BOGOTA)


def _parse(s):
    """Replica exacta del bloque de parseo en outbound_panel.py (líneas 3553-3566)."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BOGOTA)
        return dt
    except ValueError:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# GRUPO A — get_contacts_by_worker() unit tests
# ══════════════════════════════════════════════════════════════════════════════

class TestGetContactsByWorkerQuery:
    """Valida que get_contacts_by_worker() construye el dt_filter correcto en MongoDB."""

    @pytest.mark.asyncio
    async def test_no_dates_usa_ventana_4h(self):
        """Sin fechas → dt_filter usa $gte: now - 4h (sin $lte)."""
        manager, mock_appts, _ = _build_mongo_manager()
        fixed_now = _dt(2026, 4, 1, 10, 0)

        with patch("database.mongodb_client.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            await manager.get_contacts_by_worker("w1")

        f = mock_appts.find.call_args[0][0]["appointment_dt"]
        assert "$gte" in f and "$lte" not in f
        assert f["$gte"] == fixed_now - timedelta(hours=4)

    @pytest.mark.asyncio
    async def test_solo_date_from_pone_gte_sin_lte(self):
        """Solo date_from → {$gte: date_from} sin límite superior."""
        manager, mock_appts, _ = _build_mongo_manager()
        df = _dt(2026, 4, 1)
        await manager.get_contacts_by_worker("w1", date_from=df)
        f = mock_appts.find.call_args[0][0]["appointment_dt"]
        assert f == {"$gte": df}

    @pytest.mark.asyncio
    async def test_ambas_fechas_ponen_gte_y_lte(self):
        """date_from + date_to → {$gte, $lte}."""
        manager, mock_appts, _ = _build_mongo_manager()
        df = _dt(2026, 4, 1)
        dt_ = _dt(2026, 4, 7, 23, 59)
        await manager.get_contacts_by_worker("w1", date_from=df, date_to=dt_)
        f = mock_appts.find.call_args[0][0]["appointment_dt"]
        assert f == {"$gte": df, "$lte": dt_}

    @pytest.mark.asyncio
    async def test_solo_date_to_pone_lte_sin_gte(self):
        """Solo date_to → {$lte: date_to} sin límite inferior."""
        manager, mock_appts, _ = _build_mongo_manager()
        dt_ = _dt(2026, 4, 7, 23, 59)
        await manager.get_contacts_by_worker("w1", date_to=dt_)
        f = mock_appts.find.call_args[0][0]["appointment_dt"]
        assert f == {"$lte": dt_}

    @pytest.mark.asyncio
    async def test_worker_id_vacio_retorna_lista_vacia_sin_query(self):
        """worker_id='' → return [] inmediato, sin tocar MongoDB."""
        manager, mock_appts, _ = _build_mongo_manager()
        result = await manager.get_contacts_by_worker("")
        assert result == []
        mock_appts.find.assert_not_called()

    @pytest.mark.asyncio
    async def test_query_siempre_filtra_status_scheduled(self):
        """Independiente de fechas, el filtro debe incluir status='scheduled'."""
        manager, mock_appts, _ = _build_mongo_manager()
        await manager.get_contacts_by_worker("w1", date_from=_dt())
        f = mock_appts.find.call_args[0][0]
        assert f.get("status") == "scheduled"

    @pytest.mark.asyncio
    async def test_sort_asc_por_appointment_dt(self):
        """El cursor debe ordenarse ASC (1) por appointment_dt."""
        manager, mock_appts, mock_find = _build_mongo_manager()
        await manager.get_contacts_by_worker("w1", date_from=_dt())
        mock_find.sort.assert_called_once_with("appointment_dt", 1)


# ══════════════════════════════════════════════════════════════════════════════
# GRUPO B — Parseo ISO string → datetime con timezone
# ══════════════════════════════════════════════════════════════════════════════

class TestDateStringParsing:
    """Valida la lógica de parseo de parámetros de fecha en outbound_panel.py."""

    def test_iso_naive_recibe_timezone_bogota(self):
        """'2026-04-01T00:00:00' (sin tz) → adjunta America/Bogota."""
        r = _parse("2026-04-01T00:00:00")
        assert r is not None
        assert str(r.tzinfo) == "America/Bogota"
        assert r.year == 2026 and r.month == 4 and r.day == 1

    def test_string_invalido_retorna_none(self):
        """Strings no-ISO → None sin excepción."""
        assert _parse("not-a-date") is None
        assert _parse("abril_primero_2026") is None

    def test_none_retorna_none(self):
        """None → None (worker usa ventana 4h por defecto)."""
        assert _parse(None) is None

    def test_string_vacio_retorna_none(self):
        """'' → None."""
        assert _parse("") is None

    def test_iso_solo_fecha_sin_hora_parsea_a_medianoche(self):
        """'2026-04-01' (sin hora) → midnight con timezone Bogota."""
        r = _parse("2026-04-01")
        assert r is not None and r.hour == 0 and r.tzinfo is not None

    def test_iso_aware_conserva_su_timezone(self):
        """'2026-04-01T00:00:00-05:00' ya tiene tz → no se modifica."""
        r = _parse("2026-04-01T00:00:00-05:00")
        assert r is not None
        assert r.utcoffset().total_seconds() == -18000


# ══════════════════════════════════════════════════════════════════════════════
# GRUPO C — Smoke tests de integración
# ══════════════════════════════════════════════════════════════════════════════

class TestWorkerFilterIntegration:
    """Valida que _get_contacts_by_worker_filter() pasa los datetime correctos a MongoDB."""

    @pytest.mark.asyncio
    async def test_fechas_se_reenvian_a_mongodb(self):
        """_get_contacts_by_worker_filter() reenvía date_from/date_to a MongoDB."""
        from middleware.outbound_panel import _get_contacts_by_worker_filter

        df = _dt(2026, 4, 1)
        dt_ = _dt(2026, 4, 7, 23, 59)
        mock_mongo = MagicMock()
        mock_mongo.get_contacts_by_worker = AsyncMock(return_value=[])

        with patch("middleware.outbound_panel.get_mongo_manager", return_value=mock_mongo):
            result = await _get_contacts_by_worker_filter(
                worker_id="w1", advisor=None, limit=30,
                date_from=df, date_to=dt_,
            )

        mock_mongo.get_contacts_by_worker.assert_called_once_with(
            "w1", date_from=df, date_to=dt_
        )
        assert result["contacts"] == []
        assert result["filter"] == "worker"

    @pytest.mark.asyncio
    async def test_sin_fechas_pasa_none_a_mongodb(self):
        """Sin fechas → date_from=None, date_to=None → MongoDB usa ventana 4h."""
        from middleware.outbound_panel import _get_contacts_by_worker_filter

        mock_mongo = MagicMock()
        mock_mongo.get_contacts_by_worker = AsyncMock(return_value=[])

        with patch("middleware.outbound_panel.get_mongo_manager", return_value=mock_mongo):
            await _get_contacts_by_worker_filter(
                worker_id="w1", advisor=None, limit=30
            )

        mock_mongo.get_contacts_by_worker.assert_called_once_with(
            "w1", date_from=None, date_to=None
        )

    @pytest.mark.asyncio
    async def test_solo_date_from_date_to_es_none(self):
        """Solo date_from → date_to=None en el call a MongoDB."""
        from middleware.outbound_panel import _get_contacts_by_worker_filter

        df = _dt(2026, 4, 1)
        mock_mongo = MagicMock()
        mock_mongo.get_contacts_by_worker = AsyncMock(return_value=[])

        with patch("middleware.outbound_panel.get_mongo_manager", return_value=mock_mongo):
            await _get_contacts_by_worker_filter(
                worker_id="w1", advisor=None, limit=30, date_from=df
            )

        _, kwargs = mock_mongo.get_contacts_by_worker.call_args
        assert kwargs["date_from"] == df
        assert kwargs["date_to"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
