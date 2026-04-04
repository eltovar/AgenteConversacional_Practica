import redis
import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Conectar a Redis
redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
r = redis.from_url(redis_url)

# Test phone (usa uno real de tu BD)
PHONE = "+573001234567"
CANAL = "whatsapp"

print("\n" + "="*60)
print("TEST: PATCH /appointments Sync Redis")
print("="*60)

# 1. Verificar si la cita existe en Redis
appointment_key = f"appointment:{PHONE}:{CANAL}"
appointment_data = r.get(appointment_key)

if appointment_data:
    apt = json.loads(appointment_data)
    print(f"\n✅ Cita encontrada en Redis:")
    print(f"   Datetime: {apt.get('scheduled_datetime')}")
    print(f"   Reminder sent: {apt.get('reminder_sent')}")
    print(f"   Followup sent: {apt.get('followup_sent')}")
else:
    print(f"\n❌ Cita NO encontrada en Redis (normal si no existe aún)")

# 2. Verificar score en ZSET
index_key = "appointment_index"
score = r.zscore(index_key, appointment_key)
if score is not None:
    from_ts = datetime.fromtimestamp(score, tz=ZoneInfo("America/Bogota"))
    print(f"\n✅ Score en ZSET appointment_index:")
    print(f"   Unix timestamp: {score}")
    print(f"   Fecha/Hora: {from_ts.strftime('%Y-%m-%d %H:%M:%S %Z')}")
else:
    print(f"\n❌ Score NO encontrado en ZSET (normal si no existe aún)")

# 3. Verificar idempotency keys
reminder_key = f"apt_notif_sent:reminder:{PHONE}:{CANAL}"
followup_key = f"apt_notif_sent:followup:{PHONE}:{CANAL}"

reminder_exists = r.exists(reminder_key)
followup_exists = r.exists(followup_key)

print(f"\n📌 Idempotency Keys:")
print(f"   {reminder_key}: {'✅ EXISTS' if reminder_exists else '❌ NOT EXISTS'}")
print(f"   {followup_key}: {'✅ EXISTS' if followup_exists else '❌ NOT EXISTS'}")

print("\n" + "="*60)
