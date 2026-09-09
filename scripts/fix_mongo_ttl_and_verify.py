#!/usr/bin/env python3
"""
scripts/fix_mongo_ttl_and_verify.py
====================================
Reemplaza la necesidad de mongosh para:
  1) Aplicar el TTL nuevo (730 días) al índice timestamp_ttl_idx de `messages`.
  2) Verificar que la colección `conversations` se está poblando.

Usa el cliente pymongo que ya está en requirements.txt del proyecto.
NO requiere instalar nada adicional.

Toma la URL de MongoDB desde env var MONGO_URL (o MONGO_PUBLIC_URL).

EJECUCIÓN
---------
  # 1. Asegura que la variable de entorno está cargada (sin imprimirla):
  #    $env:MONGO_URL = (railway variables --kv | Select-String "^MONGO_URL=" |
  #                       ForEach-Object { $_ -replace "^MONGO_URL=", "" }).Trim()
  #
  # 2. Diagnóstico (no escribe):
  python scripts/fix_mongo_ttl_and_verify.py

  # 3. Aplicar dropIndex + createIndex con TTL 730d:
  python scripts/fix_mongo_ttl_and_verify.py --apply-ttl

NUNCA imprime la URL — solo longitud y host enmascarado.
"""

from __future__ import annotations
import os
import sys
import argparse
from datetime import datetime, timezone

try:
    from pymongo import MongoClient, ASCENDING, DESCENDING
    from pymongo.errors import OperationFailure
except ImportError:
    print("[ERROR] pymongo no está instalado en este venv.")
    print("       Instalar: pip install pymongo")
    sys.exit(2)


def get_mongo_url() -> str:
    """
    Prioridad de URL:
      1. MONGO_PUBLIC_URL (recomendado para ejecución local — usa proxy externo).
      2. MONGO_URL (solo válido si tiene un host público o estás dentro de Railway).
    Si MONGO_URL apunta a *.railway.internal y no estamos en Railway, abortar
    con instrucciones claras para evitar [Errno 11001] getaddrinfo failed.
    """
    public = os.getenv("MONGO_PUBLIC_URL") or os.getenv("MONGODB_PUBLIC_URL")
    internal = os.getenv("MONGO_URL") or os.getenv("MONGODB_URL")
    is_in_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None

    # Si la única URL disponible apunta a la red interna y NO estamos en Railway,
    # cortar temprano con guía.
    if not public and internal and "railway.internal" in internal and not is_in_railway:
        print("[ERROR] Solo encontré MONGO_URL apuntando a red interna Railway")
        print("        (mongodb.railway.internal) — no resuelve fuera del cluster.")
        print()
        print("        Carga MONGO_PUBLIC_URL en su lugar:")
        print('          $env:MONGO_URL = (railway variables --kv | Select-String "^MONGO_PUBLIC_URL=" |')
        print('                             ForEach-Object { $_ -replace "^MONGO_PUBLIC_URL=", "" }).Trim()')
        sys.exit(2)

    url = public or internal
    if not url:
        print("[ERROR] No encontré MONGO_PUBLIC_URL ni MONGO_URL en el entorno.")
        print('       Carga con: $env:MONGO_URL = (railway variables --kv | Select-String "^MONGO_PUBLIC_URL=" |')
        print('                                    ForEach-Object { $_ -replace "^MONGO_PUBLIC_URL=", "" }).Trim()')
        sys.exit(2)

    # Validación final: si la URL elegida es internal y no estamos en Railway, abortar
    if "railway.internal" in url and not is_in_railway:
        print("[ERROR] La URL configurada apunta a la red interna de Railway.")
        print("       Ese host solo resuelve dentro del cluster.")
        print("       Carga la variable MONGO_PUBLIC_URL en su lugar (ver comando arriba).")
        sys.exit(2)

    return url


def masked_host(url: str) -> str:
    """Retorna solo el host sin password ni user para logging seguro."""
    try:
        after_at = url.split("@", 1)[-1]
        host = after_at.split("/", 1)[0]
        return host
    except Exception:
        return "(unparseable)"


def cmd_diagnose(db) -> None:
    """Imprime estado actual: índices messages, conteo conversations, ejemplo."""
    print("\n=== DIAGNÓSTICO ===\n")

    print("[1] Índice TTL en `messages`:")
    found = False
    for idx in db.messages.list_indexes():
        if idx.get("name") == "timestamp_ttl_idx":
            seconds = idx.get("expireAfterSeconds")
            days = seconds // 86400 if seconds else None
            print(f"    name=timestamp_ttl_idx")
            print(f"    expireAfterSeconds={seconds} ({days} días)")
            print(f"    key={dict(idx.get('key', {}))}")
            found = True
            break
    if not found:
        print("    NO ENCONTRADO — algo raro, posiblemente cambió el nombre.")

    print("\n[2] Colección `conversations`:")
    total = db.conversations.count_documents({})
    print(f"    Total documentos: {total}")
    if total > 0:
        sample = db.conversations.find(
            {},
            {"phone": 1, "canal": 1, "last_message_at": 1, "message_count": 1,
             "owner_id": 1, "display_name": 1}
        ).sort("last_message_at", DESCENDING).limit(3)
        print("    Top 3 más recientes:")
        for d in sample:
            print(f"      - phone={d.get('phone')} canal={d.get('canal')} "
                  f"count={d.get('message_count')} "
                  f"last={d.get('last_message_at')} "
                  f"name={d.get('display_name') or '(s/n)'}")
    else:
        print("    ⚠️ VACÍA — el upsert estaba fallando (bug message_count conflict).")
        print("    Tras el deploy del fix, esto debería crecer con cada mensaje nuevo.")

    print("\n[3] Índices en `conversations`:")
    for idx in db.conversations.list_indexes():
        print(f"    - {idx.get('name')}: keys={dict(idx.get('key', {}))}")

    print("\n[4] Total documentos `messages` (referencia):")
    msg_total = db.messages.count_documents({})
    print(f"    {msg_total} documentos")


def cmd_apply_ttl(db) -> int:
    """Aplica dropIndex + createIndex para que timestamp_ttl_idx sea 730 días."""
    print("\n=== APLICANDO TTL 730 DÍAS ===\n")

    # Verificar estado actual
    current = None
    for idx in db.messages.list_indexes():
        if idx.get("name") == "timestamp_ttl_idx":
            current = idx
            break

    if not current:
        print("[ERROR] Índice timestamp_ttl_idx no existe. Aborto.")
        return 1

    seconds = current.get("expireAfterSeconds")
    print(f"Estado actual: expireAfterSeconds={seconds} ({seconds // 86400}d)")

    if seconds == 63072000:
        print("✅ El TTL ya es 730 días. Nada que hacer.")
        return 0

    confirm = input(f"\n¿Cambiar TTL de {seconds // 86400}d → 730d? (escribe 'YES' para confirmar): ")
    if confirm != "YES":
        print("Aborto por usuario.")
        return 1

    print("\n→ dropIndex('timestamp_ttl_idx')...")
    try:
        db.messages.drop_index("timestamp_ttl_idx")
        print("  ✅ Drop OK")
    except OperationFailure as e:
        print(f"  ❌ Drop falló: {e}")
        return 2


    print("\n→ createIndex(timestamp:1, expireAfterSeconds=63072000)...")
    try:
        db.messages.create_index(
            [("timestamp", ASCENDING)],
            name="timestamp_ttl_idx",
            expireAfterSeconds=63072000,  # 730 días
        )
        print("  ✅ Create OK")
    except OperationFailure as e:
        print(f"  ❌ Create falló: {e}")
        print("  ⚠️ ATENCIÓN: el índice fue dropeado pero NO recreado.")
        print("     Re-ejecuta este script para reintentar.")
        return 3

    # Verificación final
    print("\n→ Verificación final:")
    for idx in db.messages.list_indexes():
        if idx.get("name") == "timestamp_ttl_idx":
            new_seconds = idx.get("expireAfterSeconds")
            print(f"  expireAfterSeconds={new_seconds} ({new_seconds // 86400} días)")
            if new_seconds == 63072000:
                print("  ✅ TTL aplicado correctamente.")
                return 0

    print("  ⚠️ Verificación no encontró el índice esperado.")
    return 4


def main() -> int:
    parser = argparse.ArgumentParser(description="Fix MongoDB TTL + diagnóstico conversations.")
    parser.add_argument("--apply-ttl", action="store_true",
                        help="Ejecuta dropIndex + createIndex para subir TTL a 730d.")
    args = parser.parse_args()

    url = get_mongo_url()
    print(f"Conectando a MongoDB host={masked_host(url)} (URL length={len(url)})")
    try:
        client = MongoClient(url, serverSelectionTimeoutMS=10000)
        client.admin.command("ping")
        print("✅ Ping OK")
    except Exception as e:
        print(f"[ERROR] No se pudo conectar: {e}")
        return 2

    db = client.get_database("inmobiliaria_chat")

    if args.apply_ttl:
        return cmd_apply_ttl(db)
    else:
        cmd_diagnose(db)
        print("\n→ Para aplicar el TTL nuevo: python scripts/fix_mongo_ttl_and_verify.py --apply-ttl")
        return 0


if __name__ == "__main__":
    sys.exit(main())
