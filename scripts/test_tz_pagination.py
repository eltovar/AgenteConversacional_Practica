"""
Test: Verifica fixes de Hallazgo 5 (Timezone) y Hallazgo 6 (ZSET pagination).
Checks estáticos únicamente — sin conexión a servicios externos.

Uso: python scripts/test_tz_pagination.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─── HALLAZGO 5: TIMEZONE ────────────────────────────────────────────────────

def test_outbound_handler_no_utcnow():
    print("\n[1] outbound_handler.py — human_active_since usa get_bogota_now()")
    try:
        with open("integrations/hubspot/outbound_handler.py", "r", encoding="utf-8") as f:
            lines = f.readlines()

        utcnow_human = [
            (i + 1, l.strip())
            for i, l in enumerate(lines)
            if "utcnow()" in l and "human_active_since" in l
        ]
        bogota_human = [
            (i + 1, l.strip())
            for i, l in enumerate(lines)
            if "get_bogota_now()" in l and "human_active_since" in l
        ]

        if not utcnow_human and len(bogota_human) >= 2:
            print(f"  [OK] {len(bogota_human)} call(s) a get_bogota_now() para human_active_since")
            return True
        else:
            if utcnow_human:
                print(f"  [FALLO] Todavía hay utcnow() en human_active_since:")
                for ln, code in utcnow_human:
                    print(f"    Línea {ln}: {code}")
            if len(bogota_human) < 2:
                print(f"  [FALLO] Solo {len(bogota_human)} de 2 call(s) reemplazados")
            return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


def test_outbound_handler_import():
    print("\n[2] outbound_handler.py — importa get_bogota_now")
    try:
        with open("integrations/hubspot/outbound_handler.py", "r", encoding="utf-8") as f:
            content = f.read()
        if "from middleware.conversation_state import get_bogota_now" in content:
            print("  [OK] get_bogota_now importado correctamente")
            return True
        else:
            print("  [FALLO] import get_bogota_now no encontrado")
            return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


def test_outbound_panel_no_datetime_now():
    print("\n[3] outbound_panel.py — timestamp fields usan get_bogota_now()")
    try:
        with open("middleware/outbound_panel.py", "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Buscar datetime.now().isoformat() en líneas que no sean filenames (no strftime)
        remaining = [
            (i + 1, l.strip())
            for i, l in enumerate(lines)
            if "datetime.now().isoformat()" in l
            and "strftime" not in l
            and "filename" not in l.lower()
        ]
        bogota_ts = [
            (i + 1, l.strip())
            for i, l in enumerate(lines)
            if "get_bogota_now().isoformat()" in l
            and "def " not in l
        ]

        if not remaining:
            print(f"  [OK] Sin datetime.now().isoformat() en timestamp fields. {len(bogota_ts)} call(s) a get_bogota_now()")
            return True
        else:
            print(f"  [FALLO] Todavía hay datetime.now().isoformat() sin timezone:")
            for ln, code in remaining:
                print(f"    Línea {ln}: {code}")
            return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


def test_outbound_panel_import():
    print("\n[4] outbound_panel.py — importa get_bogota_now")
    try:
        with open("middleware/outbound_panel.py", "r", encoding="utf-8") as f:
            content = f.read()
        if "get_bogota_now" in content.split("# Router")[0]:  # solo en sección imports
            print("  [OK] get_bogota_now en imports de outbound_panel.py")
            return True
        else:
            print("  [FALLO] get_bogota_now no encontrado en imports")
            return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


# ─── HALLAZGO 6: ZSET PAGINATION ─────────────────────────────────────────────

def test_app_pagination():
    print("\n[5] app.py — check_conversation_timeouts() pagina el ZSET")
    try:
        with open("app.py", "r", encoding="utf-8") as f:
            content = f.read()

        # Verificar que el loop de paginación existe
        has_while = "while True:" in content and "_offset" in content and "_page_size" in content
        has_paginated_call = "get_all_human_active_contacts(limit=_page_size, offset=_offset)" in content
        no_bare_call = "get_all_human_active_contacts()" not in content

        if has_while and has_paginated_call and no_bare_call:
            print("  [OK] Loop de paginación presente con limit + offset")
            return True
        else:
            if not has_while:
                print("  [FALLO] No se encontró 'while True:' con _offset/_page_size")
            if not has_paginated_call:
                print("  [FALLO] No se encontró llamada paginada a get_all_human_active_contacts()")
            if not no_bare_call:
                print("  [FALLO] Todavía hay llamada sin parámetros a get_all_human_active_contacts()")
            return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 58)
    print("TEST: Timezone Mismatch + ZSET Pagination (H5 + H6)")
    print("=" * 58)

    results = [
        test_outbound_handler_no_utcnow(),
        test_outbound_handler_import(),
        test_outbound_panel_no_datetime_now(),
        test_outbound_panel_import(),
        test_app_pagination(),
    ]

    passed = sum(1 for r in results if r)
    total = len(results)

    print("\n" + "=" * 58)
    print(f"Resultado: {passed}/{total} checks pasaron")
    if passed == total:
        print("[OK] Hallazgo 5 (Timezone) y Hallazgo 6 (ZSET paginación) corregidos")
    else:
        print("[FALLO] Revisar los items marcados arriba")
    print("=" * 58)
