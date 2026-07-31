"""
Tests de validacion para la redistribucion de canales jul 2026.
Jubeny (89096378) recibe todos los canales excepto finca_raiz y metrocuadrado.
Luisa (89096380) solo recibe finca_raiz y metrocuadrado.

Ejecutar: python -m pytest tests/test_channel_redistribution.py -v
O:       python tests/test_channel_redistribution.py
"""
import asyncio
import sys
import os
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

JUBENY = "89096378"
LUISA = "89096380"
LUISA_CHANNELS = {"finca_raiz", "metrocuadrado"}

os.environ.setdefault("HUBSPOT_API_KEY", "test-dummy-key")
os.environ.setdefault("HUBSPOT_ACCESS_TOKEN", "test-dummy-token")


# ── Test 1: Jubeny owns all except finca_raiz and metrocuadrado ───────────

def test_1_jubeny_owns_all_except_finca_metro():
    from utils.channels_registry import CHANNELS
    for name, cfg in CHANNELS.items():
        if name in LUISA_CHANNELS:
            assert cfg["owner_id"] == LUISA, (
                f"{name} debe ser Luisa ({LUISA}), tiene {cfg['owner_id']}"
            )
        else:
            assert cfg["owner_id"] == JUBENY, (
                f"{name} debe ser Jubeny ({JUBENY}), tiene {cfg['owner_id']}"
            )
    print("  [PASS] Jubeny tiene todos los canales excepto finca_raiz y metrocuadrado")


# ── Test 2: No round-robin channels (owner_id=None) ──────────────────────

def test_2_no_round_robin_channels():
    from utils.channels_registry import CHANNELS
    for name, cfg in CHANNELS.items():
        assert cfg["owner_id"] is not None, (
            f"{name} tiene owner_id=None (round-robin eliminado)"
        )
    print("  [PASS] Ningun canal tiene owner_id=None")


# ── Test 3: get_channel_to_owner only returns non-None owners ─────────────

def test_3_channel_to_owner_complete():
    from utils.channels_registry import CHANNELS, get_channel_to_owner
    owners = get_channel_to_owner()
    assert len(owners) == len(CHANNELS), (
        f"get_channel_to_owner devuelve {len(owners)} canales, CHANNELS tiene {len(CHANNELS)}"
    )
    print(f"  [PASS] get_channel_to_owner cubre los {len(CHANNELS)} canales")


# ── Test 4: LeadAssigner routes pagina_web to Jubeny ─────────────────────

def test_4_lead_assigner_pagina_web():
    from integrations.hubspot.lead_assigner import LeadAssigner
    mock_redis = MagicMock()
    mock_redis.ping = MagicMock()
    mock_redis.get = MagicMock(return_value=None)
    la = LeadAssigner(redis_client=mock_redis)
    result = la.get_next_owner("pagina_web")
    assert result == JUBENY, f"pagina_web debe ir a Jubeny, fue a {result}"
    print("  [PASS] LeadAssigner asigna pagina_web a Jubeny")


# ── Test 5: LeadAssigner routes finca_raiz to Luisa ───────────────────────

def test_5_lead_assigner_finca_raiz():
    from integrations.hubspot.lead_assigner import LeadAssigner
    mock_redis = MagicMock()
    mock_redis.ping = MagicMock()
    mock_redis.get = MagicMock(return_value=None)
    la = LeadAssigner(redis_client=mock_redis)
    result = la.get_next_owner("finca_raiz")
    assert result == LUISA, f"finca_raiz debe ir a Luisa, fue a {result}"
    print("  [PASS] LeadAssigner asigna finca_raiz a Luisa")


# ── Test 6: LeadAssigner routes google_ads to Jubeny (was round-robin) ────

def test_6_lead_assigner_google_ads():
    from integrations.hubspot.lead_assigner import LeadAssigner
    mock_redis = MagicMock()
    mock_redis.ping = MagicMock()
    mock_redis.get = MagicMock(return_value=None)
    la = LeadAssigner(redis_client=mock_redis)
    result = la.get_next_owner("google_ads")
    assert result == JUBENY, f"google_ads debe ir a Jubeny, fue a {result}"
    print("  [PASS] LeadAssigner asigna google_ads a Jubeny (antes round-robin)")


# ── Test 7: LeadAssigner default fallback goes to Jubeny ─────────────────

def test_7_lead_assigner_default_fallback():
    from integrations.hubspot.lead_assigner import LeadAssigner
    mock_redis = MagicMock()
    mock_redis.ping = MagicMock()
    mock_redis.get = MagicMock(return_value=None)
    la = LeadAssigner(redis_client=mock_redis)
    result = la.get_next_owner("canal_inexistente_xyz")
    assert result == JUBENY, f"Canal desconocido debe ir a Jubeny via default, fue a {result}"
    print("  [PASS] LeadAssigner fallback default va a Jubeny")


# ── Test 8: Registry integrity — 16 channels, all fields present ──────────

def test_8_registry_integrity():
    from utils.channels_registry import CHANNELS
    assert len(CHANNELS) == 16, f"Se esperan 16 canales, hay {len(CHANNELS)}"
    required_fields = {"team", "owner_id", "category", "score_bonus", "link_patterns"}
    for name, cfg in CHANNELS.items():
        missing = required_fields - set(cfg.keys())
        assert not missing, f"{name} le faltan campos: {missing}"
    print("  [PASS] Registry tiene 16 canales con todos los campos")


# ── Test 9: equipo_directo only has finca_raiz and metrocuadrado ──────────

def test_9_equipo_directo_only_luisa_channels():
    from utils.channels_registry import CHANNELS
    directo_channels = {n for n, c in CHANNELS.items() if c["team"] == "equipo_directo"}
    assert directo_channels == LUISA_CHANNELS, (
        f"equipo_directo debe tener solo {LUISA_CHANNELS}, tiene {directo_channels}"
    )
    print("  [PASS] equipo_directo solo contiene finca_raiz y metrocuadrado")


# ── Test 10: STAGES_TRANSFER_TO_LUISA unaffected ─────────────────────────

def test_10_stage_transfer_unaffected():
    import re
    filepath = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "middleware", "outbound_panel.py"
    )
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    assert "LUISA_TRANSFER_TARGET" in content, (
        "LUISA_TRANSFER_TARGET no encontrado en outbound_panel.py"
    )
    assert 'LUISA_TRANSFER_TARGET = "89096380"' in content, (
        "LUISA_TRANSFER_TARGET debe apuntar a Luisa (89096380)"
    )
    print("  [PASS] STAGES_TRANSFER_TO_LUISA sigue apuntando a Luisa")


# ── Runner ────────────────────────────────────────────────────────────────

def run_all():
    tests = [
        ("Test 1: Jubeny tiene todos excepto finca/metro", test_1_jubeny_owns_all_except_finca_metro),
        ("Test 2: Sin canales round-robin", test_2_no_round_robin_channels),
        ("Test 3: get_channel_to_owner completo", test_3_channel_to_owner_complete),
        ("Test 4: pagina_web a Jubeny", test_4_lead_assigner_pagina_web),
        ("Test 5: finca_raiz a Luisa", test_5_lead_assigner_finca_raiz),
        ("Test 6: google_ads a Jubeny", test_6_lead_assigner_google_ads),
        ("Test 7: Default fallback a Jubeny", test_7_lead_assigner_default_fallback),
        ("Test 8: Registry integrity (16 canales)", test_8_registry_integrity),
        ("Test 9: equipo_directo solo finca/metro", test_9_equipo_directo_only_luisa_channels),
        ("Test 10: Stage transfer a Luisa intacto", test_10_stage_transfer_unaffected),
    ]

    passed = 0
    failed = 0

    print("\n" + "=" * 60)
    print("  Tests — Redistribucion de canales jul 2026")
    print("=" * 60 + "\n")

    for name, test_fn in tests:
        try:
            print(f"[RUN] {name}")
            test_fn()
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {e}")
            failed += 1
        except Exception as e:
            print(f"  [ERROR] {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"  Resultado: {passed} PASS | {failed} FAIL")
    print(f"{'=' * 60}\n")

    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
