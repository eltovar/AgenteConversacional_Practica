"""
cleanup_no_history.py
---------------------
Borra de Redis los contactos del panel que NO tienen ningún mensaje en MongoDB.

Modos:
  python cleanup_no_history.py          → DRY RUN (solo muestra, no borra)
  python cleanup_no_history.py --delete → Borra realmente de Redis
"""

import sys
import redis
from pymongo import MongoClient

REDIS_URL = "redis://default:dJuBLJwnXguDHOipiUJsnJotsPbHLNTJ@hopper.proxy.rlwy.net:22690"
MONGO_URL = "mongodb://mongo:mFZFdHNRvPcevfYqivewTsFTTwjtzFGp@shortline.proxy.rlwy.net:45260"
MONGO_DB  = "inmobiliaria_chat"

DRY_RUN = "--delete" not in sys.argv

# ── Conexiones ──────────────────────────────────────────────────────────────
r  = redis.from_url(REDIS_URL, decode_responses=True)
mc = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
db = mc[MONGO_DB]

# ── Obtener todos los miembros del ZSET ─────────────────────────────────────
members = r.zrange("active_conversations_sorted", 0, -1)
total   = len(members)
print(f"Total contactos en Redis: {total}")
print(f"Modo: {'DRY RUN (sin cambios)' if DRY_RUN else '*** BORRADO REAL ***'}\n")

# ── Agrupar teléfonos únicos para consulta bulk en Mongo ────────────────────
phone_to_members: dict[str, list[str]] = {}
for member in members:
    phone = member.split(":", 1)[0]
    phone_to_members.setdefault(phone, []).append(member)

phones = list(phone_to_members.keys())

# ── Consulta bulk: qué teléfonos SÍ tienen mensajes en Mongo ────────────────
pipeline = [
    {"$match": {"phone": {"$in": phones}}},
    {"$group": {"_id": "$phone", "count": {"$sum": 1}}}
]
phones_with_msgs = {doc["_id"] for doc in db.messages.aggregate(pipeline)}

# ── Clasificar ──────────────────────────────────────────────────────────────
to_delete = []
for phone, mbs in phone_to_members.items():
    if phone not in phones_with_msgs:
        to_delete.extend(mbs)

phones_with_history    = len(phones_with_msgs)
phones_without_history = total - sum(len(v) for v in phone_to_members.items()
                                     if v[0] in phones_with_msgs)

print(f"Con historial en MongoDB    : {phones_with_history} números")
print(f"Sin historial (a eliminar)  : {len(to_delete)} entradas Redis\n")

if not to_delete:
    print("Nada que borrar.")
    sys.exit(0)

# ── Mostrar los que se borrarán ──────────────────────────────────────────────
print("Contactos SIN historial:")
for m in sorted(to_delete):
    print(f"  {m}")

print()

if DRY_RUN:
    print("Corre con --delete para borrarlos:")
    print("  python cleanup_no_history.py --delete")
else:
    # Borrar en pipeline
    pipe = r.pipeline()
    for member in to_delete:
        phone, canal = member.split(":", 1)
        pipe.delete(f"conv_meta:{phone}:{canal}")
        pipe.delete(f"conv_state:{phone}:{canal}")
        pipe.delete(f"conv_was_panel:{phone}:{canal}")
        pipe.zrem("active_conversations_sorted", member)
    pipe.execute()
    print(f"Borrados {len(to_delete)} contactos de Redis.")
