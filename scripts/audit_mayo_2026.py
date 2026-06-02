"""
Auditoría de Mayo 2026 — SofIA (Inmobiliaria Proteger)
Extrae datos de MongoDB y Twilio para el informe de rendimiento.
Ejecutar desde la raíz del proyecto:
    python scripts/audit_mayo_2026.py
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ─── MongoDB ───────────────────────────────────────────────────────────────
try:
    import pymongo
except ImportError:
    print("Instalando pymongo...")
    os.system(f"{sys.executable} -m pip install pymongo")
    import pymongo

# ─── Twilio ────────────────────────────────────────────────────────────────
try:
    from twilio.rest import Client as TwilioClient
except ImportError:
    print("Instalando twilio...")
    os.system(f"{sys.executable} -m pip install twilio")
    from twilio.rest import Client as TwilioClient

# ─── Load .env ─────────────────────────────────────────────────────────────
from pathlib import Path
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                val = val.strip().strip('"').strip("'")
                if key.strip() not in os.environ:
                    os.environ[key.strip()] = val

# ─── Config ────────────────────────────────────────────────────────────────
MONGO_URL = os.getenv("MONGO_PUBLIC_URL") or os.getenv("MONGO_URL") or os.getenv("MONGODB_URL")
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "+573123969894")

START = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

OUTPUT_FILE = Path(__file__).resolve().parent / "audit_mayo_2026_results.json"

results = {}

# ═══════════════════════════════════════════════════════════════════════════
# MONGODB
# ═══════════════════════════════════════════════════════════════════════════
print("\n" + "="*60)
print("  MONGODB — Auditoría Mayo 2026")
print("="*60)

try:
    client = pymongo.MongoClient(MONGO_URL, serverSelectionTimeoutMS=15000)
    db = client["inmobiliaria_chat"]

    # 1. Total messages in May
    total_msgs = db.messages.count_documents({"timestamp": {"$gte": START, "$lt": END}})
    print(f"\n[1] Total mensajes en Mayo: {total_msgs}")

    # 2. Unique client phones
    pipe_clients = [
        {"$match": {"timestamp": {"$gte": START, "$lt": END}, "sender": "client"}},
        {"$group": {"_id": "$phone"}},
        {"$count": "total"}
    ]
    r = list(db.messages.aggregate(pipe_clients))
    unique_clients = r[0]["total"] if r else 0
    print(f"[2] Teléfonos únicos (sender=client): {unique_clients}")

    # 3. Messages by sender
    pipe_sender = [
        {"$match": {"timestamp": {"$gte": START, "$lt": END}}},
        {"$group": {"_id": "$sender", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}
    ]
    sender_breakdown = {d["_id"]: d["count"] for d in db.messages.aggregate(pipe_sender)}
    print(f"[3] Mensajes por sender: {sender_breakdown}")

    # 4. Messages by channel
    pipe_channel = [
        {"$match": {"timestamp": {"$gte": START, "$lt": END}}},
        {"$group": {"_id": "$channel", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}
    ]
    channel_breakdown = {d["_id"]: d["count"] for d in db.messages.aggregate(pipe_channel)}
    print(f"[4] Mensajes por canal: {channel_breakdown}")

    # 5. Phones with advisor messages (HUMAN intervention)
    pipe_advisor_phones = [
        {"$match": {"timestamp": {"$gte": START, "$lt": END}, "sender": "advisor"}},
        {"$group": {"_id": "$phone"}}
    ]
    advisor_phones = [d["_id"] for d in db.messages.aggregate(pipe_advisor_phones)]
    print(f"[5] Teléfonos con intervención humana (advisor): {len(advisor_phones)}")

    # 6. Phones ONLY bot (no advisor)
    pipe_bot_only = [
        {"$match": {"timestamp": {"$gte": START, "$lt": END}}},
        {"$group": {"_id": "$phone", "senders": {"$addToSet": "$sender"}}},
        {"$match": {"senders": {"$nin": ["advisor"]}}}
    ]
    bot_only_phones = [d["_id"] for d in db.messages.aggregate(pipe_bot_only)]
    print(f"[6] Teléfonos SOLO bot (sin advisor): {len(bot_only_phones)}")

    # 7. First-time users (first message EVER was in May)
    pipe_first = [
        {"$match": {"sender": "client"}},
        {"$group": {"_id": "$phone", "first_msg": {"$min": "$timestamp"}}},
        {"$match": {"first_msg": {"$gte": START, "$lt": END}}},
        {"$count": "total"}
    ]
    r_first = list(db.messages.aggregate(pipe_first, allowDiskUse=True))
    first_time = r_first[0]["total"] if r_first else 0
    print(f"[7] Usuarios primera vez en Mayo: {first_time}")

    # 8. Daily distribution (client messages)
    pipe_daily = [
        {"$match": {"timestamp": {"$gte": START, "$lt": END}, "sender": "client"}},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$timestamp", "timezone": "America/Bogota"}},
            "unique_clients": {"$addToSet": "$phone"},
            "msg_count": {"$sum": 1}
        }},
        {"$project": {"_id": 1, "unique_clients": {"$size": "$unique_clients"}, "msg_count": 1}},
        {"$sort": {"_id": 1}}
    ]
    daily_data = list(db.messages.aggregate(pipe_daily))
    print(f"\n[8] Distribución diaria (mensajes de clientes):")
    print(f"{'Fecha':<12} {'Clientes':<10} {'Mensajes':<10}")
    for d in daily_data:
        print(f"  {d['_id']:<12} {d['unique_clients']:<10} {d['msg_count']:<10}")

    # 9. All unique phones in May (for cross-reference)
    pipe_all_phones = [
        {"$match": {"timestamp": {"$gte": START, "$lt": END}}},
        {"$group": {"_id": "$phone"}}
    ]
    all_mongo_phones = [d["_id"] for d in db.messages.aggregate(pipe_all_phones)]
    print(f"\n[9] Total teléfonos únicos en MongoDB (mayo): {len(all_mongo_phones)}")

    # 10. Conversations collection
    try:
        conv_total = db.conversations.count_documents({})
        conv_may = db.conversations.count_documents({"first_message_at": {"$gte": START, "$lt": END}})

        pipe_conv_status = [
            {"$match": {"first_message_at": {"$gte": START, "$lt": END}}},
            {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}}
        ]
        conv_statuses = {d["_id"]: d["count"] for d in db.conversations.aggregate(pipe_conv_status)}
        print(f"\n[10] Conversations collection:")
        print(f"  Total: {conv_total}")
        print(f"  Creadas en mayo: {conv_may}")
        print(f"  Por status: {conv_statuses}")
    except Exception as e:
        print(f"[10] Conversations collection error: {e}")
        conv_may = 0
        conv_statuses = {}

    # 11. Hourly distribution
    pipe_hourly = [
        {"$match": {"timestamp": {"$gte": START, "$lt": END}, "sender": "client"}},
        {"$group": {
            "_id": {"$hour": {"date": "$timestamp", "timezone": "America/Bogota"}},
            "count": {"$sum": 1}
        }},
        {"$sort": {"_id": 1}}
    ]
    hourly = {d["_id"]: d["count"] for d in db.messages.aggregate(pipe_hourly)}
    print(f"\n[11] Distribución horaria (hora Bogotá):")
    for h in range(24):
        c = hourly.get(h, 0)
        bar = "█" * (c // 10)
        print(f"  {h:02d}:00  {c:>5}  {bar}")

    # Save MongoDB results
    results["mongodb"] = {
        "total_messages": total_msgs,
        "unique_client_phones": unique_clients,
        "sender_breakdown": sender_breakdown,
        "channel_breakdown": channel_breakdown,
        "phones_with_advisor": len(advisor_phones),
        "advisor_phone_list": advisor_phones,
        "bot_only_phones": len(bot_only_phones),
        "first_time_users": first_time,
        "daily_data": [{
            "date": d["_id"],
            "unique_clients": d["unique_clients"],
            "messages": d["msg_count"]
        } for d in daily_data],
        "all_phones": all_mongo_phones,
        "conversations_may": conv_may,
        "conversations_statuses": conv_statuses,
        "hourly": hourly,
    }

    client.close()
    print("\n✓ MongoDB completado")

except Exception as e:
    print(f"\n✗ Error MongoDB: {e}")
    results["mongodb"] = {"error": str(e)}

# ═══════════════════════════════════════════════════════════════════════════
# TWILIO
# ═══════════════════════════════════════════════════════════════════════════
print("\n" + "="*60)
print("  TWILIO — Auditoría Mayo 2026")
print("="*60)

try:
    twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)

    # Inbound messages (To = our WhatsApp number)
    print("\n[1] Consultando mensajes entrantes de WhatsApp...")
    inbound_msgs = twilio_client.messages.list(
        to=f"whatsapp:{TWILIO_NUMBER}",
        date_sent_after=datetime(2026, 5, 1),
        date_sent_before=datetime(2026, 6, 1),
    )

    inbound_senders = set()
    inbound_daily = defaultdict(int)
    inbound_statuses = defaultdict(int)

    for m in inbound_msgs:
        sender = m.from_ or ""
        inbound_senders.add(sender)
        if m.date_sent:
            day = m.date_sent.strftime("%Y-%m-%d")
            inbound_daily[day] += 1
        inbound_statuses[m.status] += 1

    print(f"  Total mensajes entrantes: {len(inbound_msgs)}")
    print(f"  Remitentes únicos: {len(inbound_senders)}")
    print(f"  Statuses: {dict(inbound_statuses)}")

    # Outbound messages (From = our WhatsApp number)
    print("\n[2] Consultando mensajes salientes...")
    outbound_msgs = twilio_client.messages.list(
        from_=f"whatsapp:{TWILIO_NUMBER}",
        date_sent_after=datetime(2026, 5, 1),
        date_sent_before=datetime(2026, 6, 1),
    )

    outbound_recipients = set()
    for m in outbound_msgs:
        outbound_recipients.add(m.to or "")

    print(f"  Total mensajes salientes: {len(outbound_msgs)}")
    print(f"  Destinatarios únicos: {len(outbound_recipients)}")

    # Clean phone numbers for cross-reference
    twilio_phones_clean = set()
    for s in inbound_senders:
        phone = s.replace("whatsapp:", "")
        twilio_phones_clean.add(phone)

    results["twilio"] = {
        "inbound_total": len(inbound_msgs),
        "inbound_unique_senders": len(inbound_senders),
        "inbound_senders_list": list(twilio_phones_clean),
        "inbound_statuses": dict(inbound_statuses),
        "inbound_daily": dict(inbound_daily),
        "outbound_total": len(outbound_msgs),
        "outbound_unique_recipients": len(outbound_recipients),
    }

    print("\n✓ Twilio completado")

except Exception as e:
    print(f"\n✗ Error Twilio: {e}")
    results["twilio"] = {"error": str(e)}

# ═══════════════════════════════════════════════════════════════════════════
# SAVE RESULTS
# ═══════════════════════════════════════════════════════════════════════════
# Convert sets and non-serializable types
def serialize(obj):
    if isinstance(obj, set):
        return list(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, default=serialize, ensure_ascii=False)

print(f"\n{'='*60}")
print(f"  Resultados guardados en: {OUTPUT_FILE}")
print(f"{'='*60}")
