#!/usr/bin/env python3
# scripts/diagnostico_hubspot_vs_redis.py
"""
Diagnóstico inverso: HubSpot → Redis → MongoDB

Para cada contacto en HubSpot asignado a los owner_ids indicados, verifica:
  1. ¿Existe conv_meta en Redis? (cualquier canal)
  2. ¿Tiene mensajes en MongoDB?

Detecta contactos que el panel debería mostrar pero no pueden hacerlo porque
no tienen estado activo en Redis.

Uso:
    python scripts/diagnostico_hubspot_vs_redis.py
    python scripts/diagnostico_hubspot_vs_redis.py --owner-ids 89096378,89096379,89096380
    python scripts/diagnostico_hubspot_vs_redis.py --limit 500
    python scripts/diagnostico_hubspot_vs_redis.py --sin-redis   # solo los que faltan en Redis
"""

import asyncio
import os
import sys
import json
import argparse
from typing import Optional, Dict, List, Set, Tuple

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT_DIR, ".env"))

import redis.asyncio as aioredis
import httpx
from motor.motor_asyncio import AsyncIOMotorClient

# ─── Colores ANSI ─────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

# Owner IDs de los asesores (default, puede sobreescribirse con --owner-ids)
DEFAULT_OWNER_IDS = ["89096378", "89096379", "89096380"]


# ─── Config ───────────────────────────────────────────────────────────────────

def get_redis_url() -> str:
    return (
        os.getenv("REDIS_PUBLIC_URL") or
        os.getenv("REDIS_URL") or
        "redis://localhost:6379"
    )

def get_mongo_url() -> Optional[str]:
    return (
        os.getenv("MONGO_PUBLIC_URL") or
        os.getenv("MONGODB_PUBLIC_URL") or
        os.getenv("MONGO_URL") or
        os.getenv("MONGODB_URL")
    )

def get_hubspot_key() -> Optional[str]:
    return os.getenv("HUBSPOT_API_KEY")


# ─── HubSpot: obtener contactos por owner ─────────────────────────────────────

async def fetch_hubspot_contacts(
    api_key: str,
    owner_ids: List[str],
    max_contacts: int = 1000,
) -> List[Dict]:
    """
    Busca todos los contactos en HubSpot asignados a los owner_ids dados.
    Usa paginación con cursor 'after'. Máximo max_contacts resultados.

    Retorna lista de dicts con: contact_id, phone (whatsapp_id), name, lifecyclestage, owner_id
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    endpoint = "https://api.hubapi.com/crm/v3/objects/contacts/search"
    contacts = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        after = None
        while len(contacts) < max_contacts:
            payload = {
                "filterGroups": [
                    {
                        "filters": [
                            {
                                "propertyName": "hubspot_owner_id",
                                "operator": "IN",
                                "values": owner_ids,
                            }
                        ]
                    }
                ],
                "properties": [
                    "whatsapp_id", "phone", "firstname", "lastname",
                    "lifecyclestage", "hubspot_owner_id", "canal_origen",
                ],
                "limit": 100,
                **({"after": after} if after else {}),
            }

            try:
                resp = await client.post(endpoint, headers=headers, json=payload)
                if resp.status_code == 429:
                    print(f"{YELLOW}  [HubSpot] Rate limit (429), esperando 10s...{RESET}")
                    await asyncio.sleep(10)
                    continue
                if resp.status_code not in (200, 207):
                    print(f"{RED}  [HubSpot] Error {resp.status_code}: {resp.text[:200]}{RESET}")
                    break

                data = resp.json()
                results = data.get("results", [])
                for r in results:
                    props = r.get("properties", {})
                    phone = (props.get("whatsapp_id") or props.get("phone") or "").strip()
                    if not phone:
                        continue
                    # Normalizar a E.164 si no tiene "+"
                    if not phone.startswith("+"):
                        phone = f"+{phone}"
                    contacts.append({
                        "contact_id": r.get("id"),
                        "phone": phone,
                        "name": f"{props.get('firstname') or ''} {props.get('lastname') or ''}".strip() or "—",
                        "lifecyclestage": props.get("lifecyclestage") or "—",
                        "owner_id": props.get("hubspot_owner_id") or "—",
                        "canal_origen": props.get("canal_origen") or "—",
                    })

                paging = data.get("paging", {})
                after = paging.get("next", {}).get("after")
                if not after or not results:
                    break

            except Exception as e:
                print(f"{RED}  [HubSpot] Error: {e}{RESET}")
                break

    return contacts


# ─── Redis: buscar cualquier conv_meta para un teléfono ───────────────────────

async def scan_redis_meta_for_phones(
    r: aioredis.Redis,
    phones: List[str],
) -> Dict[str, List[str]]:
    """
    Para cada phone, busca claves Redis del patrón conv_meta:{phone}:*
    usando SCAN (seguro en producción, no bloquea).
    Retorna dict: phone → lista de canales encontrados ([] si ninguno).
    """
    result = {p: [] for p in phones}

    for phone in phones:
        pattern = f"conv_meta:{phone}:*"
        keys_found = []
        cursor = 0
        while True:
            cursor, keys = await r.scan(cursor=cursor, match=pattern, count=50)
            keys_found.extend(keys)
            if cursor == 0:
                break
        # Extraer el canal de cada clave encontrada
        for k in keys_found:
            # k = "conv_meta:+57XXXXXXXXX:canal"
            parts = k.split(":", 2)
            canal = parts[2] if len(parts) == 3 else "?"
            result[phone].append(canal)

    return result


async def get_redis_meta_details(
    r: aioredis.Redis,
    phone: str,
    canal: str,
) -> Optional[dict]:
    """Lee conv_meta:{phone}:{canal} y retorna JSON parseado."""
    key = f"conv_meta:{phone}:{canal.lower()}"
    raw = await r.get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


# ─── MongoDB ───────────────────────────────────────────────────────────────────

async def check_mongo_phones_bulk(mongo_url: str, phones: List[str]) -> Dict[str, int]:
    """
    Retorna dict: phone → conteo de mensajes en inmobiliaria_chat.messages.
    """
    client = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=5000)
    try:
        db = client.get_database("inmobiliaria_chat")
        pipeline = [
            {"$match": {"phone": {"$in": phones}}},
            {"$group": {"_id": "$phone", "count": {"$sum": 1}}},
        ]
        docs = await db.messages.aggregate(pipeline).to_list(length=None)
        return {d["_id"]: d["count"] for d in docs}
    finally:
        client.close()


# ─── Main ──────────────────────────────────────────────────────────────────────

async def main(owner_ids: List[str], max_contacts: int, solo_sin_redis: bool):
    print(f"\n{BOLD}{CYAN}═══════════════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}{CYAN}  Diagnóstico HubSpot → Redis → MongoDB                    {RESET}")
    print(f"{BOLD}{CYAN}═══════════════════════════════════════════════════════════{RESET}\n")
    print(f"Owner IDs consultados: {BOLD}{', '.join(owner_ids)}{RESET}\n")

    # ── 1. HubSpot ─────────────────────────────────────────────────────────────
    hs_key = get_hubspot_key()
    if not hs_key:
        print(f"{RED}✗ HUBSPOT_API_KEY no configurada{RESET}")
        return

    print(f"Consultando HubSpot (máx {max_contacts} contactos)...")
    hs_contacts = await fetch_hubspot_contacts(hs_key, owner_ids, max_contacts)

    # Deduplicar por phone (un mismo número puede estar dos veces si tiene 2 registros)
    seen_phones: Dict[str, Dict] = {}
    for c in hs_contacts:
        p = c["phone"]
        if p not in seen_phones:
            seen_phones[p] = c
    hs_contacts = list(seen_phones.values())

    if not hs_contacts:
        print(f"{YELLOW}No se encontraron contactos en HubSpot para esos owner_ids.{RESET}")
        return

    print(f"{GREEN}✓ HubSpot: {len(hs_contacts)} contactos únicos encontrados{RESET}")
    phones = [c["phone"] for c in hs_contacts]

    # ── 2. Redis ───────────────────────────────────────────────────────────────
    redis_url = get_redis_url()
    print(f"Escaneando Redis para {len(phones)} teléfonos...")
    r = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True)
    try:
        await r.ping()
    except Exception as e:
        print(f"{RED}✗ Redis no disponible: {e}{RESET}")
        await r.aclose()
        return

    redis_canales = await scan_redis_meta_for_phones(r, phones)

    # Leer status de cada meta encontrada
    redis_status: Dict[str, str] = {}  # phone → status del primer canal encontrado
    for phone, canales in redis_canales.items():
        if canales:
            meta = await get_redis_meta_details(r, phone, canales[0])
            if meta:
                redis_status[phone] = meta.get("status", "?")

    await r.aclose()
    phones_en_redis = sum(1 for c in redis_canales.values() if c)
    print(f"{GREEN}✓ Redis: {phones_en_redis}/{len(phones)} tienen conv_meta{RESET}")

    # ── 3. MongoDB ─────────────────────────────────────────────────────────────
    mongo_url = get_mongo_url()
    msg_counts: Dict[str, int] = {}
    if mongo_url:
        print(f"Consultando MongoDB...")
        try:
            msg_counts = await check_mongo_phones_bulk(mongo_url, phones)
            phones_con_msgs = sum(1 for v in msg_counts.values() if v > 0)
            print(f"{GREEN}✓ MongoDB: {phones_con_msgs}/{len(phones)} tienen mensajes{RESET}")
        except Exception as e:
            print(f"{RED}✗ MongoDB error: {e}{RESET}")
    else:
        print(f"{YELLOW}⚠ MONGO_URL no configurada{RESET}")

    # ── 4. Reporte ─────────────────────────────────────────────────────────────
    W = 110
    print(f"\n{BOLD}{'─'*W}{RESET}")
    print(
        f"{BOLD}{'Teléfono':<20} {'Nombre':<22} {'Owner':<12} {'Canal HS':<18} "
        f"{'Redis (canal)':<22} {'Status Redis':<14} {'Msgs':<6} Observación{RESET}"
    )
    print(f"{'─'*W}{RESET}")

    sin_redis_con_msgs = []   # En HubSpot + MongoDB pero SIN Redis → deberían estar en panel
    sin_todo = []             # Solo en HubSpot, sin Redis ni MongoDB → contactos nuevos/sin actividad
    ok_list = []

    for c in sorted(hs_contacts, key=lambda x: x["phone"]):
        phone    = c["phone"]
        name     = c["name"][:20]
        owner    = c["owner_id"]
        canal_hs = c["canal_origen"][:16]

        canales   = redis_canales.get(phone, [])
        in_redis  = bool(canales)
        status_r  = redis_status.get(phone, "—") if in_redis else "—"
        msg_cnt   = msg_counts.get(phone, 0)

        redis_str = ", ".join(canales) if canales else "✗"
        msgs_str  = f"{msg_cnt:>4}" if msg_cnt else "   0"

        if solo_sin_redis and in_redis:
            continue  # --sin-redis: omitir los que ya están en Redis

        if not in_redis and msg_cnt > 0:
            row_color = RED
            obs = f"{RED}⚠ SIN REDIS (tiene {msg_cnt} msgs){RESET}"
            sin_redis_con_msgs.append((phone, name, owner, canal_hs, msg_cnt, canales))
        elif not in_redis and msg_cnt == 0:
            row_color = YELLOW
            obs = f"{YELLOW}nuevo / sin actividad{RESET}"
            sin_todo.append((phone, name, owner, canal_hs))
        else:
            row_color = ""
            obs = f"{GREEN}✓ OK ({status_r}){RESET}"
            ok_list.append(phone)

        print(
            f"{row_color}{phone:<20}{RESET} "
            f"{name:<22} "
            f"{owner:<12} "
            f"{canal_hs:<18} "
            f"{redis_str:<22} "
            f"{status_r:<14} "
            f"{msgs_str}  "
            f"{obs}"
        )

    # ── 5. Resumen ─────────────────────────────────────────────────────────────
    print(f"\n{BOLD}{'─'*W}{RESET}")
    print(f"{BOLD}RESUMEN{RESET}")
    print(f"  Total HubSpot (owner={','.join(owner_ids)}): {len(hs_contacts)}")
    print(f"  {GREEN}Con Redis activo             : {len(ok_list)}{RESET}")
    print(f"  {RED}SIN Redis PERO con historial : {len(sin_redis_con_msgs)}{RESET}  ← deben reactivarse")
    print(f"  {YELLOW}Sin Redis y sin historial   : {len(sin_todo)}{RESET}  ← leads nuevos sin contacto")

    if sin_redis_con_msgs:
        print(f"\n{BOLD}{RED}CONTACTOS CON HISTORIAL PERO SIN ESTADO REDIS:{RESET}")
        print(f"{BOLD}  (Existen en MongoDB pero no están en el ZSET del panel){RESET}")
        for phone, name, owner, canal_hs, msgs, _ in sin_redis_con_msgs:
            print(f"  {RED}• {phone}  {name:<20} owner={owner}  canal={canal_hs}  msgs={msgs}{RESET}")

    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Diagnóstico HubSpot vs Redis: contactos sin estado activo"
    )
    parser.add_argument(
        "--owner-ids",
        default=",".join(DEFAULT_OWNER_IDS),
        help=f"Owner IDs separados por coma (default: {','.join(DEFAULT_OWNER_IDS)})"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Máximo de contactos a traer de HubSpot (default: 500)"
    )
    parser.add_argument(
        "--sin-redis",
        action="store_true",
        help="Mostrar solo contactos que NO tienen estado en Redis"
    )
    args = parser.parse_args()

    owner_ids = [o.strip() for o in args.owner_ids.split(",") if o.strip()]
    asyncio.run(main(owner_ids, args.limit, args.sin_redis))
