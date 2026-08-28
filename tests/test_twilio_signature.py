"""
Tests de la validacion de firma Twilio en el webhook (middleware/webhook_handler.py).

Foco en las propiedades que lo hacen seguro para produccion:
  - La firma se calcula distinto en JSON (bodySHA256 en el query) que en
    form-encoded (params ordenados) — ambas ramas deben validar
  - Falla CERRADO: firma ausente, invalida o token faltante -> False
  - El log de diagnostico NUNCA emite telefono ni contenido del mensaje
  - El modo log_only no bloquea; solo enforce devuelve 403

Ejecutar: pytest tests/test_twilio_signature.py -v
"""
import hashlib
import json
import os
import sys
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from starlette.requests import Request
from twilio.request_validator import RequestValidator

from middleware import webhook_handler as wh

# Token dummy de tests/conftest.py — no autentica contra nada real.
TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
BASE_URL = "https://agenteconversacionalpractica-production.up.railway.app"
WEBHOOK_PATH = "/whatsapp/webhook"
FULL_URL = f"{BASE_URL}{WEBHOOK_PATH}"

# PII sintetica: si alguna aparece en un log, el test de fuga debe cazarla.
FAKE_PHONE = "+573138405930"
FAKE_BODY = "quiero informacion del apartamento"


def _make_request(headers: dict, body: bytes, query_string: bytes = b"") -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": WEBHOOK_PATH,
        "query_string": query_string,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "scheme": "https",
        "server": ("agenteconversacionalpractica-production.up.railway.app", 443),
    }
    return Request(scope, receive)


def _form_request(signature: str | None):
    """Peticion form-encoded (Programmable Messaging legacy)."""
    params = {
        "From": f"whatsapp:{FAKE_PHONE}",
        "Body": FAKE_BODY,
        "MessageSid": "SM00000000000000000000000000000000",
    }
    body = urlencode(params).encode()
    headers = {"content-type": "application/x-www-form-urlencoded"}
    if signature is not None:
        headers["x-twilio-signature"] = signature
    return _make_request(headers, body), body, params


def _json_request(signature: str | None, body_str: str, body_hash: str):
    """Peticion JSON (Conversations API). Twilio pone bodySHA256 en el query."""
    headers = {"content-type": "application/json"}
    if signature is not None:
        headers["x-twilio-signature"] = signature
    qs = f"bodySHA256={body_hash}".encode()
    return _make_request(headers, body_str.encode(), query_string=qs)


# ═══════════════════════════════════════════════════════════════
# 1. Rama form-encoded (Programmable Messaging)
# ═══════════════════════════════════════════════════════════════

async def test_01_form_valid_signature(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    validator = RequestValidator(TOKEN)
    _, body, params = _form_request(None)
    sig = validator.compute_signature(FULL_URL, params)
    req, body, _ = _form_request(sig)
    assert await wh._twilio_signature_is_valid(req, body, is_json=False) is True


async def test_02_form_invalid_signature(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    req, body, _ = _form_request("firmaFalsaQueNoCorresponde=")
    assert await wh._twilio_signature_is_valid(req, body, is_json=False) is False


async def test_03_form_missing_signature(monkeypatch):
    """Sin cabecera X-Twilio-Signature: falla cerrado."""
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    req, body, _ = _form_request(None)
    assert await wh._twilio_signature_is_valid(req, body, is_json=False) is False


async def test_04_form_tampered_body(monkeypatch):
    """Firma valida para el payload original, pero el body fue alterado."""
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    validator = RequestValidator(TOKEN)
    _, _, params = _form_request(None)
    sig = validator.compute_signature(FULL_URL, params)

    tampered = dict(params, Body="MENSAJE INYECTADO POR UN TERCERO")
    body = urlencode(tampered).encode()
    req = _make_request(
        {"content-type": "application/x-www-form-urlencoded", "x-twilio-signature": sig},
        body,
    )
    assert await wh._twilio_signature_is_valid(req, body, is_json=False) is False


# ═══════════════════════════════════════════════════════════════
# 2. Rama JSON (Conversations API)
# ═══════════════════════════════════════════════════════════════

async def test_05_json_valid_signature(monkeypatch):
    """
    Twilio firma los webhooks JSON sobre la URL + bodySHA256 en el query,
    no sobre params ordenados. Si _twilio_signed_url() perdiera el query
    string, este test fallaria.
    """
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    body_str = json.dumps({"Author": f"whatsapp:{FAKE_PHONE}", "Body": FAKE_BODY})
    body_hash = hashlib.sha256(body_str.encode()).hexdigest()
    sig = RequestValidator(TOKEN).compute_signature(f"{FULL_URL}?bodySHA256={body_hash}", {})

    req = _json_request(sig, body_str, body_hash)
    assert await wh._twilio_signature_is_valid(req, body_str.encode(), is_json=True) is True


async def test_06_json_invalid_signature(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    body_str = json.dumps({"Author": f"whatsapp:{FAKE_PHONE}", "Body": FAKE_BODY})
    body_hash = hashlib.sha256(body_str.encode()).hexdigest()
    req = _json_request("firmaFalsaQueNoCorresponde=", body_str, body_hash)
    assert await wh._twilio_signature_is_valid(req, body_str.encode(), is_json=True) is False


async def test_07_json_missing_signature(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    body_str = json.dumps({"Author": f"whatsapp:{FAKE_PHONE}", "Body": FAKE_BODY})
    body_hash = hashlib.sha256(body_str.encode()).hexdigest()
    req = _json_request(None, body_str, body_hash)
    assert await wh._twilio_signature_is_valid(req, body_str.encode(), is_json=True) is False


async def test_08_json_tampered_body(monkeypatch):
    """El bodySHA256 firmado ya no coincide con el cuerpo recibido."""
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    body_str = json.dumps({"Author": f"whatsapp:{FAKE_PHONE}", "Body": FAKE_BODY})
    body_hash = hashlib.sha256(body_str.encode()).hexdigest()
    sig = RequestValidator(TOKEN).compute_signature(f"{FULL_URL}?bodySHA256={body_hash}", {})

    tampered = json.dumps({"Author": f"whatsapp:{FAKE_PHONE}", "Body": "INYECTADO"})
    req = _json_request(sig, tampered, body_hash)
    assert await wh._twilio_signature_is_valid(req, tampered.encode(), is_json=True) is False


# ═══════════════════════════════════════════════════════════════
# 3. Fallo cerrado ante configuracion ausente
# ═══════════════════════════════════════════════════════════════

async def test_09_missing_auth_token_fails_closed(monkeypatch):
    """Sin TWILIO_AUTH_TOKEN no se puede validar: jamas se asume valido."""
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")
    req, body, _ = _form_request("cualquierCosa=")
    assert await wh._twilio_signature_is_valid(req, body, is_json=False) is False


# ═══════════════════════════════════════════════════════════════
# 4. Reconstruccion de la URL firmada (riesgo R1: proxy de Railway)
# ═══════════════════════════════════════════════════════════════

def test_10_signed_url_uses_configured_base(monkeypatch):
    """
    Detras del proxy de Railway request.url puede reportar http:// y el host
    interno. La base configurada tiene prioridad — sin esto, ninguna firma
    legitima validaria en produccion.
    """
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    req = _make_request({"host": "interno.railway.internal"}, b"")
    assert wh._twilio_signed_url(req) == FULL_URL


def test_11_signed_url_preserves_query_string(monkeypatch):
    """El bodySHA256 vive en el query: perderlo rompe la validacion JSON."""
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    req = _make_request({}, b"", query_string=b"bodySHA256=abc123")
    assert wh._twilio_signed_url(req) == f"{FULL_URL}?bodySHA256=abc123"


def test_12_signed_url_fallback_honors_proxy_headers(monkeypatch):
    """Sin base configurada, se honran las cabeceras del proxy."""
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", "")
    req = _make_request(
        {"x-forwarded-proto": "https", "x-forwarded-host": "ejemplo.railway.app"}, b""
    )
    assert wh._twilio_signed_url(req) == f"https://ejemplo.railway.app{WEBHOOK_PATH}"


# ═══════════════════════════════════════════════════════════════
# 5. El log de diagnostico no puede filtrar PII (regla de CLAUDE.md)
# ═══════════════════════════════════════════════════════════════

def _raw_and_sig_logs(caplog) -> str:
    """Solo las lineas que este cambio controla: [Webhook][RAW] y [Webhook][Sig].

    El resto de webhook_handler.py todavia loguea telefonos completos en INFO
    (~9 lineas: 1371, 1582, 1601, 1615...). Es una violacion preexistente de
    la regla de CLAUDE.md, con alcance propio y ticket aparte — deliberadamente
    fuera de este cambio para no ensanchar el diff sobre main en produccion.
    Estos tests fijan el contrato de las lineas corregidas aqui; no pueden
    pasar sobre el archivo entero hasta que ese otro trabajo se haga.
    """
    return "\n".join(
        r.getMessage() for r in caplog.records
        if "[Webhook][RAW]" in r.getMessage() or "[Webhook][Sig]" in r.getMessage()
    )


async def test_13_diagnostic_log_never_emits_pii(monkeypatch, caplog):
    """
    El hallazgo original volcaba 1500 chars del body crudo a INFO — con el
    telefono y el texto del cliente dentro. Este test falla si eso vuelve.
    """
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "log_only")

    req, body, _ = _form_request("firmaInvalida=")
    with caplog.at_level("DEBUG"):
        await wh.whatsapp_webhook(req, _DummyBackgroundTasks())

    emitido = _raw_and_sig_logs(caplog)
    assert FAKE_PHONE not in emitido, "El telefono del cliente aparecio en un log"
    assert "3138405930" not in emitido, "El telefono sin prefijo aparecio en un log"
    assert FAKE_BODY not in emitido, "El texto del mensaje aparecio en un log"
    # Lo que SI debe conservarse para diagnostico:
    assert "[Webhook][RAW]" in emitido
    assert "sig_present=True" in emitido
    assert "'Body'" in emitido and "'From'" in emitido, "Se perdieron los nombres de campo"


async def test_14_json_diagnostic_log_never_emits_pii(monkeypatch, caplog):
    """Mismo contrato de privacidad en la rama JSON."""
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "log_only")

    body_str = json.dumps({"Author": f"whatsapp:{FAKE_PHONE}", "Body": FAKE_BODY})
    body_hash = hashlib.sha256(body_str.encode()).hexdigest()
    req = _json_request("firmaInvalida=", body_str, body_hash)

    with caplog.at_level("DEBUG"):
        await wh.whatsapp_webhook(req, _DummyBackgroundTasks())

    emitido = _raw_and_sig_logs(caplog)
    assert FAKE_PHONE not in emitido
    assert FAKE_BODY not in emitido
    assert "[Webhook][RAW]" in emitido
    assert "'Author'" in emitido and "'Body'" in emitido


# ═══════════════════════════════════════════════════════════════
# 6. Modos del flag: log_only no bloquea, enforce si
# ═══════════════════════════════════════════════════════════════

class _DummyBackgroundTasks:
    """Doble de BackgroundTasks: registra sin ejecutar nada."""

    def __init__(self):
        self.tasks = []

    def add_task(self, func, *args, **kwargs):
        self.tasks.append((func, args, kwargs))


async def test_15_log_only_does_not_block_invalid_signature(monkeypatch):
    """
    La garantia central del despliegue seguro: en log_only una firma invalida
    se registra pero NUNCA devuelve 403. Si este test falla, el modo
    observacion no protege de una caida en produccion.
    """
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "log_only")

    req, _, _ = _form_request("firmaInvalida=")
    resp = await wh.whatsapp_webhook(req, _DummyBackgroundTasks())
    assert resp.status_code != 403


async def test_16_enforce_blocks_invalid_signature(monkeypatch):
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "enforce")

    req, _, _ = _form_request("firmaInvalida=")
    resp = await wh.whatsapp_webhook(req, _DummyBackgroundTasks())
    assert resp.status_code == 403


async def test_17_enforce_blocks_missing_signature(monkeypatch):
    """El ataque del hallazgo: POST con formato Twilio pero sin firma."""
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "enforce")

    req, _, _ = _form_request(None)
    resp = await wh.whatsapp_webhook(req, _DummyBackgroundTasks())
    assert resp.status_code == 403


async def test_18_off_mode_skips_validation(monkeypatch):
    """Kill switch: en off ni siquiera se evalua la firma."""
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")

    called = False

    async def _spy(*args, **kwargs):
        nonlocal called
        called = True
        return False

    monkeypatch.setattr(wh, "_twilio_signature_is_valid", _spy)
    req, _, _ = _form_request(None)
    await wh.whatsapp_webhook(req, _DummyBackgroundTasks())
    assert called is False
