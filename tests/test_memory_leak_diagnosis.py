"""
Tests de diagnostico y QA para memory leaks en Redis pools.

DIAGNOSTICO: El sistema crea 17+ pools Redis separados. Tres de ellos
se crean repetidamente en cada ejecucion de jobs (cada 60s), causando
SIGKILL cada ~2h15m por OOM.

LEAKS IDENTIFICADOS:
  1. check_scheduled_messages() — app.py — pool nuevo cada 60s
  2. check_and_send_followups()  — app.py — pool nuevo por ejecucion
  3. RedisChatMessageHistory     — sofia_brain.py — pool sync sin max_connections

Ejecutar: python tests/test_memory_leak_diagnosis.py
"""
import sys
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _read(relpath):
    with open(os.path.join(ROOT, relpath), 'r', encoding='utf-8') as f:
        return f.read()


# ═══════════════════════════════════════════════════════════════
# FASE 1: Tests de diagnostico (analisis estatico, sin imports)
# ═══════════════════════════════════════════════════════════════

def test_01_count_redis_from_url_calls():
    """Cuenta cuantas veces se llama from_url() en todo el proyecto."""
    locations = []
    for dirpath, _, filenames in os.walk(ROOT):
        if any(skip in dirpath for skip in ['.git', 'node_modules', '__pycache__', '.claude']):
            continue
        for fname in filenames:
            if not fname.endswith('.py'):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    for i, line in enumerate(f, 1):
                        if 'from_url(' in line and '#' not in line.split('from_url')[0]:
                            rel = os.path.relpath(fpath, ROOT)
                            locations.append(f"{rel}:{i}")
            except Exception:
                continue

    print(f"  [METRIC] Total from_url() en codebase: {len(locations)}")
    for loc in locations:
        print(f"    - {loc}")
    assert len(locations) > 0
    print(f"  [PASS] {len(locations)} pools Redis identificados")
    return len(locations)


def test_02_leak1_scheduled_messages_creates_pool():
    """LEAK 1: check_scheduled_messages crea pool Redis en scope local."""
    content = _read("app.py")

    # Encontrar la funcion y su contenido
    match = re.search(
        r'(async\s+)?def check_scheduled_messages\(.*?\n(.*?)(?=\n\s{0,8}(async\s+)?def |\nclass |\Z)',
        content, re.DOTALL
    )
    assert match, "check_scheduled_messages no encontrada en app.py"
    fn_body = match.group(0)

    has_from_url = 'from_url(' in fn_body
    uses_singleton = '_get_redis_client' in fn_body

    if has_from_url and not uses_singleton:
        print("  [LEAK CONFIRMADO] check_scheduled_messages() crea pool local cada 60s")
        print("  [METRIC] Impacto: ~60 pools/hora, ~135 en 2h15m")
        return True
    else:
        print("  [PASS] check_scheduled_messages() usa singleton (fix aplicado)")
        return False


def test_03_leak2_followup_creates_pool():
    """LEAK 2: check_and_send_followups crea pool Redis en scope local."""
    content = _read("app.py")

    match = re.search(
        r'async def check_and_send_followups\(.*?\n(.*?)(?=\nasync def |\ndef |\nclass |\Z)',
        content, re.DOTALL
    )
    assert match, "check_and_send_followups no encontrada en app.py"
    fn_body = match.group(0)

    has_from_url = 'from_url(' in fn_body
    uses_singleton = '_get_redis_client' in fn_body

    if has_from_url and not uses_singleton:
        print("  [LEAK CONFIRMADO] check_and_send_followups() crea pool local")
        return True
    else:
        print("  [PASS] check_and_send_followups() usa singleton (fix aplicado)")
        return False


def test_04_leak3_langchain_history_no_cap():
    """LEAK 3: RedisChatMessageHistory se crea sin max_connections."""
    content = _read("middleware/sofia_brain.py")

    has_history_create = 'RedisChatMessageHistory(' in content
    has_shared_client = '_shared_redis_client' in content
    has_client_swap = 'history.redis_client = self._shared_redis_client' in content

    if has_shared_client and has_client_swap:
        print("  [PASS] RedisChatMessageHistory usa shared client (max_connections bounded)")
        return False
    elif has_history_create:
        print("  [LEAK CONFIRMADO] RedisChatMessageHistory sin max_connections")
        print("  [METRIC] Cada instancia crea pool sync ilimitado (hasta 100 cacheadas)")
        return True
    else:
        print("  [PASS] RedisChatMessageHistory tiene max_connections")
        return False


def test_04b_shared_client_must_not_decode():
    """
    REGRESION: el shared client NO debe usar decode_responses=True.
    RedisChatMessageHistory.messages hace m.decode("utf-8") sobre cada item;
    si Redis devuelve str, todo el historial de Sofia rompe.
    """
    content = _read("middleware/sofia_brain.py")

    idx = content.index('self._shared_redis_client = ')
    block = content[idx:idx + 300]
    block = block[:block.index(')') + 1]

    assert 'decode_responses=True' not in block, (
        "shared client con decode_responses=True — rompe history.messages"
    )
    assert 'max_connections' in block, "shared client sin max_connections"

    print("  [PASS] shared client con bytes (decode_responses=False) y max_connections")


def test_05_brain_cache_eviction_exists():
    """Verifica que el cache de historial tiene eviction."""
    content = _read("middleware/sofia_brain.py")

    has_max = '_history_cache_max' in content
    has_disconnect = 'connection_pool.disconnect()' in content

    match = re.search(r'_history_cache_max\s*=\s*(\d+)', content)
    cache_size = int(match.group(1)) if match else 0

    print(f"  [METRIC] Cache max: {cache_size} sesiones")
    print(f"  [METRIC] Eviction disconnect(): {'SI' if has_disconnect else 'NO'}")

    assert has_max, "Sin limite de cache"
    print("  [PASS] Eviction existe (cleanup sync en async es limitado)")
    return cache_size


def test_06_pools_without_max_connections():
    """Identifica pools Redis sin max_connections explicito."""
    uncapped = []
    capped = []

    for dirpath, _, filenames in os.walk(ROOT):
        if any(skip in dirpath for skip in ['.git', 'node_modules', '__pycache__', '.claude', 'tests']):
            continue
        for fname in filenames:
            if not fname.endswith('.py'):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                for i, line in enumerate(lines):
                    if 'from_url(' in line and '#' not in line.split('from_url')[0]:
                        context = ''.join(lines[max(0, i-1):min(len(lines), i+5)])
                        rel = os.path.relpath(fpath, ROOT)
                        if 'max_connections' in context:
                            capped.append(f"{rel}:{i+1}")
                        else:
                            uncapped.append(f"{rel}:{i+1}")
            except Exception:
                continue

    print(f"  [METRIC] Pools CON max_connections: {len(capped)}")
    for loc in capped:
        print(f"    [OK] {loc}")
    print(f"  [METRIC] Pools SIN max_connections: {len(uncapped)}")
    for loc in uncapped:
        print(f"    [RISK] {loc}")

    print(f"  [PASS] {len(uncapped)} pools sin cap identificados")
    return len(uncapped)


# ═══════════════════════════════════════════════════════════════
# FASE 2: Tests funcionales (analisis estatico)
# ═══════════════════════════════════════════════════════════════

def test_07_singleton_redis_pool_exists():
    """Verifica que _get_redis_client es singleton reutilizable."""
    content = _read("middleware/outbound_panel.py")

    assert '_redis_pool' in content, "_redis_pool no encontrado"
    assert 'if _redis_pool is None' in content, "Patron singleton no encontrado"
    assert 'max_connections=20' in content, "max_connections=20 no encontrado"

    print("  [PASS] _get_redis_client() es singleton con max_connections=20")


def test_08_csm_singleton_exists():
    """Verifica que ConversationStateManager tiene singleton."""
    content = _read("app.py")

    assert '_global_state_manager' in content, "_global_state_manager no encontrado"
    assert 'def get_state_manager' in content, "get_state_manager() no encontrado"

    print("  [PASS] ConversationStateManager singleton en app.py")


def test_09_channel_redistribution_intact():
    """Verifica redistribucion de canales sin importar modulos pesados."""
    content = _read("utils/channels_registry.py")

    jubeny_channels = re.findall(r'"owner_id":\s*"89096378"', content)
    luisa_channels = re.findall(r'"owner_id":\s*"89096380"', content)

    assert len(jubeny_channels) == 14, f"Jubeny tiene {len(jubeny_channels)} canales, esperado 14"
    assert len(luisa_channels) == 2, f"Luisa tiene {len(luisa_channels)} canales, esperado 2"

    print(f"  [PASS] Redistribucion intacta: Jubeny={len(jubeny_channels)}, Luisa={len(luisa_channels)}")


def test_10_watchdog_config():
    """Verifica la configuracion del memory watchdog."""
    content = _read("app.py")

    match = re.search(r'WORKER_MEM_LIMIT_MB.*?(\d+)', content)
    limit_mb = int(match.group(1)) if match else 0

    has_sigterm = 'SIGTERM' in content
    has_interval = 'minutes=2' in content

    print(f"  [METRIC] Watchdog limit: {limit_mb} MB")
    print(f"  [METRIC] Sends SIGTERM: {'SI' if has_sigterm else 'NO'}")
    print(f"  [METRIC] Check interval: {'2 min' if has_interval else 'desconocido'}")

    if limit_mb >= 4096:
        print("  [WARN] Watchdog en 4096 MB — Railway SIGKILL ocurre antes")
    else:
        print(f"  [PASS] Watchdog en {limit_mb} MB")

    return limit_mb


# ═══════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════

def run_all():
    tests = [
        ("DIAG 1: Inventario from_url()", test_01_count_redis_from_url_calls),
        ("DIAG 2: LEAK check_scheduled_messages", test_02_leak1_scheduled_messages_creates_pool),
        ("DIAG 3: LEAK check_and_send_followups", test_03_leak2_followup_creates_pool),
        ("DIAG 4: LEAK RedisChatMessageHistory", test_04_leak3_langchain_history_no_cap),
        ("REGR 4b: shared client sin decode_responses", test_04b_shared_client_must_not_decode),
        ("DIAG 5: SofiaBrain cache eviction", test_05_brain_cache_eviction_exists),
        ("DIAG 6: Pools sin max_connections", test_06_pools_without_max_connections),
        ("FUNC 7: Singleton _get_redis_client", test_07_singleton_redis_pool_exists),
        ("FUNC 8: Singleton CSM", test_08_csm_singleton_exists),
        ("FUNC 9: Redistribucion canales", test_09_channel_redistribution_intact),
        ("FUNC 10: Watchdog config", test_10_watchdog_config),
    ]

    passed = 0
    failed = 0
    leaks_confirmed = 0

    print("\n" + "=" * 70)
    print("  QA Memory Leak Diagnosis — SofIA (pre-fix baseline)")
    print("=" * 70 + "\n")

    for name, test_fn in tests:
        try:
            print(f"[RUN] {name}")
            result = test_fn()
            passed += 1
            if isinstance(result, bool) and result:
                leaks_confirmed += 1
        except AssertionError as e:
            print(f"  [FAIL] {e}")
            failed += 1
        except Exception as e:
            print(f"  [ERROR] {type(e).__name__}: {e}")
            failed += 1
        print()

    print("=" * 70)
    print(f"  Resultado: {passed} PASS | {failed} FAIL")
    print(f"  Leaks confirmados: {leaks_confirmed}")
    print(f"  Pools sin max_connections: (ver DIAG 6)")
    print("=" * 70 + "\n")

    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
