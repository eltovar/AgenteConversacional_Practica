import pytest


def test_staging_safety_defaults_block_outbound(monkeypatch):
    from utils import environment

    environment._twilio_allowlist.cache_clear()
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.delenv("TWILIO_OUTBOUND_ENABLED", raising=False)
    monkeypatch.delenv("HUBSPOT_WRITE_ENABLED", raising=False)
    monkeypatch.delenv("SCHEDULER_OUTBOUND_ENABLED", raising=False)
    monkeypatch.delenv("BUNNY_UPLOAD_ENABLED", raising=False)
    monkeypatch.delenv("RAG_INDEX_ON_STARTUP", raising=False)

    assert environment.is_staging() is True
    assert environment.twilio_outbound_enabled() is False
    assert environment.hubspot_writes_enabled() is False
    assert environment.scheduler_outbound_enabled() is False
    assert environment.bunny_uploads_enabled() is False
    assert environment.rag_index_on_startup_enabled() is False


def test_production_safety_defaults_keep_current_behavior(monkeypatch):
    from utils import environment

    environment._twilio_allowlist.cache_clear()
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("TWILIO_OUTBOUND_ENABLED", raising=False)
    monkeypatch.delenv("HUBSPOT_WRITE_ENABLED", raising=False)
    monkeypatch.delenv("SCHEDULER_OUTBOUND_ENABLED", raising=False)
    monkeypatch.delenv("BUNNY_UPLOAD_ENABLED", raising=False)
    monkeypatch.delenv("RAG_INDEX_ON_STARTUP", raising=False)

    assert environment.is_production() is True
    assert environment.twilio_outbound_enabled() is True
    assert environment.hubspot_writes_enabled() is True
    assert environment.scheduler_outbound_enabled() is True
    assert environment.bunny_uploads_enabled() is True
    assert environment.rag_index_on_startup_enabled() is True


async def test_hubspot_write_gate_blocks_before_network(monkeypatch):
    from integrations.hubspot.hubspot_client import HubSpotClient, HubSpotWriteBlockedError

    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.delenv("HUBSPOT_WRITE_ENABLED", raising=False)
    monkeypatch.setenv("HUBSPOT_API_KEY", "test-token")

    client = HubSpotClient()

    async def fail_request(*args, **kwargs):
        raise AssertionError("network request should not be reached")

    monkeypatch.setattr(client, "_request", fail_request)

    with pytest.raises(HubSpotWriteBlockedError):
        await client.create_contact({"firstname": "Test"})

    await client._http_client.aclose()


async def test_bunny_upload_gate_blocks_before_network(monkeypatch):
    from utils.media_processor import MediaProcessor

    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.delenv("BUNNY_UPLOAD_ENABLED", raising=False)

    processor = MediaProcessor()

    def fail_client():
        raise AssertionError("network client should not be reached")

    monkeypatch.setattr(processor, "_get_http_client", fail_client)

    with pytest.raises(RuntimeError, match="BUNNY_UPLOAD_ENABLED=false"):
        await processor.upload_to_bunny(b"data", "folder", "file.txt", "text/plain")


async def test_bunny_upload_applies_staging_prefix_before_put(monkeypatch):
    import utils.media_processor as media_module
    from utils.media_processor import MediaProcessor

    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("BUNNY_UPLOAD_ENABLED", "true")
    monkeypatch.setenv("BUNNY_UPLOAD_PREFIX", "staging")
    monkeypatch.setattr(media_module, "BUNNY_STORAGE_ZONE", "zone")
    monkeypatch.setattr(media_module, "BUNNY_API_KEY", "key")
    monkeypatch.setattr(media_module, "BUNNY_ENDPOINT", "storage.example.com")
    monkeypatch.setattr(media_module, "BUNNY_PULL_ZONE", "https://cdn.example.com")

    seen = {}

    class Response:
        status_code = 200
        text = ""

    class Client:
        async def put(self, url, content, headers, timeout):
            seen["put_url"] = url
            return Response()

        async def head(self, url, timeout, follow_redirects):
            seen["head_url"] = url
            return Response()

    processor = MediaProcessor()
    monkeypatch.setattr(processor, "_get_http_client", lambda: Client())

    url = await processor.upload_to_bunny(b"data", "audios", "file.txt", "text/plain")

    assert seen["put_url"] == "https://storage.example.com/zone/staging/audios/file.txt"
    assert seen["head_url"] == "https://cdn.example.com/staging/audios/file.txt"
    assert url == "https://cdn.example.com/staging/audios/file.txt"
