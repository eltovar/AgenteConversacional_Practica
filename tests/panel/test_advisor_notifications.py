"""
Tests para el sistema de notificaciones de asesores (Fase 6).

Cubre:
- Métodos de notificación en ConversationStateManager
  (add, get, remove, clear_for_contact, get_all_inbox_phones)
- Scheduler check_aprobados_daily: happy path, idempotencia, sin contactos, error HubSpot
- Scheduler check_24h_advisor_notifications: umbral, ya respondió, idempotencia
- Endpoints REST: GET /notifications, POST /notifications/{id}/read,
  POST /notifications/read-all, PATCH /contacts/{phone}/canal

Ejecutar con:
    python -m pytest tests/panel/test_advisor_notifications.py -v
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest
import pytest_asyncio

# ── Entorno mínimo para importar app ────────────────────────────────────────
os.environ.setdefault("HUBSPOT_API_KEY",        "test_key")
os.environ.setdefault("ADMIN_API_KEY",           "test_admin_key")
os.environ.setdefault("PANEL_API_KEY",           "test_admin_key")
os.environ.setdefault("REDIS_URL",               "redis://localhost:6379")
os.environ.setdefault("TWILIO_ACCOUNT_SID",      "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN",       "test_token")
os.environ.setdefault("TWILIO_WHATSAPP_NUMBER",  "+15005550006")
os.environ.setdefault("OPENAI_API_KEY",          "sk-test")

ADVISOR_ID  = "89096378"
ADVISOR2_ID = "89096380"
PHONE       = "+573001234567"
PHONE2      = "+573009876543"
HEADERS     = {"X-API-Key": "test_admin_key"}


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers / Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def fake_redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True, encoding="utf-8")
    yield r
    await r.flushall()
    await r.aclose()


@pytest_asyncio.fixture
async def state_manager(fake_redis):
    from middleware.conversation_state import ConversationStateManager
    mgr = ConversationStateManager.__new__(ConversationStateManager)
    mgr.redis = fake_redis
    mgr.ADVISOR_NOTIF_PREFIX   = "advisor_notifications:"
    mgr.ADVISOR_NOTIF_TTL      = 30 * 86400
    mgr.NOTIF_IDEM_PREFIX      = "notif_24h_sent:"
    mgr.NOTIF_IDEM_TTL         = 25 * 3600
    mgr.NOTIF_APROBADOS_PREFIX = "notif_aprobados_sent:"
    mgr.ADVISOR_INBOX_PREFIX   = "advisor_inbox:"
    mgr.META_PREFIX            = "conv_meta:"
    mgr.PANEL_TTL_SECONDS      = 365 * 86400
    yield mgr


def _bogota_iso(offset_hours: float = 0) -> str:
    """Timestamp ISO 8601 relativo a ahora en UTC-5 (Bogotá)."""
    bogota_tz = timezone(timedelta(hours=-5))
    dt = datetime.now(bogota_tz) + timedelta(hours=offset_hours)
    return dt.isoformat()


# ═══════════════════════════════════════════════════════════════════════════════
# A) MÉTODOS DE ConversationStateManager
# ═══════════════════════════════════════════════════════════════════════════════

class TestAddAdvisorNotification:

    @pytest.mark.asyncio
    async def test_returns_notif_id(self, state_manager):
        notif_id = await state_manager.add_advisor_notification(
            ADVISOR_ID, "inactivity_24h", {"phone": PHONE, "contact_name": "Juan", "hours_waiting": 26}
        )
        assert isinstance(notif_id, str) and len(notif_id) == 36  # UUID

    @pytest.mark.asyncio
    async def test_stored_in_zset(self, state_manager):
        await state_manager.add_advisor_notification(
            ADVISOR_ID, "aprobados_daily", {"count": 3, "message": "Tienes 3 aprobados"}
        )
        key = f"advisor_notifications:{ADVISOR_ID}"
        members = await state_manager.redis.zrange(key, 0, -1)
        assert len(members) == 1
        data = json.loads(members[0])
        assert data["type"] == "aprobados_daily"
        assert data["count"] == 3

    @pytest.mark.asyncio
    async def test_multiple_notifications_accumulate(self, state_manager):
        for i in range(3):
            await state_manager.add_advisor_notification(
                ADVISOR_ID, "inactivity_24h", {"phone": f"+5730000000{i}", "hours_waiting": 25}
            )
        notifs = await state_manager.get_advisor_notifications(ADVISOR_ID)
        assert len(notifs) == 3


class TestGetAdvisorNotifications:

    @pytest.mark.asyncio
    async def test_empty_when_no_notifications(self, state_manager):
        result = await state_manager.get_advisor_notifications(ADVISOR_ID)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_most_recent_first(self, state_manager, fake_redis):
        # Insertar con scores distintos (timestamps)
        key = f"advisor_notifications:{ADVISOR_ID}"
        old = json.dumps({"id": "old", "type": "inactivity_24h", "hours_waiting": 30})
        new = json.dumps({"id": "new", "type": "inactivity_24h", "hours_waiting": 25})
        await fake_redis.zadd(key, {old: 1000.0, new: 2000.0})

        notifs = await state_manager.get_advisor_notifications(ADVISOR_ID)
        assert notifs[0]["id"] == "new"
        assert notifs[1]["id"] == "old"

    @pytest.mark.asyncio
    async def test_invalid_json_skipped(self, state_manager, fake_redis):
        key = f"advisor_notifications:{ADVISOR_ID}"
        await fake_redis.zadd(key, {"not-valid-json": 1000.0})
        # No debe lanzar excepción
        result = await state_manager.get_advisor_notifications(ADVISOR_ID)
        assert isinstance(result, list)


class TestRemoveAdvisorNotification:

    @pytest.mark.asyncio
    async def test_removes_by_id(self, state_manager):
        notif_id = await state_manager.add_advisor_notification(
            ADVISOR_ID, "inactivity_24h", {"phone": PHONE, "hours_waiting": 26}
        )
        removed = await state_manager.remove_advisor_notification(ADVISOR_ID, notif_id)
        assert removed is True
        notifs = await state_manager.get_advisor_notifications(ADVISOR_ID)
        assert not any(n["id"] == notif_id for n in notifs)

    @pytest.mark.asyncio
    async def test_returns_false_for_unknown_id(self, state_manager):
        removed = await state_manager.remove_advisor_notification(ADVISOR_ID, str(uuid.uuid4()))
        assert removed is False

    @pytest.mark.asyncio
    async def test_only_removes_target(self, state_manager):
        id1 = await state_manager.add_advisor_notification(
            ADVISOR_ID, "inactivity_24h", {"phone": PHONE}
        )
        id2 = await state_manager.add_advisor_notification(
            ADVISOR_ID, "inactivity_24h", {"phone": PHONE2}
        )
        await state_manager.remove_advisor_notification(ADVISOR_ID, id1)
        remaining = await state_manager.get_advisor_notifications(ADVISOR_ID)
        assert len(remaining) == 1
        assert remaining[0]["id"] == id2


class TestClearInactivityNotifications:

    @pytest.mark.asyncio
    async def test_clears_only_inactivity_type_for_phone(self, state_manager):
        await state_manager.add_advisor_notification(
            ADVISOR_ID, "inactivity_24h", {"phone": PHONE, "hours_waiting": 26}
        )
        await state_manager.add_advisor_notification(
            ADVISOR_ID, "inactivity_24h", {"phone": PHONE2, "hours_waiting": 28}
        )
        await state_manager.add_advisor_notification(
            ADVISOR_ID, "aprobados_daily", {"count": 2}
        )

        removed = await state_manager.clear_inactivity_notifications_for_contact(ADVISOR_ID, PHONE)
        assert removed == 1

        remaining = await state_manager.get_advisor_notifications(ADVISOR_ID)
        phones = [n.get("phone") for n in remaining if n.get("type") == "inactivity_24h"]
        assert PHONE not in phones
        assert PHONE2 in phones

    @pytest.mark.asyncio
    async def test_aprobados_daily_not_cleared(self, state_manager):
        await state_manager.add_advisor_notification(
            ADVISOR_ID, "aprobados_daily", {"count": 5}
        )
        removed = await state_manager.clear_inactivity_notifications_for_contact(ADVISOR_ID, PHONE)
        assert removed == 0
        notifs = await state_manager.get_advisor_notifications(ADVISOR_ID)
        assert any(n["type"] == "aprobados_daily" for n in notifs)


class TestGetAllInboxPhones:

    @pytest.mark.asyncio
    async def test_extracts_phones_from_zset(self, state_manager, fake_redis):
        key = f"advisor_inbox:{ADVISOR_ID}"
        await fake_redis.zadd(key, {
            f"{PHONE}:whatsapp": 1.0,
            f"{PHONE2}:instagram": 2.0,
        })
        phones = await state_manager.get_all_inbox_phones(ADVISOR_ID)
        assert PHONE in phones
        assert PHONE2 in phones

    @pytest.mark.asyncio
    async def test_empty_returns_empty_set(self, state_manager):
        phones = await state_manager.get_all_inbox_phones(ADVISOR_ID)
        assert phones == set()


# ═══════════════════════════════════════════════════════════════════════════════
# B) SCHEDULER check_aprobados_daily
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckAprobadosDaily:

    def _make_hs_result(self, count: int):
        return {
            "results": [{"id": str(i), "properties": {}} for i in range(count)],
            "next_after": None,
            "total": count,
        }

    @pytest.mark.asyncio
    async def test_creates_notification_when_contacts_exist(self, fake_redis):
        """Happy path: asesor tiene 3 aprobados → notificación creada."""
        from middleware.conversation_state import ConversationStateManager

        csm = ConversationStateManager.__new__(ConversationStateManager)
        csm.redis = fake_redis
        csm.ADVISOR_NOTIF_PREFIX   = "advisor_notifications:"
        csm.ADVISOR_NOTIF_TTL      = 30 * 86400
        csm.NOTIF_IDEM_PREFIX      = "notif_24h_sent:"
        csm.NOTIF_IDEM_TTL         = 25 * 3600
        csm.NOTIF_APROBADOS_PREFIX = "notif_aprobados_sent:"

        mock_hs = AsyncMock()
        mock_hs.search_contacts_by_lifecyclestage_and_owner = AsyncMock(
            return_value=self._make_hs_result(3)
        )

        mock_assigner = MagicMock()
        mock_assigner.OWNERS_CONFIG = {
            "equipo_portales": [{"name": "Jubeny", "id": ADVISOR_ID, "active": True}],
        }

        with patch("app.get_state_manager", return_value=csm), \
             patch("app.get_bogota_now") as mock_now, \
             patch("integrations.hubspot.hubspot_client", mock_hs), \
             patch("integrations.hubspot.lead_assigner.lead_assigner", mock_assigner):

            mock_now.return_value = datetime.now(timezone(timedelta(hours=-5)))

            from app import check_aprobados_daily
            await check_aprobados_daily()

        notifs = await csm.get_advisor_notifications(ADVISOR_ID)
        assert len(notifs) == 1
        assert notifs[0]["type"] == "aprobados_daily"
        assert notifs[0]["count"] == 3

    @pytest.mark.asyncio
    async def test_idempotencia_no_duplicado(self, fake_redis):
        """Si el flag ya existe, no crea segunda notificación."""
        from middleware.conversation_state import ConversationStateManager

        csm = ConversationStateManager.__new__(ConversationStateManager)
        csm.redis = fake_redis
        csm.ADVISOR_NOTIF_PREFIX   = "advisor_notifications:"
        csm.ADVISOR_NOTIF_TTL      = 30 * 86400
        csm.NOTIF_IDEM_PREFIX      = "notif_24h_sent:"
        csm.NOTIF_IDEM_TTL         = 25 * 3600
        csm.NOTIF_APROBADOS_PREFIX = "notif_aprobados_sent:"

        today = datetime.now(timezone(timedelta(hours=-5))).strftime("%Y-%m-%d")
        idem_key = f"notif_aprobados_sent:{ADVISOR_ID}:{today}"
        await fake_redis.set(idem_key, "3", ex=90000)

        mock_hs = AsyncMock()
        mock_hs.search_contacts_by_lifecyclestage_and_owner = AsyncMock(
            return_value=self._make_hs_result(3)
        )

        mock_assigner = MagicMock()
        mock_assigner.OWNERS_CONFIG = {
            "equipo_portales": [{"name": "Jubeny", "id": ADVISOR_ID, "active": True}],
        }

        with patch("app.get_state_manager", return_value=csm), \
             patch("app.get_bogota_now") as mock_now, \
             patch("integrations.hubspot.hubspot_client", mock_hs), \
             patch("integrations.hubspot.lead_assigner.lead_assigner", mock_assigner):

            mock_now.return_value = datetime.now(timezone(timedelta(hours=-5)))

            from app import check_aprobados_daily
            await check_aprobados_daily()

        # HubSpot no debería haberse consultado
        mock_hs.search_contacts_by_lifecyclestage_and_owner.assert_not_called()
        notifs = await csm.get_advisor_notifications(ADVISOR_ID)
        assert len(notifs) == 0

    @pytest.mark.asyncio
    async def test_skip_when_count_zero(self, fake_redis):
        """Si el asesor no tiene aprobados, no se crea notificación pero se guarda el flag."""
        from middleware.conversation_state import ConversationStateManager

        csm = ConversationStateManager.__new__(ConversationStateManager)
        csm.redis = fake_redis
        csm.ADVISOR_NOTIF_PREFIX   = "advisor_notifications:"
        csm.ADVISOR_NOTIF_TTL      = 30 * 86400
        csm.NOTIF_IDEM_PREFIX      = "notif_24h_sent:"
        csm.NOTIF_IDEM_TTL         = 25 * 3600
        csm.NOTIF_APROBADOS_PREFIX = "notif_aprobados_sent:"

        mock_hs = AsyncMock()
        mock_hs.search_contacts_by_lifecyclestage_and_owner = AsyncMock(
            return_value=self._make_hs_result(0)
        )

        mock_assigner = MagicMock()
        mock_assigner.OWNERS_CONFIG = {
            "equipo_portales": [{"name": "Jubeny", "id": ADVISOR_ID, "active": True}],
        }

        with patch("app.get_state_manager", return_value=csm), \
             patch("app.get_bogota_now") as mock_now, \
             patch("integrations.hubspot.hubspot_client", mock_hs), \
             patch("integrations.hubspot.lead_assigner.lead_assigner", mock_assigner):

            mock_now.return_value = datetime.now(timezone(timedelta(hours=-5)))

            from app import check_aprobados_daily
            await check_aprobados_daily()

        notifs = await csm.get_advisor_notifications(ADVISOR_ID)
        assert len(notifs) == 0

        # Flag puesto a "0" para no volver a consultar HubSpot hoy
        today = datetime.now(timezone(timedelta(hours=-5))).strftime("%Y-%m-%d")
        flag_val = await fake_redis.get(f"notif_aprobados_sent:{ADVISOR_ID}:{today}")
        assert flag_val == "0"

    @pytest.mark.asyncio
    async def test_hubspot_error_swallowed(self, fake_redis):
        """Error en HubSpot no debe crashear el scheduler ni dejar flag."""
        from middleware.conversation_state import ConversationStateManager

        csm = ConversationStateManager.__new__(ConversationStateManager)
        csm.redis = fake_redis
        csm.ADVISOR_NOTIF_PREFIX   = "advisor_notifications:"
        csm.ADVISOR_NOTIF_TTL      = 30 * 86400
        csm.NOTIF_IDEM_PREFIX      = "notif_24h_sent:"
        csm.NOTIF_IDEM_TTL         = 25 * 3600
        csm.NOTIF_APROBADOS_PREFIX = "notif_aprobados_sent:"

        mock_hs = AsyncMock()
        mock_hs.search_contacts_by_lifecyclestage_and_owner = AsyncMock(
            side_effect=Exception("HubSpot timeout")
        )

        mock_assigner = MagicMock()
        mock_assigner.OWNERS_CONFIG = {
            "equipo_portales": [{"name": "Jubeny", "id": ADVISOR_ID, "active": True}],
        }

        with patch("app.get_state_manager", return_value=csm), \
             patch("app.get_bogota_now") as mock_now, \
             patch("integrations.hubspot.hubspot_client", mock_hs), \
             patch("integrations.hubspot.lead_assigner.lead_assigner", mock_assigner):

            mock_now.return_value = datetime.now(timezone(timedelta(hours=-5)))

            from app import check_aprobados_daily
            await check_aprobados_daily()  # No debe lanzar excepción

        notifs = await csm.get_advisor_notifications(ADVISOR_ID)
        assert len(notifs) == 0

    @pytest.mark.asyncio
    async def test_two_active_advisors_each_notified(self, fake_redis):
        """Con dos asesores activos, cada uno recibe su notificación."""
        from middleware.conversation_state import ConversationStateManager

        csm = ConversationStateManager.__new__(ConversationStateManager)
        csm.redis = fake_redis
        csm.ADVISOR_NOTIF_PREFIX   = "advisor_notifications:"
        csm.ADVISOR_NOTIF_TTL      = 30 * 86400
        csm.NOTIF_IDEM_PREFIX      = "notif_24h_sent:"
        csm.NOTIF_IDEM_TTL         = 25 * 3600
        csm.NOTIF_APROBADOS_PREFIX = "notif_aprobados_sent:"

        call_results = {
            ADVISOR_ID:  self._make_hs_result(2),
            ADVISOR2_ID: self._make_hs_result(5),
        }

        async def _hs_side_effect(stage_id, owner_id, **kw):
            return call_results[owner_id]

        mock_hs = AsyncMock()
        mock_hs.search_contacts_by_lifecyclestage_and_owner = AsyncMock(
            side_effect=_hs_side_effect
        )

        mock_assigner = MagicMock()
        mock_assigner.OWNERS_CONFIG = {
            "equipo_portales": [{"name": "Jubeny", "id": ADVISOR_ID,  "active": True}],
            "equipo_directo":  [{"name": "Luisa",  "id": ADVISOR2_ID, "active": True}],
        }

        with patch("app.get_state_manager", return_value=csm), \
             patch("app.get_bogota_now") as mock_now, \
             patch("integrations.hubspot.hubspot_client", mock_hs), \
             patch("integrations.hubspot.lead_assigner.lead_assigner", mock_assigner):

            mock_now.return_value = datetime.now(timezone(timedelta(hours=-5)))

            from app import check_aprobados_daily
            await check_aprobados_daily()

        n1 = await csm.get_advisor_notifications(ADVISOR_ID)
        n2 = await csm.get_advisor_notifications(ADVISOR2_ID)

        assert len(n1) == 1 and n1[0]["count"] == 2
        assert len(n2) == 1 and n2[0]["count"] == 5

    @pytest.mark.asyncio
    async def test_inactive_advisors_skipped(self, fake_redis):
        """Asesores con active=False no reciben notificación."""
        from middleware.conversation_state import ConversationStateManager

        csm = ConversationStateManager.__new__(ConversationStateManager)
        csm.redis = fake_redis
        csm.ADVISOR_NOTIF_PREFIX   = "advisor_notifications:"
        csm.ADVISOR_NOTIF_TTL      = 30 * 86400
        csm.NOTIF_IDEM_PREFIX      = "notif_24h_sent:"
        csm.NOTIF_IDEM_TTL         = 25 * 3600
        csm.NOTIF_APROBADOS_PREFIX = "notif_aprobados_sent:"

        mock_hs = AsyncMock()
        mock_hs.search_contacts_by_lifecyclestage_and_owner = AsyncMock(
            return_value=self._make_hs_result(4)
        )

        mock_assigner = MagicMock()
        mock_assigner.OWNERS_CONFIG = {
            "equipo_marketing": [{"name": "Marketing", "id": "82598814", "active": False}],
            "equipo_respaldo":  [{"name": "Monica",    "id": "89096379", "active": False}],
        }

        with patch("app.get_state_manager", return_value=csm), \
             patch("app.get_bogota_now") as mock_now, \
             patch("integrations.hubspot.hubspot_client", mock_hs), \
             patch("integrations.hubspot.lead_assigner.lead_assigner", mock_assigner):

            mock_now.return_value = datetime.now(timezone(timedelta(hours=-5)))

            from app import check_aprobados_daily
            await check_aprobados_daily()

        mock_hs.search_contacts_by_lifecyclestage_and_owner.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# C) SCHEDULER check_24h_advisor_notifications
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheck24hAdvisorNotifications:

    def _contact(self, phone=PHONE, status="HUMAN_ACTIVE", owner_id=ADVISOR_ID,
                 hours_since=26, advisor_responded=False) -> dict:
        last_activity = _bogota_iso(-hours_since)
        last_advisor = _bogota_iso(-1) if advisor_responded else None
        return {
            "phone": phone,
            "canal": "whatsapp",
            "status": status,
            "owner_id": owner_id,
            "display_name": "Juan García",
            "last_activity": last_activity,
            "last_advisor_message": last_advisor,
            "contact_id": "hs_123",
        }

    @pytest.mark.asyncio
    async def test_creates_notification_after_24h(self, fake_redis):
        """Contacto HUMAN_ACTIVE sin respuesta del asesor > 24h → notificación creada."""
        from middleware.conversation_state import ConversationStateManager

        csm = ConversationStateManager.__new__(ConversationStateManager)
        csm.redis = fake_redis
        csm.ADVISOR_NOTIF_PREFIX   = "advisor_notifications:"
        csm.ADVISOR_NOTIF_TTL      = 30 * 86400
        csm.NOTIF_IDEM_PREFIX      = "notif_24h_sent:"
        csm.NOTIF_IDEM_TTL         = 25 * 3600
        csm.NOTIF_APROBADOS_PREFIX = "notif_aprobados_sent:"

        contact = self._contact(hours_since=26)

        with patch("app.get_state_manager", return_value=csm), \
             patch("app.get_bogota_now") as mock_now, \
             patch.object(csm, "get_all_human_active_contacts", AsyncMock(return_value=[contact])):

            mock_now.return_value = datetime.now(timezone(timedelta(hours=-5)))

            from app import check_24h_advisor_notifications
            await check_24h_advisor_notifications()

        notifs = await csm.get_advisor_notifications(ADVISOR_ID)
        assert len(notifs) == 1
        assert notifs[0]["type"] == "inactivity_24h"
        assert notifs[0]["phone"] == PHONE

    @pytest.mark.asyncio
    async def test_no_notification_when_activity_recent(self, fake_redis):
        """Contacto activo hace solo 20h → no debe notificar."""
        from middleware.conversation_state import ConversationStateManager

        csm = ConversationStateManager.__new__(ConversationStateManager)
        csm.redis = fake_redis
        csm.ADVISOR_NOTIF_PREFIX   = "advisor_notifications:"
        csm.ADVISOR_NOTIF_TTL      = 30 * 86400
        csm.NOTIF_IDEM_PREFIX      = "notif_24h_sent:"
        csm.NOTIF_IDEM_TTL         = 25 * 3600
        csm.NOTIF_APROBADOS_PREFIX = "notif_aprobados_sent:"

        contact = self._contact(hours_since=20)

        with patch("app.get_state_manager", return_value=csm), \
             patch("app.get_bogota_now") as mock_now, \
             patch.object(csm, "get_all_human_active_contacts", AsyncMock(return_value=[contact])):

            mock_now.return_value = datetime.now(timezone(timedelta(hours=-5)))

            from app import check_24h_advisor_notifications
            await check_24h_advisor_notifications()

        notifs = await csm.get_advisor_notifications(ADVISOR_ID)
        assert len(notifs) == 0

    @pytest.mark.asyncio
    async def test_no_notification_when_advisor_responded(self, fake_redis):
        """Asesor respondió después de la última actividad → no notificar."""
        from middleware.conversation_state import ConversationStateManager

        csm = ConversationStateManager.__new__(ConversationStateManager)
        csm.redis = fake_redis
        csm.ADVISOR_NOTIF_PREFIX   = "advisor_notifications:"
        csm.ADVISOR_NOTIF_TTL      = 30 * 86400
        csm.NOTIF_IDEM_PREFIX      = "notif_24h_sent:"
        csm.NOTIF_IDEM_TTL         = 25 * 3600
        csm.NOTIF_APROBADOS_PREFIX = "notif_aprobados_sent:"

        contact = self._contact(hours_since=26, advisor_responded=True)

        with patch("app.get_state_manager", return_value=csm), \
             patch("app.get_bogota_now") as mock_now, \
             patch.object(csm, "get_all_human_active_contacts", AsyncMock(return_value=[contact])):

            mock_now.return_value = datetime.now(timezone(timedelta(hours=-5)))

            from app import check_24h_advisor_notifications
            await check_24h_advisor_notifications()

        notifs = await csm.get_advisor_notifications(ADVISOR_ID)
        assert len(notifs) == 0

    @pytest.mark.asyncio
    async def test_idempotencia_flag_prevents_duplicate(self, fake_redis):
        """Si el flag de idempotencia ya existe, no se crea otra notificación."""
        from middleware.conversation_state import ConversationStateManager

        csm = ConversationStateManager.__new__(ConversationStateManager)
        csm.redis = fake_redis
        csm.ADVISOR_NOTIF_PREFIX   = "advisor_notifications:"
        csm.ADVISOR_NOTIF_TTL      = 30 * 86400
        csm.NOTIF_IDEM_PREFIX      = "notif_24h_sent:"
        csm.NOTIF_IDEM_TTL         = 25 * 3600
        csm.NOTIF_APROBADOS_PREFIX = "notif_aprobados_sent:"

        # Poner el flag manualmente
        await fake_redis.set(f"notif_24h_sent:{ADVISOR_ID}:{PHONE}", "1", ex=90000)

        contact = self._contact(hours_since=26)

        with patch("app.get_state_manager", return_value=csm), \
             patch("app.get_bogota_now") as mock_now, \
             patch.object(csm, "get_all_human_active_contacts", AsyncMock(return_value=[contact])):

            mock_now.return_value = datetime.now(timezone(timedelta(hours=-5)))

            from app import check_24h_advisor_notifications
            await check_24h_advisor_notifications()

        notifs = await csm.get_advisor_notifications(ADVISOR_ID)
        assert len(notifs) == 0

    @pytest.mark.asyncio
    async def test_bot_active_skipped(self, fake_redis):
        """Contactos BOT_ACTIVE no deben generar notificaciones 24h."""
        from middleware.conversation_state import ConversationStateManager

        csm = ConversationStateManager.__new__(ConversationStateManager)
        csm.redis = fake_redis
        csm.ADVISOR_NOTIF_PREFIX   = "advisor_notifications:"
        csm.ADVISOR_NOTIF_TTL      = 30 * 86400
        csm.NOTIF_IDEM_PREFIX      = "notif_24h_sent:"
        csm.NOTIF_IDEM_TTL         = 25 * 3600
        csm.NOTIF_APROBADOS_PREFIX = "notif_aprobados_sent:"

        contact = self._contact(hours_since=30, status="BOT_ACTIVE")

        with patch("app.get_state_manager", return_value=csm), \
             patch("app.get_bogota_now") as mock_now, \
             patch.object(csm, "get_all_human_active_contacts", AsyncMock(return_value=[contact])):

            mock_now.return_value = datetime.now(timezone(timedelta(hours=-5)))

            from app import check_24h_advisor_notifications
            await check_24h_advisor_notifications()

        notifs = await csm.get_advisor_notifications(ADVISOR_ID)
        assert len(notifs) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# D) ENDPOINTS REST
# ═══════════════════════════════════════════════════════════════════════════════

class TestNotificationEndpoints:

    def _client(self):
        from fastapi.testclient import TestClient
        from app import app
        return TestClient(app)

    def test_get_notifications_empty(self, fake_redis):
        from middleware.conversation_state import ConversationStateManager

        csm = ConversationStateManager.__new__(ConversationStateManager)
        csm.redis = fake_redis
        csm.ADVISOR_NOTIF_PREFIX = "advisor_notifications:"

        with patch("middleware.outbound_panel._get_state_manager", return_value=csm), \
             patch.dict(os.environ, {"ADMIN_API_KEY": "test_admin_key", "PANEL_API_KEY": "test_admin_key"}):

            client = self._client()
            resp = client.get(
                f"/whatsapp/panel/notifications?advisor={ADVISOR_ID}",
                headers=HEADERS,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["notifications"] == []
        assert data["unread_count"] == 0

    def test_get_notifications_requires_auth(self):
        client = self._client()
        resp = client.get(f"/whatsapp/panel/notifications?advisor={ADVISOR_ID}")
        assert resp.status_code == 401

    def test_mark_read_removes_notification(self, fake_redis):
        import asyncio
        from middleware.conversation_state import ConversationStateManager

        csm = ConversationStateManager.__new__(ConversationStateManager)
        csm.redis = fake_redis
        csm.ADVISOR_NOTIF_PREFIX = "advisor_notifications:"
        csm.ADVISOR_NOTIF_TTL    = 30 * 86400

        # Insertar notificación directamente en fake_redis
        notif_id = str(uuid.uuid4())
        key = f"advisor_notifications:{ADVISOR_ID}"
        member = json.dumps({"id": notif_id, "type": "inactivity_24h", "phone": PHONE})
        asyncio.get_event_loop().run_until_complete(
            fake_redis.zadd(key, {member: 1000.0})
        )

        with patch("middleware.outbound_panel._get_state_manager", return_value=csm), \
             patch.dict(os.environ, {"ADMIN_API_KEY": "test_admin_key", "PANEL_API_KEY": "test_admin_key"}):

            client = self._client()
            resp = client.post(
                f"/whatsapp/panel/notifications/{notif_id}/read",
                json={"advisor": ADVISOR_ID},
                headers=HEADERS,
            )

        assert resp.status_code == 200
        assert resp.json()["removed"] is True

    def test_read_all_clears_all(self, fake_redis):
        import asyncio
        from middleware.conversation_state import ConversationStateManager

        csm = ConversationStateManager.__new__(ConversationStateManager)
        csm.redis = fake_redis
        csm.ADVISOR_NOTIF_PREFIX = "advisor_notifications:"
        csm.ADVISOR_NOTIF_TTL    = 30 * 86400

        key = f"advisor_notifications:{ADVISOR_ID}"
        loop = asyncio.get_event_loop()
        for i in range(3):
            nid = str(uuid.uuid4())
            m = json.dumps({"id": nid, "type": "inactivity_24h", "phone": PHONE})
            loop.run_until_complete(fake_redis.zadd(key, {m: float(i)}))

        with patch("middleware.outbound_panel._get_state_manager", return_value=csm), \
             patch.dict(os.environ, {"ADMIN_API_KEY": "test_admin_key", "PANEL_API_KEY": "test_admin_key"}):

            client = self._client()
            resp = client.post(
                "/whatsapp/panel/notifications/read-all",
                json={"advisor": ADVISOR_ID},
                headers=HEADERS,
            )

        assert resp.status_code == 200
        assert resp.json()["removed"] == 3


class TestCanalDisplayEndpoint:

    def _client(self):
        from fastapi.testclient import TestClient
        from app import app
        return TestClient(app)

    def _seed_meta(self, fake_redis, phone=PHONE, canal="whatsapp"):
        """Inserta una meta Redis mínima para el endpoint PATCH."""
        import asyncio
        key = f"conv_meta:{phone}:{canal}"
        meta = {"canal_origen": canal, "owner_id": ADVISOR_ID}
        asyncio.get_event_loop().run_until_complete(
            fake_redis.set(key, json.dumps(meta), ex=86400)
        )

    def test_update_canal_display_success(self, fake_redis):
        from middleware.conversation_state import ConversationStateManager

        csm = ConversationStateManager.__new__(ConversationStateManager)
        csm.redis = fake_redis
        csm.META_PREFIX        = "conv_meta:"
        csm.PANEL_TTL_SECONDS  = 365 * 86400

        self._seed_meta(fake_redis)

        with patch("middleware.outbound_panel._get_state_manager", return_value=csm), \
             patch.dict(os.environ, {"ADMIN_API_KEY": "test_admin_key", "PANEL_API_KEY": "test_admin_key"}):

            client = self._client()
            resp = client.patch(
                f"/whatsapp/panel/contacts/{PHONE}/canal",
                json={"canal_display": "instagram", "canal": "whatsapp"},
                headers=HEADERS,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["canal_display"] == "instagram"

    def test_invalid_canal_rejected(self, fake_redis):
        from middleware.conversation_state import ConversationStateManager

        csm = ConversationStateManager.__new__(ConversationStateManager)
        csm.redis = fake_redis
        csm.META_PREFIX = "conv_meta:"

        self._seed_meta(fake_redis)

        with patch("middleware.outbound_panel._get_state_manager", return_value=csm), \
             patch.dict(os.environ, {"ADMIN_API_KEY": "test_admin_key", "PANEL_API_KEY": "test_admin_key"}):

            client = self._client()
            resp = client.patch(
                f"/whatsapp/panel/contacts/{PHONE}/canal",
                json={"canal_display": "canal_inexistente", "canal": "whatsapp"},
                headers=HEADERS,
            )

        assert resp.status_code == 400

    def test_missing_contact_returns_404(self, fake_redis):
        from middleware.conversation_state import ConversationStateManager

        csm = ConversationStateManager.__new__(ConversationStateManager)
        csm.redis = fake_redis
        csm.META_PREFIX = "conv_meta:"

        # No se inserta meta → 404
        with patch("middleware.outbound_panel._get_state_manager", return_value=csm), \
             patch.dict(os.environ, {"ADMIN_API_KEY": "test_admin_key", "PANEL_API_KEY": "test_admin_key"}):

            client = self._client()
            resp = client.patch(
                f"/whatsapp/panel/contacts/{PHONE}/canal",
                json={"canal_display": "facebook", "canal": "whatsapp"},
                headers=HEADERS,
            )

        assert resp.status_code == 404

    def test_canal_origen_unchanged(self, fake_redis):
        """canal_origen no debe ser modificado al actualizar canal_display."""
        import asyncio
        from middleware.conversation_state import ConversationStateManager

        csm = ConversationStateManager.__new__(ConversationStateManager)
        csm.redis = fake_redis
        csm.META_PREFIX        = "conv_meta:"
        csm.PANEL_TTL_SECONDS  = 365 * 86400

        self._seed_meta(fake_redis)

        with patch("middleware.outbound_panel._get_state_manager", return_value=csm), \
             patch.dict(os.environ, {"ADMIN_API_KEY": "test_admin_key", "PANEL_API_KEY": "test_admin_key"}):

            client = self._client()
            client.patch(
                f"/whatsapp/panel/contacts/{PHONE}/canal",
                json={"canal_display": "tiktok", "canal": "whatsapp"},
                headers=HEADERS,
            )

        raw = asyncio.get_event_loop().run_until_complete(
            fake_redis.get(f"conv_meta:{PHONE}:whatsapp")
        )
        meta = json.loads(raw)
        assert meta["canal_origen"] == "whatsapp"  # intacto
        assert meta["canal_display"] == "tiktok"    # solo este cambió
