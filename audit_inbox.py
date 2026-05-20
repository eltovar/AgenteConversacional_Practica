import redis
import os
import json
import sys

# Conexión a Redis (soporta 3 modos)
# 1. Variable de entorno REDIS_URL (via railway run)
# 2. Tunnel local railway connect redis en localhost:16379
# 3. URL pasada como argumento: python audit_inbox.py redis://...

url = None
#comentatio para comintear algo

# Intenta 1: Railway run
url = os.environ.get('REDIS_URL')

# Intenta 2: Railway tunnel local (default)
if not url:
    try:
        r_test = redis.Redis(host='localhost', port=16379, decode_responses=True)
        r_test.ping()
        url = 'redis://localhost:16379'
        print("✓ Conectado a Railway tunnel (localhost:16379)")
    except:
        pass

# Intenta 3: Argumento command line
if not url and len(sys.argv) > 1:
    url = sys.argv[1]

if not url:
    print("❌ Error: No se pudo conectar a Redis")
    print("\nOpciones:")
    print("1. Abre otra terminal y ejecuta: railway connect redis")
    print("   Luego en esta terminal: python audit_inbox.py")
    print("\n2. O pasa la URL: python audit_inbox.py redis://user:pass@host:port")
    exit()

try:
    r = redis.from_url(url, decode_responses=True)
    r.ping()
except Exception as e:
    print(f"❌ Error conectando a Redis: {e}")
    print(f"   URL utilizada: {url}")
    exit()

# Variables de tu test
ADVISOR_ID = "89096380"
CONTACT_TEL = "+573192474089"
CONTACT_KEY = f"{CONTACT_TEL}:ciencuadras"

print(f"\n🚀 --- INICIANDO AUDITORÍA TÉCNICA: ADVISOR {ADVISOR_ID} ---")

# COMANDO 1: Estructura de advisor_inbox
print(f"\n1. 📂 Contenido de advisor_inbox:{ADVISOR_ID}:")
inbox_data = r.zrange(f"advisor_inbox:{ADVISOR_ID}", 0, -1, withscores=True)
if inbox_data:
    print(f"   Total de entries: {len(inbox_data)}")
    for member, score in inbox_data:
        print(f"   - {member} (Score/Timestamp: {score})")
    
    # Análisis de canales
    canales_unicos = set()
    for member, _ in inbox_data:
        if ':' in member:
            canal = member.split(':')[1]
            canales_unicos.add(canal)
    
    print(f"\n   📊 Canales únicos encontrados: {sorted(canales_unicos)}")
    print(f"   ⚠️  Lista hardcodeada: ['whatsapp', 'instagram', 'facebook', 'finca_raiz', 'metrocuadrado', 'pagina_web', 'whatsapp_directo']")
    
    no_hardcodeados = canales_unicos - {
        'whatsapp', 'instagram', 'facebook',
        'finca_raiz', 'metrocuadrado',
        'pagina_web', 'whatsapp_directo'
    }
    if no_hardcodeados:
        print(f"   🔴 CANALES NO HARDCODEADOS: {sorted(no_hardcodeados)} ← CAUSA DEL BUG")
else:
    print("   ✓ Vacío")

# COMANDO 2: Conversaciones activas (Bylex sim)
print(f"\n2. 💬 Buscando '{CONTACT_TEL}' en active_conversations_sorted:")
active_convs = r.zrange("active_conversations_sorted", 0, -1)
found = [c for c in active_convs if CONTACT_TEL in c]
if found:
    print(f"   Total encontrados: {len(found)}")
    for c in found:
        score = r.zscore("active_conversations_sorted", c)
        print(f"   - {c} (Score: {score})")
else:
    print("   ✓ Ninguno encontrado")

# COMANDO 3: Meta del contacto
print(f"\n3. 🔍 Meta del contacto ({CONTACT_KEY}):")
meta = r.get(f"conv_meta:{CONTACT_KEY}")
if meta:
    try:
        meta_obj = json.loads(meta)
        print(f"   ✓ Encontrado")
        print(f"   - display_name: {meta_obj.get('display_name', 'N/A')}")
        print(f"   - last_activity: {meta_obj.get('last_activity', 'N/A')}")
        print(f"   - last_advisor_message: {meta_obj.get('last_advisor_message', 'N/A')}")
        print(f"   - assigned_owner_id: {meta_obj.get('assigned_owner_id', 'N/A')}")
    except:
        print(f"   - Raw: {meta}")
else:
    print(f"   ⚠️ No encontrada")

# COMANDO 4: Total de entries
print(f"\n4. 🔢 Total de contactos en inbox:")
count = r.zcard(f"advisor_inbox:{ADVISOR_ID}")
print(f"   - Cantidad: {count}")

# COMANDO 5: Verificación específica del contacto problemático
print(f"\n5. 🎯 Verificación del contacto {CONTACT_TEL} con canal 'ciencuadras':")
test_member = f"{CONTACT_TEL}:ciencuadras"
is_in = r.zscore(f"advisor_inbox:{ADVISOR_ID}", test_member)
if is_in is not None:
    print(f"   ✓ ENCONTRADO en advisor_inbox (Score: {is_in})")
    print(f"   🔴 Este es el problema: se agrega pero remove_from_advisor_inbox NO lo remueve")
else:
    print(f"   ✓ No encontrado (podría ser removido correctamente)")

# COMANDO 6: Total de conexiones WS (si existe)
print(f"\n6. 📡 WebSocket Manager Stats:")
try:
    ws_count = r.get("ws_manager:connection_count")
    if ws_count:
        print(f"   - Conexiones activas: {ws_count}")
    else:
        print(f"   - Stat no registrado")
except:
    print(f"   - Error leyendo stat")

print("\n✅ --- FIN DE LA AUDITORÍA ---\n")
