from urllib.parse import urlencode
from types import SimpleNamespace

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
        self.activities = []
        self.meta_calls = []

    async def update_activity(self, phone, canal="whatsapp", reopen_if_closed=False):
        self.activities.append((phone, canal, reopen_if_closed))
        return True

    async def get_meta(self, phone, canal="whatsapp"):
        return None

    async def ensure_meta_with_channel(self, **kwargs):
        self.meta_calls.append(kwargs)
        return True

    async def track_bot_turn(self, phone, canal, has_signal=False):
        return 1, has_signal

    async def get_status(self, phone, canal="whatsapp"):
        return None

    async def request_handoff(self, *args, **kwargs):
        return True


class _FakeTwilioClient:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"status": "success", "message_sid": "SM_CONTACT_REQUEST"}

    async def send_whatsapp_message(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class _FakeMongoManager:
    def __init__(self):
        self.messages = []

    async def save_message(self, **kwargs):
        self.messages.append(kwargs)
        return f"mongo_{len(self.messages)}"

    async def get_message_by_sid(self, _sid):
        return None

    async def update_conversation_meta(self, **_kwargs):
        return True


class _FakeAggregator:
    def __init__(self):
        self.sessions = []

    async def add_message_to_buffer(self, session_id, message):
        self.sessions.append((session_id, message))
        return {"should_process": True, "is_aggregating": False}

    async def wait_and_get_combined_message(self, _session_id):
        return None


class _FakeSofia:
    def __init__(self):
        self.calls = []

    async def process_message_with_analysis(self, **kwargs):
        self.calls.append(kwargs)
        analysis = SimpleNamespace(
            handoff_priority="none",
            intencion_visita=False,
            emocion="neutral",
            analysis_failed=False,
        )
        return SimpleNamespace(respuesta="Respuesta BSUID", analisis=analysis)


class _FakeWSManager:
    def __init__(self):
        self.notifications = []

    async def notify_new_message(self, **kwargs):
        self.notifications.append(kwargs)

    async def publish_broadcast(self, *_args, **_kwargs):
        return None

    async def publish_to_advisor(self, *_args, **_kwargs):
        return None


async def _bot_responds(**_kwargs):
    return True, "bot_active", None, "whatsapp"


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


def _bsuid_campos(message_sid="SM_BSUID_1", bsuid="whatsapp:CO.899759302823042"):
    return {
        "From": bsuid,
        "ExternalUserId": bsuid,
        "Username": "juan_rodriguez_18",
        "Body": "Hola",
        "MessageSid": message_sid,
        "NumMedia": "0",
    }


def _external_user_id_campos():
    return {
        "From": "",
        "ExternalUserId": "whatsapp:CO.899759302823042",
        "Username": "juan_rodriguez_18",
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
        "ExternalUserId": "whatsapp:US.ABC123XYZ",
        "Username": "cliente_usa",
        "Body": "Hola",
        "MessageSid": "SM_FOREIGN_PHONE",
        "NumMedia": "0",
    }


def _phone_campos():
    return {
        "From": "whatsapp:+573001234567",
        "ExternalUserId": "whatsapp:CO.899759302823042",
        "Username": "cliente_co",
        "WaId": "573001234567",
        "Body": "Hola",
        "MessageSid": "SM_PHONE_1",
        "NumMedia": "0",
    }


async def test_feature_off_bsuid_hace_safe_stop(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    monkeypatch.setenv("FEATURE_WHATSAPP_BSUID_SUPPORT", "false")
    fake_twilio = _FakeTwilioClient()
    monkeypatch.setattr(wh, "twilio_client", fake_twilio)

    tareas = BackgroundTasks()
    response = await wh.whatsapp_webhook(_peticion_legacy(_bsuid_campos()), tareas)

    assert response.status_code == 200
    assert response.body == b""
    assert len(tareas.tasks) == 0
    assert fake_twilio.calls == []


async def test_feature_ausente_bsuid_hace_safe_stop(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    monkeypatch.delenv("FEATURE_WHATSAPP_BSUID_SUPPORT", raising=False)
    fake_twilio = _FakeTwilioClient()
    monkeypatch.setattr(wh, "twilio_client", fake_twilio)

    tareas = BackgroundTasks()
    response = await wh.whatsapp_webhook(_peticion_legacy(_bsuid_campos()), tareas)

    assert response.status_code == 200
    assert response.body == b""
    assert len(tareas.tasks) == 0
    assert fake_twilio.calls == []


async def test_bsuid_con_flag_on_encola_flujo_sin_telefono(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    monkeypatch.setenv("FEATURE_WHATSAPP_BSUID_SUPPORT", "true")
    fake_twilio = _FakeTwilioClient()
    monkeypatch.setattr(wh, "twilio_client", fake_twilio)

    tareas = BackgroundTasks()
    response = await wh.whatsapp_webhook(_peticion_legacy(_bsuid_campos()), tareas)

    assert response.status_code == 200
    assert response.body == b""
    assert len(tareas.tasks) == 1
    task = tareas.tasks[0]
    assert task.func.__name__ == "_process_message_deferred"
    assert task.args[0].startswith("bsuid_")
    assert ":" not in task.args[0]
    assert task.args[1] == "whatsapp:CO.899759302823042"
    assert task.kwargs["identity_type"] == "bsuid"
    assert task.kwargs["identity_key"] == task.args[0]
    assert task.kwargs["routing_address"] == "whatsapp:CO.899759302823042"
    assert task.kwargs["username"] == "juan_rodriguez_18"
    assert fake_twilio.calls == []


async def test_bsuid_alfanumerico_otro_pais_se_preserva(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    monkeypatch.setenv("FEATURE_WHATSAPP_BSUID_SUPPORT", "true")
    fake_twilio = _FakeTwilioClient()
    monkeypatch.setattr(wh, "twilio_client", fake_twilio)

    bsuid = "whatsapp:US.ABC123XYZ"
    tareas = BackgroundTasks()
    response = await wh.whatsapp_webhook(
        _peticion_legacy(_bsuid_campos(bsuid=bsuid)), tareas
    )

    assert response.status_code == 200
    assert len(tareas.tasks) == 1
    assert tareas.tasks[0].args[1] == bsuid
    assert tareas.tasks[0].kwargs["routing_address"] == bsuid
    assert fake_twilio.calls == []


async def test_bsuid_puede_venir_en_external_user_id(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    monkeypatch.setenv("FEATURE_WHATSAPP_BSUID_SUPPORT", "true")
    fake_twilio = _FakeTwilioClient()
    monkeypatch.setattr(wh, "twilio_client", fake_twilio)

    tareas = BackgroundTasks()
    response = await wh.whatsapp_webhook(_peticion_legacy(_external_user_id_campos()), tareas)

    assert response.status_code == 200
    assert response.body == b""
    assert len(tareas.tasks) == 1
    assert tareas.tasks[0].args[1] == "whatsapp:CO.899759302823042"
    assert tareas.tasks[0].kwargs["routing_address"] == "whatsapp:CO.899759302823042"
    assert fake_twilio.calls == []


async def test_bsuid_con_telefono_explicito_pasa_a_flujo_normal(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    monkeypatch.setenv("FEATURE_WHATSAPP_BSUID_SUPPORT", "true")
    fake_twilio = _FakeTwilioClient()
    monkeypatch.setattr(wh, "twilio_client", fake_twilio)

    campos = _bsuid_campos()
    campos["Body"] = "Mi número es 3001234567"
    tareas = BackgroundTasks()
    response = await wh.whatsapp_webhook(_peticion_legacy(campos), tareas)

    assert response.status_code == 200
    assert response.body == b""
    assert len(tareas.tasks) == 1
    task = tareas.tasks[0]
    assert task.func.__name__ == "_process_message_deferred"
    assert task.args[0] == "+573001234567"
    assert task.args[1] == "+573001234567"
    assert task.kwargs == {}
    assert fake_twilio.calls == []


async def test_username_no_se_usa_como_routing(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    monkeypatch.setenv("FEATURE_WHATSAPP_BSUID_SUPPORT", "true")
    monkeypatch.setenv("TWILIO_WHATSAPP_REQUEST_CONTACT_CONTENT_SID", "HX_CONTACT_REQUEST")
    fake_twilio = _FakeTwilioClient()
    monkeypatch.setattr(wh, "twilio_client", fake_twilio)

    tareas = BackgroundTasks()
    response = await wh.whatsapp_webhook(_peticion_legacy(_username_campos()), tareas)

    assert response.status_code == 200
    assert response.body == b""
    assert len(tareas.tasks) == 0
    assert fake_twilio.calls == []


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
    assert len(tareas.tasks) == 1
    assert fake_twilio.calls == []


async def test_bsuid_flujo_diferido_usa_identity_key_y_routing_address(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    fake_twilio = _FakeTwilioClient()
    fake_redis = _FakeRedis()
    fake_state = _FakeStateManager(fake_redis)
    fake_mongo = _FakeMongoManager()
    fake_sofia = _FakeSofia()
    fake_ws = _FakeWSManager()

    def _no_contact_manager():
        raise AssertionError("ContactManager no debe ejecutarse para BSUID")

    monkeypatch.setattr(wh, "twilio_client", fake_twilio)
    monkeypatch.setattr(wh, "get_state_manager", lambda: fake_state)
    monkeypatch.setattr(wh, "get_mongo_manager", lambda: fake_mongo)
    monkeypatch.setattr(wh, "get_sofia_brain", lambda: fake_sofia)
    monkeypatch.setattr(wh, "get_contact_manager", _no_contact_manager)
    monkeypatch.setattr(wh, "message_aggregator", _FakeAggregator())
    monkeypatch.setattr(wh, "ws_manager", fake_ws)
    monkeypatch.setattr(
        wh,
        "detect_property_code",
        lambda _body: SimpleNamespace(has_code=False, code=None, context=None),
    )
    monkeypatch.setattr(
        wh,
        "get_link_detector",
        lambda: SimpleNamespace(
            analizar_mensaje=lambda _body: SimpleNamespace(
                tiene_link=False,
                portal=None,
                url_original=None,
                es_inmueble=False,
            )
        ),
    )
    monkeypatch.setattr(wh, "should_bot_respond", _bot_responds)

    identity_key = wh.make_bsuid_identity_key("whatsapp:CO.899759302823042")
    await wh._process_message_deferred(
        phone_normalized=identity_key,
        phone_raw="whatsapp:CO.899759302823042",
        body="Hola",
        profile_name="Cliente Usuario",
        message_sid="SM_BSUID_FLOW",
        num_media=0,
        media_url=None,
        media_content_type=None,
        early_channel="whatsapp",
        incoming_channel="whatsapp",
        identity_type="bsuid",
        identity_key=identity_key,
        routing_address="whatsapp:CO.899759302823042",
        username="juan_rodriguez_18",
        external_user_id="whatsapp:CO.899759302823042",
    )

    assert fake_sofia.calls[0]["session_id"] == identity_key
    assert fake_mongo.messages[0]["phone"] == identity_key
    assert fake_mongo.messages[0]["metadata"]["identity_type"] == "bsuid"
    assert fake_mongo.messages[0]["metadata"]["routing_address"] == "whatsapp:CO.899759302823042"
    assert fake_twilio.calls[0]["to"] == "whatsapp:CO.899759302823042"
    assert fake_state.activities == [(identity_key, "whatsapp", True)]
    assert fake_state.meta_calls[0]["phone"] == identity_key
    assert fake_state.meta_calls[0]["display_name"] == "Cliente Usuario"
    assert fake_state.meta_calls[0]["identity_type"] == "bsuid"
    assert fake_state.meta_calls[0]["has_phone"] is False
    assert fake_state.meta_calls[0]["routing_address"] == "whatsapp:CO.899759302823042"
    assert fake_state.meta_calls[0]["username"] == "juan_rodriguez_18"


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
