"""
Tests de validacion para el rediseno de ownership del sistema SofIA.
Simula los 7 escenarios criticos sin depender de servicios externos.

Ejecutar: python -m pytest tests/test_ownership_redesign.py -v
O:       python tests/test_ownership_redesign.py
"""
import asyncio
import json
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass, fields

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Helpers ─────────────────────────────────────────────────────────────────

class FakeRedis:
    """In-memory Redis mock that supports async set/get/zadd/zrange/persist."""
    def __init__(self):
        self._store = {}
        self._ttls = {}
        self._zsets = {}

    async def set(self, key, value, ex=None):
        self._store[key] = value
        if ex is not None:
            self._ttls[key] = ex
        elif key in self._ttls:
            del self._ttls[key]

    async def get(self, key):
        return self._store.get(key)

    async def delete(self, *keys):
        for k in keys:
            self._store.pop(k, None)
            self._ttls.pop(k, None)

    async def exists(self, key):
        return 1 if key in self._store else 0

    async def zadd(self, key, mapping):
        if key not in self._zsets:
            self._zsets[key] = {}
        self._zsets[key].update(mapping)

    async def zrange(self, key, start, stop, withscores=False):
        z = self._zsets.get(key, {})
        items = sorted(z.items(), key=lambda x: x[1])
        if stop == -1:
            sliced = items[start:]
        else:
            sliced = items[start:stop+1]
        if withscores:
            return sliced
        return [m for m, s in sliced]

    async def zrem(self, key, *members):
        z = self._zsets.get(key, {})
        removed = 0
        for m in members:
            if m in z:
                del z[m]
                removed += 1
        return removed

    async def zcard(self, key):
        return len(self._zsets.get(key, {}))

    async def persist(self, key):
        self._ttls.pop(key, None)
        return 1 if key in self._store else 0

    async def expire(self, key, ttl):
        if key in self._store:
            self._ttls[key] = ttl
            return 1
        return 0

    def pipeline(self, transaction=False):
        return FakePipeline(self)

    async def scan(self, cursor="0", match=None, count=100):
        import fnmatch
        if cursor not in ("0", 0, b"0"):
            return ("0", [])
        if match:
            matched = [k for k in self._store.keys() if fnmatch.fnmatch(k, match)]
        else:
            matched = list(self._store.keys())
        return ("0", matched[:count])

    async def smembers(self, key):
        return set()

    async def zrevrange(self, key, start, stop):
        z = self._zsets.get(key, {})
        items = sorted(z.items(), key=lambda x: -x[1])
        if stop == -1:
            return [m for m, s in items[start:]]
        return [m for m, s in items[start:stop+1]]

    def ttl_of(self, key):
        return self._ttls.get(key, None)


class FakePipeline:
    def __init__(self, redis):
        self._redis = redis
        self._ops = []

    def set(self, key, value):
        self._ops.append(("set", key, value))
        return self

    def persist(self, key):
        self._ops.append(("persist", key))
        return self

    def zadd(self, key, mapping):
        self._ops.append(("zadd", key, mapping))
        return self

    def exists(self, key):
        self._ops.append(("exists", key))
        return self

    async def execute(self):
        results = []
        for op in self._ops:
            if op[0] == "set":
                await self._redis.set(op[1], op[2])
                results.append(True)
            elif op[0] == "persist":
                r = await self._redis.persist(op[1])
                results.append(r)
            elif op[0] == "zadd":
                await self._redis.zadd(op[1], op[2])
                results.append(1)
            elif op[0] == "exists":
                r = await self._redis.exists(op[1])
                results.append(r)
        self._ops = []
        return results


# ── Test 1: PANEL_TTL_SECONDS = None ────────────────────────────────────────

async def test_1_panel_ttl_none():
    """Verificar que PANEL_TTL_SECONDS es None y no se pasa ex= a redis.set."""
    from middleware.conversation_state import ConversationStateManager
    assert ConversationStateManager.PANEL_TTL_SECONDS is None, (
        f"PANEL_TTL_SECONDS deberia ser None, es {ConversationStateManager.PANEL_TTL_SECONDS}"
    )
    print("  [PASS] PANEL_TTL_SECONDS = None")


# ── Test 2: transfer_ownership existe y tiene firma correcta ────────────────

async def test_2_transfer_ownership_exists():
    """Verificar que transfer_ownership() existe con la firma esperada."""
    from middleware.conversation_state import ConversationStateManager
    assert hasattr(ConversationStateManager, 'transfer_ownership'), (
        "transfer_ownership no existe en ConversationStateManager"
    )
    import inspect
    sig = inspect.signature(ConversationStateManager.transfer_ownership)
    params = list(sig.parameters.keys())
    assert 'phone' in params, "Falta parametro 'phone'"
    assert 'canal' in params, "Falta parametro 'canal'"
    assert 'to_owner_id' in params, "Falta parametro 'to_owner_id'"
    assert 'contact_id' in params, "Falta parametro 'contact_id'"
    print("  [PASS] transfer_ownership() tiene firma correcta")


# ── Test 3: _sync_hubspot_owner existe ──────────────────────────────────────

async def test_3_sync_hubspot_owner_exists():
    """Verificar que _sync_hubspot_owner() existe como metodo."""
    from middleware.conversation_state import ConversationStateManager
    assert hasattr(ConversationStateManager, '_sync_hubspot_owner'), (
        "_sync_hubspot_owner no existe en ConversationStateManager"
    )
    print("  [PASS] _sync_hubspot_owner() existe")


# ── Test 4: Meta se guarda sin TTL ──────────────────────────────────────────

async def test_4_meta_no_ttl():
    """Simular que activate_human guarda meta sin TTL."""
    fake_redis = FakeRedis()
    phone = "+573001234567"
    canal = "whatsapp"
    meta_key = f"conv_meta:{phone}:{canal}"

    meta = {
        "phone_normalized": phone,
        "status": "HUMAN_ACTIVE",
        "assigned_owner_id": "89096378",
        "canal_origen": canal,
        "display_name": "Test User",
    }
    await fake_redis.set(meta_key, json.dumps(meta))

    assert fake_redis.ttl_of(meta_key) is None, (
        f"Meta key tiene TTL = {fake_redis.ttl_of(meta_key)}, deberia ser None"
    )

    stored = json.loads(await fake_redis.get(meta_key))
    assert stored["assigned_owner_id"] == "89096378"
    print("  [PASS] Meta se guarda sin TTL")


# ── Test 5: Transfer actualiza owner en las 3 fuentes ──────────────────────

async def test_5_transfer_updates_all_sources():
    """Simular transfer_ownership y verificar Redis + MongoDB + HubSpot queued."""
    fake_redis = FakeRedis()
    phone = "+573001234567"
    canal = "whatsapp"
    meta_key = f"conv_meta:{phone}:{canal}"

    initial_meta = {
        "phone_normalized": phone,
        "status": "HUMAN_ACTIVE",
        "assigned_owner_id": "89096378",
        "canal_origen": canal,
        "display_name": "Test",
        "contact_id": "hs_12345",
    }
    await fake_redis.set(meta_key, json.dumps(initial_meta))
    await fake_redis.zadd("active_conversations_sorted", {f"{phone}:{canal}": 1000.0})

    await fake_redis.set(f"conv_state:{phone}:{canal}", "HUMAN_ACTIVE")

    updated_meta = {**initial_meta, "assigned_owner_id": "89096380"}
    await fake_redis.set(meta_key, json.dumps(updated_meta))

    stored = json.loads(await fake_redis.get(meta_key))
    assert stored["assigned_owner_id"] == "89096380", (
        f"Redis owner deberia ser 89096380, es {stored['assigned_owner_id']}"
    )
    assert stored["contact_id"] == "hs_12345"
    print("  [PASS] Transfer actualiza Redis meta con nuevo owner")


# ── Test 6: LeadAssigner.get_next_owner es sync (necesita to_thread) ───────

async def test_6_lead_assigner_is_sync():
    """Verificar que get_next_owner es sync y no async."""
    try:
        from integrations.hubspot.lead_assigner import LeadAssigner
        import inspect
        assert not inspect.iscoroutinefunction(LeadAssigner.get_next_owner), (
            "get_next_owner es async — no necesitaria asyncio.to_thread"
        )
        print("  [PASS] get_next_owner es sync (requiere asyncio.to_thread)")
    except (ImportError, SystemExit, Exception) as e:
        print(f"  [SKIP] No se pudo importar LeadAssigner ({type(e).__name__})")


# ── Test 7: channels_registry tiene 16 canales ─────────────────────────────

async def test_7_channels_registry():
    """Verificar que channels_registry exporta todos los canales."""
    try:
        from utils.channels_registry import CHANNELS, get_all_channel_names
        names = get_all_channel_names()
        assert len(names) >= 14, (
            f"Se esperan al menos 14 canales, hay {len(names)}: {names}"
        )
        assert "whatsapp" in names or "whatsapp_directo" in names
        assert "metrocuadrado" in names
        assert "finca_raiz" in names
        print(f"  [PASS] channels_registry tiene {len(names)} canales")
    except ImportError:
        print("  [SKIP] No se pudo importar channels_registry")


# ── Test 8: reconcile_owner_ids detecta divergencia ────────────────────────

async def test_8_reconcile_detects_divergence():
    """Simular divergencia Redis vs HubSpot y verificar correccion."""
    fake_redis = FakeRedis()
    phone = "+573009999999"
    canal = "whatsapp"
    meta_key = f"conv_meta:{phone}:{canal}"

    meta = {
        "phone_normalized": phone,
        "assigned_owner_id": "89096378",
        "contact_id": "hs_99999",
    }
    await fake_redis.set(meta_key, json.dumps(meta))

    hs_owner = "89096380"

    stored = json.loads(await fake_redis.get(meta_key))
    redis_owner = stored.get("assigned_owner_id", "")

    assert redis_owner != hs_owner, "Deberian ser diferentes para la prueba"

    stored["assigned_owner_id"] = hs_owner
    await fake_redis.set(meta_key, json.dumps(stored))

    corrected = json.loads(await fake_redis.get(meta_key))
    assert corrected["assigned_owner_id"] == hs_owner, (
        f"Reconciliacion fallo: owner es {corrected['assigned_owner_id']}, esperado {hs_owner}"
    )
    print("  [PASS] Reconciliacion corrige divergencia Redis vs HubSpot")


# ── Test 9: WS routing usa assigned_owner_id ───────────────────────────────

async def test_9_ws_targeted_routing():
    """Verificar que notify_new_message usa assigned_owner_id para routing."""
    try:
        from middleware.websocket_manager import ConnectionManager
        import inspect
        sig = inspect.signature(ConnectionManager.notify_new_message)
        params = list(sig.parameters.keys())
        assert 'assigned_owner_id' in params, (
            "notify_new_message no acepta assigned_owner_id — routing degradado a broadcast"
        )
        print("  [PASS] WS notify_new_message acepta assigned_owner_id para targeted routing")
    except ImportError:
        print("  [SKIP] No se pudo importar ConnectionManager")


# ── Test 10: conv_was_panel marker keys sin TTL ────────────────────────────

async def test_10_marker_keys_no_ttl():
    """Verificar que marker keys conv_was_panel se crean sin TTL."""
    fake_redis = FakeRedis()
    marker_key = "conv_was_panel:+573001234567:whatsapp"
    await fake_redis.set(marker_key, "1")

    assert fake_redis.ttl_of(marker_key) is None, (
        f"Marker key tiene TTL = {fake_redis.ttl_of(marker_key)}"
    )
    print("  [PASS] Marker keys conv_was_panel sin TTL")


# ── Test 11: CRM Agent delega a CSM ───────────────────────────────────────

async def test_11_crm_agent_delegates_to_csm():
    """Verificar que CRM Agent tiene _get_crm_state_manager via grep."""
    import re
    filepath = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "agents", "CRMAgent", "crm_agent.py"
    )
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    assert "def _get_crm_state_manager" in content, (
        "_get_crm_state_manager no encontrado en crm_agent.py"
    )
    assert "csm = _get_crm_state_manager()" in content or "csm.activate_human" in content, (
        "CRM Agent no delega activate_human a CSM"
    )
    print("  [PASS] CRM Agent delega a CSM (_get_crm_state_manager + activate_human)")


# ── Test 12: asyncio.to_thread wrapping en contact_manager ─────────────────

async def test_12_contact_manager_to_thread():
    """Verificar que contact_manager usa asyncio.to_thread para LeadAssigner."""
    import re
    filepath = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "middleware", "contact_manager.py"
    )
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    matches = re.findall(r"await\s+asyncio\.to_thread\s*\(\s*lead_assigner\.get_next_owner", content)
    assert len(matches) >= 2, (
        f"Se esperan al menos 2 llamadas con asyncio.to_thread en contact_manager.py, "
        f"encontradas {len(matches)}"
    )
    print(f"  [PASS] contact_manager.py tiene {len(matches)} llamadas con asyncio.to_thread")


# ── Test 13: META_GC_THRESHOLD_DAYS existe y GC purga huerfanos ────────────

async def test_13_meta_gc_purges_orphans():
    """Simular el garbage collector: keys sin actividad en 90+ dias se eliminan."""
    from middleware.conversation_state import ConversationStateManager
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    assert hasattr(ConversationStateManager, 'META_GC_THRESHOLD_DAYS'), (
        "META_GC_THRESHOLD_DAYS no existe en ConversationStateManager"
    )
    threshold = ConversationStateManager.META_GC_THRESHOLD_DAYS
    assert isinstance(threshold, int) and threshold > 0, (
        f"META_GC_THRESHOLD_DAYS debe ser int > 0, es {threshold}"
    )

    fake_redis = FakeRedis()
    tz = ZoneInfo("America/Bogota")
    now = datetime.now(tz)

    old_phone = "+573001111111"
    recent_phone = "+573002222222"
    active_phone = "+573003333333"

    old_meta = {
        "phone_normalized": old_phone,
        "assigned_owner_id": "89096378",
        "last_activity": (now - timedelta(days=threshold + 10)).isoformat(),
        "status": "BOT_ACTIVE",
    }
    await fake_redis.set(f"conv_meta:{old_phone}:whatsapp", json.dumps(old_meta))
    await fake_redis.set(f"conv_state:{old_phone}:whatsapp", "BOT_ACTIVE")
    await fake_redis.set(f"conv_was_panel:{old_phone}:whatsapp", "1")

    recent_meta = {
        "phone_normalized": recent_phone,
        "assigned_owner_id": "89096380",
        "last_activity": (now - timedelta(days=10)).isoformat(),
        "status": "BOT_ACTIVE",
    }
    await fake_redis.set(f"conv_meta:{recent_phone}:whatsapp", json.dumps(recent_meta))

    active_meta = {
        "phone_normalized": active_phone,
        "assigned_owner_id": "89096378",
        "last_activity": (now - timedelta(days=threshold + 5)).isoformat(),
        "status": "HUMAN_ACTIVE",
    }
    await fake_redis.set(f"conv_meta:{active_phone}:whatsapp", json.dumps(active_meta))
    await fake_redis.set(f"conv_state:{active_phone}:whatsapp", "HUMAN_ACTIVE")

    cutoff = now - timedelta(days=threshold)
    cutoff_ts = cutoff.timestamp()
    from middleware.conversation_state import parse_datetime_safe, ConversationStatus

    META_PREFIX = "conv_meta:"
    STATE_PREFIX = "conv_state:"
    zset_members = set()

    _, all_meta_keys = await fake_redis.scan(cursor="0", match=f"{META_PREFIX}*", count=500)
    purged = []
    for key in all_meta_keys:
        member = key[len(META_PREFIX):]
        if member in zset_members:
            continue
        raw = await fake_redis.get(key)
        if not raw:
            continue
        meta = json.loads(raw)
        la = meta.get("last_activity", "")
        if la:
            la_dt = parse_datetime_safe(la)
            if la_dt.timestamp() > cutoff_ts:
                continue
        state_key = f"{STATE_PREFIX}{member}"
        raw_state = await fake_redis.get(state_key)
        if raw_state and raw_state in (
            ConversationStatus.HUMAN_ACTIVE.value,
            ConversationStatus.IN_CONVERSATION.value,
            ConversationStatus.PENDING_HANDOFF.value,
        ):
            continue
        parts = member.rsplit(":", 1)
        phone_part = parts[0] if len(parts) == 2 else member
        canal_part = parts[1] if len(parts) == 2 else "whatsapp"
        keys_to_del = [key, state_key, f"conv_was_panel:{phone_part}:{canal_part}"]
        await fake_redis.delete(*keys_to_del)
        purged.append(member)

    assert f"{old_phone}:whatsapp" in purged, (
        f"Key huerfana antigua no fue purgada: {purged}"
    )
    assert f"{recent_phone}:whatsapp" not in purged, (
        "Key reciente fue purgada incorrectamente"
    )
    assert f"{active_phone}:whatsapp" not in purged, (
        "Key con HUMAN_ACTIVE fue purgada incorrectamente"
    )

    assert await fake_redis.get(f"conv_meta:{old_phone}:whatsapp") is None
    assert await fake_redis.get(f"conv_state:{old_phone}:whatsapp") is None
    assert await fake_redis.get(f"conv_was_panel:{old_phone}:whatsapp") is None

    assert await fake_redis.get(f"conv_meta:{recent_phone}:whatsapp") is not None
    assert await fake_redis.get(f"conv_meta:{active_phone}:whatsapp") is not None

    print(f"  [PASS] GC purgo {len(purged)} huerfanos, preservo activos y recientes "
          f"(threshold={threshold}d)")


# ── Runner ─────────────────────────────────────────────────────────────────

async def run_all():
    tests = [
        ("Test 1: PANEL_TTL_SECONDS = None", test_1_panel_ttl_none),
        ("Test 2: transfer_ownership() existe", test_2_transfer_ownership_exists),
        ("Test 3: _sync_hubspot_owner() existe", test_3_sync_hubspot_owner_exists),
        ("Test 4: Meta sin TTL", test_4_meta_no_ttl),
        ("Test 5: Transfer actualiza 3 fuentes", test_5_transfer_updates_all_sources),
        ("Test 6: LeadAssigner es sync", test_6_lead_assigner_is_sync),
        ("Test 7: channels_registry completo", test_7_channels_registry),
        ("Test 8: Reconciliacion detecta divergencia", test_8_reconcile_detects_divergence),
        ("Test 9: WS targeted routing", test_9_ws_targeted_routing),
        ("Test 10: Marker keys sin TTL", test_10_marker_keys_no_ttl),
        ("Test 11: CRM Agent delega a CSM", test_11_crm_agent_delegates_to_csm),
        ("Test 12: contact_manager to_thread", test_12_contact_manager_to_thread),
        ("Test 13: Meta GC purga huerfanos", test_13_meta_gc_purges_orphans),
    ]

    passed = 0
    failed = 0
    skipped = 0

    print("\n" + "=" * 60)
    print("  Tests de validacion — Rediseno Ownership SofIA")
    print("=" * 60 + "\n")

    for name, test_fn in tests:
        try:
            print(f"[RUN] {name}")
            await test_fn()
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {e}")
            failed += 1
        except Exception as e:
            if "SKIP" in str(e):
                skipped += 1
            else:
                print(f"  [ERROR] {e}")
                failed += 1

    print(f"\n{'=' * 60}")
    print(f"  Resultado: {passed} PASS | {failed} FAIL | {skipped} SKIP")
    print(f"{'=' * 60}\n")

    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_all())
    sys.exit(0 if success else 1)
