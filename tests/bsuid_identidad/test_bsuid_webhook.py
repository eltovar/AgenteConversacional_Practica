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
        self.phone_asked_calls = []

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

    async def mark_phone_asked(self, phone, canal="whatsapp"):
        self.phone_asked_calls.append((phone, canal))
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
    def __init__(self, telefono_detectado=None):
        self.calls = []
        self.telefono_detectado = telefono_detectado

    async def process_message_with_analysis(self, **kwargs):
        self.calls.append(kwargs)
        analysis = SimpleNamespace(
            handoff_priority="none",
            intencion_visita=False,
            emocion="neutral",
            analysis_failed=False,
            telefono_detectado=self.telefono_detectado,
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


async def test_bsuid_que_escribe_su_telefono_no_parte_la_conversacion(monkeypatch):
    """
    Antes, un BSUID que escribiera su número hacía que el webhook reencolara todo
    bajo la clave del teléfono, sin metadata de identidad. Eso partía el historial
    en dos, hacía ping-pong entre claves en cuanto el siguiente mensaje ya no
    traía el número, y —lo más grave— cambiaba el destino de Twilio a
    whatsapp:+57…, que es justo el direccionamiento que la rama BSUID evita.

    Ahora el mensaje entra por su clave de siempre y es Sofía quien, al final del
    turno, dispara la migración con el teléfono ya validado.
    """
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    monkeypatch.setenv("FEATURE_WHATSAPP_BSUID_SUPPORT", "true")
    fake_twilio = _FakeTwilioClient()
    monkeypatch.setattr(wh, "twilio_client", fake_twilio)

    campos = _bsuid_campos()
    campos["Body"] = "Mi número es 3001234567"
    tareas = BackgroundTasks()
    response = await wh.whatsapp_webhook(_peticion_legacy(campos), tareas)

    assert response.status_code == 200
    assert len(tareas.tasks) == 1
    task = tareas.tasks[0]
    assert task.args[0].startswith("bsuid_"), "el mensaje no puede cambiar de clave a mitad del turno"
    assert task.args[1] == "whatsapp:CO.899759302823042"
    assert task.kwargs["identity_type"] == "bsuid"
    assert task.kwargs["routing_address"] == "whatsapp:CO.899759302823042"
    assert fake_twilio.calls == []


async def test_un_bsuid_ya_migrado_entra_por_su_telefono(monkeypatch):
    """
    El alias es lo que hace que la migración persista: el cliente sigue
    escribiendo desde el mismo BSUID y sus mensajes ya no traen el número.
    Sin esta consulta se recrearía la conversación vieja en cada mensaje.
    """
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    monkeypatch.setenv("FEATURE_WHATSAPP_BSUID_SUPPORT", "true")
    fake_twilio = _FakeTwilioClient()
    fake_state = _FakeStateManager(_FakeRedis())
    monkeypatch.setattr(wh, "twilio_client", fake_twilio)
    monkeypatch.setattr(wh, "get_state_manager", lambda: fake_state)

    identity_key = wh.make_bsuid_identity_key("whatsapp:CO.899759302823042")
    fake_state.redis.values[f"bsuid_alias:{identity_key}"] = "+573001234567"

    tareas = BackgroundTasks()
    response = await wh.whatsapp_webhook(_peticion_legacy(_bsuid_campos()), tareas)

    assert response.status_code == 200
    task = tareas.tasks[0]
    # Se indexa por teléfono...
    assert task.args[0] == "+573001234567"
    assert task.kwargs["identity_key"] == "+573001234567"
    # ...pero el envío sigue yendo al BSUID. Migramos el índice, no el canal.
    assert task.args[1] == "whatsapp:CO.899759302823042"
    assert task.kwargs["routing_address"] == "whatsapp:CO.899759302823042"
    assert task.kwargs["identity_type"] == "bsuid"


async def test_un_alias_ilegible_no_tumba_el_mensaje(monkeypatch):
    """Si Redis no responde, el cliente tiene que seguir siendo atendido."""
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    monkeypatch.setenv("FEATURE_WHATSAPP_BSUID_SUPPORT", "true")

    class _RedisCaido:
        async def get(self, _key):
            raise RuntimeError("Redis caido")

    fake_state = _FakeStateManager(_RedisCaido())
    monkeypatch.setattr(wh, "twilio_client", _FakeTwilioClient())
    monkeypatch.setattr(wh, "get_state_manager", lambda: fake_state)

    tareas = BackgroundTasks()
    response = await wh.whatsapp_webhook(_peticion_legacy(_bsuid_campos()), tareas)

    assert response.status_code == 200
    assert len(tareas.tasks) == 1
    assert tareas.tasks[0].args[0].startswith("bsuid_")


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


# ═══════════════════════════════════════════════════════════════════════════════
# CAPTURA DEL TELÉFONO EN EL FLUJO DIFERIDO
# ═══════════════════════════════════════════════════════════════════════════════


async def _sin_contacto_hubspot(**_kwargs):
    return None


def _cablear_flujo_diferido(monkeypatch, sofia, state, mongo=None):
    """Deja `_process_message_deferred` corriendo contra dobles."""
    monkeypatch.setattr(wh, "get_state_manager", lambda: state)
    monkeypatch.setattr(wh, "get_mongo_manager", lambda: (mongo or _FakeMongoManager()))
    monkeypatch.setattr(wh, "get_sofia_brain", lambda: sofia)
    monkeypatch.setattr(wh, "message_aggregator", _FakeAggregator())
    monkeypatch.setattr(wh, "ws_manager", _FakeWSManager())
    monkeypatch.setattr(
        wh, "detect_property_code",
        lambda _b: SimpleNamespace(has_code=False, code=None, context=None),
    )
    monkeypatch.setattr(
        wh, "get_link_detector",
        lambda: SimpleNamespace(
            analizar_mensaje=lambda _b: SimpleNamespace(
                tiene_link=False, portal=None, url_original=None, es_inmueble=False
            )
        ),
    )
    monkeypatch.setattr(wh, "should_bot_respond", _bot_responds)
    monkeypatch.setattr(wh, "_identify_or_create_bsuid_contact", _sin_contacto_hubspot)


async def test_sofia_recibe_el_aviso_de_que_falta_el_telefono(monkeypatch):
    """El contexto es lo único que hace que Sofía lo pida; sin él, no lo menciona."""
    monkeypatch.setenv("FEATURE_BSUID_PHONE_CAPTURE", "true")
    sofia = _FakeSofia()
    state = _FakeStateManager(_FakeRedis())
    monkeypatch.setattr(wh, "twilio_client", _FakeTwilioClient())
    _cablear_flujo_diferido(monkeypatch, sofia, state)

    identity_key = wh.make_bsuid_identity_key("whatsapp:CO.899759302823042")
    await wh._process_message_deferred(
        phone_normalized=identity_key,
        phone_raw="whatsapp:CO.899759302823042",
        body="Hola",
        profile_name="Cliente Usuario",
        message_sid="SM_NEEDS_PHONE",
        num_media=0, media_url=None, media_content_type=None,
        early_channel="whatsapp", incoming_channel="whatsapp",
        identity_type="bsuid", identity_key=identity_key,
        routing_address="whatsapp:CO.899759302823042",
    )

    contexto = sofia.calls[0]["lead_context"]
    assert contexto["needs_phone"] is True
    assert contexto["phone_already_asked"] is False
    # Pedido una vez: queda marcado para no repetirlo el turno siguiente.
    assert state.phone_asked_calls == [(identity_key, "whatsapp")]


async def test_con_la_captura_apagada_sofia_no_pide_nada(monkeypatch):
    """El flag existe para poder apagar esto en producción sin un deploy."""
    monkeypatch.setenv("FEATURE_BSUID_PHONE_CAPTURE", "false")
    sofia = _FakeSofia()
    state = _FakeStateManager(_FakeRedis())
    monkeypatch.setattr(wh, "twilio_client", _FakeTwilioClient())
    _cablear_flujo_diferido(monkeypatch, sofia, state)

    identity_key = wh.make_bsuid_identity_key("whatsapp:CO.899759302823042")
    await wh._process_message_deferred(
        phone_normalized=identity_key,
        phone_raw="whatsapp:CO.899759302823042",
        body="Hola", profile_name=None, message_sid="SM_FLAG_OFF",
        num_media=0, media_url=None, media_content_type=None,
        early_channel="whatsapp", incoming_channel="whatsapp",
        identity_type="bsuid", identity_key=identity_key,
        routing_address="whatsapp:CO.899759302823042",
    )

    assert "needs_phone" not in sofia.calls[0]["lead_context"]
    assert state.phone_asked_calls == []


async def test_el_telefono_detectado_dispara_la_migracion(monkeypatch):
    monkeypatch.setenv("FEATURE_BSUID_PHONE_CAPTURE", "true")
    sofia = _FakeSofia(telefono_detectado="3001234567")
    state = _FakeStateManager(_FakeRedis())
    monkeypatch.setattr(wh, "twilio_client", _FakeTwilioClient())
    _cablear_flujo_diferido(monkeypatch, sofia, state)

    migraciones = []

    async def _migrar(identity_key, phone_raw, canal="whatsapp", *, source="sofia"):
        migraciones.append((identity_key, phone_raw, canal, source))
        return SimpleNamespace(ok=True, outcome="migrated", contact_id="123456")

    monkeypatch.setattr(wh, "migrate_identity_to_phone", _migrar)

    identity_key = wh.make_bsuid_identity_key("whatsapp:CO.899759302823042")
    await wh._process_message_deferred(
        phone_normalized=identity_key,
        phone_raw="whatsapp:CO.899759302823042",
        body="Mi numero es 3001234567", profile_name=None,
        message_sid="SM_CAPTURE", num_media=0, media_url=None,
        media_content_type=None, early_channel="whatsapp",
        incoming_channel="whatsapp", identity_type="bsuid",
        identity_key=identity_key, routing_address="whatsapp:CO.899759302823042",
    )

    assert migraciones == [(identity_key, "+573001234567", "whatsapp", "sofia")]


async def test_un_telefono_alucinado_no_migra_pero_corta_la_insistencia(monkeypatch):
    """
    Si el LLM propone un presupuesto, no se migra nada. Pero tampoco se le vuelve
    a pedir cada turno: se marca como preguntado y la asesora lo corregirá.
    """
    monkeypatch.setenv("FEATURE_BSUID_PHONE_CAPTURE", "true")
    sofia = _FakeSofia(telefono_detectado="300 millones")
    state = _FakeStateManager(_FakeRedis())
    monkeypatch.setattr(wh, "twilio_client", _FakeTwilioClient())
    _cablear_flujo_diferido(monkeypatch, sofia, state)

    migraciones = []

    async def _migrar(*a, **kw):
        migraciones.append(a)
        return SimpleNamespace(ok=True, outcome="migrated", contact_id=None)

    monkeypatch.setattr(wh, "migrate_identity_to_phone", _migrar)

    identity_key = wh.make_bsuid_identity_key("whatsapp:CO.899759302823042")
    await wh._process_message_deferred(
        phone_normalized=identity_key,
        phone_raw="whatsapp:CO.899759302823042",
        body="Tengo 300 millones", profile_name=None,
        message_sid="SM_FALSO_POSITIVO", num_media=0, media_url=None,
        media_content_type=None, early_channel="whatsapp",
        incoming_channel="whatsapp", identity_type="bsuid",
        identity_key=identity_key, routing_address="whatsapp:CO.899759302823042",
    )

    assert migraciones == []
    assert state.phone_asked_calls == [(identity_key, "whatsapp")]


async def test_tras_migrar_el_envio_sigue_yendo_al_bsuid(monkeypatch):
    """
    La conversación ya se indexa por teléfono, pero el cliente solo es
    alcanzable por su BSUID. Si esto se rompiera, los mensajes dejarían de
    llegarle sin que nadie se entere.
    """
    monkeypatch.setenv("FEATURE_BSUID_PHONE_CAPTURE", "true")
    sofia = _FakeSofia()
    state = _FakeStateManager(_FakeRedis())
    mongo = _FakeMongoManager()
    fake_twilio = _FakeTwilioClient()
    monkeypatch.setattr(wh, "twilio_client", fake_twilio)
    _cablear_flujo_diferido(monkeypatch, sofia, state, mongo=mongo)

    await wh._process_message_deferred(
        phone_normalized="+573001234567",
        phone_raw="whatsapp:CO.899759302823042",
        body="Hola de nuevo", profile_name=None,
        message_sid="SM_POST_MIGRACION", num_media=0, media_url=None,
        media_content_type=None, early_channel="whatsapp",
        incoming_channel="whatsapp", identity_type="bsuid",
        identity_key="+573001234567",
        routing_address="whatsapp:CO.899759302823042",
    )

    assert fake_twilio.calls[0]["to"] == "whatsapp:CO.899759302823042"
    assert mongo.messages[0]["phone"] == "+573001234567"
    # Sigue siendo bsuid (el panel lo necesita para resolver el envío)...
    assert state.meta_calls[0]["identity_type"] == "bsuid"
    # ...pero ya NO es un contacto sin teléfono.
    assert state.meta_calls[0]["has_phone"] is True
    assert mongo.messages[0]["metadata"]["has_phone"] is True
    # Y no se le vuelve a pedir el número: ya lo tenemos.
    assert "needs_phone" not in sofia.calls[0]["lead_context"]
    assert state.phone_asked_calls == []
