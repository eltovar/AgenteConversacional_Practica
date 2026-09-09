"""
cleanup_hubspot_no_history.py
------------------------------
Borra de HubSpot los contactos que NO tienen ningún mensaje en MongoDB.

Modos:
  python cleanup_hubspot_no_history.py           → DRY RUN (solo muestra, no borra)
  python cleanup_hubspot_no_history.py --delete  → Borra realmente de HubSpot
"""

import os
import sys
import time
import requests
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

HUBSPOT_TOKEN = os.getenv("HUBSPOT_API_KEY")
MONGO_URL     = os.getenv("MONGO_PUBLIC_URL")
MONGO_DB      = "inmobiliaria_chat"

if not HUBSPOT_TOKEN:
    print("ERROR: HUBSPOT_API_KEY no definida en .env")
    sys.exit(1)
if not MONGO_URL:
    print("ERROR: MONGO_PUBLIC_URL no definida en .env")
    sys.exit(1)

DRY_RUN = "--delete" not in sys.argv

HEADERS = {
    "Authorization": f"Bearer {HUBSPOT_TOKEN}",
    "Content-Type": "application/json"
}

# ── 1. Obtener teléfonos con historial en MongoDB ────────────────────────────
print("Conectando a MongoDB...")
mc = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
db = mc[MONGO_DB]

phones_with_history = set(db.messages.distinct("phone"))
print(f"Teléfonos con mensajes en MongoDB: {len(phones_with_history)}\n")

# ── 2. Paginar todos los contactos de HubSpot ────────────────────────────────
print("Obteniendo contactos de HubSpot (paginado)...")
all_contacts = []
after = None

while True:
    params = {
        "limit": 100,
        "properties": "whatsapp_id,firstname,lastname,createdate"
    }
    if after:
        params["after"] = after

    resp = requests.get(
        "https://api.hubapi.com/crm/v3/objects/contacts",
        headers=HEADERS,
        params=params
    )
    resp.raise_for_status()
    data = resp.json()

    all_contacts.extend(data.get("results", []))

    paging = data.get("paging", {})
    after  = paging.get("next", {}).get("after")
    if not after:
        break

    time.sleep(0.1)  # respetar rate limit

print(f"Total contactos en HubSpot: {len(all_contacts)}\n")

# ── 3. Clasificar ────────────────────────────────────────────────────────────
to_delete = []
skipped_no_phone = 0

for contact in all_contacts:
    props   = contact.get("properties", {})
    wa_id   = props.get("whatsapp_id", "").strip()
    name    = f"{props.get('firstname', '')} {props.get('lastname', '')}".strip()
    created = props.get("createdate", "")[:10]

    if not wa_id:
        skipped_no_phone += 1
        continue

    if wa_id not in phones_with_history:
        to_delete.append({
            "id":      contact["id"],
            "phone":   wa_id,
            "name":    name or "(sin nombre)",
            "created": created
        })

print(f"Sin whatsapp_id (ignorados): {skipped_no_phone}")
print(f"Con historial → se conservan: {len(all_contacts) - skipped_no_phone - len(to_delete)}")
print(f"Sin historial → a eliminar:   {len(to_delete)}\n")

if not to_delete:
    print("Nada que borrar.")
    sys.exit(0)

# ── 4. Mostrar lista ─────────────────────────────────────────────────────────
print("Contactos SIN historial que se eliminarán de HubSpot:")
for c in sorted(to_delete, key=lambda x: x["created"]):
    print(f"  [{c['created']}] {c['phone']}  {c['name']}  (id: {c['id']})")

print()

if DRY_RUN:
    print("Corre con --delete para borrarlos de HubSpot:")
    print("  python cleanup_hubspot_no_history.py --delete")
    sys.exit(0)

# ── 5. Borrar en lotes de 100 (batch archive) ────────────────────────────────
print(f"Modo: *** BORRADO REAL *** — eliminando {len(to_delete)} contactos...\n")

ids = [{"id": c["id"]} for c in to_delete]
batch_size = 100
deleted = 0

for i in range(0, len(ids), batch_size):
    batch = ids[i:i + batch_size]
    resp  = requests.post(
        "https://api.hubapi.com/crm/v3/objects/contacts/batch/archive",
        headers=HEADERS,
        json={"inputs": batch}
    )
    if resp.status_code in (200, 204):
        deleted += len(batch)
        print(f"  Lote {i//batch_size + 1}: {len(batch)} contactos archivados.")
    else:
        print(f"  ERROR en lote {i//batch_size + 1}: {resp.status_code} — {resp.text}")
    time.sleep(0.2)

print(f"\nListo. {deleted} contactos eliminados de HubSpot.")
