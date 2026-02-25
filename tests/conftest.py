"""
Fixtures compartidos para toda la suite de tests.

Este archivo contiene fixtures reutilizables que simulan:
- Redis (fakeredis)
- HubSpot API (httpx mock)
- FastAPI client
- Datos de prueba consistentes
"""

import os
import pytest
import pytest_asyncio
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from typing import AsyncGenerator, Generator, Any

import fakeredis
import fakeredis.aioredis
import httpx
from fastapi.testclient import TestClient
import freezegun

# Configurar variables de entorno para tests
os.environ["HUBSPOT_API_KEY"] = "test-hubspot-key-12345"
os.environ["TWILIO_ACCOUNT_SID"] = "test-twilio-sid"
os.environ["TWILIO_AUTH_TOKEN"] = "test-twilio-token"
os.environ["TWILIO_WHATSAPP_NUMBER"] = "+15005550006"
os.environ["ADMIN_API_KEY"] = "test-admin-key"
os.environ["OPENAI_API_KEY"] = "sk-test-key"
os.environ["REDIS_URL"] = "redis://localhost:6379"


# ============================================================================
# CONFIGURACIÓN DE PYTEST - CUSTOM MARKERS
# ============================================================================

def pytest_configure(config):
    """Registra custom markers para evitar PytestUnknownMarkWarning.Ca"""
    config.addinivalue_line("markers", "unit: marca test unitarios")
    config.addinivalue_line("markers", "cache: marca tests relacionados a caché")
    config.addinivalue_line("markers", "slow: marca tests lentos")
    config.addinivalue_line("markers", "integration: marca tests de integración")
    config.addinivalue_line("markers", "e2e: marca tests end-to-end")


# ============================================================================
# FIXTURES DE TIEMPO
# ============================================================================

@pytest.fixture
def frozen_time():
    """Congela el tiempo en un momento específico."""
    with freezegun.freeze_time("2026-02-24 16:00:00"):
        yield


@pytest.fixture
def current_timestamp():
    """Timestamp actual para tests."""
    return datetime.now(timezone.utc)


# ============================================================================
# FIXTURES DE REDIS
# ============================================================================

@pytest_asyncio.fixture
async def fake_redis() -> AsyncGenerator:
    """
    Redis in-memory para tests (fakeredis async).

    Ventajas:
    - No requiere Redis real
    - Aislado entre tests
    - Rápido
    - Determinístico
    - ASYNC: Usa aioredis de fakeredis
    """
    # Usar FakeRedis async de fakeredis.aioredis
    redis_client = fakeredis.aioredis.FakeRedis(
        decode_responses=True,
        encoding="utf-8"
    )

    yield redis_client

    # Cleanup: flush all keys
    await redis_client.flushall()
    await redis_client.close()


@pytest_asyncio.fixture
async def redis_with_state(fake_redis):
    """
    Redis pre-poblado con estados de conversación de prueba.
    """
    # Estado BOT_ACTIVE
    await fake_redis.set(
        "conv_state:+573001234567:whatsapp",
        "BOT_ACTIVE",
        ex=86400
    )

    # Estado HUMAN_ACTIVE
    await fake_redis.set(
        "conv_state:+573009876543:instagram",
        "HUMAN_ACTIVE",
        ex=86400
    )

    # Metadata
    await fake_redis.set(
        "conv_meta:+573009876543:instagram",
        '{"owner_id": "88251457", "contact_id": "196843060059", "activated_at": "2026-02-24T16:00:00Z"}',
        ex=86400
    )

    # Índice de conversaciones activas
    await fake_redis.sadd(
        "active_conversations_index",
        "+573009876543:instagram"
    )

    yield fake_redis


# ============================================================================
# FIXTURES DE HUBSPOT
# ============================================================================

@pytest.fixture
def mock_hubspot_contact():
    """Contacto de prueba de HubSpot."""
    return {
        "id": "196843060059",
        "properties": {
            "firstname": "Juan",
            "lastname": "Pérez",
            "phone": "+573001234567",
            "whatsapp_id": "+573001234567",
            "email": "juan.perez@example.com",
            "sofia_activa": "true",
            "conversation_status": "BOT_ACTIVE"
        },
        "createdAt": "2026-02-20T10:00:00Z",
        "updatedAt": "2026-02-24T15:30:00Z"
    }


@pytest.fixture
def mock_hubspot_note():
    """Nota de prueba de HubSpot."""
    return {
        "id": "note123456",
        "properties": {
            "hs_note_body": "🤖 [Sofía - IA] ➡️\n\nHola, ¿en qué puedo ayudarte?\n\n---\n📅 2026-02-24 16:00:00",
            "hs_timestamp": "2026-02-24T16:00:00Z"
        },
        "createdAt": "2026-02-24T16:00:00Z"
    }


@pytest.fixture
def mock_httpx_client(mock_hubspot_contact: dict[str, Any], mock_hubspot_note: dict[str, Any]):
    """
    Mock de httpx.AsyncClient para simular HubSpot API.

    Responde a:
    - GET /crm/v3/objects/contacts/{id}
    - POST /crm/v3/objects/notes
    - GET /crm/v4/objects/contacts/{id}/associations/notes
    - POST /crm/v3/objects/notes/batch/read
    """
    mock_client = AsyncMock(spec=httpx.AsyncClient)

    # Mock context manager
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    def mock_request(method, url, **kwargs):
        """Router de requests simuladas (SÍNCRONO)."""
        response = MagicMock(spec=httpx.Response)

        # GET contact
        if "/contacts/" in url and method == "GET":
            response.status_code = 200
            response.json.return_value = mock_hubspot_contact

        # POST note (create)
        elif "/notes" in url and method == "POST" and "batch" not in url:
            response.status_code = 201
            response.json.return_value = {
                "id": "note123456",
                **kwargs.get("json", {})
            }

        # GET associations
        elif "/associations/notes" in url and method == "GET":
            response.status_code = 200
            response.json.return_value = {
                "results": [
                    {"toObjectId": "note123456"}
                ]
            }

        # POST batch read
        elif "/batch/read" in url and method == "POST":
            response.status_code = 200
            response.json.return_value = {
                "results": [mock_hubspot_note]
            }

        # PATCH contact/note
        elif method == "PATCH":
            response.status_code = 200
            response.json.return_value = mock_hubspot_contact

        else:
            response.status_code = 404
            response.text = "Not Found"

        return response

    # Usar AsyncMock que retorna correctamente la respuesta
    mock_client.get.side_effect = lambda url, **kwargs: mock_request("GET", url, **kwargs)
    mock_client.post.side_effect = lambda url, **kwargs: mock_request("POST", url, **kwargs)
    mock_client.patch.side_effect = lambda url, **kwargs: mock_request("PATCH", url, **kwargs)

    return mock_client


# ============================================================================
# FIXTURES DE FASTAPI
# ============================================================================

@pytest.fixture
def api_client():
    """
    Cliente de prueba para FastAPI.

    NOTA: Importar app lazy para evitar side effects.
    """
    from app import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def api_headers():
    """Headers de autenticación para API."""
    return {
        "X-API-Key": "test-admin-key",
        "Content-Type": "application/json"
    }


# ============================================================================
# FIXTURES DE DATOS DE PRUEBA
# ============================================================================

@pytest.fixture
def sample_phones():
    """Números de teléfono de prueba."""
    return {
        "normalized": "+573001234567",
        "raw": "3001234567",
        "with_country": "573001234567",
        "twilio_format": "whatsapp:+573001234567",
        "invalid": "123",
        "another": "+573009876543"
    }


@pytest.fixture
def sample_message_bodies():
    """
    Cuerpos de mensajes de prueba para detección de sender.
    """
    return {
        "client": "📱 [Cliente - WhatsApp] ⬅️\n\nHola, necesito información\n\n---\n📅 2026-02-24 16:00:00",
        "bot": "🤖 [Sofía - IA] ➡️\n\n¡Hola! ¿En qué puedo ayudarte?\n\n---\n📅 2026-02-24 16:00:00",
        "advisor": "👤 [Asesor] ➡️\n\nClaro, te ayudo con eso\n\n---\n📅 2026-02-24 16:00:00",
        "template": "[TEMPLATE: reactivacion_general] Hola, ¿sigues interesado?",
        "system": "[Sistema] Handoff activado",
        "manual_note": "Esta es una nota manual sin emoji",
        "ambiguous": "Mensaje sin indicadores claros",
        "empty": "",
        "none": None
    }


@pytest.fixture
def sample_canales():
    """Canales de prueba."""
    return [
        "whatsapp",
        "whatsapp_directo",
        "instagram",
        "facebook",
        "ciencuadras",
        "finca_raiz",
        "metrocuadrado",
        "pagina_web"
    ]


# ============================================================================
# FIXTURES DE MOCKING
# ============================================================================

@pytest.fixture
def mock_timeline_logger(mock_httpx_client: AsyncMock):
    """
    Mock del TimelineLogger con todas las dependencias.
    """
    with patch("integrations.hubspot.timeline_logger.httpx.AsyncClient", return_value=mock_httpx_client):
        from integrations.hubspot.timeline_logger import TimelineLogger

        logger = TimelineLogger()

        # Mock Redis connection
        logger._redis = AsyncMock(spec=fakeredis.FakeRedis)

        yield logger


@pytest.fixture
def mock_conversation_state_manager(fake_redis: AsyncGenerator[Any, None]):
    """
    Mock del ConversationStateManager con Redis fake.
    """
    from middleware.conversation_state import ConversationStateManager

    with patch("middleware.conversation_state.aioredis.from_url", return_value=fake_redis):
        manager = ConversationStateManager("redis://fake")
        manager.redis = fake_redis

        yield manager


# ============================================================================
# FIXTURES DE UTILIDADES
# ============================================================================

@pytest.fixture
def event_loop():
    """
    Event loop para tests async.

    Necesario para pytest-asyncio.
    """
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def caplog_debug(caplog: pytest.LogCaptureFixture):
    """Captura logs en nivel DEBUG."""
    import logging
    caplog.set_level(logging.DEBUG)
    return caplog


# ============================================================================
# FIXTURES DE PARAMETRIZACIÓN
# ============================================================================

@pytest.fixture(params=["whatsapp", "instagram", "facebook"])
def any_canal(request: pytest.FixtureRequest):
    """Parametriza tests para diferentes canales."""
    return request.param


@pytest.fixture(params=[None, "BOT_ACTIVE", "HUMAN_ACTIVE", "PENDING_HANDOFF", "IN_CONVERSATION"])
def any_conversation_status(request: pytest.FixtureRequest):
    """Parametriza tests para diferentes estados."""
    return request.param