"""
Script one-shot: Sincronizar MongoDB conversations.owner_id con HubSpot hubspot_owner_id.

Resuelve:
1. Contactos de Monica que no le pertenecen (stale owner_id en MongoDB)
2. Contactos de Luisa que desaparecieron del panel (owner_id incorrecto impide el fallback)

Uso:
    python scripts/sync_mongodb_owners.py              # dry-run (solo reporta)
    python scripts/sync_mongodb_owners.py --apply      # aplica los cambios
    python scripts/sync_mongodb_owners.py --apply --redis  # también corrige Redis meta
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import asyncio
import json
import httpx
from datetime import datetime, timedelta, timezone

MONGODB_URI = os.getenv("MONGODB_URI")
REDIS_URL = os.getenv("REDIS_URL")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN")
APPLY = "--apply" in sys.argv
FIX_REDIS = "--redis" in sys.argv


async def get_hubspot_owner_for_phone(client: httpx.AsyncClient, phone: str) -> dict:
    """Busca contacto en HubSpot por teléfono y retorna owner_id + canal_origen."""
    phone_clean = phone.lstrip("+")
    resp = await client.post(
        "https://api.hubapi.com/crm/v3/objects/contacts/search",
        json={
            "filterGroups": [{
                "filters": [{
                    "propertyName": "phone",
                    "operator": "EQ",
                    "value": phone,
                }]
            }],
            "properties": ["hubspot_owner_id", "canal_origen", "firstname", "lastname"],
            "limit": 1,
        },
    )
    if resp.status_code == 200:
        results = resp.json().get("results", [])
        if results:
            props = results[0].get("properties", {})
            return {
                "contact_id": results[0]["id"],
                "hubspot_owner_id": props.get("hubspot_owner_id"),
                "canal_origen": props.get("canal_origen"),
                "name": f"{props.get('firstname', '')} {props.get('lastname', '')}".strip(),
            }
    return {}


async def main():
    import motor.motor_asyncio as motor

    print(f"=== Sync MongoDB owners con HubSpot ===")
    print(f"Modo: {'APPLY' if APPLY else 'DRY-RUN'}")
    print(f"Redis: {'SÍ' if FIX_REDIS else 'NO'}")
    print()

    mongo_client = motor.AsyncIOMotorClient(MONGODB_URI)
    db = mongo_client.get_default_database()

    # Obtener TODAS las conversaciones con owner_id
    cursor = db.conversations.find(
        {"owner_id": {"$exists": True, "$ne": None}},
        {"phone": 1, "canal": 1, "owner_id": 1, "display_name": 1, "last_message_at": 1, "_id": 0},
    ).sort("last_message_at", -1)

    all_convs = await cursor.to_list(length=5000)
    print(f"Total conversaciones con owner_id en MongoDB: {len(all_convs)}")

    # Estadísticas por owner
    owner_counts = {}
    for c in all_convs:
        oid = c.get("owner_id", "None")
        owner_counts[oid] = owner_counts.get(oid, 0) + 1

    owner_names = {
        "89096378": "Jubeny",
        "89096379": "Monica",
        "89096380": "Luisa",
    }
    print("\nDistribución actual MongoDB:")
    for oid, count in sorted(owner_counts.items(), key=lambda x: -x[1]):
        name = owner_names.get(oid, "???")
        print(f"  {oid} ({name}): {count} conversaciones")

    # Verificar contra HubSpot (en batches para evitar rate limit)
    mismatches = []
    no_hubspot = []
    correct = 0

    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {HUBSPOT_TOKEN}"},
        timeout=15.0,
    ) as client:
        for i, conv in enumerate(all_convs):
            phone = conv.get("phone", "")
            if not phone:
                continue

            hs = await get_hubspot_owner_for_phone(client, phone)

            if not hs or not hs.get("hubspot_owner_id"):
                no_hubspot.append(conv)
                continue

            hs_owner = hs["hubspot_owner_id"]
            mongo_owner = conv.get("owner_id")

            if str(mongo_owner) != str(hs_owner):
                mismatches.append({
                    "phone": phone,
                    "canal": conv.get("canal"),
                    "mongo_owner": mongo_owner,
                    "hubspot_owner": hs_owner,
                    "name": hs.get("name") or conv.get("display_name"),
                    "canal_origen": hs.get("canal_origen"),
                    "last_message_at": conv.get("last_message_at"),
                })
            else:
                correct += 1

            # Rate limit: ~5 req/sec
            if (i + 1) % 5 == 0:
                await asyncio.sleep(1.1)

            if (i + 1) % 50 == 0:
                print(f"  Verificados {i+1}/{len(all_convs)}... ({len(mismatches)} mismatches)")

    print(f"\n=== Resultados ===")
    print(f"Correctos: {correct}")
    print(f"Mismatches: {len(mismatches)}")
    print(f"Sin HubSpot: {len(no_hubspot)}")

    if mismatches:
        print(f"\n=== Mismatches detallados ===")

        # Agrupar por tipo de cambio
        changes = {}
        for m in mismatches:
            key = f"{owner_names.get(m['mongo_owner'], m['mongo_owner'])} → {owner_names.get(m['hubspot_owner'], m['hubspot_owner'])}"
            changes.setdefault(key, []).append(m)

        for change_type, items in sorted(changes.items(), key=lambda x: -len(x[1])):
            print(f"\n  {change_type}: {len(items)} contactos")
            for m in items[:5]:  # Mostrar primeros 5
                name = m.get("name") or m["phone"]
                print(f"    - {name} ({m['phone']}) canal={m.get('canal_origen', '?')}")
            if len(items) > 5:
                print(f"    ... y {len(items) - 5} más")

    if APPLY and mismatches:
        print(f"\n=== Aplicando {len(mismatches)} correcciones en MongoDB ===")
        fixed = 0
        for m in mismatches:
            try:
                result = await db.conversations.update_one(
                    {"phone": m["phone"], "canal": m.get("canal", "whatsapp")},
                    {"$set": {
                        "owner_id": m["hubspot_owner"],
                        "updated_at": datetime.now(timezone.utc),
                    }},
                )
                if result.modified_count > 0:
                    fixed += 1
            except Exception as e:
                print(f"  Error actualizando {m['phone']}: {e}")

        print(f"  MongoDB: {fixed}/{len(mismatches)} actualizados")

        if FIX_REDIS:
            import redis.asyncio as aioredis
            print(f"\n=== Aplicando correcciones en Redis meta ===")
            r = aioredis.from_url(REDIS_URL, decode_responses=True)
            redis_fixed = 0
            for m in mismatches:
                phone = m["phone"]
                canal = m.get("canal", "whatsapp")
                meta_key = f"conv_meta:{phone}:{canal}"
                try:
                    raw = await r.get(meta_key)
                    if raw:
                        meta = json.loads(raw)
                        old = meta.get("assigned_owner_id")
                        if str(old) != str(m["hubspot_owner"]):
                            meta["assigned_owner_id"] = m["hubspot_owner"]
                            ttl = await r.ttl(meta_key)
                            if ttl > 0:
                                await r.setex(meta_key, ttl, json.dumps(meta))
                            else:
                                await r.set(meta_key, json.dumps(meta))
                            redis_fixed += 1
                except Exception as e:
                    print(f"  Redis error {phone}: {e}")

            await r.aclose()
            print(f"  Redis: {redis_fixed} metas actualizados")

    # Reporte de contactos de Luisa activos últimas 24h
    print(f"\n=== Contactos Luisa (89096380) con actividad últimas 24h ===")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    luisa_recent = [
        c for c in all_convs
        if c.get("owner_id") == "89096380"
        and c.get("last_message_at")
        and c["last_message_at"].replace(tzinfo=timezone.utc) > cutoff
    ]

    # También buscar en mismatches los que DEBERÍAN ser de Luisa
    luisa_should_have = [m for m in mismatches if m["hubspot_owner"] == "89096380"]
    luisa_lost = [m for m in mismatches if m["mongo_owner"] == "89096380"]

    print(f"  Activos últimas 24h (MongoDB dice Luisa): {len(luisa_recent)}")
    print(f"  Contactos que DEBERÍAN ser de Luisa (HubSpot) pero MongoDB dice otro: {len(luisa_should_have)}")
    print(f"  Contactos que MongoDB dice Luisa pero HubSpot dice otro: {len(luisa_lost)}")

    if luisa_should_have:
        print(f"\n  Contactos perdidos de Luisa (recuperables):")
        for m in luisa_should_have[:20]:
            name = m.get("name") or m["phone"]
            mongo_name = owner_names.get(m["mongo_owner"], m["mongo_owner"])
            print(f"    - {name} ({m['phone']}) — MongoDB dice {mongo_name}, HubSpot dice Luisa")

    mongo_client.close()
    print(f"\n{'=' * 50}")
    if not APPLY:
        print("DRY-RUN completo. Ejecuta con --apply para corregir.")
        print("Ejecuta con --apply --redis para también corregir Redis.")
    else:
        print("Correcciones aplicadas.")


if __name__ == "__main__":
    asyncio.run(main())
