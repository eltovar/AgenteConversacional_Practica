"""
Bug 3 — Naming: Backfill retroactivo de contact_name en citas existentes.

Consulta HubSpot por contact_id de cada cita sin nombre y escribe el firstname
en Redis. Solo procesa citas con status=pending y fecha futura (las que aún
pueden disparar recordatorios).

Uso:
    python scripts/backfill_appointment_names.py          # dry-run (solo muestra, no escribe)
    python scripts/backfill_appointment_names.py --apply  # aplica los cambios en Redis

Seguro de ejecutar: las citas completed/cancelled no se tocan.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from dotenv import load_dotenv
load_dotenv()

import httpx
import redis.asyncio as redis_async
from datetime import datetime
from zoneinfo import ZoneInfo

REDIS_URL   = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
HS_API_KEY  = os.getenv("HUBSPOT_API_KEY")
BOGOTA_TZ   = ZoneInfo("America/Bogota")

RESET  = "\033[0m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BLUE   = "\033[94m"
BOLD   = "\033[1m"

DRY_RUN = "--apply" not in sys.argv


async def get_firstname_from_hubspot(client: httpx.AsyncClient, contact_id: str) -> str:
    """Consulta HubSpot y retorna el firstname del contacto, o '' si falla."""
    if not HS_API_KEY or not contact_id:
        return ""
    try:
        url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {HS_API_KEY}"},
            params={"properties": "firstname"},
            timeout=8.0
        )
        if resp.status_code == 200:
            props = resp.json().get("properties", {})
            return (props.get("firstname") or "").strip()
        elif resp.status_code == 429:
            print(f"  {YELLOW}⚠ HubSpot 429 (rate limit) para {contact_id}{RESET}")
        elif resp.status_code == 404:
            print(f"  {YELLOW}⚠ Contacto {contact_id} no encontrado en HubSpot{RESET}")
        else:
            print(f"  {YELLOW}⚠ HubSpot {resp.status_code} para {contact_id}{RESET}")
    except Exception as e:
        print(f"  {RED}✗ Error HubSpot para {contact_id}: {e}{RESET}")
    return ""


async def main():
    if not HS_API_KEY:
        print(f"{RED}✗ HUBSPOT_API_KEY no configurada. Abortando.{RESET}")
        sys.exit(1)

    r = redis_async.from_url(REDIS_URL, decode_responses=True)
    now_bogota = datetime.now(BOGOTA_TZ)

    print(f"\n{BOLD}=== Bug 3 Naming — Backfill de Nombres ==={RESET}")
    print(f"Redis : {REDIS_URL.split('@')[-1]}")
    print(f"Ahora : {now_bogota.strftime('%Y-%m-%d %H:%M')} (Bogotá)")
    print(f"Modo  : {'DRY-RUN (sin cambios)' if DRY_RUN else f'{BOLD}APLICANDO CAMBIOS{RESET}'}\n")

    if DRY_RUN:
        print(f"{YELLOW}  Para aplicar cambios: python scripts/backfill_appointment_names.py --apply{RESET}\n")

    # Obtener todas las citas
    keys = await r.keys("appointment:+*")
    print(f"Total citas en Redis: {len(keys)}\n")

    candidates = []
    skipped    = []

    for key in sorted(keys):
        data = await r.get(key)
        if not data:
            continue
        try:
            apt = json.loads(data)
        except json.JSONDecodeError:
            continue

        status       = apt.get("status", "")
        contact_name = apt.get("contact_name")
        contact_id   = apt.get("contact_id", "")
        scheduled_str = apt.get("scheduled_datetime", "")

        # Solo procesar citas pending sin nombre
        if status != "pending":
            skipped.append((key, f"status={status}"))
            continue
        if contact_name:
            skipped.append((key, f"ya tiene nombre='{contact_name}'"))
            continue
        if not contact_id:
            skipped.append((key, "sin contact_id — no se puede consultar HubSpot"))
            continue

        # Solo citas futuras (que aún pueden disparar scheduler)
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_str)
            if scheduled_dt.tzinfo is None:
                scheduled_dt = scheduled_dt.replace(tzinfo=BOGOTA_TZ)
            if scheduled_dt <= now_bogota:
                skipped.append((key, f"fecha pasada ({scheduled_str[:16]})"))
                continue
        except (ValueError, TypeError):
            skipped.append((key, f"fecha inválida: {scheduled_str!r}"))
            continue

        candidates.append((key, apt, scheduled_str[:16]))

    print(f"Citas candidatas a backfill (pending + futuras + sin nombre): {len(candidates)}")
    print(f"Citas omitidas: {len(skipped)}\n")

    if not candidates:
        print(f"{GREEN}✓ No hay citas que necesiten backfill.{RESET}")
        await r.aclose()
        return

    # Procesar candidatas
    updated = 0
    failed  = 0

    async with httpx.AsyncClient() as hs_client:
        for key, apt, scheduled_display in candidates:
            contact_id = apt.get("contact_id", "")
            parts = key.split(":")
            phone = parts[1] if len(parts) > 1 else key

            print(f"  {BLUE}→{RESET} {phone}  scheduled={scheduled_display}  contact_id={contact_id}")

            firstname = await get_firstname_from_hubspot(hs_client, contact_id)

            if firstname:
                if DRY_RUN:
                    print(f"     {GREEN}[DRY-RUN] Escribiría contact_name='{firstname}'{RESET}")
                    updated += 1
                else:
                    # Actualizar solo el campo contact_name en el JSON existente
                    apt["contact_name"] = firstname
                    ttl = await r.ttl(key)
                    ttl = ttl if ttl > 0 else 30 * 24 * 3600  # mantener TTL existente

                    await r.set(key, json.dumps(apt), ex=ttl)
                    print(f"     {GREEN}✓ contact_name='{firstname}' escrito en Redis{RESET}")
                    updated += 1

                # Pequeña pausa para no saturar HubSpot (rate limit: 100 req/10s)
                await asyncio.sleep(0.15)
            else:
                print(f"     {YELLOW}⚠ Sin firstname en HubSpot — se mantendrá fallback 'cliente'{RESET}")
                failed += 1

    print(f"\n{BOLD}Resumen:{RESET}")
    if DRY_RUN:
        print(f"  {GREEN}Actualizarían : {updated}{RESET}")
    else:
        print(f"  {GREEN}✓ Actualizados : {updated}{RESET}")
    print(f"  {YELLOW}⚠ Sin nombre  : {failed}{RESET}")
    print(f"  Omitidos      : {len(skipped)}")

    if DRY_RUN and updated > 0:
        print(f"\n{YELLOW}  Ejecuta con --apply para guardar los cambios.{RESET}")

    await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())
