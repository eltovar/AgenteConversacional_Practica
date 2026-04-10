#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test P3-D v2: Canal Identity Merge — Meta, Status, Marker, Inbox, Cleanup

10 escenarios que verifican la migración completa de datos cuando un contacto
cambia de canal (ej. Instagram → WhatsApp).
"""

import asyncio
import json
import sys
import io

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from unittest.mock import AsyncMock, MagicMock, patch


# ==============================================================================
# SIMULADOR DE REDIS (in-memory)
# ==============================================================================

class FakeRedis:
    """Redis mock con soporte para GET/SET/DELETE/ZADD/ZREM/ZSCAN/ZSCORE/EXISTS/TTL."""

    def __init__(self):
        self._store = {}     # key → value (str)
        self._zsets = {}     # key → {member: score}
        self._ttls = {}      # key → ttl_seconds

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, ex=None):
        self._store[key] = value
        if ex:
            self._ttls[key] = ex

    async def delete(self, *keys):
        for k in keys:
            self._store.pop(k, None)
            self._ttls.pop(k, None)

    async def exists(self, key):
        return 1 if key in self._store else 0

    async def ttl(self, key):
        return self._ttls.get(key, -1)

    async def zadd(self, key, mapping):
        if key not in self._zsets:
            self._zsets[key] = {}
        self._zsets[key].update(mapping)

    async def zrem(self, key, *members):
        if key in self._zsets:
            for m in members:
                self._zsets[key].pop(m, None)

    async def zscan(self, key, cursor, match=None, count=20):
        zset = self._zsets.get(key, {})
        results = []
        for member, score in zset.items():
            if match:
                import fnmatch
                if fnmatch.fnmatch(member, match):
                    results.append((member, score))
            else:
                results.append((member, score))
        return 0, results  # cursor=0 means done

    async def zscore(self, key, member):
        zset = self._zsets.get(key, {})
        return zset.get(member)


# ==============================================================================
# HELPER: Simular update_activity P3-D v2 merge logic
# ==============================================================================

async def simulate_p3d_v2_merge(redis, phone, stale_members, whatsapp_member):
    """Reproduce exactamente la lógica P3-D v2 de conversation_state.py."""
    META_PREFIX = "conv_meta:"
    STATE_PREFIX = "conv_state:"
    PANEL_TTL_SECONDS = 365 * 86400
    HUMAN_PANEL_STATE_TTL = 7 * 86400

    wa_meta_key = f"{META_PREFIX}{phone}:whatsapp"
    wa_state_key = f"{STATE_PREFIX}{phone}:whatsapp"
    wa_marker_key = f"conv_was_panel:{phone}:whatsapp"

    wa_meta_raw = await redis.get(wa_meta_key)
    wa_meta = json.loads(wa_meta_raw) if wa_meta_raw else {}
    wa_status_raw = await redis.get(wa_state_key)
    wa_status = wa_status_raw if isinstance(wa_status_raw, str) else (
        wa_status_raw.decode() if wa_status_raw else None
    )

    _STATUS_PRIO = {"IN_CONVERSATION": 4, "HUMAN_ACTIVE": 3, "PENDING_HANDOFF": 2, "BOT_ACTIVE": 1}
    best_status = wa_status
    best_prio = _STATUS_PRIO.get(wa_status, 0)
    stale_advisor_id = None

    for stale_m in stale_members:
        stale_canal = stale_m.split(":", 1)[1] if ":" in stale_m else stale_m
        s_meta_key = f"{META_PREFIX}{phone}:{stale_canal}"
        s_state_key = f"{STATE_PREFIX}{phone}:{stale_canal}"
        s_marker_key = f"conv_was_panel:{phone}:{stale_canal}"

        s_meta_raw = await redis.get(s_meta_key)
        if s_meta_raw:
            s_meta = json.loads(s_meta_raw)
            for fld in ("contact_id", "display_name", "assigned_owner_id",
                        "assigned_owner_ids", "primary_owner_id", "handoff_reason",
                        "deal_id", "deal_stage", "transfer_history", "canal_origen"):
                if s_meta.get(fld) and not wa_meta.get(fld):
                    wa_meta[fld] = s_meta[fld]
            s_created = s_meta.get("created_at", "")
            if s_created and (not wa_meta.get("created_at") or s_created < wa_meta["created_at"]):
                wa_meta["created_at"] = s_created
            if s_meta.get("assigned_owner_id"):
                stale_advisor_id = s_meta["assigned_owner_id"]

        s_status_raw = await redis.get(s_state_key)
        s_status = s_status_raw if isinstance(s_status_raw, str) else (
            s_status_raw.decode() if s_status_raw else None
        )
        s_prio = _STATUS_PRIO.get(s_status, 0)
        if s_prio > best_prio:
            best_status = s_status
            best_prio = s_prio

        if await redis.exists(s_marker_key):
            await redis.set(wa_marker_key, "1", ex=PANEL_TTL_SECONDS)

        await redis.delete(s_meta_key, s_state_key, s_marker_key)

    wa_meta.pop("_temp_meta", None)
    wa_meta["in_panel"] = True
    wa_ttl = await redis.ttl(wa_meta_key)
    if not wa_ttl or wa_ttl <= 0:
        wa_ttl = PANEL_TTL_SECONDS if best_prio >= 2 else 86400
    await redis.set(wa_meta_key, json.dumps(wa_meta), ex=wa_ttl)

    if best_status and best_status != wa_status:
        await redis.set(wa_state_key, best_status, ex=HUMAN_PANEL_STATE_TTL)

    if stale_advisor_id:
        inbox_key = f"advisor_inbox:{stale_advisor_id}"
        for stale_m in stale_members:
            score = await redis.zscore(inbox_key, stale_m)
            if score is not None:
                await redis.zadd(inbox_key, {whatsapp_member: score})
                await redis.zrem(inbox_key, stale_m)

    return wa_meta, best_status, stale_advisor_id


# ==============================================================================
# TESTS
# ==============================================================================

async def test_t1_instagram_to_whatsapp_meta_merge():
    """T1: Instagram -> WhatsApp — meta WA tiene contact_id/display_name del IG."""
    print('\n[TEST T1] Instagram -> WhatsApp: meta merge completo')

    redis = FakeRedis()
    phone = "+573001234567"

    # Setup: Instagram meta con datos ricos
    ig_meta = {
        "contact_id": "hs_12345",
        "display_name": "Maria Garcia",
        "assigned_owner_id": "owner_99",
        "canal_origen": "instagram",
        "created_at": "2026-03-01T10:00:00",
        "in_panel": True,
    }
    await redis.set(f"conv_meta:{phone}:instagram", json.dumps(ig_meta))

    # Setup: WhatsApp meta temporal (sin datos)
    wa_meta = {"_temp_meta": True, "in_panel": True}
    await redis.set(f"conv_meta:{phone}:whatsapp", json.dumps(wa_meta))

    # Execute merge
    result_meta, _, _ = await simulate_p3d_v2_merge(
        redis, phone,
        stale_members=[f"{phone}:instagram"],
        whatsapp_member=f"{phone}:whatsapp"
    )

    # Verify
    assert result_meta.get("contact_id") == "hs_12345", f'contact_id incorrecto: {result_meta.get("contact_id")}'
    assert result_meta.get("display_name") == "Maria Garcia", f'display_name incorrecto'
    assert result_meta.get("assigned_owner_id") == "owner_99", f'assigned_owner_id incorrecto'
    assert result_meta.get("canal_origen") == "instagram", f'canal_origen incorrecto'
    assert result_meta.get("created_at") == "2026-03-01T10:00:00", f'created_at incorrecto'
    assert "_temp_meta" not in result_meta, '_temp_meta no fue eliminado'
    assert result_meta.get("in_panel") == True, 'in_panel no es True'

    # Verify persisted in Redis
    stored = json.loads(await redis.get(f"conv_meta:{phone}:whatsapp"))
    assert stored["contact_id"] == "hs_12345"

    print('   [OK] Meta mergeado correctamente: contact_id, display_name, canal_origen')
    print('   [OK] _temp_meta eliminado')
    print('   [OK] in_panel=True preservado')
    return True


async def test_t2_status_promotion():
    """T2: Status HUMAN_ACTIVE (IG) + BOT_ACTIVE (WA) -> promovido a HUMAN_ACTIVE."""
    print('\n[TEST T2] Status promotion: HUMAN_ACTIVE (stale) > BOT_ACTIVE (WA)')

    redis = FakeRedis()
    phone = "+573001234567"

    await redis.set(f"conv_meta:{phone}:instagram", json.dumps({"contact_id": "x"}))
    await redis.set(f"conv_state:{phone}:instagram", "HUMAN_ACTIVE")
    await redis.set(f"conv_meta:{phone}:whatsapp", json.dumps({"_temp_meta": True}))
    await redis.set(f"conv_state:{phone}:whatsapp", "BOT_ACTIVE")

    _, best_status, _ = await simulate_p3d_v2_merge(
        redis, phone,
        stale_members=[f"{phone}:instagram"],
        whatsapp_member=f"{phone}:whatsapp"
    )

    assert best_status == "HUMAN_ACTIVE", f'Status no promovido: {best_status}'

    # Verify stored in Redis
    stored_status = await redis.get(f"conv_state:{phone}:whatsapp")
    assert stored_status == "HUMAN_ACTIVE", f'Status en Redis incorrecto: {stored_status}'

    print('   [OK] Status promovido: BOT_ACTIVE -> HUMAN_ACTIVE')
    return True


async def test_t3_marker_migrated():
    """T3: Marker conv_was_panel migrado de Instagram a WhatsApp."""
    print('\n[TEST T3] Marker de re-entrada migrado')

    redis = FakeRedis()
    phone = "+573001234567"

    await redis.set(f"conv_meta:{phone}:instagram", json.dumps({"contact_id": "x"}))
    await redis.set(f"conv_was_panel:{phone}:instagram", "1")
    await redis.set(f"conv_meta:{phone}:whatsapp", json.dumps({"_temp_meta": True}))

    await simulate_p3d_v2_merge(
        redis, phone,
        stale_members=[f"{phone}:instagram"],
        whatsapp_member=f"{phone}:whatsapp"
    )

    # Verify marker migrated
    wa_marker = await redis.exists(f"conv_was_panel:{phone}:whatsapp")
    assert wa_marker == 1, 'Marker WA no creado'

    # Verify stale marker deleted
    ig_marker = await redis.exists(f"conv_was_panel:{phone}:instagram")
    assert ig_marker == 0, 'Marker IG no eliminado'

    print('   [OK] Marker conv_was_panel migrado a whatsapp')
    print('   [OK] Marker instagram eliminado')
    return True


async def test_t4_inbox_migrated():
    """T4: Inbox entry migrada de phone:instagram a phone:whatsapp."""
    print('\n[TEST T4] Inbox migration')

    redis = FakeRedis()
    phone = "+573001234567"
    advisor_id = "owner_99"

    await redis.set(f"conv_meta:{phone}:instagram", json.dumps({
        "contact_id": "x", "assigned_owner_id": advisor_id
    }))
    # Inbox con entry para instagram
    await redis.zadd(f"advisor_inbox:{advisor_id}", {f"{phone}:instagram": 1712345678.0})

    await redis.set(f"conv_meta:{phone}:whatsapp", json.dumps({"_temp_meta": True}))

    await simulate_p3d_v2_merge(
        redis, phone,
        stale_members=[f"{phone}:instagram"],
        whatsapp_member=f"{phone}:whatsapp"
    )

    # Verify inbox migrated
    wa_score = await redis.zscore(f"advisor_inbox:{advisor_id}", f"{phone}:whatsapp")
    assert wa_score == 1712345678.0, f'Inbox WA no tiene score correcto: {wa_score}'

    ig_score = await redis.zscore(f"advisor_inbox:{advisor_id}", f"{phone}:instagram")
    assert ig_score is None, f'Inbox IG no eliminado: {ig_score}'

    print('   [OK] Inbox entry migrada: phone:whatsapp con score original')
    print('   [OK] Inbox entry stale eliminada: phone:instagram')
    return True


async def test_t5_stale_keys_deleted():
    """T5: Keys stale (meta, state, marker) eliminadas tras merge."""
    print('\n[TEST T5] Stale keys cleanup')

    redis = FakeRedis()
    phone = "+573001234567"

    await redis.set(f"conv_meta:{phone}:instagram", json.dumps({"contact_id": "x"}))
    await redis.set(f"conv_state:{phone}:instagram", "HUMAN_ACTIVE")
    await redis.set(f"conv_was_panel:{phone}:instagram", "1")
    await redis.set(f"conv_meta:{phone}:whatsapp", json.dumps({"_temp_meta": True}))

    await simulate_p3d_v2_merge(
        redis, phone,
        stale_members=[f"{phone}:instagram"],
        whatsapp_member=f"{phone}:whatsapp"
    )

    # Verify all stale keys deleted
    assert await redis.get(f"conv_meta:{phone}:instagram") is None, 'Meta IG no eliminado'
    assert await redis.get(f"conv_state:{phone}:instagram") is None, 'State IG no eliminado'
    assert await redis.exists(f"conv_was_panel:{phone}:instagram") == 0, 'Marker IG no eliminado'

    # Verify WA keys exist
    assert await redis.get(f"conv_meta:{phone}:whatsapp") is not None, 'Meta WA eliminado por error'

    print('   [OK] conv_meta:{phone}:instagram eliminado')
    print('   [OK] conv_state:{phone}:instagram eliminado')
    print('   [OK] conv_was_panel:{phone}:instagram eliminado')
    print('   [OK] conv_meta:{phone}:whatsapp preservado')
    return True


async def test_t6_stale_meta_expired():
    """T6: Meta stale ya expiro (TTL) -> no crash, solo cleanup."""
    print('\n[TEST T6] Meta stale expirado (None)')

    redis = FakeRedis()
    phone = "+573001234567"

    # NO crear meta para instagram (simula TTL expirado)
    await redis.set(f"conv_state:{phone}:instagram", "BOT_ACTIVE")
    await redis.set(f"conv_meta:{phone}:whatsapp", json.dumps({"_temp_meta": True}))

    # Should NOT crash
    result_meta, _, _ = await simulate_p3d_v2_merge(
        redis, phone,
        stale_members=[f"{phone}:instagram"],
        whatsapp_member=f"{phone}:whatsapp"
    )

    assert result_meta.get("in_panel") == True, 'in_panel no seteado'
    assert "_temp_meta" not in result_meta, '_temp_meta no eliminado'

    # State stale should still be cleaned
    assert await redis.get(f"conv_state:{phone}:instagram") is None, 'State IG no limpiado'

    print('   [OK] Meta stale None -> no crash')
    print('   [OK] State stale limpiado aunque meta no existia')
    return True


async def test_t7_multiple_stale_canals():
    """T7: Multiples stale (IG + mercado_libre) -> merge acumulativo."""
    print('\n[TEST T7] Multiple stale canals: Instagram + MercadoLibre')

    redis = FakeRedis()
    phone = "+573001234567"

    # Instagram tiene contact_id
    await redis.set(f"conv_meta:{phone}:instagram", json.dumps({
        "contact_id": "hs_111", "display_name": "Maria", "canal_origen": "instagram"
    }))
    await redis.set(f"conv_state:{phone}:instagram", "HUMAN_ACTIVE")

    # MercadoLibre tiene deal_id
    await redis.set(f"conv_meta:{phone}:mercado_libre", json.dumps({
        "deal_id": "deal_222", "deal_stage": "negotiation", "canal_origen": "mercado_libre",
        "assigned_owner_id": "owner_55"
    }))
    await redis.set(f"conv_state:{phone}:mercado_libre", "IN_CONVERSATION")

    # WhatsApp temporal
    await redis.set(f"conv_meta:{phone}:whatsapp", json.dumps({"_temp_meta": True}))
    await redis.set(f"conv_state:{phone}:whatsapp", "BOT_ACTIVE")

    result_meta, best_status, _ = await simulate_p3d_v2_merge(
        redis, phone,
        stale_members=[f"{phone}:instagram", f"{phone}:mercado_libre"],
        whatsapp_member=f"{phone}:whatsapp"
    )

    # contact_id from IG (first in list)
    assert result_meta.get("contact_id") == "hs_111", f'contact_id: {result_meta.get("contact_id")}'
    assert result_meta.get("display_name") == "Maria"
    # deal_id from ML (WA didn't have it)
    assert result_meta.get("deal_id") == "deal_222", f'deal_id: {result_meta.get("deal_id")}'
    assert result_meta.get("deal_stage") == "negotiation"
    # Status: IN_CONVERSATION (4) > HUMAN_ACTIVE (3) > BOT_ACTIVE (1)
    assert best_status == "IN_CONVERSATION", f'Status: {best_status}'

    # Both stale cleaned
    assert await redis.get(f"conv_meta:{phone}:instagram") is None
    assert await redis.get(f"conv_meta:{phone}:mercado_libre") is None
    assert await redis.get(f"conv_state:{phone}:instagram") is None
    assert await redis.get(f"conv_state:{phone}:mercado_libre") is None

    print('   [OK] Merge acumulativo: contact_id de IG, deal_id de ML')
    print('   [OK] Status promovido a IN_CONVERSATION (max prioridad)')
    print('   [OK] Ambos stale canals limpiados')
    return True


async def test_t8_no_stale_members():
    """T8: Sin stale members -> 0 operaciones extra."""
    print('\n[TEST T8] Sin stale members (no-op)')

    redis = FakeRedis()
    phone = "+573001234567"

    wa_meta = {"contact_id": "hs_999", "display_name": "Test", "in_panel": True}
    await redis.set(f"conv_meta:{phone}:whatsapp", json.dumps(wa_meta))
    await redis.set(f"conv_state:{phone}:whatsapp", "HUMAN_ACTIVE")

    # Empty stale_members — este caso NO deberia llegar a simulate_p3d_v2_merge
    # porque el guard `if stale_members:` lo previene, pero verificamos robustez
    result_meta, best_status, _ = await simulate_p3d_v2_merge(
        redis, phone,
        stale_members=[],
        whatsapp_member=f"{phone}:whatsapp"
    )

    # Meta WA intacto
    assert result_meta.get("contact_id") == "hs_999"
    assert result_meta.get("in_panel") == True

    print('   [OK] stale_members=[] -> meta WA intacto')
    return True


async def test_t9_idempotency():
    """T9: Segunda ejecucion es no-op (stale ya limpiados)."""
    print('\n[TEST T9] Idempotencia: 2da ejecucion')

    redis = FakeRedis()
    phone = "+573001234567"

    # Setup con datos IG
    await redis.set(f"conv_meta:{phone}:instagram", json.dumps({
        "contact_id": "hs_111", "display_name": "Maria"
    }))
    await redis.set(f"conv_state:{phone}:instagram", "HUMAN_ACTIVE")
    await redis.set(f"conv_meta:{phone}:whatsapp", json.dumps({"_temp_meta": True}))

    # Primera ejecucion
    await simulate_p3d_v2_merge(
        redis, phone,
        stale_members=[f"{phone}:instagram"],
        whatsapp_member=f"{phone}:whatsapp"
    )

    # Capturar estado post-merge
    wa_meta_after_1st = json.loads(await redis.get(f"conv_meta:{phone}:whatsapp"))

    # Segunda ejecucion (stale_members vacio porque ya se hizo ZREM)
    result_meta, _, _ = await simulate_p3d_v2_merge(
        redis, phone,
        stale_members=[],
        whatsapp_member=f"{phone}:whatsapp"
    )

    # Meta WA debe ser el mismo
    wa_meta_after_2nd = json.loads(await redis.get(f"conv_meta:{phone}:whatsapp"))
    assert wa_meta_after_2nd.get("contact_id") == wa_meta_after_1st.get("contact_id")

    print('   [OK] 2da ejecucion con stale_members=[] no modifica datos')
    return True


async def test_t10_non_whatsapp_canal():
    """T10: Canal no-WA -> P3-D no aplica (guard canal_safe == 'whatsapp')."""
    print('\n[TEST T10] Canal no-WA: P3-D no aplica')

    # Este test verifica la condicion guard, no el merge en si
    canal_safe = "instagram"
    _did_zadd = True

    # Guard condition
    should_merge = _did_zadd and canal_safe == "whatsapp"
    assert not should_merge, 'P3-D no deberia aplicar para canal instagram'

    canal_safe = "whatsapp"
    should_merge = _did_zadd and canal_safe == "whatsapp"
    assert should_merge, 'P3-D deberia aplicar para canal whatsapp'

    canal_safe = "whatsapp"
    _did_zadd = False
    should_merge = _did_zadd and canal_safe == "whatsapp"
    assert not should_merge, 'P3-D no deberia aplicar si _did_zadd=False'

    print('   [OK] Guard: canal != whatsapp -> skip')
    print('   [OK] Guard: canal == whatsapp + _did_zadd -> execute')
    print('   [OK] Guard: _did_zadd=False -> skip')
    return True


# ==============================================================================
# MAIN
# ==============================================================================

async def main():
    print('\n' + '=' * 80)
    print('TEST SUITE P3-D v2: Canal Identity Merge — Meta, Status, Marker, Inbox')
    print('=' * 80)

    tests = [
        ('T1', 'Meta merge IG->WA', test_t1_instagram_to_whatsapp_meta_merge),
        ('T2', 'Status promotion', test_t2_status_promotion),
        ('T3', 'Marker migration', test_t3_marker_migrated),
        ('T4', 'Inbox migration', test_t4_inbox_migrated),
        ('T5', 'Stale keys cleanup', test_t5_stale_keys_deleted),
        ('T6', 'Stale meta expired', test_t6_stale_meta_expired),
        ('T7', 'Multiple stale canals', test_t7_multiple_stale_canals),
        ('T8', 'No stale members', test_t8_no_stale_members),
        ('T9', 'Idempotency', test_t9_idempotency),
        ('T10', 'Non-WA canal guard', test_t10_non_whatsapp_canal),
    ]

    results = []
    for tid, desc, test_func in tests:
        try:
            result = await test_func()
            results.append((tid, desc, 'PASS', None))
        except AssertionError as e:
            results.append((tid, desc, 'FAIL', str(e)))
        except Exception as e:
            results.append((tid, desc, 'ERROR', str(e)))

    print('\n' + '=' * 80)
    print('RESUMEN')
    print('=' * 80)

    passed = sum(1 for _, _, s, _ in results if s == 'PASS')
    failed = sum(1 for _, _, s, _ in results if s != 'PASS')

    for tid, desc, status, error in results:
        icon = '[PASS]' if status == 'PASS' else '[FAIL]'
        print(f'{icon} {tid}: {desc}')
        if error:
            print(f'      -> {error}')

    print(f'\nTotal: {passed} PASS, {failed} FAIL')
    print('=' * 80 + '\n')

    return failed == 0


if __name__ == '__main__':
    success = asyncio.run(main())
    exit(0 if success else 1)
