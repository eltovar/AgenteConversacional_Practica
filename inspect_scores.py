import redis
from datetime import datetime, timezone

r = redis.from_url("redis://default:dJuBLJwnXguDHOipiUJsnJotsPbHLNTJ@hopper.proxy.rlwy.net:22690", decode_responses=True)

# Ver los primeros 5 y últimos 5 miembros con su score real
print("=== PRIMEROS 5 (más antiguos) ===")
first = r.zrange("active_conversations_sorted", 0, 4, withscores=True)
for member, score in first:
    print(f"  score={score}  raw={member}")
    try:
        dt = datetime.fromtimestamp(score, tz=timezone.utc)
        print(f"  → como UTC timestamp: {dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    except Exception as e:
        print(f"  → error: {e}")

print()
print("=== ÚLTIMOS 5 (más recientes) ===")
last = r.zrange("active_conversations_sorted", -5, -1, withscores=True)
for member, score in last:
    print(f"  score={score}  raw={member}")
    try:
        dt = datetime.fromtimestamp(score, tz=timezone.utc)
        print(f"  → como UTC timestamp: {dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    except Exception as e:
        print(f"  → error: {e}")
