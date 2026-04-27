"""
Tests para _invalidate_stale_conv_sid (fix split-brain).

Contexto: cuando Twilio devuelve 404 al enviar a un conversation_sid que ya
expiró, el cleanup debe (1) cerrar la conv en Twilio y (2) limpiar Mongo.
Sin el paso 1, Twilio sigue enrutando inbound a la conv vieja mientras
los outbound crean una nueva → split-brain → WAMid→IM SID irresoluble.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from utils.twilio_client import TwilioClient


@pytest.fixture
def client():
    c = TwilioClient()
    c.account_sid = "ACtest"
    c.auth_token = "tokentest"
    c.messaging_service_sid = "MGtest"
    c._available = True
    return c


@pytest.mark.asyncio
async def test_invalidate_closes_conv_in_twilio_and_cleans_mongo(client):
    """200 al cerrar en Twilio → debe limpiar Mongo después."""
    close_resp = MagicMock(status_code=200, text="ok")
    http_mock = MagicMock()
    http_mock.post = AsyncMock(return_value=close_resp)
    client._http_client = http_mock

    mongo_mock = MagicMock()
    mongo_mock.connect = AsyncMock(return_value=True)
    update_result = MagicMock(modified_count=3)
    mongo_mock.db.messages.update_many = AsyncMock(return_value=update_result)

    with patch("database.mongodb_client.get_mongo_manager", return_value=mongo_mock):
        await client._invalidate_stale_conv_sid("CHabc123")

    # Paso 1: cerró en Twilio
    http_mock.post.assert_awaited_once()
    args, kwargs = http_mock.post.call_args
    assert "Conversations/CHabc123" in args[0]
    assert kwargs["data"] == {"State": "closed"}
    assert kwargs["auth"] == ("ACtest", "tokentest")

    # Paso 2: limpió Mongo
    mongo_mock.db.messages.update_many.assert_awaited_once()
    filt, upd = mongo_mock.db.messages.update_many.call_args.args
    assert filt == {"conversation_sid": "CHabc123"}
    assert "$unset" in upd
    assert "conversation_sid" in upd["$unset"]
    assert "conversations_message_sid" in upd["$unset"]


@pytest.mark.asyncio
async def test_invalidate_handles_404_from_twilio(client):
    """Si la conv ya no existe en Twilio (404), debe limpiar Mongo igual."""
    close_resp = MagicMock(status_code=404, text="not found")
    http_mock = MagicMock()
    http_mock.post = AsyncMock(return_value=close_resp)
    client._http_client = http_mock

    mongo_mock = MagicMock()
    mongo_mock.connect = AsyncMock(return_value=True)
    mongo_mock.db.messages.update_many = AsyncMock(return_value=MagicMock(modified_count=1))

    with patch("database.mongodb_client.get_mongo_manager", return_value=mongo_mock):
        await client._invalidate_stale_conv_sid("CHdead")

    mongo_mock.db.messages.update_many.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalidate_continues_if_twilio_close_raises(client):
    """Excepción en POST a Twilio no debe abortar el cleanup en Mongo."""
    http_mock = MagicMock()
    http_mock.post = AsyncMock(side_effect=Exception("network down"))
    client._http_client = http_mock

    mongo_mock = MagicMock()
    mongo_mock.connect = AsyncMock(return_value=True)
    mongo_mock.db.messages.update_many = AsyncMock(return_value=MagicMock(modified_count=2))

    with patch("database.mongodb_client.get_mongo_manager", return_value=mongo_mock):
        await client._invalidate_stale_conv_sid("CHboom")

    mongo_mock.db.messages.update_many.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_via_conversations_404_triggers_invalidate(client):
    """El 404 en _send_via_conversations debe disparar _invalidate_stale_conv_sid."""
    resp_404 = MagicMock(status_code=404, text="conv expired")
    http_mock = MagicMock()
    http_mock.post = AsyncMock(return_value=resp_404)
    client._http_client = http_mock

    invalidate_mock = AsyncMock()
    with patch.object(client, "_invalidate_stale_conv_sid", invalidate_mock):
        result = await client._send_via_conversations(
            conversation_sid="CHexpired",
            body="hola",
        )
        # Dejar que el create_task agendado se ejecute
        import asyncio
        await asyncio.sleep(0)

    assert result["status"] == "error"
    assert result["code"] == 404
    invalidate_mock.assert_awaited_with("CHexpired")
