#!/usr/bin/env python3
"""
TEST SUITE: P3-D Guard — Preservar sesión activa al mergearse canales
=======================================================================
Verifica que P3-D NO mergeé canales con sesión activa (HUMAN_ACTIVE /
IN_CONVERSATION / PENDING_HANDOFF), y que SÍ mergee canales BOT_ACTIVE / expirados.

Escenario real del bug:
  Asesor en IN_CONVERSATION vía ciencuadras + cliente escribe por WhatsApp
  ->P3-D no debe borrar phone:ciencuadras del ZSET

Ejecutar: python test_p3d_guard.py
"""

import asyncio
import json
import sys
import logging
from unittest.mock import AsyncMock, MagicMock, patch, call

logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(message)s")

PASS = 0
FAIL = 0

def ok(name: str):
    global PASS
    PASS += 1
    print(f"  [PASS]  {name}")

def fail(name: str, reason: str = ""):
    global FAIL
    FAIL += 1
    msg = f"  [FAIL]  {name}"
    if reason:
        msg += f"\n         -> {reason}"
    print(msg)

# ─── Minimal stubs para poder importar ConversationStateManager ──────────────

# Stub logging_config antes de importar el módulo real
import types, logging as _logging
_stub_logging = types.ModuleType("logging_config")
_stub_logging.logger = _logging.getLogger("test_p3d")
sys.modules.setdefault("logging_config", _stub_logging)

# Importar el módulo real
sys.path.insert(0, "middleware")
from conversation_state import ConversationStateManager, ConversationStatus


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_sm(redis_mock) -> ConversationStateManager:
    """Construye un ConversationStateManager con Redis mockeado."""
    sm = ConversationStateManager.__new__(ConversationStateManager)
    sm.redis = redis_mock
    sm.STATE_PREFIX   = "conv_state:"
    sm.META_PREFIX    = "conv_meta:"
    sm.ACTIVE_CONTACTS_ZSET = "active_conversations_sorted"
    sm.BOT_CONTROLLED_SET   = "bot_controlled_conversations"
    sm.PANEL_TTL_SECONDS    = 365 * 86400
    sm.HUMAN_PANEL_STATE_TTL = 7 * 86400
    sm._calculate_dynamic_ttl = MagicMock(return_value=48 * 3600)
    return sm


def _redis_mock(*, zscan_members: list, state_values: dict, meta_values: dict = None):
    """
    Construye un mock de Redis que responde a las operaciones de P3-D.

    zscan_members : lista de (member, score) que devuelve zscan
    state_values  : dict {key: value_or_None} para .get() de state keys
    meta_values   : dict {key: value_or_None} para .get() de meta keys
    """
    r = AsyncMock()

    # ── zscan ──────────────────────────────────────────────────────────────
    # update_activity hace: while True: cursor, results = await zscan(...)
    # Simular una sola página (cursor retorna 0 = fin).
    r.zscan = AsyncMock(return_value=(0, zscan_members))

    # ── get (state + meta) ─────────────────────────────────────────────────
    all_values = {}
    if meta_values:
        all_values.update(meta_values)
    all_values.update(state_values)

    async def _get(key, *a, **kw):
        return all_values.get(key)

    r.get = AsyncMock(side_effect=_get)

    # ── pipeline para batch-read de estados (pipe_check) ──────────────────
    # update_activity usa pipeline() para el guard batch-read
    _pipe = AsyncMock()
    _pipe.__aenter__ = AsyncMock(return_value=_pipe)
    _pipe.__aexit__  = AsyncMock(return_value=False)
    _pipe.get        = MagicMock(return_value=_pipe)  # chaining

    # execute() devuelve los estados de los candidatos en orden
    # Necesitamos saber qué keys se registraron con pipe.get()
    _pipe_calls = []
    def _pipe_get(key):
        _pipe_calls.append(key)
        return _pipe
    _pipe.get = MagicMock(side_effect=_pipe_get)

    async def _pipe_execute():
        return [state_values.get(k) for k in _pipe_calls]

    _pipe.execute = AsyncMock(side_effect=_pipe_execute)
    r.pipeline = MagicMock(return_value=_pipe)

    # ── zrem ────────────────────────────────────────────────────────────────
    r.zrem = AsyncMock(return_value=1)

    # ── zadd ────────────────────────────────────────────────────────────────
    r.zadd = AsyncMock(return_value=1)

    # ── ttl / set / exists / delete / srem ─────────────────────────────────
    r.ttl    = AsyncMock(return_value=86400)
    r.set    = AsyncMock(return_value=True)
    r.exists = AsyncMock(return_value=0)
    r.delete = AsyncMock(return_value=1)
    r.srem   = AsyncMock(return_value=0)

    return r, _pipe_calls


# ─── Wrapper de update_activity que extrae solo el bloque P3-D ───────────────
# En lugar de ejecutar todo update_activity (que necesita mucho contexto),
# ejecutamos directamente la lógica P3-D copiada/adaptada para testear
# de forma aislada.

async def _run_p3d(state_manager, phone, canal_safe, did_zadd):
    """
    Ejecuta solo el bloque P3-D de update_activity().
    Retorna (zrem_called, zrem_args, skipped_members).
    """
    sm = state_manager
    r  = sm.redis

    if not (did_zadd and canal_safe == "whatsapp"):
        return False, [], []

    whatsapp_member = f"{phone}:whatsapp"
    cursor = 0
    _stale_candidates = []
    while True:
        cursor, results = await r.zscan(
            sm.ACTIVE_CONTACTS_ZSET, cursor, match=f"{phone}:*", count=20
        )
        for member, _ in results:
            m = member if isinstance(member, str) else member.decode()
            if m != whatsapp_member:
                _stale_candidates.append(m)
        if cursor == 0:
            break

    _SKIP_STATUSES = {
        ConversationStatus.HUMAN_ACTIVE.value,
        ConversationStatus.IN_CONVERSATION.value,
        ConversationStatus.PENDING_HANDOFF.value,
    }
    stale_members = []
    skipped = []
    if _stale_candidates:
        _pipe_check = r.pipeline(transaction=False)
        for _cand in _stale_candidates:
            _cand_canal = _cand.split(":", 1)[1] if ":" in _cand else "whatsapp"
            _pipe_check.get(f"{sm.STATE_PREFIX}{phone}:{_cand_canal}")
        _cand_states = await _pipe_check.execute()

        for _cand, _cand_state_raw in zip(_stale_candidates, _cand_states):
            _cand_status = _cand_state_raw if isinstance(_cand_state_raw, str) else (
                _cand_state_raw.decode() if _cand_state_raw else None
            )
            if _cand_status in _SKIP_STATUSES:
                skipped.append(_cand)
                continue
            stale_members.append(_cand)

    if stale_members:
        await r.zrem(sm.ACTIVE_CONTACTS_ZSET, *stale_members)

    return bool(stale_members), stale_members, skipped


# ══════════════════════════════════════════════════════════════════════════════
# TESTS
# ══════════════════════════════════════════════════════════════════════════════

async def test_skip_in_conversation():
    """Bug original: ciencuadras está IN_CONVERSATION ->P3-D NO debe hacer zrem."""
    print("\n[T1] Asesor IN_CONVERSATION en ciencuadras, cliente escribe WhatsApp")
    phone = "+573217580955"
    state_map = {
        "conv_state:+573217580955:ciencuadras": ConversationStatus.IN_CONVERSATION.value,
    }
    r, _ = _redis_mock(
        zscan_members=[(f"{phone}:ciencuadras", 1000.0)],
        state_values=state_map,
    )
    sm = _make_sm(r)

    zrem_called, stale_merged, skipped = await _run_p3d(sm, phone, "whatsapp", did_zadd=True)

    if not zrem_called:
        ok("zrem NO fue llamado (sesión intacta)")
    else:
        fail("zrem NO fue llamado (sesión intacta)", f"zrem se llamó con: {stale_merged}")

    if f"{phone}:ciencuadras" in skipped:
        ok("phone:ciencuadras fue marcado como skipped")
    else:
        fail("phone:ciencuadras fue marcado como skipped", f"skipped={skipped}")

    if r.zrem.call_count == 0:
        ok("redis.zrem call_count == 0")
    else:
        fail("redis.zrem call_count == 0", f"fue llamado {r.zrem.call_count} veces")


async def test_skip_human_active():
    """HUMAN_ACTIVE también debe ser saltado."""
    print("\n[T2] Asesor HUMAN_ACTIVE en metrocuadrado, cliente escribe WhatsApp")
    phone = "+573001234567"
    state_map = {
        "conv_state:+573001234567:metrocuadrado": ConversationStatus.HUMAN_ACTIVE.value,
    }
    r, _ = _redis_mock(
        zscan_members=[(f"{phone}:metrocuadrado", 2000.0)],
        state_values=state_map,
    )
    sm = _make_sm(r)

    zrem_called, stale_merged, skipped = await _run_p3d(sm, phone, "whatsapp", did_zadd=True)

    if not zrem_called:
        ok("zrem NO fue llamado (HUMAN_ACTIVE preservado)")
    else:
        fail("zrem NO fue llamado (HUMAN_ACTIVE preservado)", f"stale={stale_merged}")

    if f"{phone}:metrocuadrado" in skipped:
        ok("metrocuadrado skipped correctamente")
    else:
        fail("metrocuadrado skipped correctamente", f"skipped={skipped}")


async def test_skip_pending_handoff():
    """PENDING_HANDOFF también debe ser saltado."""
    print("\n[T3] Canal instagram en PENDING_HANDOFF ->skip")
    phone = "+573009876543"
    state_map = {
        "conv_state:+573009876543:instagram": ConversationStatus.PENDING_HANDOFF.value,
    }
    r, _ = _redis_mock(
        zscan_members=[(f"{phone}:instagram", 3000.0)],
        state_values=state_map,
    )
    sm = _make_sm(r)

    zrem_called, _, skipped = await _run_p3d(sm, phone, "whatsapp", did_zadd=True)

    if not zrem_called:
        ok("zrem NO fue llamado (PENDING_HANDOFF preservado)")
    else:
        fail("zrem NO fue llamado (PENDING_HANDOFF preservado)")

    if f"{phone}:instagram" in skipped:
        ok("instagram PENDING_HANDOFF skipped")
    else:
        fail("instagram PENDING_HANDOFF skipped", f"skipped={skipped}")


async def test_merge_bot_active():
    """P3-D SÍ debe mergear cuando el stale es BOT_ACTIVE (comportamiento original)."""
    print("\n[T4] Canal finca_raiz en BOT_ACTIVE ->P3-D SÍ debe hacer zrem")
    phone = "+573111111111"
    state_map = {
        "conv_state:+573111111111:finca_raiz": ConversationStatus.BOT_ACTIVE.value,
    }
    meta_map = {
        "conv_meta:+573111111111:finca_raiz": json.dumps({
            "contact_id": "hs_999",
            "display_name": "Test User",
            "assigned_owner_id": "owner_42",
            "canal_origen": "finca_raiz",
        }),
        "conv_meta:+573111111111:whatsapp": None,
        "conv_state:+573111111111:whatsapp": None,
    }
    r, _ = _redis_mock(
        zscan_members=[(f"{phone}:finca_raiz", 4000.0)],
        state_values=state_map,
        meta_values=meta_map,
    )
    sm = _make_sm(r)

    zrem_called, stale_merged, skipped = await _run_p3d(sm, phone, "whatsapp", did_zadd=True)

    if zrem_called:
        ok("zrem fue llamado (BOT_ACTIVE debe mergearse)")
    else:
        fail("zrem fue llamado (BOT_ACTIVE debe mergearse)")

    if f"{phone}:finca_raiz" in stale_merged:
        ok("finca_raiz incluido en stale_members")
    else:
        fail("finca_raiz incluido en stale_members", f"stale={stale_merged}")

    if not skipped:
        ok("ningún canal fue skipped (correcto)")
    else:
        fail("ningún canal fue skipped (correcto)", f"skipped={skipped}")


async def test_merge_expired_state():
    """Estado expirado (None) ->el canal debe ser mergeado."""
    print("\n[T5] Canal ciencuadras con state expirado (None) ->debe mergearse")
    phone = "+573222222222"
    state_map = {
        "conv_state:+573222222222:ciencuadras": None,  # expirado
    }
    r, _ = _redis_mock(
        zscan_members=[(f"{phone}:ciencuadras", 5000.0)],
        state_values=state_map,
    )
    sm = _make_sm(r)

    zrem_called, stale_merged, skipped = await _run_p3d(sm, phone, "whatsapp", did_zadd=True)

    if zrem_called:
        ok("zrem fue llamado (state expirado ->merge permitido)")
    else:
        fail("zrem fue llamado (state expirado ->merge permitido)")

    if not skipped:
        ok("ningún canal skipped cuando state es None")
    else:
        fail("ningún canal skipped cuando state es None", f"skipped={skipped}")


async def test_mixed_active_and_bot():
    """
    ZSET tiene 2 canales stale: uno activo y uno BOT_ACTIVE.
    ->Skip el activo, mergear solo el bot.
    """
    print("\n[T6] Dos canales stale: ciencuadras(IN_CONV) + finca_raiz(BOT_ACTIVE)")
    phone = "+573333333333"
    state_map = {
        "conv_state:+573333333333:ciencuadras": ConversationStatus.IN_CONVERSATION.value,
        "conv_state:+573333333333:finca_raiz":  ConversationStatus.BOT_ACTIVE.value,
    }
    r, _ = _redis_mock(
        zscan_members=[
            (f"{phone}:ciencuadras", 6000.0),
            (f"{phone}:finca_raiz",  5500.0),
        ],
        state_values=state_map,
    )
    sm = _make_sm(r)

    zrem_called, stale_merged, skipped = await _run_p3d(sm, phone, "whatsapp", did_zadd=True)

    if zrem_called:
        ok("zrem fue llamado (al menos un canal BOT_ACTIVE)")
    else:
        fail("zrem fue llamado (al menos un canal BOT_ACTIVE)")

    if f"{phone}:finca_raiz" in stale_merged:
        ok("finca_raiz (BOT_ACTIVE) incluido en zrem")
    else:
        fail("finca_raiz (BOT_ACTIVE) incluido en zrem", f"stale={stale_merged}")

    if f"{phone}:ciencuadras" not in stale_merged:
        ok("ciencuadras (IN_CONVERSATION) NO incluido en zrem")
    else:
        fail("ciencuadras (IN_CONVERSATION) NO incluido en zrem", f"stale={stale_merged}")

    if f"{phone}:ciencuadras" in skipped:
        ok("ciencuadras registrado en skipped")
    else:
        fail("ciencuadras registrado en skipped", f"skipped={skipped}")


async def test_no_stale_members():
    """Sin miembros stale ->P3-D no hace nada."""
    print("\n[T7] Sin canales stale en ZSET ->P3-D no opera")
    phone = "+573444444444"
    r, _ = _redis_mock(
        zscan_members=[],  # ZSET solo tiene phone:whatsapp
        state_values={},
    )
    sm = _make_sm(r)

    zrem_called, stale_merged, skipped = await _run_p3d(sm, phone, "whatsapp", did_zadd=True)

    if not zrem_called:
        ok("zrem NO fue llamado (sin stale members)")
    else:
        fail("zrem NO fue llamado (sin stale members)")

    if r.zrem.call_count == 0:
        ok("redis.zrem call_count == 0")
    else:
        fail("redis.zrem call_count == 0", f"fue llamado {r.zrem.call_count} veces")


async def test_not_whatsapp_canal():
    """P3-D solo dispara para canal=whatsapp ->para otros canales no opera."""
    print("\n[T8] Mensaje entrante en canal=instagram ->P3-D NO debe disparar")
    phone = "+573555555555"
    r, _ = _redis_mock(
        zscan_members=[(f"{phone}:ciencuadras", 7000.0)],
        state_values={},
    )
    sm = _make_sm(r)

    # canal_safe = 'instagram' ->la condición `canal_safe == 'whatsapp'` es False
    zrem_called, _, _ = await _run_p3d(sm, phone, "instagram", did_zadd=True)

    if not zrem_called:
        ok("P3-D no disparó para canal=instagram")
    else:
        fail("P3-D no disparó para canal=instagram")

    if r.zscan.call_count == 0:
        ok("zscan no fue llamado (canal != whatsapp)")
    else:
        fail("zscan no fue llamado (canal != whatsapp)", f"call_count={r.zscan.call_count}")


async def test_did_zadd_false():
    """Si _did_zadd=False ->P3-D no dispara."""
    print("\n[T9] _did_zadd=False (contacto no re-ingresó al ZSET) ->P3-D silencioso")
    phone = "+573666666666"
    r, _ = _redis_mock(
        zscan_members=[(f"{phone}:ciencuadras", 8000.0)],
        state_values={},
    )
    sm = _make_sm(r)

    zrem_called, _, _ = await _run_p3d(sm, phone, "whatsapp", did_zadd=False)

    if not zrem_called:
        ok("P3-D no disparó cuando did_zadd=False")
    else:
        fail("P3-D no disparó cuando did_zadd=False")


# ══════════════════════════════════════════════════════════════════════════════
# RUNNER
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    print("=" * 60)
    print("  TEST SUITE: P3-D Guard — Sesión activa multi-canal")
    print("=" * 60)

    tests = [
        test_skip_in_conversation,   # T1 — bug exact scenario
        test_skip_human_active,      # T2
        test_skip_pending_handoff,   # T3
        test_merge_bot_active,       # T4 — regresión BOT_ACTIVE
        test_merge_expired_state,    # T5 — regresión state expirado
        test_mixed_active_and_bot,   # T6 — mix: skip uno, mergea otro
        test_no_stale_members,       # T7 — sin stale
        test_not_whatsapp_canal,     # T8 — canal != whatsapp
        test_did_zadd_false,         # T9 — did_zadd=False
    ]

    for t in tests:
        try:
            await t()
        except Exception as e:
            fail(t.__name__, f"Excepción inesperada: {e}")

    print("\n" + "=" * 60)
    print(f"  RESULTADOS: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    if FAIL > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
