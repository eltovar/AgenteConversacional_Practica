#!/usr/bin/env python3
"""
TEST SUITE — Fixes Real-Time Ordering + Notifications (Abr 11, 2026)

Valida los 4 fixes de esta sesión:
  - Fix-A: notify_new_message enruta targeted via assigned_owner_id
  - Fix-D: get_inbox_unread_map canal-agnostic
  - Fix-D: remove_from_advisor_inbox canal-agnostic

Ejecutar:
    python test_fixes_targeted_routing.py

Exit code 0 = todos los tests pasaron.
"""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock


# ═══════════════════════════════════════════════════════════════════
# Test harness
# ═══════════════════════════════════════════════════════════════════

_results = []


def _ok(name):
    _results.append((name, True, None))
    print(f"  [OK]{name}")


def _fail(name, reason):
    _results.append((name, False, reason))
    print(f"  [FAIL]{name} → {reason}")


def _assert(cond, name, reason=""):
    if cond:
        _ok(name)
    else:
        _fail(name, reason or "assertion failed")


# ═══════════════════════════════════════════════════════════════════
# Fix-A: Targeted routing
# ═══════════════════════════════════════════════════════════════════


async def test_fix_a_targeted_with_explicit_owner():
    """notify_new_message con assigned_owner_id explícito → publish_to_advisor"""
    from middleware.websocket_manager import ConnectionManager

    cm = ConnectionManager()
    cm.publish_to_advisor = AsyncMock()
    cm.publish_broadcast = AsyncMock()

    redis_mock = MagicMock()

    await cm.notify_new_message(
        phone="+573001111111",
        canal="whatsapp",
        message_preview="hola",
        redis_client=redis_mock,
        assigned_owner_id="89096379",
    )

    _assert(
        cm.publish_to_advisor.await_count == 1,
        "Fix-A/1: publish_to_advisor llamado 1 vez con owner explícito",
        f"got {cm.publish_to_advisor.await_count}",
    )
    _assert(
        cm.publish_broadcast.await_count == 0,
        "Fix-A/2: publish_broadcast NO llamado cuando hay owner",
        f"got {cm.publish_broadcast.await_count}",
    )
    if cm.publish_to_advisor.await_count == 1:
        call = cm.publish_to_advisor.await_args
        _assert(
            call.args[1] == "89096379",
            "Fix-A/3: owner correcto pasado a publish_to_advisor",
            f"got {call.args[1]}",
        )


async def test_fix_a_fallback_get_meta():
    """notify_new_message sin owner pero con state_manager → leer de meta Redis"""
    from middleware.websocket_manager import ConnectionManager

    cm = ConnectionManager()
    cm.publish_to_advisor = AsyncMock()
    cm.publish_broadcast = AsyncMock()

    # Mock state_manager devolviendo meta con assigned_owner_id
    fake_meta = MagicMock()
    fake_meta.assigned_owner_id = "89096380"
    sm_mock = MagicMock()
    sm_mock.get_meta = AsyncMock(return_value=fake_meta)

    await cm.notify_new_message(
        phone="+573002222222",
        canal="whatsapp",
        redis_client=MagicMock(),
        state_manager=sm_mock,
    )

    _assert(
        sm_mock.get_meta.await_count == 1,
        "Fix-A/4: get_meta consultado como fallback",
    )
    _assert(
        cm.publish_to_advisor.await_count == 1,
        "Fix-A/5: publish_to_advisor llamado tras fallback",
    )
    _assert(
        cm.publish_broadcast.await_count == 0,
        "Fix-A/6: publish_broadcast NO llamado tras fallback exitoso",
    )


async def test_fix_a_broadcast_last_resort():
    """notify_new_message sin owner + sin state_manager → broadcast global"""
    from middleware.websocket_manager import ConnectionManager

    cm = ConnectionManager()
    cm.publish_to_advisor = AsyncMock()
    cm.publish_broadcast = AsyncMock()

    await cm.notify_new_message(
        phone="+573003333333",
        canal="whatsapp",
        redis_client=MagicMock(),
    )

    _assert(
        cm.publish_broadcast.await_count == 1,
        "Fix-A/7: broadcast global como último recurso",
    )
    _assert(
        cm.publish_to_advisor.await_count == 0,
        "Fix-A/8: publish_to_advisor NO llamado sin owner",
    )


async def test_fix_a_phone_to_advisor_local_cache():
    """notify_new_message con phone_to_advisor poblado (transferencia) → usar cache local"""
    from middleware.websocket_manager import ConnectionManager

    cm = ConnectionManager()
    cm.publish_to_advisor = AsyncMock()
    cm.publish_broadcast = AsyncMock()
    cm.phone_to_advisor["+573004444444"] = "89096381"

    await cm.notify_new_message(
        phone="+573004444444",
        canal="whatsapp",
        redis_client=MagicMock(),
    )

    _assert(
        cm.publish_to_advisor.await_count == 1,
        "Fix-A/9: cache local phone_to_advisor respetado",
    )
    if cm.publish_to_advisor.await_count == 1:
        call = cm.publish_to_advisor.await_args
        _assert(
            call.args[1] == "89096381",
            "Fix-A/10: owner del cache local es correcto",
            f"got {call.args[1]}",
        )


# ═══════════════════════════════════════════════════════════════════
# Fix-D: Canal-agnostic inbox
# ═══════════════════════════════════════════════════════════════════


async def test_fix_d_canal_mismatch_detection():
    """get_inbox_unread_map detecta unread aunque canal difiera entre escritura/lectura"""
    from middleware.conversation_state import ConversationStateManager

    # Simulamos que el inbox tiene el phone escrito con canal "whatsapp"
    # pero get_active_contacts lo devuelve con canal "mercado_libre"
    cm = ConversationStateManager.__new__(ConversationStateManager)
    cm.redis = MagicMock()
    cm.redis.zrange = AsyncMock(
        return_value=[
            "+573001111111:whatsapp",
            "+573002222222:whatsapp",
        ]
    )

    contacts = [
        {"phone": "+573001111111", "canal": "mercado_libre"},  # mismatch
        {"phone": "+573002222222", "canal": "whatsapp"},        # match
        {"phone": "+573009999999", "canal": "whatsapp"},        # no en inbox
    ]

    result = await cm.get_inbox_unread_map("89096379", contacts)

    _assert(
        result.get("+573001111111") is True,
        "Fix-D/1: badge detectado con canal mismatch (mercado_libre)",
        f"got {result}",
    )
    _assert(
        result.get("+573002222222") is True,
        "Fix-D/2: badge detectado con canal exacto (whatsapp)",
    )
    _assert(
        result.get("+573009999999") is False,
        "Fix-D/3: phone no en inbox → no unread",
    )


async def test_fix_d_bytes_decoding():
    """get_inbox_unread_map tolera members como bytes (redis sin decode_responses)"""
    from middleware.conversation_state import ConversationStateManager

    cm = ConversationStateManager.__new__(ConversationStateManager)
    cm.redis = MagicMock()
    cm.redis.zrange = AsyncMock(
        return_value=[b"+573001111111:whatsapp"]
    )

    result = await cm.get_inbox_unread_map(
        "89096379", [{"phone": "+573001111111", "canal": "whatsapp"}]
    )

    _assert(
        result.get("+573001111111") is True,
        "Fix-D/4: members en bytes decodificados correctamente",
    )


async def test_fix_d_remove_canal_agnostic():
    """remove_from_advisor_inbox remueve TODOS los members con el mismo phone"""
    from middleware.conversation_state import ConversationStateManager

    cm = ConversationStateManager.__new__(ConversationStateManager)
    cm.redis = MagicMock()
    cm.redis.zrange = AsyncMock(
        return_value=[
            "+573001111111:whatsapp",
            "+573001111111:mercado_libre",  # mismo phone, canal distinto
            "+573009999999:whatsapp",
        ]
    )
    cm.redis.zrem = AsyncMock(return_value=2)

    await cm.remove_from_advisor_inbox("89096379", "+573001111111", "whatsapp")

    _assert(
        cm.redis.zrem.await_count == 1,
        "Fix-D/5: zrem llamado una vez con múltiples targets",
    )
    if cm.redis.zrem.await_count == 1:
        args = cm.redis.zrem.await_args.args
        members_removed = set(args[1:])
        _assert(
            "+573001111111:whatsapp" in members_removed
            and "+573001111111:mercado_libre" in members_removed,
            "Fix-D/6: todos los canales del phone removidos",
            f"got {members_removed}",
        )
        _assert(
            "+573009999999:whatsapp" not in members_removed,
            "Fix-D/7: otros phones NO afectados",
        )


async def test_fix_d_empty_inbox_safe():
    """get_inbox_unread_map con inbox vacío retorna map de Falses"""
    from middleware.conversation_state import ConversationStateManager

    cm = ConversationStateManager.__new__(ConversationStateManager)
    cm.redis = MagicMock()
    cm.redis.zrange = AsyncMock(return_value=[])

    result = await cm.get_inbox_unread_map(
        "89096379",
        [{"phone": "+573001111111", "canal": "whatsapp"}],
    )
    _assert(
        result == {"+573001111111": False},
        "Fix-D/8: inbox vacío → todos los contactos leídos",
        f"got {result}",
    )


async def test_fix_d_fail_safe_redis_error():
    """get_inbox_unread_map con error de Redis retorna {} (no rompe UI)"""
    from middleware.conversation_state import ConversationStateManager

    cm = ConversationStateManager.__new__(ConversationStateManager)
    cm.redis = MagicMock()
    cm.redis.zrange = AsyncMock(side_effect=Exception("Redis down"))

    result = await cm.get_inbox_unread_map(
        "89096379", [{"phone": "+573001111111", "canal": "whatsapp"}]
    )
    _assert(
        result == {},
        "Fix-D/9: fail-safe con error Redis → {}",
        f"got {result}",
    )


# ═══════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════


async def main():
    print("\n" + "=" * 60)
    print(" TEST SUITE — Real-Time Routing + Canal-Agnostic Inbox")
    print("=" * 60 + "\n")

    print(">>Fix-A: Targeted routing")
    await test_fix_a_targeted_with_explicit_owner()
    await test_fix_a_fallback_get_meta()
    await test_fix_a_broadcast_last_resort()
    await test_fix_a_phone_to_advisor_local_cache()

    print("\n>>Fix-D: Canal-agnostic inbox")
    await test_fix_d_canal_mismatch_detection()
    await test_fix_d_bytes_decoding()
    await test_fix_d_remove_canal_agnostic()
    await test_fix_d_empty_inbox_safe()
    await test_fix_d_fail_safe_redis_error()

    print("\n" + "=" * 60)
    total = len(_results)
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = total - passed
    print(f" Total: {total} | PASS Passed: {passed} | FAIL Failed: {failed}")
    print("=" * 60 + "\n")

    if failed > 0:
        print("FALLOS:")
        for name, ok, reason in _results:
            if not ok:
                print(f"  - {name}: {reason}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())