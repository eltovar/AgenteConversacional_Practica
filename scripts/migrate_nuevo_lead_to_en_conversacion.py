"""
Script de migración única: mueve TODOS los contactos de 'Nuevo Lead' (1326631578)
a 'En Conversación' (1326623075) en HubSpot.

Ejecutar UNA VEZ antes de hacer el deploy del código actualizado:
    python -m scripts.migrate_nuevo_lead_to_en_conversacion

La migración es forward (stage 1 → stage 2), por lo que un PATCH directo es suficiente
(no se requiere el two-step de limpiar primero que se necesita para movimientos backward).

Post-migración: verificar en HubSpot que 0 contactos quedan en 'Nuevo Lead',
luego eliminar ese stage desde HubSpot → Settings → Properties → Lifecycle stage.
"""

import asyncio
import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY") or os.getenv("HUBSPOT_TOKEN")
STAGE_ORIGEN = "1326631578"   # Nuevo Lead (a eliminar)
STAGE_DESTINO = "1326623075"  # En Conversación (stage unificado)
HUBSPOT_BASE = "https://api.hubapi.com"
PAGE_SIZE = 100
PAUSE_BETWEEN_PATCHES_S = 0.15  # 150ms — HubSpot soft cap 10 req/s


def _headers() -> dict:
    return {"Authorization": f"Bearer {HUBSPOT_API_KEY}", "Content-Type": "application/json"}


async def search_contacts_in_stage(client: httpx.AsyncClient, after: str = "") -> dict:
    """Pagina contactos con lifecyclestage = STAGE_ORIGEN."""
    body = {
        "filterGroups": [
            {"filters": [{"propertyName": "lifecyclestage", "operator": "EQ", "value": STAGE_ORIGEN}]}
        ],
        "properties": ["firstname", "lastname", "phone", "lifecyclestage"],
        "limit": PAGE_SIZE,
    }
    if after:
        body["after"] = after

    r = await client.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
        headers=_headers(),
        json=body,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


async def patch_contact_stage(client: httpx.AsyncClient, contact_id: str) -> bool:
    """PATCH directo (forward move). Retorna True si exitoso."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
    for attempt in range(3):
        r = await client.patch(
            url,
            headers=_headers(),
            json={"properties": {"lifecyclestage": STAGE_DESTINO}},
            timeout=20,
        )
        if r.status_code == 200:
            return True
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 12 * (attempt + 1)))
            print(f"  [429] Rate limit — esperando {wait}s...")
            await asyncio.sleep(wait)
        else:
            print(f"  [ERROR] {contact_id}: HTTP {r.status_code} — {r.text[:120]}")
            return False
    return False


async def main():
    if not HUBSPOT_API_KEY:
        print("ERROR: HUBSPOT_API_KEY no configurada en .env")
        sys.exit(1)

    print(f"Migración: {STAGE_ORIGEN} (Nuevo Lead) → {STAGE_DESTINO} (En Conversación)")
    print("=" * 60)

    async with httpx.AsyncClient() as client:
        total_found = 0
        total_ok = 0
        total_err = 0
        after = ""

        while True:
            data = await search_contacts_in_stage(client, after)
            results = data.get("results", [])
            if not results:
                break

            total_found += len(results)
            print(f"\nPágina: {len(results)} contactos encontrados (total hasta ahora: {total_found})")

            for c in results:
                cid = c["id"]
                name = f"{c['properties'].get('firstname', '')} {c['properties'].get('lastname', '')}".strip() or "Sin nombre"
                phone = c["properties"].get("phone", "—")
                ok = await patch_contact_stage(client, cid)
                if ok:
                    total_ok += 1
                    print(f"  ✓ {cid} | {name} | {phone}")
                else:
                    total_err += 1
                    print(f"  ✗ {cid} | {name} | {phone} — FALLÓ")
                await asyncio.sleep(PAUSE_BETWEEN_PATCHES_S)

            paging = data.get("paging", {})
            after = paging.get("next", {}).get("after", "")
            if not after:
                break

    print("\n" + "=" * 60)
    print(f"MIGRACIÓN COMPLETA")
    print(f"  Encontrados : {total_found}")
    print(f"  Migrados OK : {total_ok}")
    print(f"  Errores     : {total_err}")
    if total_err == 0:
        print("\n✓ 0 errores. Puedes proceder con el deploy y luego eliminar el stage")
        print("  'Nuevo Lead' (1326631578) desde HubSpot Settings → Properties → Lifecycle stage.")
    else:
        print(f"\n⚠ {total_err} contactos no migrados. Revisa los errores arriba y re-ejecuta.")


if __name__ == "__main__":
    asyncio.run(main())
