"""
Bug 3 — Naming: Verifica que las citas guardadas en Redis tienen contact_name poblado.

Muestra todas las citas activas en Redis con su campo contact_name para confirmar
que el fix de outbound_panel.py está persistiendo el firstname correctamente.

Uso:
    python scripts/check_naming_redis.py
    python scripts/check_naming_redis.py +573001234567   # filtrar por phone específico
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Fix encoding en Windows (cp1252 no soporta caracteres Unicode como ⚠)
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from dotenv import load_dotenv
load_dotenv()

import redis.asyncio as redis_async

REDIS_URL = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))

RESET  = "\033[0m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BOLD   = "\033[1m"


async def main():
    phone_filter = sys.argv[1] if len(sys.argv) > 1 else None

    r = redis_async.from_url(REDIS_URL, decode_responses=True)

    print(f"\n{BOLD}=== Bug 3 Naming — Citas en Redis ==={RESET}")
    print(f"Redis: {REDIS_URL.split('@')[-1]}")
    if phone_filter:
        print(f"Filtro: {phone_filter}\n")

    # Buscar todas las claves de citas
    pattern = f"appointment:{phone_filter}:*" if phone_filter else "appointment:+*"
    keys = await r.keys(pattern)

    if not keys:
        print(f"{YELLOW}⚠  No se encontraron citas con el patrón '{pattern}'{RESET}")
        print("   Crea una cita desde el panel primero.\n")
        await r.aclose()
        return

    print(f"Citas encontradas: {len(keys)}\n")

    ok_count = 0
    fallback_count = 0

    for key in sorted(keys):
        data = await r.get(key)
        if not data:
            continue

        try:
            apt = json.loads(data)
        except json.JSONDecodeError:
            print(f"  {RED}✗ {key} — JSON inválido{RESET}")
            continue

        contact_name = apt.get("contact_name")
        contact_id   = apt.get("contact_id", "N/A")
        canal        = apt.get("canal", "N/A")
        scheduled    = apt.get("scheduled_datetime", "N/A")[:16]  # solo fecha+hora
        status       = apt.get("status", "N/A")

        if contact_name:
            indicator = f"{GREEN}✓{RESET}"
            name_display = f"{GREEN}{contact_name!r}{RESET}"
            ok_count += 1
        else:
            indicator = f"{YELLOW}⚠{RESET}"
            name_display = f"{YELLOW}None  ← fallback 'cliente' en scheduler{RESET}"
            fallback_count += 1

        # Extraer phone de la key (appointment:+57...:whatsapp)
        parts = key.split(":")
        phone = parts[1] if len(parts) > 1 else key

        print(f"  {indicator} {phone} ({canal})")
        print(f"     contact_name : {name_display}")
        print(f"     contact_id   : {contact_id}")
        print(f"     scheduled    : {scheduled}")
        print(f"     status       : {status}")
        print()

    # Resumen
    print(f"{BOLD}Resumen:{RESET}")
    print(f"  {GREEN}✓ Con nombre   : {ok_count}{RESET}")
    print(f"  {YELLOW}⚠ Sin nombre   : {fallback_count}{RESET}")

    if fallback_count > 0:
        print(f"\n{YELLOW}  Estas citas usarán 'cliente' en recordatorios.")
        print(f"  Son citas creadas ANTES del fix o contactos sin firstname en HubSpot.{RESET}")
    else:
        print(f"\n{GREEN}  ✅ Todas las citas tienen nombre. Fix funcionando correctamente.{RESET}")

    await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())
