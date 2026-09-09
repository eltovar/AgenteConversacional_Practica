"""
Tests para auto-cierre al seleccionar embudo "No responde".

Cuando la asesora selecciona "No responde" (stage_id="other") en el dropdown
del embudo y se envía `phone` en el body, el endpoint PATCH /contacts/{id}/stage
debe:
  1. Actualizar lifecyclestage en HubSpot (two-step PATCH)
  2. Disparar _close_conversation_internal(phone, canal)
  3. Devolver closed=True en la respuesta

Para otros stage_ids o cuando no se envía phone, NO debe disparar el cierre.

Ejecutar con:
    python -m pytest tests/panel/test_no_responde_auto_close.py -v
"""

import os
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

os.environ.setdefault("HUBSPOT_API_KEY", "test_key")
os.environ.setdefault("ADMIN_API_KEY", "test_admin_key")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")


def _mock_hubspot_response(status_code: int = 200, text: str = "{}"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    return resp


class TestNoRespondeAutoClose:
    """PATCH /contacts/{id}/stage con stage_id='other' debe disparar cierre."""

    def test_no_responde_with_phone_triggers_close(self):
        """stage_id='other' + phone → llama _close_conversation_internal y devuelve closed=True."""
        from fastapi.testclient import TestClient
        from app import app

        client = TestClient(app)

        with patch.dict(os.environ, {"ADMIN_API_KEY": "test_admin_key", "PANEL_API_KEY": "test_admin_key"}), \
             patch("middleware.outbound_panel._hubspot_patch", new=AsyncMock(return_value=_mock_hubspot_response(200))) as mock_patch, \
             patch("middleware.outbound_panel._invalidate_contact_stage_cache", new=AsyncMock()), \
             patch("middleware.outbound_panel._close_conversation_internal", new=AsyncMock(return_value={"phone": "+573001234567", "canal": "whatsapp", "new_status": "BOT_ACTIVE"})) as mock_close:

            response = client.patch(
                "/whatsapp/panel/contacts/12345/stage",
                json={"stage_id": "other", "phone": "+573001234567", "canal": "whatsapp"},
                headers={"X-API-Key": "test_admin_key"},
            )

            assert response.status_code == 200, response.text
            data = response.json()
            assert data["status"] == "success"
            assert data["stage_id"] == "other"
            assert data["closed"] is True
            assert data["close_result"]["new_status"] == "BOT_ACTIVE"

            # HubSpot two-step PATCH (clear + set)
            assert mock_patch.await_count == 2
            # Cierre llamado con phone y canal correctos
            mock_close.assert_awaited_once_with("+573001234567", "whatsapp")

    def test_no_responde_without_phone_does_not_close(self):
        """stage_id='other' sin phone → solo actualiza stage, NO cierra."""
        from fastapi.testclient import TestClient
        from app import app

        client = TestClient(app)

        with patch.dict(os.environ, {"ADMIN_API_KEY": "test_admin_key", "PANEL_API_KEY": "test_admin_key"}), \
             patch("middleware.outbound_panel._hubspot_patch", new=AsyncMock(return_value=_mock_hubspot_response(200))), \
             patch("middleware.outbound_panel._invalidate_contact_stage_cache", new=AsyncMock()), \
             patch("middleware.outbound_panel._close_conversation_internal", new=AsyncMock()) as mock_close:

            response = client.patch(
                "/whatsapp/panel/contacts/12345/stage",
                json={"stage_id": "other"},
                headers={"X-API-Key": "test_admin_key"},
            )

            assert response.status_code == 200, response.text
            data = response.json()
            assert data["closed"] is False
            assert "close_result" not in data
            mock_close.assert_not_awaited()

    def test_other_stage_with_phone_does_not_close(self):
        """stage_id distinto de 'other' (con phone) NO debe cerrar — solo 'No responde' cierra."""
        from fastapi.testclient import TestClient
        from app import app

        client = TestClient(app)

        with patch.dict(os.environ, {"ADMIN_API_KEY": "test_admin_key", "PANEL_API_KEY": "test_admin_key"}), \
             patch("middleware.outbound_panel._hubspot_patch", new=AsyncMock(return_value=_mock_hubspot_response(200))), \
             patch("middleware.outbound_panel._invalidate_contact_stage_cache", new=AsyncMock()), \
             patch("middleware.outbound_panel._close_conversation_internal", new=AsyncMock()) as mock_close:

            response = client.patch(
                "/whatsapp/panel/contacts/12345/stage",
                json={"stage_id": "customer", "phone": "+573001234567", "canal": "whatsapp"},
                headers={"X-API-Key": "test_admin_key"},
            )

            assert response.status_code == 200, response.text
            data = response.json()
            assert data["closed"] is False
            mock_close.assert_not_awaited()

    def test_close_failure_does_not_fail_stage_update(self):
        """Si el cierre lanza excepción, la actualización de stage sigue siendo exitosa
        y la respuesta incluye close_error en lugar de fallar la request."""
        from fastapi.testclient import TestClient
        from app import app

        client = TestClient(app)

        with patch.dict(os.environ, {"ADMIN_API_KEY": "test_admin_key", "PANEL_API_KEY": "test_admin_key"}), \
             patch("middleware.outbound_panel._hubspot_patch", new=AsyncMock(return_value=_mock_hubspot_response(200))), \
             patch("middleware.outbound_panel._invalidate_contact_stage_cache", new=AsyncMock()), \
             patch("middleware.outbound_panel._close_conversation_internal", new=AsyncMock(side_effect=RuntimeError("redis down"))):

            response = client.patch(
                "/whatsapp/panel/contacts/12345/stage",
                json={"stage_id": "other", "phone": "+573001234567", "canal": "whatsapp"},
                headers={"X-API-Key": "test_admin_key"},
            )

            assert response.status_code == 200, response.text
            data = response.json()
            assert data["status"] == "success"
            assert data["closed"] is False
            assert "redis down" in data["close_error"]

    def test_invalid_stage_id_rejected(self):
        """stage_id no presente en PIPELINE_STAGES debe rechazarse con 400 (no-regresión)."""
        from fastapi.testclient import TestClient
        from app import app

        client = TestClient(app)

        with patch.dict(os.environ, {"ADMIN_API_KEY": "test_admin_key", "PANEL_API_KEY": "test_admin_key"}):
            response = client.patch(
                "/whatsapp/panel/contacts/12345/stage",
                json={"stage_id": "not_a_real_stage"},
                headers={"X-API-Key": "test_admin_key"},
            )

        assert response.status_code == 400


class TestCloseConversationInternalHelper:
    """El helper extraído debe ejercitar la misma lógica que el endpoint DELETE /close."""

    @pytest.mark.asyncio
    async def test_helper_activates_bot_and_cleans_zset(self):
        """_close_conversation_internal llama activate_bot y remueve del ZSET."""
        from middleware import outbound_panel as op

        sm = MagicMock()
        sm.activate_bot = AsyncMock()
        sm.get_meta = AsyncMock(return_value=None)
        sm.PANEL_TTL_SECONDS = 86400

        redis_mock = AsyncMock()
        redis_mock.zrem = AsyncMock()
        redis_mock.srem = AsyncMock()
        redis_mock.get = AsyncMock(return_value=None)
        redis_mock.ttl = AsyncMock(return_value=-1)
        redis_mock.set = AsyncMock()

        with patch.object(op, "_get_state_manager", return_value=sm), \
             patch.object(op, "_get_redis_client", new=AsyncMock(return_value=redis_mock)):

            result = await op._close_conversation_internal("+573001234567", "whatsapp")

        assert result["new_status"] == "BOT_ACTIVE"
        assert result["canal"] == "whatsapp"
        sm.activate_bot.assert_awaited()
        redis_mock.zrem.assert_awaited_with("active_conversations_sorted", "+573001234567:whatsapp")
        redis_mock.srem.assert_awaited_with("bot_controlled_conversations", "+573001234567:whatsapp")
