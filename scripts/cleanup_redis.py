import os, redis, json

CUTOFF_TS = 1772427600  # 2026-03-02T00:00:00-05:00

REDIS_URL = os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL")
if not REDIS_URL:
    raise SystemExit("Falta REDIS_PUBLIC_URL o REDIS_URL en el entorno")
r = redis.from_url(REDIS_URL, decode_responses=True)

total = r.zcard("active_conversations_sorted")
print(f"Total en ZSET: {total}")

old_members = r.zrangebyscore("active_conversations_sorted", "-inf", CUTOFF_TS)
print(f"Miembros antes del 2 de marzo: {len(old_members)}")

for member in old_members:
    parts = member.split(":", 1)
    if len(parts) != 2:
        continue
    phone, canal = parts[0], parts[1]
    keys_to_del = [
        f"conv_meta:{phone}:{canal}",
        f"conv_state:{phone}:{canal}",
        f"conv_was_panel:{phone}:{canal}",
    ]
    deleted = r.delete(*keys_to_del)
    r.zrem("active_conversations_sorted", member)
    print(f"  {phone}:{canal} — {deleted} keys borradas")

print("Listo.")
