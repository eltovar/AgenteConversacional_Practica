import redis
from datetime import datetime, timezone, timedelta

r = redis.from_url("redis://default:dJuBLJwnXguDHOipiUJsnJotsPbHLNTJ@hopper.proxy.rlwy.net:22690", decode_responses=True)

TZ_BOG = timezone(timedelta(hours=-5))

# Timestamps para 04/03/2026 (Bogotá UTC-5)
# 04/03/2026 00:00:00 -05:00
ts_mar4_start = datetime(2026, 3, 4, 0, 0, 0, tzinfo=TZ_BOG).timestamp()
ts_mar4_end   = datetime(2026, 3, 4, 23, 59, 59, tzinfo=TZ_BOG).timestamp()

# Timestamps para <= 12/03/2026 23:59:59 -05:00
ts_mar12_end  = datetime(2026, 3, 12, 23, 59, 59, tzinfo=TZ_BOG).timestamp()

total = r.zcard("active_conversations_sorted")
print(f"Total contactos en ZSET: {total}\n")

# 1. Contactos que llegaron el 04/03/2026
members_mar4 = r.zrangebyscore("active_conversations_sorted", ts_mar4_start, ts_mar4_end, withscores=True)
print(f"1. Contactos llegados el 04/03/2026: {len(members_mar4)}")
for member, score in members_mar4:
    dt = datetime.fromtimestamp(score, tz=TZ_BOG)
    print(f"   {member}  —  {dt.strftime('%Y-%m-%d %H:%M:%S')}")

print()

# 2. Contactos del 12/03/2026 para atrás (desde el inicio)
members_before_mar12 = r.zrangebyscore("active_conversations_sorted", "-inf", ts_mar12_end, withscores=True)
print(f"2. Contactos del 12/03/2026 para atrás: {len(members_before_mar12)}")
for member, score in members_before_mar12:
    dt = datetime.fromtimestamp(score, tz=TZ_BOG)
    print(f"   {member}  —  {dt.strftime('%Y-%m-%d %H:%M:%S')}")
