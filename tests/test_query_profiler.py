"""
Tests del profiler de consultas (middleware/query_profiler.py).

Foco especial en las dos propiedades que lo hacen seguro para produccion:
  - NUNCA loguea PII (los eventos de PyMongo traen el filtro con telefonos)
  - Es fail-open: si el profiling revienta, el request sale igual

Ejecutar: pytest tests/test_query_profiler.py -v
"""
import asyncio
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from middleware import query_profiler as qp

# ═══════════════════════════════════════════════════════════════
# 1. Acumulador por request — aislamiento entre requests concurrentes
# ═══════════════════════════════════════════════════════════════

def test_01_record_outside_request_is_noop():
    """Sin request activo, _record no debe explotar (jobs de APScheduler)."""
    qp._request_stats.set(None)
    qp._record("mongo", 12.3)  # no debe lanzar
    assert qp.get_request_profile() is None


def test_02_record_accumulates():
    qp.start_request_profile()
    qp._record("mongo", 10.0)
    qp._record("mongo", 5.0)
    qp._record("hubspot", 100.0)
    stats = qp.get_request_profile()
    assert stats["mongo_ms"] == 15.0
    assert stats["mongo_count"] == 2
    assert stats["hubspot_ms"] == 100.0
    assert stats["hubspot_count"] == 1


async def test_03_concurrent_requests_do_not_share_stats():
    """
    Dos requests concurrentes NO pueden mezclar mediciones.

    Es la razon de usar contextvars y no un dict global: con el panel abierto
    por varias asesoras a la vez, un acumulador compartido daria numeros
    inventados.
    """
    async def fake_request(mongo_ms, hubspot_ms):
        qp.start_request_profile()
        qp._record("mongo", mongo_ms)
        await asyncio.sleep(0.01)  # fuerza intercalado de las tasks
        qp._record("hubspot", hubspot_ms)
        return qp.get_request_profile()

    a, b = await asyncio.gather(fake_request(10, 100), fake_request(20, 200))
    assert a["mongo_ms"] == 10 and a["hubspot_ms"] == 100
    assert b["mongo_ms"] == 20 and b["hubspot_ms"] == 200


# ═══════════════════════════════════════════════════════════════
# 2. PII — CLAUDE.md regla 2
# ═══════════════════════════════════════════════════════════════

class _FakeMongoEvent:
    """Evento con un filtro que contiene PII, como los reales de PyMongo."""
    def __init__(self, duration_ms):
        self.command_name = "find"
        self.duration_micros = duration_ms * 1000
        self.command = {
            "find": "messages",
            "filter": {"phone": "+573138405930", "name": "Maria Gomez"},
        }


def test_04_slow_query_log_never_contains_pii(caplog):
    """
    El log de query lenta puede traer nombre de comando y coleccion,
    JAMAS el telefono ni el nombre del cliente.
    """
    listener = qp._build_mongo_listener()
    with caplog.at_level(logging.WARNING):
        listener.succeeded(_FakeMongoEvent(qp.SLOW_MS + 100))

    text = caplog.text
    assert "SLOW" in text, "No se logueo la query lenta"
    assert "messages" in text, "Deberia identificar la coleccion"
    assert "3138405930" not in text, "FUGA DE PII: telefono en logs"
    assert "Maria" not in text, "FUGA DE PII: nombre en logs"
    assert "filter" not in text, "El filtro completo no debe loguearse"


def test_04b_safe_path_masks_phone_in_url():
    """
    HALLAZGO FASE 6: el panel tiene 10+ rutas con el telefono EN EL PATH
    (/contacts/{phone}/detail, /conversations/{phone}, /reset-bot/{phone}...).
    Loguear request.url.path crudo mandaba telefonos completos a Railway.
    CLAUDE.md regla 2 lo prohibe explicitamente.
    """
    assert qp._safe_path("/panel/contacts/+573138405930/detail") == "/panel/contacts/{id}/detail"
    assert qp._safe_path("/panel/conversations/573138405930") == "/panel/conversations/{id}"
    assert qp._safe_path("/panel/reset-bot/+573138405930") == "/panel/reset-bot/{id}"
    # Rutas sin PII quedan intactas
    assert qp._safe_path("/panel/contacts") == "/panel/contacts"
    assert qp._safe_path("/panel/metrics") == "/panel/metrics"


def test_04c_safe_path_never_raises():
    assert qp._safe_path(None) == "?"


async def test_04d_slow_request_log_never_leaks_phone(caplog, monkeypatch):
    """Regresion end-to-end del hallazgo anterior, por el camino real del log."""
    monkeypatch.setattr(qp, "PROFILER_ENABLED", True)
    monkeypatch.setattr(qp, "SLOW_MS", 0.0)  # todo request cuenta como lento

    class _Req:
        method = "GET"
        class url:
            path = "/panel/contacts/+573138405930/detail"

    async def call_next(request):
        return _FakeResponse()

    with caplog.at_level(logging.WARNING):
        await qp.server_timing_middleware(_Req(), call_next)

    assert "SLOW" in caplog.text, "No se logueo el request lento"
    assert "3138405930" not in caplog.text, "FUGA DE PII: telefono en el log de request lento"
    assert "{id}" in caplog.text, "El path deberia aparecer enmascarado"


def test_05_failed_query_log_never_contains_pii(caplog):
    listener = qp._build_mongo_listener()
    with caplog.at_level(logging.ERROR):
        listener.failed(_FakeMongoEvent(10))
    assert "3138405930" not in caplog.text
    assert "Maria" not in caplog.text


def test_06_fast_query_is_not_logged(caplog):
    """Bajo el umbral no se loguea nada — evita inundar Railway."""
    listener = qp._build_mongo_listener()
    with caplog.at_level(logging.WARNING):
        listener.succeeded(_FakeMongoEvent(1))
    assert "SLOW" not in caplog.text


def test_07_fast_query_still_counted():
    """No loguear no significa no medir: el acumulador cuenta siempre."""
    qp.start_request_profile()
    listener = qp._build_mongo_listener()
    listener.succeeded(_FakeMongoEvent(1))
    assert qp.get_request_profile()["mongo_count"] == 1


def test_08_driver_internal_commands_ignored():
    """ping/hello/createIndexes son ruido del driver, no consultas."""
    qp.start_request_profile()
    listener = qp._build_mongo_listener()
    for name in ("ping", "hello", "createIndexes", "listIndexes"):
        ev = _FakeMongoEvent(500)
        ev.command_name = name
        listener.succeeded(ev)
    assert qp.get_request_profile()["mongo_count"] == 0


def test_09_collection_extraction_never_raises():
    """Un evento malformado no puede tumbar el listener."""
    listener = qp._build_mongo_listener()

    class Broken:
        command_name = "find"
        duration_micros = 1000
        @property
        def command(self):
            raise RuntimeError("boom")

    assert listener._collection(Broken()) == "?"


# ═══════════════════════════════════════════════════════════════
# 3. Registro idempotente del listener global
# ═══════════════════════════════════════════════════════════════

def test_10_register_mongo_listener_is_idempotent(monkeypatch):
    """
    pymongo.monitoring.register() es process-wide y NO se puede desregistrar.
    Registrar dos veces duplicaria cada medicion (mongo_ms al doble).
    """
    calls = []
    monkeypatch.setattr(qp, "_mongo_listener_registered", False)
    import pymongo.monitoring as m
    monkeypatch.setattr(m, "register", lambda listener: calls.append(listener))

    assert qp.register_mongo_listener() is True
    assert qp.register_mongo_listener() is True
    assert qp.register_mongo_listener() is True
    assert len(calls) == 1, f"Listener registrado {len(calls)} veces, esperaba 1"


def test_10b_register_is_fail_open(monkeypatch, caplog):
    """
    Si pymongo.monitoring.register() explota, el arranque de la app NO puede
    caerse. La instrumentacion es opcional; MongoDB no.
    """
    monkeypatch.setattr(qp, "_mongo_listener_registered", False)
    monkeypatch.setattr(qp, "PROFILER_ENABLED", True)
    import pymongo.monitoring as m

    def explode(listener):
        raise RuntimeError("monitoring roto")
    monkeypatch.setattr(m, "register", explode)

    with caplog.at_level(logging.WARNING):
        assert qp.register_mongo_listener() is False  # no lanza
    assert "No se pudo registrar" in caplog.text


def test_10c_env_bool_parsing(monkeypatch):
    """Metrica de config: los flags de entorno deben leerse como se documenta."""
    for raw, expected in [("true", True), ("TRUE", True), ("1", True), ("yes", True),
                          ("on", True), ("false", False), ("0", False), ("no", False),
                          ("", False), ("  True  ", True)]:
        monkeypatch.setenv("QP_TEST_FLAG", raw)
        assert qp._env_bool("QP_TEST_FLAG", False) is expected, f"'{raw}' mal parseado"
    monkeypatch.delenv("QP_TEST_FLAG", raising=False)
    assert qp._env_bool("QP_TEST_FLAG", True) is True   # default cuando no existe
    assert qp._env_bool("QP_TEST_FLAG", False) is False


async def test_10d_response_hook_without_start_is_noop():
    """
    Una respuesta cuyo request no paso por el hook de entrada (retry interno
    de httpx, request construido a mano) no debe contaminar las metricas.
    """
    import httpx

    qp.start_request_profile()
    req = httpx.Request("GET", "https://api.hubapi.com/x")   # sin _qp_start
    resp = httpx.Response(200, request=req)
    await qp._on_response(resp)
    assert qp.get_request_profile()["hubspot_count"] == 0


def test_10e_failed_ignores_driver_commands():
    """Simetria con succeeded(): los comandos internos tampoco cuentan al fallar."""
    qp.start_request_profile()
    listener = qp._build_mongo_listener()
    ev = _FakeMongoEvent(500)
    ev.command_name = "ping"
    listener.failed(ev)
    assert qp.get_request_profile()["mongo_count"] == 0


def test_10f_started_hook_is_noop():
    """started() es parte del contrato de CommandListener; debe existir y no hacer nada."""
    listener = qp._build_mongo_listener()
    qp.start_request_profile()
    listener.started(_FakeMongoEvent(10))
    assert qp.get_request_profile()["mongo_count"] == 0


def test_11_disabled_profiler_does_not_register(monkeypatch):
    monkeypatch.setattr(qp, "PROFILER_ENABLED", False)
    monkeypatch.setattr(qp, "_mongo_listener_registered", False)
    assert qp.register_mongo_listener() is False


def test_12_disabled_profiler_returns_no_hooks(monkeypatch):
    monkeypatch.setattr(qp, "PROFILER_ENABLED", False)
    assert qp.build_httpx_event_hooks() == {}


def test_13_enabled_profiler_returns_both_hooks(monkeypatch):
    monkeypatch.setattr(qp, "PROFILER_ENABLED", True)
    hooks = qp.build_httpx_event_hooks()
    assert "request" in hooks and "response" in hooks


async def test_13b_httpx_hooks_actually_measure(monkeypatch, caplog):
    """
    Integracion real con httpx: los hooks deben medir de verdad.

    El timestamp se guarda en request.extensions y se lee luego desde
    response.request. Si httpx dejara de propagar ese dict, el profiler
    reportaria 0ms de HubSpot para siempre, en silencio y sin error.

    Ademas verifica que el query string (que lleva telefonos) no acabe
    en los logs.
    """
    import httpx

    monkeypatch.setattr(qp, "PROFILER_ENABLED", True)
    monkeypatch.setattr(qp, "SLOW_MS", 0.0)  # forzar el log de "lento"

    def handler(request):
        return httpx.Response(200, json={"ok": True})

    qp.start_request_profile()
    with caplog.at_level(logging.WARNING):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            event_hooks=qp.build_httpx_event_hooks(),
        ) as client:
            await client.get(
                "https://api.hubapi.com/crm/v3/objects/contacts?phone=%2B573138405930"
            )
            await client.get("https://api.hubapi.com/crm/v3/objects/contacts")

    stats = qp.get_request_profile()
    assert stats["hubspot_count"] == 2, (
        f"Los hooks no midieron las 2 llamadas: {stats}"
    )
    assert stats["hubspot_ms"] > 0.0, "Duracion de HubSpot quedo en 0"
    assert "3138405930" not in caplog.text, (
        "FUGA DE PII: el query string llego a los logs"
    )


# ═══════════════════════════════════════════════════════════════
# 4. Middleware — fail-open y correccion del header
# ═══════════════════════════════════════════════════════════════

class _FakeResponse:
    def __init__(self):
        self.headers = {}


class _FakeRequest:
    method = "GET"
    class url:
        path = "/panel/contacts"


async def test_14_middleware_emits_server_timing(monkeypatch):
    monkeypatch.setattr(qp, "PROFILER_ENABLED", True)
    monkeypatch.setattr(qp, "SERVER_TIMING_ENABLED", True)

    async def call_next(request):
        qp._record("mongo", 30.0)
        qp._record("hubspot", 250.0)
        return _FakeResponse()

    resp = await qp.server_timing_middleware(_FakeRequest(), call_next)
    header = resp.headers["Server-Timing"]
    assert "mongo;dur=30.0" in header
    assert "hubspot;dur=250.0" in header
    assert "total;dur=" in header


async def test_15_other_bucket_is_never_negative(monkeypatch):
    """
    'other' = total - mongo - hubspot. Las mediciones de capa son
    concurrentes (asyncio.gather), asi que su suma PUEDE superar el total
    real del request. Sin el clamp saldrian duraciones negativas.
    """
    monkeypatch.setattr(qp, "PROFILER_ENABLED", True)

    async def call_next(request):
        qp._record("mongo", 5000.0)     # imposible, pero fuerza el caso
        qp._record("hubspot", 5000.0)
        return _FakeResponse()

    resp = await qp.server_timing_middleware(_FakeRequest(), call_next)
    other = [p for p in resp.headers["Server-Timing"].split(",") if "other" in p][0]
    dur = float(other.split("dur=")[1].split(";")[0])
    assert dur >= 0.0, f"Duracion negativa en el header: {dur}"


async def test_16_middleware_is_fail_open(monkeypatch):
    """
    Si el profiling falla, el request DEBE completarse igual.
    La instrumentacion nunca puede tumbar el panel.
    """
    monkeypatch.setattr(qp, "PROFILER_ENABLED", True)

    def explode(*a, **k):
        raise RuntimeError("profiler roto")
    monkeypatch.setattr(qp, "get_request_profile", explode)

    sentinel = _FakeResponse()

    async def call_next(request):
        return sentinel

    resp = await qp.server_timing_middleware(_FakeRequest(), call_next)
    assert resp is sentinel, "El middleware se trago la respuesta al fallar"


async def test_17_disabled_profiler_adds_no_header(monkeypatch):
    monkeypatch.setattr(qp, "PROFILER_ENABLED", False)

    async def call_next(request):
        return _FakeResponse()

    resp = await qp.server_timing_middleware(_FakeRequest(), call_next)
    assert "Server-Timing" not in resp.headers


# ═══════════════════════════════════════════════════════════════
# 5. Conectores — el profiler debe estar realmente enchufado
# ═══════════════════════════════════════════════════════════════

def _read(relpath):
    with open(os.path.join(ROOT, relpath), "r", encoding="utf-8") as f:
        return f.read()


def test_18_mongo_client_registers_listener_before_construction():
    """
    PyMongo captura los listeners globales al CONSTRUIR el cliente.
    Registrar despues no instrumenta nada.
    """
    src = _read(os.path.join("database", "mongodb_client.py"))
    idx_register = src.index("register_mongo_listener()")
    idx_client = src.index("self.client = AsyncIOMotorClient(")
    assert idx_register < idx_client, (
        "register_mongo_listener() debe llamarse ANTES de crear "
        "AsyncIOMotorClient, si no el listener no se aplica"
    )


def test_19_httpx_singleton_has_event_hooks():
    src = _read(os.path.join("middleware", "outbound_panel.py"))
    start = src.index("def get_httpx_client()")
    body = src[start:start + 2000]
    assert "event_hooks=" in body, (
        "El cliente httpx singleton no lleva event_hooks — "
        "las llamadas a HubSpot quedarian sin medir"
    )


def test_20_app_registers_middleware():
    src = _read("app.py")
    assert "server_timing_middleware" in src
    idx_app = src.index('app = FastAPI(')
    idx_mw = src.index("app.middleware(\"http\")(_server_timing)")
    assert idx_mw > idx_app


def test_21_end_to_end_through_real_starlette():
    """
    Integracion real: mediciones hechas DENTRO del endpoint deben llegar al
    middleware que emite el header.

    Es la propiedad mas fragil del diseño. Starlette ejecuta el endpoint en
    una task hija via BaseHTTPMiddleware, y un ContextVar REASIGNADO alli no
    seria visible al volver al middleware. Funciona porque el ContextVar
    guarda un dict MUTABLE: la task hija hereda la referencia y la muta, en
    vez de rebindear la variable.

    Si alguien "simplifica" _record() a `_request_stats.set({...})`, este test
    falla y ese es exactamente el punto.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    mini = FastAPI()
    mini.middleware("http")(qp.server_timing_middleware)

    @mini.get("/fake")
    async def fake():
        qp._record("mongo", 42.0)
        qp._record("hubspot", 310.0)
        return {"ok": True}

    resp = TestClient(mini).get("/fake")
    assert resp.status_code == 200

    header = resp.headers.get("Server-Timing")
    assert header is not None, "El middleware no emitio Server-Timing"
    assert "mongo;dur=42.0" in header, (
        f"Las mediciones del endpoint no llegaron al middleware: {header}"
    )
    assert "hubspot;dur=310.0" in header
