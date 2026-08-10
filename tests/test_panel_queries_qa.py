"""
QA de los modulos de consulta del panel (fechas, busqueda, embudos).

FASE 6 del proceso: tests unitarios + metricas de calidad + deteccion de
anomalias de acoplamiento entre modulos.

Cubre las regresiones del hotfix 221e429:
  1. /contacts/search creaba un MongoDBManager() por request (pool de 20
     conexiones nunca cerrado + 24 createIndex por busqueda).
  2. La key de cache de GET /contacts omitia include_phone, sirviendo
     respuestas con contactos cross-advisor a otras peticiones.

Estilo: analisis estatico, sin necesidad de Redis/Mongo vivos — consistente
con el resto de la suite del repo.

Ejecutar: pytest tests/test_panel_queries_qa.py -v
"""
import ast
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PANEL = os.path.join(ROOT, "middleware", "outbound_panel.py")
MONGO = os.path.join(ROOT, "database", "mongodb_client.py")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ═══════════════════════════════════════════════════════════════
# 1. Singleton de MongoDB — regresion del memory leak
# ═══════════════════════════════════════════════════════════════

def test_01_no_mongodbmanager_instantiation_outside_factory():
    """
    MongoDBManager() solo puede instanciarse dentro de get_mongo_manager().

    Cualquier otra instanciacion crea un AsyncIOMotorClient nuevo
    (maxPoolSize=20) que nunca se cierra → memory leak. Viola la regla 1
    de CLAUDE.md sobre singletons.
    """
    offenders = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel = os.path.relpath(dirpath, ROOT).split(os.sep)
        if any(s in rel for s in [".git", "node_modules", "__pycache__", ".claude", "tests"]):
            continue
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fname)
            for i, line in enumerate(_read(fpath).splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if re.search(r"(?<![\w.])MongoDBManager\s*\(", line):
                    relpath = os.path.relpath(fpath, ROOT)
                    # Unica excepcion legitima: la factory del singleton
                    if relpath.replace(os.sep, "/") == "database/mongodb_client.py":
                        continue
                    offenders.append(f"{relpath}:{i}")

    assert not offenders, (
        "MongoDBManager() instanciado fuera de get_mongo_manager() en: "
        f"{offenders}. Usar el singleton get_mongo_manager()."
    )


def test_02_get_mongo_manager_returns_singleton():
    """get_mongo_manager() debe devolver siempre la MISMA instancia."""
    from database.mongodb_client import get_mongo_manager

    a = get_mongo_manager()
    b = get_mongo_manager()
    assert a is b, "get_mongo_manager() devolvio instancias distintas"


def test_03_search_endpoint_uses_singleton():
    """El endpoint /contacts/search debe obtener Mongo via el singleton."""
    src = _read(PANEL)
    start = src.index("async def search_contacts_by_keyword")
    end = src.index("async def _get_contacts_by_worker_filter")
    # Descartar comentarios: el propio fix documenta el antipatron citando
    # "MongoDBManager()" en prosa, y eso no es una instanciacion real.
    body = "\n".join(
        ln for ln in src[start:end].splitlines()
        if not ln.strip().startswith("#")
    )

    assert "get_mongo_manager()" in body, (
        "search_contacts_by_keyword no usa get_mongo_manager()"
    )
    assert not re.search(r"(?<![\w.])MongoDBManager\s*\(", body), (
        "search_contacts_by_keyword sigue instanciando MongoDBManager()"
    )


# ═══════════════════════════════════════════════════════════════
# 2. Correctitud de la key de cache de GET /contacts
# ═══════════════════════════════════════════════════════════════

def _cache_params_expression(src):
    """Extrae el texto de la asignacion de _cache_params en GET /contacts."""
    m = re.search(r"_cache_params\s*=\s*\(?(.*?)\)?\n\s*_contacts_cache_key", src, re.DOTALL)
    assert m, "No se encontro la asignacion de _cache_params"
    return m.group(1)


def test_04_cache_key_includes_include_phone():
    """
    include_phone DEBE formar parte de la key de cache.

    Una request con deep link inyecta un contacto cross-advisor en la
    respuesta. Sin include_phone en la key, esa respuesta contaminada se
    sirve durante el TTL a cualquier peticion con los mismos parametros,
    saltandose la segregacion por equipo.
    """
    expr = _cache_params_expression(_read(PANEL))
    assert "include_phone" in expr, (
        "include_phone falta en la key de cache de GET /contacts — "
        "fuga de contactos cross-advisor entre asesoras"
    )


def test_05_cache_key_covers_every_response_shaping_param():
    """
    Metrica de calidad: todo parametro que altera la RESPUESTA debe estar
    en la key de cache. Los que hacen return temprano (stage, worker_id)
    quedan excluidos a proposito porque nunca llegan al cache.
    """
    expr = _cache_params_expression(_read(PANEL))
    required = ["advisor", "filter_time", "date_from", "date_to",
                "date_field", "page", "limit", "include_phone"]
    missing = [p for p in required if p not in expr]
    assert not missing, f"Parametros ausentes en la key de cache: {missing}"


def test_06_early_return_params_never_reach_cache():
    """
    stage y worker_id no estan en la key porque retornan antes de cachear.
    Este test falla si alguien mueve el cache ARRIBA de esos branches sin
    añadirlos a la key — que reintroduciria el mismo bug de contaminacion.
    """
    src = _read(PANEL)
    start = src.index("async def get_active_contacts")
    body = src[start:start + 20000]

    idx_cache = body.index("_cache_params")
    idx_stage_return = body.index('"filter_mode": "stage_full"')
    idx_worker_return = body.index("_get_contacts_by_worker_filter(")

    assert idx_stage_return < idx_cache, (
        "El branch de stage dejo de retornar antes del cache — "
        "stage debe añadirse a _cache_params"
    )
    assert idx_worker_return < idx_cache, (
        "El branch de worker_id dejo de retornar antes del cache — "
        "worker_id debe añadirse a _cache_params"
    )


# ═══════════════════════════════════════════════════════════════
# 2b. PIEZA 0 — N+1 sobre Redis en el cache de nombres
#
# Medido en produccion (9-ago-2026) con el profiler: GET /contacts tardaba
# ~8.1s de los cuales ~5.6s eran `otro` (Redis+CPU) y solo 18ms MongoDB.
# Causa: `for cid in ids: await _get_cached_contact_name(cid)` hacia un
# round-trip por contacto. Con ~300 contactos y 15-20ms de RTT = ~5s.
# ═══════════════════════════════════════════════════════════════

class _FakeRedis:
    """Redis mínimo que CUENTA round-trips — la propiedad que importa aquí."""

    def __init__(self, data=None, fail=False):
        self.data = data or {}
        self.fail = fail
        self.mget_calls = 0
        self.get_calls = 0

    async def mget(self, keys):
        if self.fail:
            raise ConnectionError("Redis caido")
        self.mget_calls += 1
        return [self.data.get(k) for k in keys]

    async def get(self, key):
        if self.fail:
            raise ConnectionError("Redis caido")
        self.get_calls += 1
        return self.data.get(key)


def _patch_redis(monkeypatch, fake):
    import middleware.outbound_panel as op

    async def _fake_client():
        return fake
    monkeypatch.setattr(op, "_get_redis_client", _fake_client)
    return op


async def test_08_name_cache_batch_uses_single_round_trip(monkeypatch):
    """
    LA propiedad de la Pieza 0: N contactos = 1 viaje a Redis, no N.

    Si alguien revierte a un bucle secuencial, este test falla.
    """
    ids = [str(i) for i in range(300)]
    data = {
        f"contact_name:{i}": json.dumps({"firstname": f"N{i}", "lastname": "X"})
        for i in range(300)
    }
    fake = _FakeRedis(data)
    op = _patch_redis(monkeypatch, fake)

    result = await op._get_cached_contact_names_batch(ids)

    assert len(result) == 300
    assert result["7"] == {"firstname": "N7", "lastname": "X"}
    assert fake.mget_calls == 1, f"Se esperaba 1 MGET, hubo {fake.mget_calls}"
    assert fake.get_calls == 0, (
        f"Hubo {fake.get_calls} GETs individuales — volvio el N+1"
    )


async def test_09_name_cache_batch_partial_hits(monkeypatch):
    """Los no cacheados simplemente no aparecen (van a HubSpot)."""
    fake = _FakeRedis({
        "contact_name:a": json.dumps({"firstname": "Ana", "lastname": "Gomez"}),
    })
    op = _patch_redis(monkeypatch, fake)

    result = await op._get_cached_contact_names_batch(["a", "b", "c"])
    assert set(result.keys()) == {"a"}


async def test_10_name_cache_batch_survives_corrupt_entry(monkeypatch):
    """Una entrada corrupta no puede tumbar toda la lista de contactos."""
    fake = _FakeRedis({
        "contact_name:a": "{no es json",
        "contact_name:b": json.dumps({"firstname": "Ben", "lastname": ""}),
    })
    op = _patch_redis(monkeypatch, fake)

    result = await op._get_cached_contact_names_batch(["a", "b"])
    assert set(result.keys()) == {"b"}, "La entrada corrupta debe tratarse como miss"


async def test_11_name_cache_batch_is_fail_open(monkeypatch):
    """
    Si Redis falla, devolver {} = cache miss total. El panel sigue
    funcionando porque los nombres se resuelven via HubSpot batch.
    """
    op = _patch_redis(monkeypatch, _FakeRedis(fail=True))
    assert await op._get_cached_contact_names_batch(["a", "b"]) == {}


async def test_12_name_cache_batch_empty_input_touches_nothing(monkeypatch):
    fake = _FakeRedis()
    op = _patch_redis(monkeypatch, fake)
    assert await op._get_cached_contact_names_batch([]) == {}
    assert fake.mget_calls == 0, "Sin ids no deberia haber viaje a Redis"


def test_13_no_sequential_name_cache_loop_in_hot_path():
    """
    Regresion estatica: `await _get_cached_contact_name(...)` NO puede volver
    a aparecer dentro de un bucle en get_active_contacts.

    Se analiza el AST en vez del texto para no depender del formato.
    """
    tree = ast.parse(_read(PANEL))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != "get_active_contacts":
            continue
        for loop in ast.walk(node):
            if not isinstance(loop, (ast.For, ast.AsyncFor, ast.While)):
                continue
            for inner in ast.walk(loop):
                if isinstance(inner, ast.Await) and isinstance(inner.value, ast.Call):
                    fn = ast.unparse(inner.value.func)
                    if "_get_cached_contact_name" in fn:
                        offenders.append(f"linea {inner.lineno}: {fn}")

    assert not offenders, (
        f"Volvio el N+1 sobre Redis en get_active_contacts: {offenders}. "
        "Usar _get_cached_contact_names_batch() (1 MGET)."
    )


# ═══════════════════════════════════════════════════════════════
# 3. Conectores entre modulos — deteccion de acoplamiento
# ═══════════════════════════════════════════════════════════════

def test_07_motor_client_never_used_across_threads():
    """
    AsyncIOMotorClient esta ligado a un event loop. Usarlo desde
    asyncio.to_thread / run_in_executor lo rompe silenciosamente.

    Hoy to_thread solo envuelve LeadAssigner (HubSpot sync) y tools del
    InfoAgent. Este test falla si alguien mete Mongo ahi dentro.
    """
    offenders = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel = os.path.relpath(dirpath, ROOT).split(os.sep)
        if any(s in rel for s in [".git", "node_modules", "__pycache__", ".claude", "tests"]):
            continue
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fname)
            for i, line in enumerate(_read(fpath).splitlines(), 1):
                if "to_thread(" in line or "run_in_executor(" in line:
                    if "mongo" in line.lower() or "get_mongo_manager" in line:
                        offenders.append(f"{os.path.relpath(fpath, ROOT)}:{i}")

    assert not offenders, (
        f"Cliente Motor usado fuera del event loop en: {offenders}. "
        "AsyncIOMotorClient no es thread-safe entre loops."
    )


def test_08_fulltext_search_contract():
    """
    Contrato del conector panel → mongodb_client.

    search_messages_fulltext devuelve List[str] de telefonos (ya deduplicado).
    El panel depende de eso para hidratar. Si la firma cambia, el endpoint
    de busqueda rompe en silencio.
    """
    src = _read(MONGO)
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "search_messages_fulltext":
            fn = node
            break

    assert fn is not None, "search_messages_fulltext desaparecio de mongodb_client"

    args = [a.arg for a in fn.args.args]
    assert "query_text" in args and "limit" in args, (
        f"Firma de search_messages_fulltext cambio: {args}"
    )
    returns = ast.unparse(fn.returns) if fn.returns else ""
    assert "List[str]" in returns, (
        f"search_messages_fulltext ya no devuelve List[str] sino {returns} — "
        "el panel asume una lista de telefonos"
    )


def test_09_text_index_exists_for_fulltext_search():
    """
    La busqueda por palabra clave depende de un text index sobre content.
    Sin el, $text lanza error y la busqueda devuelve [] en silencio
    (el except de search_messages_fulltext se lo traga).
    """
    src = _read(MONGO)
    assert 'name="content_text_idx"' in src, (
        "El text index content_text_idx desaparecio — $text fallaria y la "
        "busqueda por palabra clave devolveria vacio silenciosamente"
    )


def test_10_date_range_index_exists():
    """
    El filtro [desde/hasta] se apoyara en conv_owner_last_msg_idx
    {owner_id:1, last_message_at:-1}. Debe existir antes de construirlo.
    """
    src = _read(MONGO)
    assert 'name="conv_owner_last_msg_idx"' in src, (
        "conv_owner_last_msg_idx no existe — el filtro por rango de fechas "
        "haria COLLSCAN sobre conversations"
    )
