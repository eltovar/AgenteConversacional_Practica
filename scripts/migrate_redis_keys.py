#!/usr/bin/env python3
"""
Script de migración para actualizar keys de Redis al nuevo formato con canal.

ANTES: conv_state:{phone}
DESPUES: conv_state:{phone}:{canal}

Este script:
1. Escanea todas las keys existentes con formato legacy
2. Lee la metadata para obtener el canal_origen
3. Crea nuevas keys con el formato phone:canal
4. Opcionalmente elimina las keys legacy

Uso:
    python scripts/migrate_redis_keys.py --dry-run    # Solo muestra qué haría
    python scripts/migrate_redis_keys.py --migrate    # Ejecuta la migración
    python scripts/migrate_redis_keys.py --cleanup    # Elimina keys legacy después de migrar
"""

import asyncio
import os
import sys
import json
import argparse
from datetime import datetime

# Agregar directorio raíz al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import redis.asyncio as aioredis


# Prefijos de keys
STATE_PREFIX = "conv_state:"
META_PREFIX = "conv_meta:"
MESSAGE_PREFIX = "message_store:"

# Canal por defecto para keys sin canal
DEFAULT_CANAL = "default"


def is_legacy_key(key: str, prefix: str) -> bool:
    """
    Determina si una key es formato legacy (sin canal).

    Legacy: conv_state:+573001234567
    Nuevo: conv_state:+573001234567:instagram
    """
    key_without_prefix = key.replace(prefix, "")

    # Si tiene más de un ":" después del prefijo, es formato nuevo
    # (El teléfono puede tener "+" pero no ":")
    parts = key_without_prefix.split(":")

    # Si solo hay una parte o la última parte es solo el teléfono, es legacy
    if len(parts) == 1:
        return True

    # Si la segunda parte parece un canal (no es vacío y no es numérico), es nuevo
    if len(parts) >= 2 and parts[-1] and not parts[-1].replace("+", "").isdigit():
        return False

    return True


async def scan_keys(redis_client, prefix: str) -> list:
    """Escanea todas las keys con un prefijo dado."""
    keys = []
    async for key in redis_client.scan_iter(match=f"{prefix}*"):
        keys.append(key)
    return keys


async def get_canal_from_meta(redis_client, phone: str) -> str:
    """
    Obtiene el canal_origen de la metadata de un teléfono.
    """
    meta_key = f"{META_PREFIX}{phone}"
    meta_str = await redis_client.get(meta_key)

    if meta_str:
        try:
            meta = json.loads(meta_str)
            return meta.get("canal_origen") or DEFAULT_CANAL
        except json.JSONDecodeError:
            pass

    return DEFAULT_CANAL


async def migrate_key(
    redis_client,
    old_key: str,
    prefix: str,
    canal: str,
    dry_run: bool = True
) -> dict:
    """
    Migra una key legacy al nuevo formato con canal.

    Returns:
        Dict con información de la migración
    """
    # Extraer teléfono del key legacy
    phone = old_key.replace(prefix, "")

    # Construir nueva key
    new_key = f"{prefix}{phone}:{canal}"

    result = {
        "old_key": old_key,
        "new_key": new_key,
        "phone": phone,
        "canal": canal,
        "migrated": False,
        "error": None
    }

    if dry_run:
        result["action"] = "DRY_RUN"
        return result

    try:
        # Obtener valor y TTL de la key original
        value = await redis_client.get(old_key)
        ttl = await redis_client.ttl(old_key)

        if value is None:
            result["error"] = "Key no existe"
            return result

        # Crear nueva key con el valor
        if ttl > 0:
            await redis_client.set(new_key, value, ex=ttl)
        else:
            await redis_client.set(new_key, value)

        result["migrated"] = True
        result["action"] = "MIGRATED"
        result["ttl"] = ttl

    except Exception as e:
        result["error"] = str(e)
        result["action"] = "ERROR"

    return result


async def cleanup_legacy_key(redis_client, old_key: str, dry_run: bool = True) -> dict:
    """
    Elimina una key legacy después de la migración.
    """
    result = {
        "key": old_key,
        "deleted": False
    }

    if dry_run:
        result["action"] = "DRY_RUN"
        return result

    try:
        await redis_client.delete(old_key)
        result["deleted"] = True
        result["action"] = "DELETED"
    except Exception as e:
        result["error"] = str(e)
        result["action"] = "ERROR"

    return result


async def run_migration(dry_run: bool = True, cleanup: bool = False):
    """
    Ejecuta la migración completa.
    """
    redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))

    print(f"\n{'='*60}")
    print(f"  MIGRACION DE KEYS REDIS - Segregación por Canal")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Modo: {'DRY-RUN (simulación)' if dry_run else 'PRODUCCION'}")
    print(f"{'='*60}\n")

    print(f"Conectando a Redis: {redis_url[:30]}...")

    try:
        r = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True)
        await r.ping()
        print("[OK] Conexión exitosa\n")
    except Exception as e:
        print(f"[ERROR] No se pudo conectar: {e}")
        return

    # Estadísticas
    stats = {
        "state_legacy": 0,
        "state_new": 0,
        "state_migrated": 0,
        "meta_legacy": 0,
        "meta_new": 0,
        "meta_migrated": 0,
        "message_legacy": 0,
        "message_new": 0,
        "message_migrated": 0,
        "errors": []
    }

    # 1. Migrar STATE keys
    print("PASO 1: Migrando keys de estado (conv_state:*)")
    print("-" * 40)

    state_keys = await scan_keys(r, STATE_PREFIX)

    for key in state_keys:
        if is_legacy_key(key, STATE_PREFIX):
            stats["state_legacy"] += 1
            phone = key.replace(STATE_PREFIX, "")
            canal = await get_canal_from_meta(r, phone)

            result = await migrate_key(r, key, STATE_PREFIX, canal, dry_run)
            print(f"  {result['old_key']} -> {result['new_key']} [{result.get('action', 'PENDING')}]")

            if result.get("migrated"):
                stats["state_migrated"] += 1
            if result.get("error"):
                stats["errors"].append(result)
        else:
            stats["state_new"] += 1

    print(f"\n  Legacy: {stats['state_legacy']}, Nuevo formato: {stats['state_new']}, Migradas: {stats['state_migrated']}\n")

    # 2. Migrar META keys
    print("PASO 2: Migrando keys de metadata (conv_meta:*)")
    print("-" * 40)

    meta_keys = await scan_keys(r, META_PREFIX)

    for key in meta_keys:
        if is_legacy_key(key, META_PREFIX):
            stats["meta_legacy"] += 1
            phone = key.replace(META_PREFIX, "")
            canal = await get_canal_from_meta(r, phone)

            result = await migrate_key(r, key, META_PREFIX, canal, dry_run)
            print(f"  {result['old_key']} -> {result['new_key']} [{result.get('action', 'PENDING')}]")

            if result.get("migrated"):
                stats["meta_migrated"] += 1
            if result.get("error"):
                stats["errors"].append(result)
        else:
            stats["meta_new"] += 1

    print(f"\n  Legacy: {stats['meta_legacy']}, Nuevo formato: {stats['meta_new']}, Migradas: {stats['meta_migrated']}\n")

    # 3. Migrar MESSAGE STORE keys
    print("PASO 3: Migrando keys de historial (message_store:*)")
    print("-" * 40)

    message_keys = await scan_keys(r, MESSAGE_PREFIX)

    for key in message_keys:
        if is_legacy_key(key, MESSAGE_PREFIX):
            stats["message_legacy"] += 1
            phone = key.replace(MESSAGE_PREFIX, "")
            # Para message_store, no tenemos metadata, usar DEFAULT_CANAL
            canal = DEFAULT_CANAL

            result = await migrate_key(r, key, MESSAGE_PREFIX, canal, dry_run)
            print(f"  {result['old_key']} -> {result['new_key']} [{result.get('action', 'PENDING')}]")

            if result.get("migrated"):
                stats["message_migrated"] += 1
            if result.get("error"):
                stats["errors"].append(result)
        else:
            stats["message_new"] += 1

    print(f"\n  Legacy: {stats['message_legacy']}, Nuevo formato: {stats['message_new']}, Migradas: {stats['message_migrated']}\n")

    # 4. Cleanup (opcional)
    if cleanup and not dry_run:
        print("PASO 4: Limpiando keys legacy")
        print("-" * 40)

        # Solo eliminar si la migración fue exitosa
        all_legacy_keys = [
            k for k in state_keys if is_legacy_key(k, STATE_PREFIX)
        ] + [
            k for k in meta_keys if is_legacy_key(k, META_PREFIX)
        ] + [
            k for k in message_keys if is_legacy_key(k, MESSAGE_PREFIX)
        ]

        deleted_count = 0
        for key in all_legacy_keys:
            result = await cleanup_legacy_key(r, key, dry_run=False)
            if result.get("deleted"):
                deleted_count += 1
                print(f"  [DELETED] {key}")

        print(f"\n  Keys eliminadas: {deleted_count}\n")

    # Resumen
    print("=" * 60)
    print("RESUMEN")
    print("=" * 60)

    total_legacy = stats["state_legacy"] + stats["meta_legacy"] + stats["message_legacy"]
    total_migrated = stats["state_migrated"] + stats["meta_migrated"] + stats["message_migrated"]

    print(f"  Keys legacy encontradas: {total_legacy}")
    print(f"  Keys migradas: {total_migrated}")
    print(f"  Errores: {len(stats['errors'])}")

    if dry_run:
        print("\n  [INFO] Esto fue una simulación. Ejecuta con --migrate para aplicar cambios.")
    else:
        print("\n  [OK] Migración completada.")

    if stats["errors"]:
        print("\n  ERRORES:")
        for err in stats["errors"]:
            print(f"    - {err['old_key']}: {err.get('error', 'Unknown')}")

    await r.close()


def main():
    parser = argparse.ArgumentParser(
        description="Migra keys de Redis al nuevo formato con segregación por canal"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Solo muestra qué haría sin aplicar cambios"
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Ejecuta la migración real"
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Elimina keys legacy después de migrar"
    )

    args = parser.parse_args()

    if not args.dry_run and not args.migrate:
        print("Debes especificar --dry-run o --migrate")
        print("Uso: python scripts/migrate_redis_keys.py --dry-run")
        sys.exit(1)

    dry_run = args.dry_run or not args.migrate

    asyncio.run(run_migration(dry_run=dry_run, cleanup=args.cleanup))


if __name__ == "__main__":
    main()
