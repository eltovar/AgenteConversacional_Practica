# scripts/test_panel_data.py
"""
Script para insertar datos de prueba en Redis y visualizar el panel de control.

Uso:
    python scripts/test_panel_data.py

Esto insertará contactos de prueba en estado HUMAN_ACTIVE que aparecerán
automáticamente en el panel de asesores.
"""

import asyncio
import os
import sys
import json
from datetime import datetime, timedelta

# Agregar el directorio raíz al path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

# Cargar variables de entorno desde .env
from dotenv import load_dotenv
env_path = os.path.join(ROOT_DIR, ".env")
load_dotenv(env_path)

import redis.asyncio as redis


# Configuración - Usar REDIS_PUBLIC_URL para conexión externa
REDIS_URL = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
print(f"📡 Usando Redis URL: {REDIS_URL}")

# Prefijos de Redis (deben coincidir con conversation_state.py)
STATE_PREFIX = "conv_state:"
META_PREFIX = "conv_meta:"

# Datos de prueba - Contactos simulados
TEST_CONTACTS = [
    {
        "phone": "+573001234567",
        "contact_id": "test_contact_001",
        "display_name": "Juan Pérez",
        "handoff_reason": "Cliente solicitó hablar con asesor",
        "ttl_seconds": 7200,  # 2 horas
    },
    {
        "phone": "+573009876543",
        "contact_id": "test_contact_002",
        "display_name": "María García",
        "handoff_reason": "Interesado en apartamento código 12345",
        "ttl_seconds": 5400,  # 1.5 horas
    },
    {
        "phone": "+573005551234",
        "contact_id": "test_contact_003",
        "display_name": "Carlos Rodríguez",
        "handoff_reason": "Quiere agendar visita urgente",
        "ttl_seconds": 3600,  # 1 hora
    },
]


async def insert_test_contacts():
    """Inserta contactos de prueba en Redis con estado HUMAN_ACTIVE."""
    print("=" * 60)
    print("🧪 INSERTANDO DATOS DE PRUEBA EN REDIS")
    print("=" * 60)

    r = redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)

    try:
        # Verificar conexión
        await r.ping()
        print(f"✅ Conectado a Redis: {REDIS_URL}")
        print()

        for contact in TEST_CONTACTS:
            phone = contact["phone"]

            # 1. Insertar estado HUMAN_ACTIVE
            state_key = f"{STATE_PREFIX}{phone}"
            await r.set(state_key, "HUMAN_ACTIVE", ex=contact["ttl_seconds"])

            # 2. Insertar metadata
            meta_key = f"{META_PREFIX}{phone}"
            now = datetime.now()
            metadata = {
                "phone_normalized": phone,
                "contact_id": contact["contact_id"],
                "status": "HUMAN_ACTIVE",
                "last_activity": now.isoformat(),
                "handoff_reason": contact["handoff_reason"],
                "assigned_owner_id": None,
                "message_count": 5,
                "created_at": (now - timedelta(hours=1)).isoformat(),
            }
            await r.set(meta_key, json.dumps(metadata), ex=contact["ttl_seconds"])

            # Calcular TTL restante para mostrar
            hours = contact["ttl_seconds"] // 3600
            minutes = (contact["ttl_seconds"] % 3600) // 60

            print(f"📱 Contacto insertado:")
            print(f"   Nombre: {contact['display_name']}")
            print(f"   Teléfono: {phone}")
            print(f"   Razón: {contact['handoff_reason']}")
            print(f"   TTL: {hours}h {minutes}m")
            print()

        print("=" * 60)
        print("✅ DATOS INSERTADOS CORRECTAMENTE")
        print("=" * 60)
        print()
        print("Ahora puedes abrir el panel en:")
        print(f"   http://localhost:8001/whatsapp/panel/?key=TU_ADMIN_API_KEY")
        print()
        print("O probar el endpoint de contactos:")
        print("   Invoke-RestMethod -Uri 'http://localhost:8001/whatsapp/panel/contacts' -Headers @{'X-API-Key'='TU_ADMIN_API_KEY'}")
        print()

    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        await r.close()


async def clear_test_contacts():
    """Elimina los contactos de prueba de Redis."""
    print("🧹 Limpiando contactos de prueba...")

    r = redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)

    try:
        for contact in TEST_CONTACTS:
            phone = contact["phone"]
            state_key = f"{STATE_PREFIX}{phone}"
            meta_key = f"{META_PREFIX}{phone}"

            await r.delete(state_key, meta_key)
            print(f"   ✓ Eliminado: {phone}")

        print("✅ Contactos de prueba eliminados")

    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        await r.close()


async def show_current_contacts():
    """Muestra los contactos actualmente en HUMAN_ACTIVE."""
    print("=" * 60)
    print("📋 CONTACTOS ACTUALES EN HUMAN_ACTIVE")
    print("=" * 60)

    r = redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)

    try:
        count = 0
        async for key in r.scan_iter(match=f"{STATE_PREFIX}*"):
            status = await r.get(key)
            if status == "HUMAN_ACTIVE":
                phone = key.replace(STATE_PREFIX, "")
                ttl = await r.ttl(key)

                # Obtener metadata
                meta_key = f"{META_PREFIX}{phone}"
                meta_str = await r.get(meta_key)
                meta = json.loads(meta_str) if meta_str else {}

                hours = ttl // 3600 if ttl > 0 else 0
                minutes = (ttl % 3600) // 60 if ttl > 0 else 0

                count += 1
                print(f"\n{count}. 📱 {phone}")
                print(f"   Estado: HUMAN_ACTIVE")
                print(f"   Contact ID: {meta.get('contact_id', 'N/A')}")
                print(f"   Razón: {meta.get('handoff_reason', 'N/A')}")
                print(f"   TTL restante: {hours}h {minutes}m")

        if count == 0:
            print("\n   (No hay contactos en HUMAN_ACTIVE)")

        print()

    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        await r.close()


def print_usage():
    """Muestra las instrucciones de uso."""
    print("""
╔══════════════════════════════════════════════════════════════╗
║           TEST DEL PANEL DE ASESORES                         ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Uso:                                                        ║
║    python scripts/test_panel_data.py [comando]               ║
║                                                              ║
║  Comandos:                                                   ║
║    insert   - Insertar contactos de prueba                   ║
║    clear    - Eliminar contactos de prueba                   ║
║    show     - Mostrar contactos actuales                     ║
║    help     - Mostrar esta ayuda                             ║
║                                                              ║
║  Sin argumentos ejecuta 'insert' por defecto                 ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
""")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "insert"

    if command == "insert":
        asyncio.run(insert_test_contacts())
    elif command == "clear":
        asyncio.run(clear_test_contacts())
    elif command == "show":
        asyncio.run(show_current_contacts())
    elif command == "help":
        print_usage()
    else:
        print(f"Comando desconocido: {command}")
        print_usage()
