from urllib.parse import urlencode

from fastapi import BackgroundTasks
from starlette.requests import Request

from middleware import webhook_handler as wh


class _FakeRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, key):
        self.values.pop(key, None)
        return 1


class _FakeStateManager:
    def __init__(self, redis):
        self.redis = redis


class _FakeTwilioClient:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"status": "success", "message_sid": "SM_CONTACT_REQUEST"}

    async def send_whatsapp_message(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _peticion_legacy(campos: dict) -> Request:
    cuerpo = urlencode(campos).encode()

    async def receive():
        return {"type": "http.request", "body": cuerpo, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/whatsapp/webhook",
        "query_string": b"",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "scheme": "https",
        "server": ("ejemplo.railway.app", 443),
    }
    return Request(scope, receive)


def _bsuid_campos(message_sid="SM_BSUID_1"):
    return {
        "From": "whatsapp:CO.899759302823042",
        "Body": "Hola",
        "MessageSid": message_sid,
        "NumMedia": "0",
    }


def _external_user_id_campos():
    return {
        "From": "",
        "ExternalUserId": "whatsapp:CO.899759302823042",
        "Body": "Hola",
        "MessageSid": "SM_BSUID_EXTERNAL",
        "NumMedia": "0",
    }


def _username_campos():
    return {
        "From": "whatsapp:cliente.usuario",
        "Username": "cliente.usuario",
        "Body": "Hola",
        "MessageSid": "SM_USERNAME",
        "NumMedia": "0",
    }


def _foreign_phone_campos():
    return {
        "From": "whatsapp:+14155552671",
        "Body": "Hola",
        "MessageSid": "SM_FOREIGN_PHONE",
        "NumMedia": "0",
    }


def _phone_campos():
    return {
        "From": "whatsapp:+573001234567",
        "Body": "Hola",
        "MessageSid": "SM_PHONE_1",
        "NumMedia": "0",
    }


async def test_feature_off_mantiene_comportamiento_actual(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    monkeypatch.setenv("FEATURE_WHATSAPP_BSUID_SUPPORT", "false")
    fake_twilio = _FakeTwilioClient()
    monkeypatch.setattr(wh, "twilio_client", fake_twilio)

    tareas = BackgroundTasks()
    response = await wh.whatsapp_webhook(_peticion_legacy(_bsuid_campos()), tareas)

    assert response.status_code == 200
    assert len(tareas.tasks) == 0
    assert fake_twilio.calls == []
    assert b"Lo siento" in response.body


async def test_bsuid_con_flag_on_solicita_contacto_y_no_encola(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    monkeypatch.setenv("FEATURE_WHATSAPP_BSUID_SUPPORT", "true")
    monkeypatch.setenv("TWILIO_WHATSAPP_REQUEST_CONTACT_CONTENT_SID", "HX_CONTACT_REQUEST")
    fake_twilio = _FakeTwilioClient()
    fake_redis = _FakeRedis()
    monkeypatch.setattr(wh, "twilio_client", fake_twilio)
    monkeypatch.setattr(wh, "get_state_manager", lambda: _FakeStateManager(fake_redis))

    tareas = BackgroundTasks()
    response = await wh.whatsapp_webhook(_peticion_legacy(_bsuid_campos()), tareas)

    assert response.status_code == 200
    assert response.body == b""
    assert len(tareas.tasks) == 0
    assert fake_twilio.calls == [
        {
            "to": "whatsapp:CO.899759302823042",
            "body": "",
            "content_sid": "HX_CONTACT_REQUEST",
            "conversation_sid": None,
            "chat_service_sid": None,
        }
    ]


async def test_bsuid_puede_venir_en_external_user_id(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    monkeypatch.setenv("FEATURE_WHATSAPP_BSUID_SUPPORT", "true")
    monkeypatch.setenv("TWILIO_WHATSAPP_REQUEST_CONTACT_CONTENT_SID", "HX_CONTACT_REQUEST")
    fake_twilio = _FakeTwilioClient()
    fake_redis = _FakeRedis()
    monkeypatch.setattr(wh, "twilio_client", fake_twilio)
    monkeypatch.setattr(wh, "get_state_manager", lambda: _FakeStateManager(fake_redis))

    tareas = BackgroundTasks()
    response = await wh.whatsapp_webhook(_peticion_legacy(_external_user_id_campos()), tareas)

    assert response.status_code == 200
    assert response.body == b""
    assert len(tareas.tasks) == 0
    assert fake_twilio.calls[0]["to"] == "whatsapp:CO.899759302823042"


async def test_username_solicita_contacto_y_no_encola(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    monkeypatch.setenv("FEATURE_WHATSAPP_BSUID_SUPPORT", "true")
    monkeypatch.setenv("TWILIO_WHATSAPP_REQUEST_CONTACT_CONTENT_SID", "HX_CONTACT_REQUEST")
    fake_twilio = _FakeTwilioClient()
    fake_redis = _FakeRedis()
    monkeypatch.setattr(wh, "twilio_client", fake_twilio)
    monkeypatch.setattr(wh, "get_state_manager", lambda: _FakeStateManager(fake_redis))

    tareas = BackgroundTasks()
    response = await wh.whatsapp_webhook(_peticion_legacy(_username_campos()), tareas)

    assert response.status_code == 200
    assert response.body == b""
    assert len(tareas.tasks) == 0
    assert fake_twilio.calls[0]["to"] == "whatsapp:cliente.usuario"


async def test_bsuid_sin_content_sid_no_contamina_ni_encola(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    monkeypatch.setenv("FEATURE_WHATSAPP_BSUID_SUPPORT", "true")
    monkeypatch.delenv("TWILIO_WHATSAPP_REQUEST_CONTACT_CONTENT_SID", raising=False)
    fake_twilio = _FakeTwilioClient()
    monkeypatch.setattr(wh, "twilio_client", fake_twilio)

    tareas = BackgroundTasks()
    response = await wh.whatsapp_webhook(_peticion_legacy(_bsuid_campos()), tareas)

    assert response.status_code == 200
    assert response.body == b""
    assert len(tareas.tasks) == 0
    assert fake_twilio.calls == []


async def test_bsuid_contact_request_es_idempotente_por_message_sid(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    monkeypatch.setenv("FEATURE_WHATSAPP_BSUID_SUPPORT", "true")
    monkeypatch.setenv("TWILIO_WHATSAPP_REQUEST_CONTACT_CONTENT_SID", "HX_CONTACT_REQUEST")
    fake_twilio = _FakeTwilioClient()
    fake_redis = _FakeRedis()
    monkeypatch.setattr(wh, "twilio_client", fake_twilio)
    monkeypatch.setattr(wh, "get_state_manager", lambda: _FakeStateManager(fake_redis))

    await wh.whatsapp_webhook(_peticion_legacy(_bsuid_campos("SM_DUPLICATE")), BackgroundTasks())
    await wh.whatsapp_webhook(_peticion_legacy(_bsuid_campos("SM_DUPLICATE")), BackgroundTasks())

    assert len(fake_twilio.calls) == 1


async def test_telefono_tradicional_sigue_encolando(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    monkeypatch.setenv("FEATURE_WHATSAPP_BSUID_SUPPORT", "true")
    fake_twilio = _FakeTwilioClient()
    monkeypatch.setattr(wh, "twilio_client", fake_twilio)

    tareas = BackgroundTasks()
    response = await wh.whatsapp_webhook(_peticion_legacy(_phone_campos()), tareas)

    assert response.status_code == 200
    assert response.body == b""
    assert "_process_message_deferred" in [task.func.__name__ for task in tareas.tasks]
    assert fake_twilio.calls == []


async def test_telefono_extranjero_sigue_encolando(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    monkeypatch.setenv("FEATURE_WHATSAPP_BSUID_SUPPORT", "true")
    fake_twilio = _FakeTwilioClient()
    monkeypatch.setattr(wh, "twilio_client", fake_twilio)

    tareas = BackgroundTasks()
    response = await wh.whatsapp_webhook(_peticion_legacy(_foreign_phone_campos()), tareas)

    assert response.status_code == 200
    assert response.body == b""
    assert "_process_message_deferred" in [task.func.__name__ for task in tareas.tasks]
    assert fake_twilio.calls == []
