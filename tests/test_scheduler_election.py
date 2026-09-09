"""
Tests de la eleccion continua del scheduler y del uso del singleton Redis.

Dos defectos del PR #15, ambos observados en produccion el 10-ago-2026:

  A) El heartbeat creaba un pool Redis nuevo cada 120s con from_url — ~30
     pools/hora. Es literalmente el patron que _get_app_redis() existe para
     evitar; su propio comentario lo documenta como causa del OOM SIGKILL ~2h15m.
     CLAUDE.md regla 1 lo prohibe explicitamente.

  B) El liderazgo se decidia UNA vez al arrancar. Railway levanta el worker nuevo
     ANTES de matar el viejo, y el viejo seguia renovando su lock, asi que el
     nuevo se rendia tras 3 intentos (6s) y quedaba sin ninguno de los 13 jobs
     de forma permanente. Medido:
         20:30:57  worker viejo renueva el lock
         20:32:17  worker nuevo arranca
         20:32:29  "omite scheduler tras 3 intentos"   <- se rinde
         20:32:41  worker viejo muere y libera         <- 12s tarde
     Paso dos veces y cada una costo un redeploy manual.

Ejecutar: python tests/test_scheduler_election.py
O:        python -m pytest tests/test_scheduler_election.py -v
"""
import ast
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PATH = os.path.join(ROOT, "app.py")


def _app_src():
    with open(APP_PATH, "r", encoding="utf-8") as f:
        return f.read()


class FakeRedis:
    """Redis en memoria con SET NX/EX y los dos scripts Lua del lock."""

    def __init__(self):
        self.store = {}
        self.ttls = {}
        self.from_url_calls = 0

    async def set(self, key, value, nx=False, ex=None):
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
        if "del" in script:                      # release
            if owns:
                del self.store[key]
                self.ttls.pop(key, None)
                return 1
            return 0
        if "expire" in script:                   # renew
            if owns:
                self.ttls[key] = int(args[2])
                return 1
            return 0
        return 0


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


# -- Test 1: ningun pool Redis nuevo en el codigo del lock --------------------

def test_1_no_adhoc_pools_in_lock_code():
    """CLAUDE.md regla 1: nunca instanciar Redis fuera de los singletons."""
    src = _app_src()
    tree = ast.parse(src)
    for fn_name in ("_renew_scheduler_lock", "_release_scheduler_lock",
                    "_try_become_scheduler_leader"):
        fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.AsyncFunctionDef) and n.name == fn_name),
            None
        )
        assert fn is not None, f"No existe {fn_name}"

        # Se inspeccionan las llamadas reales, no el texto: los comentarios que
        # explican por que NO se usa from_url contienen esa palabra.
        called = {
            n.func.attr for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        called |= {
            n.func.id for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "from_url" not in called, (
            f"{fn_name} crea un pool Redis propio. El heartbeat corre cada 120s: "
            f"son ~30 pools/hora, el patron que causo el OOM SIGKILL"
        )
        assert "_get_app_redis" in called, f"{fn_name} no usa el singleton"
    print("  [PASS] los 3 puntos del lock usan _get_app_redis(), sin pools ad-hoc")


# -- Test 2: la adquisicion del arranque tambien usa el singleton -------------

def test_2_startup_acquisition_uses_singleton():
    src = _app_src()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and n.name == "startup_event"
    )
    seg = ast.get_source_segment(src, fn)
    # El lock del RAG es pre-existente y esta fuera del alcance; se comprueba
    # que la adquisicion del scheduler concretamente use el singleton.
    assert "_sched_redis = await _get_app_redis()" in seg, (
        "La adquisicion del lock del scheduler no usa el singleton"
    )
    print("  [PASS] la adquisicion del arranque usa el singleton")


# -- Test 3: el heartbeat no renueva si perdio el lock ------------------------

def test_3_heartbeat_does_not_steal_lock():
    """Renovar a ciegas reclamaria un lock que ya es de otro worker: dos
    schedulers activos y mensajes duplicados a clientes reales."""
    import app

    fake = FakeRedis()
    fake.store[app.SCHEDULER_LOCK_KEY] = "otro-worker-9999"
    fake.ttls[app.SCHEDULER_LOCK_KEY] = 300
    sched = FakeScheduler(["rescue_stalled_leads", "memory_watchdog"])

    _orig = (app._scheduler_lock_held, app.scheduler, app._get_app_redis)
    try:
        app._scheduler_lock_held = True      # creemos serlo, ya no lo somos
        app.scheduler = sched

        async def _fake_redis():
            return fake
        app._get_app_redis = _fake_redis
        asyncio.run(app._renew_scheduler_lock())
    finally:
        app._scheduler_lock_held, app.scheduler, app._get_app_redis = _orig

    assert fake.store[app.SCHEDULER_LOCK_KEY] == "otro-worker-9999", (
        "El heartbeat piso el lock de otro worker"
    )
    print("  [PASS] el heartbeat no reclama un lock ajeno")


# -- Test 4: al perder el lock, cede el liderazgo ------------------------------

def test_4_heartbeat_yields_on_lost_lock():
    import app

    fake = FakeRedis()
    fake.store[app.SCHEDULER_LOCK_KEY] = "otro-worker-9999"
    sched = FakeScheduler(["rescue_stalled_leads", "memory_watchdog", "conv_timeouts"])

    _orig = (app._scheduler_lock_held, app.scheduler, app._get_app_redis)
    try:
        app._scheduler_lock_held = True
        app.scheduler = sched

        async def _fake_redis():
            return fake
        app._get_app_redis = _fake_redis
        asyncio.run(app._renew_scheduler_lock())
        held_after = app._scheduler_lock_held
    finally:
        app._scheduler_lock_held, app.scheduler, app._get_app_redis = _orig

    assert held_after is False, "Debio marcarse como no-lider tras perder el lock"
    assert all(j.paused for j in sched.get_jobs()
               if j.id != app.SCHEDULER_ELECTION_JOB_ID), (
        "Los jobs deben pausarse: seguir ejecutandolos duplicaria el trabajo del "
        "worker que ahora es lider"
    )
    assert app.SCHEDULER_ELECTION_JOB_ID in sched.added, (
        "Sin re-armar la eleccion, el worker quedaria pausado para siempre"
    )
    print("  [PASS] al perder el lock pausa los jobs y re-arma la eleccion")


# -- Test 5: el heartbeat renueva cuando SI es nuestro ------------------------

def test_5_heartbeat_renews_when_owner():
    import app

    fake = FakeRedis()
    fake.store[app.SCHEDULER_LOCK_KEY] = str(os.getpid())
    fake.ttls[app.SCHEDULER_LOCK_KEY] = 10          # a punto de expirar
    sched = FakeScheduler(["rescue_stalled_leads"])

    _orig = (app._scheduler_lock_held, app.scheduler, app._get_app_redis)
    try:
        app._scheduler_lock_held = True
        app.scheduler = sched

        async def _fake_redis():
            return fake
        app._get_app_redis = _fake_redis
        asyncio.run(app._renew_scheduler_lock())
        held_after = app._scheduler_lock_held
    finally:
        app._scheduler_lock_held, app.scheduler, app._get_app_redis = _orig

    assert fake.ttls[app.SCHEDULER_LOCK_KEY] == app.SCHEDULER_LOCK_TTL, (
        f"El TTL debio renovarse a {app.SCHEDULER_LOCK_TTL}, quedo en "
        f"{fake.ttls[app.SCHEDULER_LOCK_KEY]}"
    )
    assert held_after is True, "No debia ceder el liderazgo siendo el dueño"
    print("  [PASS] renueva el TTL cuando el lock sigue siendo nuestro")


# -- Test 6: la eleccion gana y reanuda los jobs -------------------------------

def test_6_election_resumes_jobs_on_win():
    """El caso del rollover: el lider anterior murio y este worker toma el relevo."""
    import app

    fake = FakeRedis()                       # lock libre: el anterior ya murio
    sched = FakeScheduler(["rescue_stalled_leads", "memory_watchdog",
                           app.SCHEDULER_ELECTION_JOB_ID])
    for j in sched.get_jobs():
        if j.id != app.SCHEDULER_ELECTION_JOB_ID:
            j.pause()

    _orig = (app._scheduler_lock_held, app.scheduler, app._get_app_redis)
    try:
        app._scheduler_lock_held = False
        app.scheduler = sched

        async def _fake_redis():
            return fake
        app._get_app_redis = _fake_redis
        asyncio.run(app._try_become_scheduler_leader())
        held_after = app._scheduler_lock_held
    finally:
        app._scheduler_lock_held, app.scheduler, app._get_app_redis = _orig

    assert held_after is True, "Con el lock libre debio ganar el liderazgo"
    assert all(not j.paused for j in sched.get_jobs()), "Los jobs debieron reanudarse"
    assert app.SCHEDULER_ELECTION_JOB_ID not in [j.id for j in sched.get_jobs()], (
        "El job de eleccion debe eliminarse tras ganar; si no, seguiria corriendo en vano"
    )
    print("  [PASS] al ganar reanuda los jobs y se elimina a si mismo")


# -- Test 7: la eleccion no hace nada si el lock sigue ocupado ------------------

def test_7_election_noop_while_lock_busy():
    import app

    fake = FakeRedis()
    fake.store[app.SCHEDULER_LOCK_KEY] = "lider-vivo-1234"
    sched = FakeScheduler(["rescue_stalled_leads", app.SCHEDULER_ELECTION_JOB_ID])
    for j in sched.get_jobs():
        if j.id != app.SCHEDULER_ELECTION_JOB_ID:
            j.pause()

    _orig = (app._scheduler_lock_held, app.scheduler, app._get_app_redis)
    try:
        app._scheduler_lock_held = False
        app.scheduler = sched

        async def _fake_redis():
            return fake
        app._get_app_redis = _fake_redis
        asyncio.run(app._try_become_scheduler_leader())
        held_after = app._scheduler_lock_held
    finally:
        app._scheduler_lock_held, app.scheduler, app._get_app_redis = _orig

    assert held_after is False, "No debio ganar con el lock ocupado"
    assert fake.store[app.SCHEDULER_LOCK_KEY] == "lider-vivo-1234", "Piso el lock ajeno"
    assert any(j.paused for j in sched.get_jobs()
               if j.id != app.SCHEDULER_ELECTION_JOB_ID), (
        "Los jobs deben seguir pausados mientras otro sea lider"
    )
    print("  [PASS] con el lock ocupado no gana y deja los jobs pausados")


# -- Test 8: los jobs se registran siempre ------------------------------------

def test_8_jobs_registered_unconditionally():
    """Si solo se registran siendo lider, perder la eleccion de arranque deja al
    worker sin jobs para siempre — el bug original."""
    src = _app_src()
    assert "_register_jobs = True" in src, (
        "Los jobs deben registrarse siempre, no solo si somos lideres"
    )
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and n.name == "startup_event"
    )
    # No debe quedar el condicional viejo gobernando el registro de los 13 jobs.
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name):
            if node.test.id == "_is_scheduler_leader" and (node.end_lineno - node.lineno) > 50:
                raise AssertionError(
                    "El bloque de registro de jobs sigue condicionado a "
                    "_is_scheduler_leader"
                )
    print("  [PASS] los 13 jobs se registran siempre")


# -- Test 9: sin liderazgo, se pausan y se arma la eleccion -------------------

def test_9_startup_pauses_and_arms_election():
    src = _app_src()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and n.name == "startup_event"
    )
    seg = ast.get_source_segment(src, fn)
    assert "_job.pause()" in seg, "El arranque sin liderazgo no pausa los jobs"
    assert "SCHEDULER_ELECTION_JOB_ID" in seg, "No se arma el job de eleccion"
    assert "_try_become_scheduler_leader" in seg, "La eleccion no esta conectada"
    print("  [PASS] sin liderazgo pausa los jobs y arma la eleccion")


# -- Test 10: el arranque ya no bloquea con sleeps ----------------------------

def test_10_no_blocking_retries_at_startup():
    """Los 3 reintentos x 2s no cubrian el rollover (~24s) y alargaban el arranque."""
    src = _app_src()
    assert "SCHEDULER_LOCK_RETRIES" not in src, (
        "Quedan reintentos bloqueantes: la eleccion en segundo plano los sustituye"
    )
    assert "SCHEDULER_LOCK_RETRY_DELAY" not in src, "Queda la constante del delay"
    print("  [PASS] sin reintentos bloqueantes en el arranque")


# -- Test 11: el heartbeat se registra aunque no seamos lider ------------------

def test_11_heartbeat_registered_even_without_leadership():
    """Si solo se registrara siendo lider, ganar por reintento dejaria el lock
    sin renovar y caducaria a los 300s."""
    src = _app_src()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and n.name == "startup_event"
    )
    seg = ast.get_source_segment(src, fn)
    idx_hb = seg.find('id="scheduler_lock_heartbeat"')
    assert idx_hb != -1, "No se registra el heartbeat"
    # El registro no debe estar gobernado por un guard de propiedad del lock.
    before = seg[:idx_hb]
    assert "if _scheduler_lock_held:" not in before.split("_register_jobs")[-1], (
        "El heartbeat sigue condicionado a tener el lock en el momento del arranque"
    )
    print("  [PASS] el heartbeat se registra siempre (es no-op sin lock)")


# -- Test 12: compila ----------------------------------------------------------

def test_12_app_compiles():
    ast.parse(_app_src())
    print("  [PASS] app.py compila")


if __name__ == "__main__":
    tests = [
        test_1_no_adhoc_pools_in_lock_code,
        test_2_startup_acquisition_uses_singleton,
        test_3_heartbeat_does_not_steal_lock,
        test_4_heartbeat_yields_on_lost_lock,
        test_5_heartbeat_renews_when_owner,
        test_6_election_resumes_jobs_on_win,
        test_7_election_noop_while_lock_busy,
        test_8_jobs_registered_unconditionally,
        test_9_startup_pauses_and_arms_election,
        test_10_no_blocking_retries_at_startup,
        test_11_heartbeat_registered_even_without_leadership,
        test_12_app_compiles,
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
