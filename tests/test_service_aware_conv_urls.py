"""
Tests del fix service-aware Conversations API.

El problema raíz: convs creadas en una Conversations Service distinta a la
Default devuelven 404 cuando se llaman vía URLs sin namespace de Service.
Estos tests verifican que las URLs incluyen /Services/{ISxxx}/ cuando corresponde.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from utils.twilio_client import TwilioClient, _conv_url


@pytest.fixture
def client():
    c = TwilioClient()
    c.account_sid = "ACtest"
    c.auth_token = "tokentest"
    c.messaging_service_sid = "MGtest"
    c._available = True
    return c


# ────────────────────────────── Helper URL ───────────────────────────────

def test_conv_url_default_path_when_no_service():
    url = _conv_url("CHabc123")
    assert url == "https://conversations.twilio.com/v1/Conversations/CHabc123"


def test_conv_url_namespaced_when_service_provided():
    url = _conv_url("CHabc123", "ISxyz789")
    assert url == "https://conversations.twilio.com/v1/Services/ISxyz789/Conversations/CHabc123"


def test_conv_url_messages_suffix_default():
    url = _conv_url("CHabc", suffix="/Messages")
    assert url.endswith("/Conversations/CHabc/Messages")
    assert "/Services/" not in url


def test_conv_url_messages_suffix_namespaced():
    url = _conv_url("CHabc", "ISxyz", suffix="/Messages")
    assert url.endswith("/Services/ISxyz/Conversations/CHabc/Messages")


def test_conv_url_message_specific_suffix():
    url = _conv_url("CHabc", "ISxyz", suffix="/Messages/IMfoo")
    assert url.endswith("/Services/ISxyz/Conversations/CHabc/Messages/IMfoo")


# ────────────────────── _invalidate_stale_conv_sid ───────────────────────

@pytest.mark.asyncio
async def test_invalidate_uses_service_namespaced_close_url(client):
    """Si se conoce el ChatServiceSid, la URL de close lo incluye."""
    close_resp = MagicMock(status_code=200, text="ok")
    http_mock = MagicMock()
    http_mock.post = AsyncMock(return_value=close_resp)
    client._http_client = http_mock

    mongo_mock = MagicMock()
    mongo_mock.connect = AsyncMock(return_value=True)
    mongo_mock.db.messages.update_many = AsyncMock(return_value=MagicMock(modified_count=1))

    with patch("database.mongodb_client.get_mongo_manager", return_value=mongo_mock):
        await client._invalidate_stale_conv_sid("CH2762070edc", "ISservice123")

    args, _ = http_mock.post.call_args
    assert "/Services/ISservice123/Conversations/CH2762070edc" in args[0]


@pytest.mark.asyncio
async def test_invalidate_falls_back_to_default_without_service(client):
    """Sin ChatServiceSid, usa URL Default (legacy compat)."""
    close_resp = MagicMock(status_code=200, text="ok")
    http_mock = MagicMock()
    http_mock.post = AsyncMock(return_value=close_resp)
    client._http_client = http_mock

    mongo_mock = MagicMock()
    mongo_mock.connect = AsyncMock(return_value=True)
    mongo_mock.db.messages.update_many = AsyncMock(return_value=MagicMock(modified_count=0))

    with patch("database.mongodb_client.get_mongo_manager", return_value=mongo_mock):
        await client._invalidate_stale_conv_sid("CHabc")

    args, _ = http_mock.post.call_args
    assert "/Conversations/CHabc" in args[0]
    assert "/Services/" not in args[0]


# ─────────────────────── _send_via_conversations ─────────────────────────

@pytest.mark.asyncio
async def test_send_via_conversations_uses_service_url(client):
    success_resp = MagicMock(status_code=201)
    success_resp.json = MagicMock(return_value={"sid": "IMxxx"})
    http_mock = MagicMock()
    http_mock.post = AsyncMock(return_value=success_resp)
    client._http_client = http_mock

    result = await client._send_via_conversations(
        conversation_sid="CH123",
        body="hola",
        chat_service_sid="ISservice456",
    )

    args, _ = http_mock.post.call_args
    assert "/Services/ISservice456/Conversations/CH123/Messages" in args[0]
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_send_via_conversations_404_invalidates_with_service(client):
    """En 404 dispara invalidate con el chat_service_sid correcto."""
    resp_404 = MagicMock(status_code=404, text="not found")
    http_mock = MagicMock()
    http_mock.post = AsyncMock(return_value=resp_404)
    client._http_client = http_mock

    invalidate_mock = AsyncMock()
    with patch.object(client, "_invalidate_stale_conv_sid", invalidate_mock):
        await client._send_via_conversations(
            conversation_sid="CHexpired",
            body="hola",
            chat_service_sid="ISsvc",
        )
        import asyncio
        await asyncio.sleep(0)

    invalidate_mock.assert_awaited_with("CHexpired", "ISsvc")


# ─────────────────────────── send_whatsapp_message ───────────────────────

@pytest.mark.asyncio
async def test_send_whatsapp_message_uses_resolved_service_sid(client):
    """Lookup de Mongo retorna (sid, service_sid); send_via_conversations debe recibir ambos."""
    success_resp = MagicMock(status_code=201)
    success_resp.json = MagicMock(return_value={"sid": "IMok"})
    http_mock = MagicMock()
    http_mock.post = AsyncMock(return_value=success_resp)
    client._http_client = http_mock

    with patch.object(
        client, "_resolve_conv_sid_for_phone",
        AsyncMock(return_value=("CHfound", "ISfromMongo")),
    ):
        result = await client.send_whatsapp_message(
            to="+573195652633",
            body="hola",
        )

    args, _ = http_mock.post.call_args
    assert "/Services/ISfromMongo/Conversations/CHfound/Messages" in args[0]
    assert result["status"] == "success"
    assert result["chat_service_sid"] == "ISfromMongo"
