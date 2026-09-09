#!/usr/bin/env python3
# scripts/diagnostico_contactos_panel.py
"""
Diagnóstico de integridad de contactos del Panel de Asesores.

Para cada número que aparece en el ZSET de Redis (active_conversations_sorted),
verifica si existe en:
  1. Redis  — conv_meta:{phone}:{canal}
  2. MongoDB — colección contacts (campo phone)
  3. HubSpot — propiedad whatsapp_id

Uso:
    python scripts/diagnostico_contactos_panel.py
    python scripts/diagnostico_contactos_panel.py --skip-hubspot   # más rápido
    python scripts/diagnostico_contactos_panel.py --clean-ghosts   # elimina fantasmas de Redis
"""

import asyncio
import os
import sys
import json
import argparse
from typing import Optional, Dict, List, Tuple

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


# ─── Config desde entorno ──────────────────────────────────────────────────────

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


# ─── Redis ─────────────────────────────────────────────────────────────────────

async def get_panel_phones(r: aioredis.Redis) -> List[Tuple[str, str]]:
    """
    Lee todos los miembros del ZSET active_conversations_sorted.
    Formato de cada miembro: "{phone}:{canal}"
    Retorna lista de (phone, canal).
    """
    members = await r.zrevrange("active_conversations_sorted", 0, -1)
    result = []
    for m in members:
        if ":" not in m:
            continue
        # El phone puede tener "+" y el canal no, así que dividir desde la derecha
        # Formato: +573001234567:whatsapp  o  +573001234567:whatsapp_directo
        # Usamos rsplit con maxsplit=1
        parts = m.rsplit(":", 1)
        if len(parts) == 2:
            phone, canal = parts
            result.append((phone, canal))
    return result


async def get_redis_meta(r: aioredis.Redis, phones_canales: List[Tuple[str, str]]) -> Dict[str, Optional[dict]]:
    """
    Usa pipeline para leer conv_meta:{phone}:{canal} de todos los contactos.
    Retorna dict keyed por "{phone}:{canal}" → dict parsed o None.
    """
    if not phones_canales:
        return {}

    pipe = r.pipeline()
    for phone, canal in phones_canales:
        pipe.get(f"conv_meta:{phone}:{canal.lower()}")
    raw_values = await pipe.execute()

    result = {}
    for (phone, canal), raw in zip(phones_canales, raw_values):
        key = f"{phone}:{canal}"
        if raw:
            try:
                result[key] = json.loads(raw)
            except Exception:
                result[key] = {"_parse_error": True}
        else:
            result[key] = None
    return result


# ─── MongoDB ───────────────────────────────────────────────────────────────────

async def check_mongo_phones(mongo_url: str, phones: List[str]) -> Dict[str, dict]:
    """
    Busca en bulk si los teléfonos existen en MongoDB.
    Revisa tanto 'messages' (historial) como 'contacts' (cache HubSpot).
    Retorna dict: phone → {"messages": bool, "contacts": bool, "msg_count": int}.
    """
    client = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=5000)
    try:
        db = client.get_database("inmobiliaria_chat")

        # ── Colección messages: ¿tiene mensajes? ──────────────────────────────
        # Agrupamos por phone para obtener count
        pipeline = [
            {"$match": {"phone": {"$in": phones}}},
            {"$group": {"_id": "$phone", "count": {"$sum": 1}}},
        ]
        msg_docs = await db.messages.aggregate(pipeline).to_list(length=None)
        msg_counts = {d["_id"]: d["count"] for d in msg_docs}

        # ── Colección contacts: ¿existe el contacto? ──────────────────────────
        contact_docs = await db.contacts.find(
            {"phone": {"$in": phones}},
            {"phone": 1, "_id": 0}
        ).to_list(length=None)
        contacts_found = {d["phone"] for d in contact_docs}

        result = {}
        for p in phones:
            result[p] = {
                "messages": p in msg_counts,
                "msg_count": msg_counts.get(p, 0),
                "contacts": p in contacts_found,
            }
        return result
    finally:
        client.close()


# ─── HubSpot ───────────────────────────────────────────────────────────────────

async def check_hubspot_phones(api_key: str, phones: List[str]) -> Dict[str, bool]:
    """
    Batch lookup en HubSpot por propiedad whatsapp_id.
    HubSpot permite hasta 100 inputs por request.
    Retorna dict: phone → True/False.

    La respuesta 207 (multi-status) indica mezcla de encontrados/no encontrados;
    los encontrados van en "results", los no-encontrados en "errors".
    """
    found = {p: False for p in phones}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    endpoint = "https://api.hubapi.com/crm/v3/objects/contacts/batch/read"

    async with httpx.AsyncClient(timeout=20.0) as client:
        for i in range(0, len(phones), 100):
            batch = phones[i:i + 100]
            # Pedimos whatsapp_id en properties para poder correlacionar con el input
            payload = {
                "properties": ["whatsapp_id"],
                "idProperty": "whatsapp_id",
                "inputs": [{"id": p} for p in batch],
            }
            try:
                resp = await client.post(endpoint, headers=headers, json=payload)
                if resp.status_code in (200, 207):
                    data = resp.json()
                    for result in data.get("results", []):
                        wid = result.get("properties", {}).get("whatsapp_id")
                        if wid and wid in found:
                            found[wid] = True
                else:
                    print(f"{YELLOW}  [HubSpot] Status {resp.status_code} para batch {i}–{i+len(batch)}{RESET}")
            except Exception as e:
                print(f"{RED}  [HubSpot] Error en batch {i}: {e}{RESET}")

    return found


# ─── Main ──────────────────────────────────────────────────────────────────────

async def main(skip_hubspot: bool = False):
    print(f"\n{BOLD}{CYAN}═══════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}{CYAN}  Diagnóstico de Contactos del Panel de Asesores   {RESET}")
    print(f"{BOLD}{CYAN}═══════════════════════════════════════════════════{RESET}\n")

    # ── 1. Redis ───────────────────────────────────────────────────────────────
    redis_url = get_redis_url()
    print(f"Conectando a Redis: {redis_url[:40]}...")
    r = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True)

    try:
        await r.ping()
        print(f"{GREEN}✓ Redis OK{RESET}")
    except Exception as e:
        print(f"{RED}✗ Redis no disponible: {e}{RESET}")
        return

    phones_canales = await get_panel_phones(r)
    if not phones_canales:
        print(f"{YELLOW}  No hay miembros en active_conversations_sorted.{RESET}")
        await r.aclose()
        return

    unique_phones = list({p for p, _ in phones_canales})
    print(f"\n{BOLD}Contactos en ZSET:{RESET} {len(phones_canales)} entradas, {len(unique_phones)} teléfonos únicos\n")

    # Leer metas de Redis
    metas = await get_redis_meta(r, phones_canales)
    await r.aclose()

    # ── 2. MongoDB ─────────────────────────────────────────────────────────────
    mongo_url = get_mongo_url()
    # mongo_results: phone → {"messages": bool, "msg_count": int, "contacts": bool}
    mongo_results: Dict[str, Optional[dict]] = {}
    if mongo_url:
        print(f"Consultando MongoDB (inmobiliaria_chat)...")
        try:
            mongo_results = await check_mongo_phones(mongo_url, unique_phones)
            with_msgs = sum(1 for v in mongo_results.values() if v and v["messages"])
            with_contact = sum(1 for v in mongo_results.values() if v and v["contacts"])
            print(f"{GREEN}✓ MongoDB: {with_msgs}/{len(unique_phones)} con mensajes, "
                  f"{with_contact}/{len(unique_phones)} en colección contacts{RESET}")
        except Exception as e:
            print(f"{RED}✗ MongoDB error: {e}{RESET}")
            mongo_results = {p: None for p in unique_phones}
    else:
        print(f"{YELLOW}⚠ MONGO_URL no configurada — salteando MongoDB{RESET}")
        mongo_results = {p: None for p in unique_phones}

    # ── 3. HubSpot ─────────────────────────────────────────────────────────────
    hubspot_results: Dict[str, Optional[bool]] = {}
    if not skip_hubspot:
        hs_key = get_hubspot_key()
        if hs_key:
            print(f"Consultando HubSpot (batch de 100)...")
            try:
                hubspot_results = await check_hubspot_phones(hs_key, unique_phones)
                found_count = sum(1 for v in hubspot_results.values() if v)
                print(f"{GREEN}✓ HubSpot: {found_count}/{len(unique_phones)} encontrados{RESET}")
            except Exception as e:
                print(f"{RED}✗ HubSpot error: {e}{RESET}")
                hubspot_results = {p: None for p in unique_phones}
        else:
            print(f"{YELLOW}⚠ HUBSPOT_API_KEY no configurada — salteando HubSpot{RESET}")
            hubspot_results = {p: None for p in unique_phones}
    else:
        print(f"{YELLOW}⚠ HubSpot salteado (--skip-hubspot){RESET}")
        hubspot_results = {p: None for p in unique_phones}

    # ── 4. Reporte ─────────────────────────────────────────────────────────────
    W = 85
    print(f"\n{BOLD}{'─'*W}{RESET}")
    print(f"{BOLD}{'Teléfono':<20} {'Canal':<20} {'Status':<13} {'Msgs':<6} {'Contacts':<10} {'HubSpot':<10} Observación{RESET}")
    print(f"{'─'*W}{RESET}")

    ghosts = []       # Sin mensajes en MongoDB Y sin contacto HubSpot
    inconsistent = [] # Tiene datos pero falta en algún lado
    ok_contacts = []  # Todo OK

    for phone, canal in phones_canales:
        key = f"{phone}:{canal}"
        meta = metas.get(key)
        has_meta = meta is not None and not meta.get("_parse_error")
        status = meta.get("status", "?") if has_meta else "NO_META"

        mongo_data = mongo_results.get(phone)
        has_msgs     = mongo_data["messages"] if mongo_data else None
        msg_count    = mongo_data["msg_count"] if mongo_data else 0
        has_contact  = mongo_data["contacts"] if mongo_data else None
        in_hs        = hubspot_results.get(phone)

        # Iconos
        redis_icon   = f"{GREEN}✓{RESET}" if has_meta else f"{RED}✗{RESET}"
        msgs_icon    = (f"{GREEN}✓{str(msg_count):>3}{RESET}" if has_msgs else
                        f"{RED}✗  0{RESET}" if has_msgs is False else
                        f"{YELLOW}?   {RESET}")
        contact_icon = (f"{GREEN}✓{RESET}" if has_contact else
                        f"{RED}✗{RESET}" if has_contact is False else
                        f"{YELLOW}?{RESET}")
        hs_icon      = (f"{GREEN}✓{RESET}" if in_hs else
                        f"{RED}✗{RESET}" if in_hs is False else
                        f"{YELLOW}?{RESET}")

        missing = []
        if not has_meta:
            missing.append("sin_meta_redis")
        if has_msgs is False and has_contact is False:
            missing.append("no_mongo_total")
        elif has_msgs is False:
            missing.append("sin_mensajes")
        if in_hs is False:
            missing.append("no_hubspot")

        if missing:
            is_ghost = "no_mongo_total" in missing or "no_hubspot" in missing
            row_color = RED if is_ghost else YELLOW
            obs = f"{row_color}⚠ {', '.join(missing)}{RESET}"
            if is_ghost:
                ghosts.append((phone, canal, status, missing, msg_count))
            else:
                inconsistent.append((phone, canal, status, missing, msg_count))
        else:
            row_color = ""
            obs = f"{GREEN}✓ OK{RESET}"
            ok_contacts.append((phone, canal, status))

        print(
            f"{row_color}{phone:<20}{RESET} "
            f"{canal:<20} "
            f"{redis_icon} {status:<11} "
            f"{msgs_icon}  "
            f"{contact_icon}         "
            f"{hs_icon}         "
            f"{obs}"
        )

    # ── 5. Resumen ─────────────────────────────────────────────────────────────
    print(f"\n{BOLD}{'─'*W}{RESET}")
    print(f"{BOLD}RESUMEN{RESET}")
    print(f"  Total en panel      : {len(phones_canales)}")
    print(f"  {GREEN}OK (completos)      : {len(ok_contacts)}{RESET}")
    print(f"  {YELLOW}Parciales           : {len(inconsistent)}{RESET}")
    print(f"  {RED}Fantasmas (ghost)   : {len(ghosts)}{RESET}")

    if ghosts:
        print(f"\n{BOLD}{RED}CONTACTOS FANTASMA (Redis sin respaldo en MongoDB/HubSpot):{RESET}")
        for phone, canal, status, missing, msgs in ghosts:
            print(f"  {RED}• {phone} [{canal}] status={status} msgs={msgs} falta={missing}{RESET}")

    if inconsistent:
        print(f"\n{BOLD}{YELLOW}CONTACTOS CON DATOS INCOMPLETOS:{RESET}")
        for phone, canal, status, missing, msgs in inconsistent:
            print(f"  {YELLOW}• {phone} [{canal}] status={status} msgs={msgs} falta={missing}{RESET}")

    print()
    return ghosts


async def clean_ghosts(ghosts: list):
    """
    Elimina contactos fantasma de Redis:
      - ZSET active_conversations_sorted  (zrem "{phone}:{canal}")
      - conv_meta:{phone}:{canal}
      - conv_state:{phone}:{canal}
    """
    if not ghosts:
        print(f"{GREEN}No hay fantasmas que limpiar.{RESET}")
        return

    print(f"\n{BOLD}{RED}Claves a eliminar de Redis:{RESET}")
    keys_to_delete = []
    zset_members = []
    for phone, canal, status, missing, msgs in ghosts:
        member = f"{phone}:{canal}"
        meta_key  = f"conv_meta:{phone}:{canal.lower()}"
        state_key = f"conv_state:{phone}:{canal.lower()}"
        print(f"  ZREM  active_conversations_sorted  {member}")
        print(f"  DEL   {meta_key}")
        print(f"  DEL   {state_key}")
        zset_members.append(member)
        keys_to_delete.extend([meta_key, state_key])

    confirm = input(f"\n{BOLD}¿Confirmar eliminación? [s/N]: {RESET}").strip().lower()
    if confirm != "s":
        print("Cancelado.")
        return

    redis_url = get_redis_url()
    r = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True)
    try:
        pipe = r.pipeline()
        pipe.zrem("active_conversations_sorted", *zset_members)
        for k in keys_to_delete:
            pipe.delete(k)
        results = await pipe.execute()
        zrem_count = results[0]
        del_count  = sum(results[1:])
        print(f"\n{GREEN}✓ Eliminados: {zrem_count} entrada(s) del ZSET, {del_count} clave(s) Redis.{RESET}")
    finally:
        await r.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnóstico de contactos del panel")
    parser.add_argument(
        "--skip-hubspot",
        action="store_true",
        help="Omitir verificación en HubSpot (más rápido)"
    )
    parser.add_argument(
        "--clean-ghosts",
        action="store_true",
        help="Eliminar contactos fantasma de Redis (pide confirmación)"
    )
    args = parser.parse_args()

    async def run():
        ghosts = await main(skip_hubspot=args.skip_hubspot or args.clean_ghosts)
        if args.clean_ghosts and ghosts:
            await clean_ghosts(ghosts)

    asyncio.run(run())
