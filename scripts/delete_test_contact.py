#!/usr/bin/env python3
"""
delete_test_contact.py
======================
Elimina TODOS los datos de un número de teléfono específico para pruebas.

Limpia:
- Redis: conv_state, conv_meta, session, índices del panel
- MongoDB: mensajes del contacto

USO:
  python scripts/delete_test_contact.py +573001234567
  python scripts/delete_test_contact.py +573001234567 --dry-run
"""

import os
import sys
from pathlib import Path

# Setup path para imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import redis
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# Colores
RED = '\033[91m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
CYAN = '\033[96m'
RESET = '\033[0m'


def main():
    if len(sys.argv) < 2:
        print(f"{RED}Error: Debes proporcionar un número de teléfono{RESET}")
        print(f"Uso: python scripts/delete_test_contact.py +573001234567")
        sys.exit(1)
    
    phone = sys.argv[1].strip()
    if not phone.startswith("+"):
        phone = f"+{phone}"
    
    dry_run = "--dry-run" in sys.argv
    
    print(f"\n{CYAN}{'='*60}{RESET}")
    print(f"{CYAN}  ELIMINAR CONTACTO DE PRUEBA: {phone}{RESET}")
    print(f"{CYAN}  Modo: {'[DRY RUN]' if dry_run else '[REAL]'}{RESET}")
    print(f"{CYAN}{'='*60}{RESET}\n")
    
    # ═══════════════════════════════════════════════════════════════
    # REDIS
    # ═══════════════════════════════════════════════════════════════
    redis_url = os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
    r = redis.from_url(redis_url, decode_responses=True, socket_timeout=10)
    
    print(f"{YELLOW}[Redis] Conectando...{RESET}")
    r.ping()
    
    deleted_keys = 0
    
    # Patrones de claves a eliminar
    patterns = [
        f"conv_state:{phone}:*",
        f"conv_meta:{phone}:*",
        f"session:{phone}",
        f"last_client_msg:{phone}",
        f"phone_cache:{phone}",
        f"lead_creation_lock:{phone}",
    ]
    
    for pattern in patterns:
        for key in r.scan_iter(match=pattern):
            print(f"  {GREEN}DELETE{RESET} {key}")
            if not dry_run:
                r.delete(key)
            deleted_keys += 1
    
    # Limpiar de índices (ZSET y SET)
    indices = [
        ("active_conversations_sorted", "zset"),
        ("active_conversations_index", "set"),
        ("bot_controlled_conversations", "set"),
    ]
    
    for idx_name, idx_type in indices:
        try:
            if idx_type == "zset":
                members = r.zrange(idx_name, 0, -1)
            else:
                members = r.smembers(idx_name)
            
            for member in members:
                if phone in member:
                    print(f"  {GREEN}REMOVE{RESET} {member} from {idx_name}")
                    if not dry_run:
                        if idx_type == "zset":
                            r.zrem(idx_name, member)
                        else:
                            r.srem(idx_name, member)
                    deleted_keys += 1
        except Exception as e:
            print(f"  {YELLOW}SKIP{RESET} {idx_name}: {e}")
    
    print(f"\n{GREEN}[Redis] {deleted_keys} items eliminados{RESET}")
    
    # ═══════════════════════════════════════════════════════════════
    # MONGODB
    # ═══════════════════════════════════════════════════════════════
    mongo_uri = os.getenv("MONGODB_URI")
    if mongo_uri:
        print(f"\n{YELLOW}[MongoDB] Conectando...{RESET}")
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
        db = client["sofia_db"]
        
        # Mensajes
        if dry_run:
            count = db.messages.count_documents({"phone": phone})
            print(f"  {GREEN}WOULD DELETE{RESET} {count} mensajes")
        else:
            result = db.messages.delete_many({"phone": phone})
            print(f"  {GREEN}DELETED{RESET} {result.deleted_count} mensajes")
        
        # Citas (si existen)
        if dry_run:
            count = db.appointments.count_documents({"phone": phone})
            if count > 0:
                print(f"  {GREEN}WOULD DELETE{RESET} {count} citas")
        else:
            result = db.appointments.delete_many({"phone": phone})
            if result.deleted_count > 0:
                print(f"  {GREEN}DELETED{RESET} {result.deleted_count} citas")
        
        client.close()
    else:
        print(f"\n{YELLOW}[MongoDB] MONGODB_URI no configurada, saltando...{RESET}")
    
    # ═══════════════════════════════════════════════════════════════
    # RESUMEN
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{CYAN}{'='*60}{RESET}")
    if dry_run:
        print(f"{YELLOW}  [DRY RUN] No se eliminó nada. Ejecuta sin --dry-run para borrar.{RESET}")
    else:
        print(f"{GREEN}  ✅ Contacto {phone} eliminado de Redis y MongoDB{RESET}")
        print(f"{YELLOW}  ⚠️  HubSpot NO fue modificado (eliminar manualmente si es necesario){RESET}")
    print(f"{CYAN}{'='*60}{RESET}\n")


if __name__ == "__main__":
    main()
