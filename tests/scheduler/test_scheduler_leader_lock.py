"""
Tests del lock de liderazgo del scheduler.

Cubre el lock en si —renovarlo, cederlo, no pisar el ajeno—. La eleccion en
segundo plano y el uso del singleton Redis viven en test_scheduler_election.py.

Historia: el liderazgo se decidia con un unico SET NX al arrancar, sin liberacion
al apagar. En un redeploy el worker viejo moria dejando el lock vivo hasta 300s,
el nuevo lo encontraba ocupado y —al no reintentar jamas— el sistema quedaba sin
NINGUNO de los 13 jobs de fondo, con el deploy reportando SUCCESS. Ocurrio el
10-ago-2026 y costo dos redeploys manuales.

Ejecutar: python tests/test_scheduler_leader_lock.py
O:        python -m pytest tests/test_scheduler_leader_lock.py -v
"""
import ast
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_PATH = os.path.join(ROOT, "app.py")


class FakeRedis:
    """Redis en memoria con SET NX/EX y los dos scripts Lua del lock."""

    def __init__(self):
        self.store = {}
        self.ttls = {}
        self.set_calls = 0
        self.closed = False

    async def set(self, key, value, nx=False, ex=None):
        self.set_calls += 1
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex:
            self.ttls[key] = ex
        return True

    async def get(self, key):
        return self.store.get(key)

    async def eval(self, script, numkeys, *args):
        key, expected = args[0], args[1]
        owns = self.store.get(key) == expected
        if "del" in script:                       # release
            if owns:
                del self.store[key]
                self.ttls.pop(key, None)
                return 1
            return 0
        if "expire" in script:                    # renew
            if owns:
                self.ttls[key] = int(args[2])
                return 1
            return 0
        return 0

    async def aclose(self):
        self.closed = True


class FakeJob:
    def __init__(self, job_id):
        self.id = job_id
        self.paused = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False


class FakeScheduler:
    def __init__(self, job_ids):
        self._jobs = {j: FakeJob(j) for j in job_ids}
        self.added = []

    def get_jobs(self):
        return list(self._jobs.values())

    def add_job(self, func, trigger=None, id=None, replace_existing=False, **kw):
        self.added.append(id)
        self._jobs[id] = FakeJob(id)
        return self._jobs[id]

    def remove_job(self, job_id):
        if job_id not in self._jobs:
            raise KeyError(job_id)
        del self._jobs[job_id]


def _patch_redis(app, fake):
    """Sustituye el singleton por el doble y devuelve el original."""
    _orig = app._get_app_redis

    async def _fake():
        return fake
    app._get_app_redis = _fake
    return _orig


def _app_src():
    with open(APP_PATH, "r", encoding="utf-8") as f:
        return f.read()


# -- Test 1: constantes coherentes ---------------------------------------------

def test_1_lock_constants_are_sane():
    import app
    assert app.SCHEDULER_LOCK_TTL > 0
    assert app.SCHEDULER_HEARTBEAT_SECONDS < app.SCHEDULER_LOCK_TTL, (
        "El heartbeat debe renovar ANTES de que el TTL expire, si no el lock "
        "caduca con el worker vivo y otro podria levantar un segundo scheduler"
    )
    # Margen real: deben caber al menos 2 heartbeats dentro de un TTL, para que
    # un fallo puntual de red no cueste el liderazgo.
    assert app.SCHEDULER_LOCK_TTL / app.SCHEDULER_HEARTBEAT_SECONDS >= 2, (
        "Un solo heartbeat fallido no puede costar el liderazgo"
    )
    assert app.SCHEDULER_ELECTION_INTERVAL < app.SCHEDULER_LOCK_TTL, (
        "La eleccion debe reintentar mas seguido que el TTL; si no, el relevo "
        "tardaria mas que la propia expiracion del lock"
    )
    print(
        f"  [PASS] ttl={app.SCHEDULER_LOCK_TTL}s heartbeat={app.SCHEDULER_HEARTBEAT_SECONDS}s "
        f"eleccion={app.SCHEDULER_ELECTION_INTERVAL}s"
    )


# -- Test 2: el heartbeat renueva el TTL ---------------------------------------

def test_2_heartbeat_renews_ttl():
    import app
    fake = FakeRedis()
    fake.store[app.SCHEDULER_LOCK_KEY] = str(os.getpid())
    fake.ttls[app.SCHEDULER_LOCK_KEY] = 10  # a punto de expirar

    _held, _sched = app._scheduler_lock_held, app.scheduler
    _orig_redis = _patch_redis(app, fake)
    try:
        app._scheduler_lock_held = True
        app.scheduler = FakeScheduler(["rescue_stalled_leads"])
        asyncio.run(app._renew_scheduler_lock())
    finally:
        app._get_app_redis = _orig_redis
        app._scheduler_lock_held, app.scheduler = _held, _sched

    assert fake.ttls[app.SCHEDULER_LOCK_KEY] == app.SCHEDULER_LOCK_TTL, (
        f"El TTL debio renovarse a {app.SCHEDULER_LOCK_TTL}, quedo en "
        f"{fake.ttls[app.SCHEDULER_LOCK_KEY]}"
    )
    print("  [PASS] el heartbeat renueva el TTL")


# -- Test 3: el singleton NUNCA se cierra --------------------------------------

def test_3_singleton_is_never_closed():
    """Cerrar el pool compartido romperia a los demas jobs que lo usan."""
    import app
    fake = FakeRedis()
    fake.store[app.SCHEDULER_LOCK_KEY] = str(os.getpid())

    _held, _sched = app._scheduler_lock_held, app.scheduler
    _orig_redis = _patch_redis(app, fake)
    try:
        app._scheduler_lock_held = True
        app.scheduler = FakeScheduler(["rescue_stalled_leads"])
        asyncio.run(app._renew_scheduler_lock())
        asyncio.run(app._release_scheduler_lock())
    finally:
        app._get_app_redis = _orig_redis
        app._scheduler_lock_held, app.scheduler = _held, _sched

    assert not fake.closed, (
        "Se cerro el pool singleton: los demas jobs programados lo comparten y "
        "quedarian sin conexion"
    )
    print("  [PASS] el singleton no se cierra tras usarlo")


# -- Test 4: sin liderazgo, el heartbeat no toca nada --------------------------

def test_4_heartbeat_noop_when_not_leader():
    import app
    fake = FakeRedis()
    fake.store[app.SCHEDULER_LOCK_KEY] = "9999"  # de otro worker

    _held = app._scheduler_lock_held
    _orig_redis = _patch_redis(app, fake)
    try:
        app._scheduler_lock_held = False
        asyncio.run(app._renew_scheduler_lock())
    finally:
        app._get_app_redis = _orig_redis
        app._scheduler_lock_held = _held

    assert fake.set_calls == 0, "No debe escribir nada si no es lider"
    assert fake.store[app.SCHEDULER_LOCK_KEY] == "9999", "No debe pisar el lock ajeno"
    print("  [PASS] sin liderazgo el heartbeat es no-op")


# -- Test 5: liberar el lock propio al apagar ----------------------------------

def test_5_release_frees_own_lock():
    import app
    fake = FakeRedis()
    fake.store[app.SCHEDULER_LOCK_KEY] = str(os.getpid())

    _held = app._scheduler_lock_held
    _orig_redis = _patch_redis(app, fake)
    try:
        app._scheduler_lock_held = True
        asyncio.run(app._release_scheduler_lock())
    finally:
        app._get_app_redis = _orig_redis
        app._scheduler_lock_held = _held

    assert app.SCHEDULER_LOCK_KEY not in fake.store, (
        "El lock debe quedar libre para que el worker siguiente arranque ya"
    )
    print("  [PASS] el shutdown libera el lock propio")


# -- Test 6: nunca borrar el lock de otro worker -------------------------------

def test_6_release_never_steals_foreign_lock():
    """Si nuestro TTL expiro y otro tomo el relevo, apagarnos NO debe borrarle el
    lock: se quedaria sin proteccion y podrian correr dos schedulers."""
    import app
    fake = FakeRedis()
    fake.store[app.SCHEDULER_LOCK_KEY] = "otro-worker-4242"

    _held = app._scheduler_lock_held
    _orig_redis = _patch_redis(app, fake)
    try:
        app._scheduler_lock_held = True  # creemos serlo, ya no lo somos
        asyncio.run(app._release_scheduler_lock())
    finally:
        app._get_app_redis = _orig_redis
        app._scheduler_lock_held = _held

    assert fake.store.get(app.SCHEDULER_LOCK_KEY) == "otro-worker-4242", (
        "Borro el lock de otro worker — eso permitiria 2 schedulers simultaneos "
        "y duplicaria recordatorios y followups"
    )
    print("  [PASS] el borrado es condicional: no roba el lock ajeno")


# -- Test 7: el shutdown libera el lock ----------------------------------------

def test_7_shutdown_releases_lock():
    src = _app_src()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and n.name == "shutdown_event"
    )
    called = {
        n.func.id for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_release_scheduler_lock" in called, (
        "El shutdown no libera el lock — el worker siguiente lo encontraria ocupado"
    )
    print("  [PASS] shutdown_event libera el lock")


# -- Test 8: el heartbeat esta registrado como job -----------------------------

def test_8_heartbeat_registered():
    src = _app_src()
    assert 'id="scheduler_lock_heartbeat"' in src, "El heartbeat no esta registrado"
    assert "_renew_scheduler_lock" in src
    print("  [PASS] el heartbeat esta registrado como job")


# -- Test 9: el fallback sin Redis no reclama un lock inexistente --------------

def test_9_no_redis_fallback_is_consistent():
    """Si Redis no responde al arrancar, el scheduler corre igual pero SIN lock:
    marcar lo contrario haria que el shutdown intentara liberar algo inexistente."""
    src = _app_src()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and n.name == "startup_event"
    )
    seg = ast.get_source_segment(src, fn)
    assert "_scheduler_lock_held = False" in seg, (
        "El fallback sin Redis debe dejar _scheduler_lock_held en False"
    )
    print("  [PASS] el fallback sin Redis no reclama un lock que no tiene")


# -- Test 10: compila -----------------------------------------------------------

def test_10_app_compiles():
    ast.parse(_app_src())
    print("  [PASS] app.py compila")


if __name__ == "__main__":
    tests = [
        test_1_lock_constants_are_sane,
        test_2_heartbeat_renews_ttl,
        test_3_singleton_is_never_closed,
        test_4_heartbeat_noop_when_not_leader,
        test_5_release_frees_own_lock,
        test_6_release_never_steals_foreign_lock,
        test_7_shutdown_releases_lock,
        test_8_heartbeat_registered,
        test_9_no_redis_fallback_is_consistent,
        test_10_app_compiles,
    ]
    failed = 0
    for t in tests:
        try:
            print(f"\n{t.__name__}:")
            t()
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {e}")
        except Exception as e:
            failed += 1
            print(f"  [ERROR] {type(e).__name__}: {e}")
    print(f"\n{'=' * 60}")
    print(f"Resultado: {len(tests) - failed}/{len(tests)} tests PASS")
    sys.exit(1 if failed else 0)
