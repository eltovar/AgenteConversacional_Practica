#!/usr/bin/env python3
"""
Script de migración: Transferir contactos de Jubeny → Luisa
en embudos de precio (Hasta 1.5M, Hasta 2M, Hasta 2.5M).

Solo cambia hubspot_owner_id — los contactos permanecen en el mismo embudo.
También actualiza Redis (conv_meta, advisor_inbox) si hay conversación activa.

Uso:
    # Modo DRY RUN (por defecto) — solo lista, no modifica nada:
    python scripts/migrate_price_funnels_to_luisa.py

    # Modo EJECUCIÓN — aplica cambios:
    python scripts/migrate_price_funnels_to_luisa.py --execute

Variables de entorno requeridas:
    HUBSPOT_API_KEY, REDIS_URL
"""

import asyncio
import os
import sys
import json
import argparse
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("migration")

if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ─── Constants ──────────────────────────────────────────────────────────

JUBENY_OWNER_ID = "89096378"
LUISA_OWNER_ID = "89096380"

FUNNELS_TO_MIGRATE = {
    "1326623067": "Hasta 1.5M",
    "1326631573": "Hasta 2M",
    "1326632625": "Hasta 2.5M",
}

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
REDIS_URL = os.getenv("REDIS_URL", "")
PANEL_BASE_URL = os.getenv("PANEL_BASE_URL", "").rstrip("/")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")


# ─── HubSpot Helpers ───────────────────────────────────────────────────

async def _get_httpx_client():
    import httpx
    return httpx.AsyncClient(timeout=30.0)


async def hubspot_search_contacts(
    client, stage_id: str, owner_id: str
) -> List[Dict[str, Any]]:
    """Busca todos los contactos en un embudo + owner, paginando."""
    all_contacts = []
    after = None
    pages = 0

    while pages < 50:
        payload = {
            "filterGroups": [{
                "filters": [
                    {"propertyName": "lifecyclestage", "operator": "EQ", "value": stage_id},
                    {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": owner_id},
                ]
            }],
            "properties": [
                "firstname", "lastname", "phone", "whatsapp_id",
                "hubspot_owner_id", "lifecyclestage", "email",
            ],
            "limit": 100,
        }
        if after:
            payload["after"] = after

        resp = await client.post(
            "https://api.hubapi.com/crm/v3/objects/contacts/search",
            json=payload,
            headers={
                "Authorization": f"Bearer {HUBSPOT_API_KEY}",
                "Content-Type": "application/json",
            },
        )

        if resp.status_code == 429:
            logger.warning("HubSpot 429 — esperando 10s...")
            await asyncio.sleep(10)
            continue

        if resp.status_code != 200:
            logger.error(f"HubSpot search error: {resp.status_code} {resp.text[:200]}")
            break

        data = resp.json()
        for item in data.get("results", []):
            props = item.get("properties", {})
            phone = (props.get("whatsapp_id") or props.get("phone") or "").strip()
            all_contacts.append({
                "contact_id": str(item.get("id")),
                "phone": phone,
                "firstname": (props.get("firstname") or "").strip(),
                "lastname": (props.get("lastname") or "").strip(),
                "owner_id": props.get("hubspot_owner_id") or owner_id,
                "stage": stage_id,
            })

        paging = data.get("paging") or {}
        after = (paging.get("next") or {}).get("after")
        if not after:
            break
        pages += 1
        await asyncio.sleep(0.2)

    return all_contacts


async def hubspot_reassign_owner(
    client, contact_id: str, to_owner_id: str, phone: str = ""
) -> bool:
    """Cambia hubspot_owner_id de un contacto."""
    from urllib.parse import quote

    properties: Dict[str, str] = {"hubspot_owner_id": to_owner_id}

    if phone and PANEL_BASE_URL and ADMIN_API_KEY:
        phone_encoded = quote(phone, safe="")
        properties["url_chat"] = (
            f"{PANEL_BASE_URL}/whatsapp/panel/"
            f"?key={ADMIN_API_KEY}&advisor={to_owner_id}&phone={phone_encoded}"
        )

    resp = await client.patch(
        f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}",
        json={"properties": properties},
        headers={
            "Authorization": f"Bearer {HUBSPOT_API_KEY}",
            "Content-Type": "application/json",
        },
    )

    if resp.status_code == 429:
        logger.warning(f"  429 en contact {contact_id} — esperando 10s...")
        await asyncio.sleep(10)
        resp = await client.patch(
            f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}",
            json={"properties": properties},
            headers={
                "Authorization": f"Bearer {HUBSPOT_API_KEY}",
                "Content-Type": "application/json",
            },
        )

    if resp.status_code == 200:
        return True
    logger.error(f"  PATCH falló contact {contact_id}: {resp.status_code} {resp.text[:200]}")
    return False


# ─── Redis Helpers ──────────────────────────────────────────────────────

async def get_redis():
    if not REDIS_URL:
        return None
    import redis.asyncio as aioredis
    return aioredis.from_url(REDIS_URL, decode_responses=True)


async def migrate_redis_owner(redis_client, phone: str, canal: str = "whatsapp"):
    """Actualiza conv_meta y advisor_inbox en Redis si existe."""
    if not redis_client or not phone:
        return False

    meta_key = f"conv_meta:{phone}:{canal}"
    try:
        meta_raw = await redis_client.hgetall(meta_key)
        if not meta_raw:
            return False

        current_owner = meta_raw.get("assigned_owner_id", "")
        if current_owner != JUBENY_OWNER_ID:
            return False

        await redis_client.hset(meta_key, mapping={
            "assigned_owner_id": LUISA_OWNER_ID,
        })

        ts = datetime.now(timezone.utc).timestamp()
        old_inbox = f"advisor_inbox:{JUBENY_OWNER_ID}"
        new_inbox = f"advisor_inbox:{LUISA_OWNER_ID}"
        await redis_client.zrem(old_inbox, phone)
        await redis_client.zadd(new_inbox, {phone: ts})

        logger.info(f"  Redis: {phone} owner {JUBENY_OWNER_ID} → {LUISA_OWNER_ID}")
        return True
    except Exception as e:
        logger.warning(f"  Redis error para {phone}: {e}")
        return False


# ─── Main ───────────────────────────────────────────────────────────────

async def main(execute: bool = False):
    mode = "EJECUCIÓN" if execute else "DRY RUN"
    logger.info(f"{'=' * 60}")
    logger.info(f"  MIGRACIÓN EMBUDOS PRECIO: Jubeny → Luisa [{mode}]")
    logger.info(f"{'=' * 60}")

    if not HUBSPOT_API_KEY:
        logger.error("HUBSPOT_API_KEY no configurada. Abortando.")
        sys.exit(1)

    client = await _get_httpx_client()
    redis_client = await get_redis()

    total_found = 0
    total_migrated = 0
    total_failed = 0
    total_redis = 0
    results_by_funnel: Dict[str, Dict] = {}

    for stage_id, stage_name in FUNNELS_TO_MIGRATE.items():
        logger.info(f"\n--- Embudo: {stage_name} (stage_id={stage_id}) ---")

        contacts = await hubspot_search_contacts(client, stage_id, JUBENY_OWNER_ID)
        count = len(contacts)
        total_found += count
        logger.info(f"  Encontrados: {count} contactos de Jubeny")

        funnel_result = {"found": count, "migrated": 0, "failed": 0, "redis": 0}

        for i, contact in enumerate(contacts, 1):
            cid = contact["contact_id"]
            name = contact["firstname"] or "(sin nombre)"
            phone = contact["phone"]

            if execute:
                success = await hubspot_reassign_owner(client, cid, LUISA_OWNER_ID, phone)
                if success:
                    funnel_result["migrated"] += 1
                    total_migrated += 1
                    logger.info(f"  [{i}/{count}] {name} (ID:{cid}) → Luisa OK")

                    if phone and redis_client:
                        redis_ok = await migrate_redis_owner(redis_client, phone)
                        if redis_ok:
                            funnel_result["redis"] += 1
                            total_redis += 1
                else:
                    funnel_result["failed"] += 1
                    total_failed += 1
                    logger.error(f"  [{i}/{count}] {name} (ID:{cid}) → FALLÓ")

                await asyncio.sleep(0.3)
            else:
                logger.info(f"  [{i}/{count}] {name} | phone={phone or '(vacío)'} | ID={cid}")

        results_by_funnel[stage_name] = funnel_result

    # Summary
    logger.info(f"\n{'=' * 60}")
    logger.info(f"  RESUMEN [{mode}]")
    logger.info(f"{'=' * 60}")
    for name, r in results_by_funnel.items():
        if execute:
            logger.info(f"  {name}: {r['found']} encontrados, {r['migrated']} migrados, {r['failed']} fallos, {r['redis']} Redis")
        else:
            logger.info(f"  {name}: {r['found']} contactos de Jubeny")
    logger.info(f"\n  TOTAL: {total_found} contactos encontrados")
    if execute:
        logger.info(f"  Migrados: {total_migrated} | Fallidos: {total_failed} | Redis: {total_redis}")

    if not execute and total_found > 0:
        logger.info(f"\n  Para ejecutar la migración real:")
        logger.info(f"  python scripts/migrate_price_funnels_to_luisa.py --execute")

    await client.aclose()
    if redis_client:
        await redis_client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrar contactos de embudos de precio Jubeny → Luisa")
    parser.add_argument("--execute", action="store_true", help="Ejecutar migración real (sin esto solo hace dry run)")
    args = parser.parse_args()

    asyncio.run(main(execute=args.execute))
