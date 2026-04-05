"""
Fix puntual: restaura la ventana de 24h para un contacto específico.

Caso: el mensaje del cliente llegó al WhatsApp pero la llave Redis
`last_client_msg:{phone}` no se actualizó (ej. hipo en Railway al
procesar el webhook).

Uso:
    python scripts/fix_window_alejandro.py
    python scripts/fix_window_alejandro.py +573198765432   ← otro teléfono
"""
import sys
import redis
from datetime import datetime, timezone

REDIS_URL = "redis://default:dJuBLJwnXguDHOipiUJsnJotsPbHLNTJ@hopper.proxy.rlwy.net:22690"
PREFIX    = "last_client_msg:"
TTL_SEC   = 25 * 60 * 60  # 25 horas (igual que en outbound_panel.py)

# Teléfono por defecto (Alejandro) — se puede pasar otro como arg
phone = sys.argv[1] if len(sys.argv) > 1 else "+573147190419"
key   = f"{PREFIX}{phone}"

r = redis.from_url(REDIS_URL, decode_responses=True)

# Mostrar estado ANTES
current = r.get(key)
ttl     = r.ttl(key)
print(f"\nANTES:")
print(f"  key : {key}")
print(f"  val : {current or '(no existe)'}")
print(f"  ttl : {ttl}s  ({'no existe' if ttl < 0 else f'{ttl//3600}h {(ttl%3600)//60}m restantes'})")

# Escribir timestamp actual (ventana abierta desde ahora por 25h)
timestamp = datetime.now(timezone.utc).isoformat() + "Z"
r.set(key, timestamp, ex=TTL_SEC)

# Verificar DESPUÉS
new_val = r.get(key)
new_ttl = r.ttl(key)
print(f"\nDESPUES:")
print(f"  val : {new_val}")
print(f"  ttl : {new_ttl//3600}h {(new_ttl%3600)//60}m")
print(f"\nListo. Pide a la asesora que haga F5 - la ventana aparecera abierta.")
r.close()
