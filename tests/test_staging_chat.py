from fastapi import FastAPI
from fastapi.testclient import TestClient


class _DeleteResult:
    deleted_count = 0


class _Collection:
    async def delete_many(self, query):
        return _DeleteResult()


class _Db:
    messages = _Collection()
    conversations = _Collection()


class _Mongo:
    db = _Db()

    def __init__(self):
        self.saved = []

    async def connect(self):
        return True

    async def save_message(self, **kwargs):
        self.saved.append(kwargs)
        return f"msg-{len(self.saved)}"


class _Analysis:
    def to_dict(self):
        return {"handoff_priority": "none", "analysis_failed": False}


class _BrainResult:
    respuesta = "Respuesta de prueba"
    analisis = _Analysis()


class _Brain:
    async def process_message_with_analysis(self, **kwargs):
        self.last_kwargs = kwargs
        return _BrainResult()


def _client():
    from middleware.staging_chat import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_staging_chat_is_not_available_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")

    response = _client().get("/staging/chat")

    assert response.status_code == 404


def test_staging_chat_message_uses_staging_identity_and_persists_only_test_data(monkeypatch):
    import middleware.staging_chat as staging_chat

    mongo = _Mongo()
    brain = _Brain()
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setattr(staging_chat, "get_mongo_manager", lambda: mongo)
    monkeypatch.setattr(staging_chat, "get_staging_sofia_brain", lambda: brain)

    response = _client().post(
        "/staging/chat/message",
        json={
            "message": "Hola, busco apartamento",
            "conversation_id": "usuario_001",
            "user_id": "Juan_rodriguez_18",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["session_id"] == "stg:test:Juan_rodriguez_18:usuario_001"
    assert data["response"] == "Respuesta de prueba"
    assert brain.last_kwargs["session_id"] == data["session_id"]
    assert brain.last_kwargs["lead_context"]["staging_simulator"] is True
    assert [item["sender"] for item in mongo.saved] == ["client", "bot"]
    assert all(item["phone"] == data["session_id"] for item in mongo.saved)
    assert all(item["channel"] == "staging" for item in mongo.saved)
    assert all(item["metadata"]["staging_simulator"] is True for item in mongo.saved)
