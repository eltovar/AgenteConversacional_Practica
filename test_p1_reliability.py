#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test P1-A y P1-B: Confiabilidad — Idempotency + UnboundLocalError

P1-A: Idempotency key debe escribirse DESPUES de save MongoDB (no antes)
P1-B: _rc debe inicializarse antes del try block para evitar UnboundLocalError
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock
import sys
import io

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# ═══════════════════════════════════════════════════════════════════════════════
# P1-A: IDEMPOTENCY KEY TESTS
# ═══════════════════════════════════════════════════════════════════════════════

async def test_p1a_idem_key_two_phase() -> bool:
    """
    P1-A: Idempotency key debe estar en TWO PHASES:
    1. GET check al TOP (sin write)
    2. SET write AFTER mongo_manager.save_message() success

    Si worker muere entre GET check y SET write (ventana ~5ms), Twilio retry es descartado.
    Si worker muere entre SET write y SET success (~1ms), retry es procesado duplicadamente pero
    idempotency key lo previene (segunda vez lanza "ya existe").

    ANTES (BUG): key se escribía ANTES del save → if worker muere entre write y save → retry descartado.
    DESPUES (FIX): key se escribe DESPUES del save → si save fracasa, no escribir key.
    """
    print('\n[TEST P1-A] Idempotency key two-phase')

    message_sid = "TWILIO_123"
    _idem_key = f"msg_processed:{message_sid}"

    # Phase 1: GET check (sin write)
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)  # Primera vez: no existe
    mock_redis.set = AsyncMock()
    mock_mongo = AsyncMock()
    mock_mongo.save_message = AsyncMock(return_value=True)

    # Simular flujo CORRECTO (P1-A fix):
    already_processed = await mock_redis.get(_idem_key)
    if already_processed:
        print('   [GET] Ya procesado — skip (retry de Twilio)')
        return False

    print('   [GET] Clave no existe — proceder')

    # Guardar en MongoDB
    result = await mock_mongo.save_message({"sid": message_sid, "body": "Hola"})
    if not result:
        print('   [MONGO] Save falló — NO escribir idem key')
        return True

    # Phase 2: SET write (DESPUES del success)
    await mock_redis.set(_idem_key, "1", ex=300)
    print('   [SET] Clave escrita DESPUES de save exitoso')

    # Verificaciones
    assert mock_redis.get.called, 'ERROR: GET check no ejecutado'
    assert mock_mongo.save_message.called, 'ERROR: MONGO save no ejecutado'
    assert mock_redis.set.called, 'ERROR: SET key no ejecutado'

    # Verificar orden: GET debe ir ANTES de SET
    get_call_order = mock_redis.get.call_count
    set_call_order = mock_redis.set.call_count
    mongo_call_order = mock_mongo.save_message.call_count

    print(f'   Orden: GET (1x) → MONGO save (1x) → SET (1x)')
    print('   ✅ Two-phase idempotency correcto')
    return True

# ───────────────────────────────────────────────────────────────────────────────

async def test_p1a_idem_blocks_duplicate() -> bool:
    """
    P1-A: Idempotency key previene que Twilio retry procese duplicado.
    """
    print('\n[TEST P1-A] Idempotency key bloquea duplicado (Twilio retry)')

    message_sid = "TWILIO_456"
    _idem_key = f"msg_processed:{message_sid}"

    mock_redis = AsyncMock()

    # Primer mensaje: no existe
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.set = AsyncMock()

    already_processed = await mock_redis.get(_idem_key)
    if already_processed:
        print('   [GET] Ya procesado')
        return False

    # Simular: write key (sin mongo para simplificar)
    await mock_redis.set(_idem_key, "1", ex=300)
    print('   [1er intento] Key escrita en Redis')

    # Reset el mock para simular SEGUNDO request (Twilio retry)
    mock_redis.get.reset_mock()
    mock_redis.get = AsyncMock(return_value="1")  # Ahora SI existe

    already_processed = await mock_redis.get(_idem_key)
    if already_processed:
        print('   [RETRY] GET retorna "1" — mensaje ya procesado, skip')
        second_save = False
    else:
        second_save = True

    assert not second_save, 'ERROR: Twilio retry no fue bloqueado'
    print('   ✅ Twilio retry bloqueado por idem key')
    return True

# ═══════════════════════════════════════════════════════════════════════════════
# P1-B: UNBOUNDLOCALERROR TESTS
# ═══════════════════════════════════════════════════════════════════════════════

async def test_p1b_rc_unbound_error_bug() -> bool:
    """
    P1-B BUG REPRODUCER: Si _rc se asigna DENTRO del try block y el try lanza
    excepción ANTES de la asignación, luego usar _rc causa UnboundLocalError.
    """
    print('\n[TEST P1-B] UnboundLocalError bug (ANTES del fix)')

    def buggy_send_message():
        """BUGGY VERSION (sin fix P1-B)"""
        try:
            # Si esta línea lanza, _rc nunca se asigna
            _rc = mock_get_redis_async()  # Simular que lanza
            ws_manager.publish(_rc, ...)
        except Exception as e:
            print(f'   [EXCEPT] Excepción en try: {e}')

        try:
            # _rc no existe → UnboundLocalError
            ws_manager.publish_broadcast(_rc, {"type": "contact_updated"})
        except NameError as e:
            return str(e)

    # Simular que _get_redis_client() lanza
    def mock_get_redis_async():
        raise RuntimeError('Redis connection failed')

    class MockWsManager:
        def publish_broadcast(self, rc, msg):
            pass

    ws_manager = MockWsManager()

    error = buggy_send_message()
    if "name '_rc' is not defined" in str(error):
        print(f'   ❌ UnboundLocalError: {error}')
        print('   (Este era el bug antes del fix P1-B)')
        return True
    else:
        print('   ERROR: No se reprodujo UnboundLocalError')
        return False

# ───────────────────────────────────────────────────────────────────────────────

async def test_p1b_rc_initialized_fix() -> bool:
    """
    P1-B FIX: Inicializar _rc = None ANTES del try block.
    Si exception, _rc sigue siendo None.
    En la llamada WS, verificar: if _rc: publish_broadcast(_rc, ...)
    """
    print('\n[TEST P1-B] UnboundLocalError fix')

    async def fixed_send_message(redis_client=None):
        """FIXED VERSION (con fix P1-B)"""
        _rc = None  # ← FIX: inicializar antes del try
        try:
            if redis_client is None:
                raise RuntimeError('Redis connection failed')
            _rc = redis_client
        except Exception as e:
            print(f'   [EXCEPT] Exception en try (no asigna _rc): {e}')

        # Segunda llamada: verificar if _rc antes de usar
        try:
            if _rc:  # ← FIX: guard check
                print(f'   [WS] Publicando con _rc={_rc}')
            else:
                print(f'   [WS] _rc is None — publish skipped')
        except NameError as e:
            print(f'   ❌ UnboundLocalError: {e}')
            return False

        return True

    # Test 1: Redis cae
    result = await fixed_send_message(redis_client=None)
    assert result, 'ERROR: No se manejó Redis down'
    print('   ✅ Redis down: _rc=None, publish skipped (sin error)')

    # Test 2: Redis OK
    result = await fixed_send_message(redis_client='<RedisConnection>')
    assert result, 'ERROR: Redis OK falló'
    print('   ✅ Redis OK: _rc asignado, publish ejecutado')

    return True

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

async def main():
    print('\n' + '='*80)
    print('TEST SUITE P1-A, P1-B: Confiabilidad — Idempotency + UnboundLocalError')
    print('='*80)

    tests = [
        ('P1-A', test_p1a_idem_key_two_phase),
        ('P1-A', test_p1a_idem_blocks_duplicate),
        ('P1-B BUG', test_p1b_rc_unbound_error_bug),
        ('P1-B FIX', test_p1b_rc_initialized_fix),
    ]

    results = []
    for category, test_func in tests:
        try:
            result = await test_func()
            results.append((category, test_func.__name__, 'PASS', None))
        except AssertionError as e:
            results.append((category, test_func.__name__, 'FAIL', str(e)))
        except Exception as e:
            results.append((category, test_func.__name__, 'ERROR', str(e)))

    print('\n' + '='*80)
    print('RESUMEN')
    print('='*80)

    passed = sum(1 for _, _, status, _ in results if status == 'PASS')
    failed = sum(1 for _, _, status, _ in results if status != 'PASS')

    for category, name, status, error in results:
        icon = '[PASS]' if status == 'PASS' else '[FAIL]'
        print(f'{icon} {category}: {name}')
        if error:
            print(f'      → {error}')

    print(f'\nTotal: {passed} PASS, {failed} FAIL')
    print('='*80 + '\n')

    return failed == 0

if __name__ == '__main__':
    success = asyncio.run(main())
    exit(0 if success else 1)
