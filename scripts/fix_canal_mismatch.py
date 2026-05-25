#!/usr/bin/env python3
"""
Migración de canal en MongoDB + Redis para contactos afectados por el bug
donde final_channel se forzaba a 'whatsapp' ignorando el canal histórico.

CONTEXTO:
  Mensajes de contactos con canal_origen != 'whatsapp' (ej: 'whatsapp_directo')
  quedaron guardados en MongoDB con channel='whatsapp' en lugar del canal correcto.
  Adicionalmente, el ZSET active_conversations_sorted tiene miembros incorrectos
  ({phone}:whatsapp en vez de {phone}:whatsapp_directo), lo que hace al contacto
  invisible en el panel hasta que envíe un nuevo mensaje post-fix.

SEGURIDAD:
  - Por defecto corre en DRY RUN: solo muestra la vista previa, NO modifica nada.
  - Para aplicar cambios usar el flag --execute.

USO (desde la raíz del proyecto):
  python scripts/fix_canal_mismatch.py              ← Vista previa
  python scripts/fix_canal_mismatch.py --execute    ← Migración real

AGREGAR MÁS CONTACTOS:
  Editar la lista TARGET_CONTACTS al inicio del script.
  Formato: {"phone": "+57...", "wrong_canal": "whatsapp", "correct_canal": "whatsapp_directo"}
"""

import sys
import os
import asyncio
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import motor.motor_asyncio
import redis.asyncio as aioredis

# ============================================================
# CONFIGURACIÓN — editar aquí para agregar contactos afectados
# ============================================================

TARGET_CONTACTS = [
    {
        "phone":         "+573113271182",
        "wrong_canal":   "whatsapp",
        "correct_canal": "whatsapp_directo",
        "note":          "Juan David Saldarriaga — canal_origen ignorado por bug final_channel",
    },
    # Agregar más contactos si se identifican otros afectados:
    # {
    #     "phone":         "+57XXXXXXXXXX",
    #     "wrong_canal":   "whatsapp",
    #     "correct_canal": "whatsapp_directo",
    #     "note":          "Descripción del caso",
    # },
]

MONGO_URL = (
    os.getenv("MONGO_PUBLIC_URL") or
    os.getenv("MONGODB_PUBLIC_URL") or
    os.getenv("MONGO_URL") or
    os.getenv("MONGODB_URL")
)
MONGO_DB_NAME = "inmobiliaria_chat"

REDIS_URL = (
    os.getenv("REDIS_PUBLIC_URL") or
    os.getenv("REDIS_URL")
)

ACTIVE_CONTACTS_ZSET = "active_conversations_sorted"


# ============================================================
# DIAGNÓSTICO (dry run)
# ============================================================
async def diagnose_contact(db, redis_client, contact: dict) -> dict:
    """Lee el estado actual sin modificar nada. Retorna resumen."""
    phone        = contact["phone"]
    wrong_canal  = contact["wrong_canal"]
    correct_canal = contact["correct_canal"]

    # MongoDB: mensajes con canal incorrecto
    wrong_count = await db.messages.count_documents({
        "phone":   phone,
        "channel": wrong_canal,
    })
    # MongoDB: mensajes con canal correcto ya existentes
    correct_count = await db.messages.count_documents({
        "phone":   phone,
        "channel": correct_canal,
    })
    # MongoDB: total de mensajes para este teléfono
    total_count = await db.messages.count_documents({"phone": phone})

    # Redis ZSET
    old_member = f"{phone}:{wrong_canal}"
    new_member = f"{phone}:{correct_canal}"
    old_score  = await redis_client.zscore(ACTIVE_CONTACTS_ZSET, old_member)
    new_score  = await redis_client.zscore(ACTIVE_CONTACTS_ZSET, new_member)

    return {
        "phone":          phone,
        "wrong_canal":    wrong_canal,
        "correct_canal":  correct_canal,
        "mongo_wrong":    wrong_count,
        "mongo_correct":  correct_count,
        "mongo_total":    total_count,
        "zset_old_score": old_score,   # None si no existe
        "zset_new_score": new_score,   # None si no existe
    }


def print_diagnosis(diag: dict):
    phone         = diag["phone"]
    wrong_canal   = diag["wrong_canal"]
    correct_canal = diag["correct_canal"]

    print(f"\n  Teléfono:     {phone}")
    print(f"  Canal actual: '{wrong_canal}'  →  Canal correcto: '{correct_canal}'")
    print()
    print(f"  MongoDB — colección messages:")
    print(f"    Mensajes con canal incorrecto ('{wrong_canal}'):   {diag['mongo_wrong']:>5}  ← se actualizarán")
    print(f"    Mensajes con canal correcto ('{correct_canal}'): {diag['mongo_correct']:>5}  (ya correctos)")
    print(f"    Total mensajes del contacto:                      {diag['mongo_total']:>5}")
    print()
    print(f"  Redis — ZSET '{ACTIVE_CONTACTS_ZSET}':")

    old_member = f"{phone}:{wrong_canal}"
    new_member = f"{phone}:{correct_canal}"

    if diag["zset_old_score"] is not None:
        ts = datetime.fromtimestamp(diag["zset_old_score"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"    Miembro incorrecto '{old_member}': score={diag['zset_old_score']:.0f} ({ts})  ← se migrará")
    else:
        print(f"    Miembro incorrecto '{old_member}': NO existe en ZSET")

    if diag["zset_new_score"] is not None:
        print(f"    Miembro correcto   '{new_member}': ya existe (score={diag['zset_new_score']:.0f})")
    else:
        print(f"    Miembro correcto   '{new_member}': NO existe  ← se creará (si el incorrecto existe)")


# ============================================================
# MIGRACIÓN (execute)
# ============================================================
async def migrate_contact(db, redis_client, contact: dict, dry_run: bool) -> dict:
    """Aplica la corrección en MongoDB y Redis para un contacto."""
    phone         = contact["phone"]
    wrong_canal   = contact["wrong_canal"]
    correct_canal = contact["correct_canal"]

    result = {
        "phone":            phone,
        "mongo_updated":    0,
        "zset_migrated":    False,
        "errors":           [],
    }

    # ── PASO 1: MongoDB ──────────────────────────────────────────────────────
    wrong_count = await db.messages.count_documents({
        "phone":   phone,
        "channel": wrong_canal,
    })

    if wrong_count == 0:
        print(f"  MongoDB: sin mensajes con channel='{wrong_canal}' — nada que migrar.")
    elif dry_run:
        print(f"  MongoDB: {wrong_count} mensaje(s) se actualizarían "
              f"(channel: '{wrong_canal}' → '{correct_canal}')")
        result["mongo_updated"] = wrong_count
    else:
        try:
            update_result = await db.messages.update_many(
                {"phone": phone, "channel": wrong_canal},
                {
                    "$set": {
                        "channel": correct_canal,
                        "metadata.canal_fix_applied_at": datetime.now(timezone.utc).isoformat(),
                        "metadata.canal_fix_from": wrong_canal,
                    }
                }
            )
            result["mongo_updated"] = update_result.modified_count
            print(f"  MongoDB: {update_result.modified_count} mensaje(s) actualizados "
                  f"('{wrong_canal}' → '{correct_canal}')")
        except Exception as e:
            msg = f"Error actualizando MongoDB: {e}"
            result["errors"].append(msg)
            print(f"  ❌ {msg}")

    # ── PASO 2: Redis ZSET ───────────────────────────────────────────────────
    old_member = f"{phone}:{wrong_canal}"
    new_member = f"{phone}:{correct_canal}"

    score = await redis_client.zscore(ACTIVE_CONTACTS_ZSET, old_member)

    if score is None:
        print(f"  Redis ZSET: miembro '{old_member}' no encontrado — nada que migrar.")
    elif dry_run:
        ts = datetime.fromtimestamp(score, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"  Redis ZSET: miembro '{old_member}' (score={score:.0f}, {ts}) "
              f"se reemplazaría por '{new_member}'")
        result["zset_migrated"] = True
    else:
        try:
            # Atómico: agregar nuevo miembro con mismo score, luego eliminar el viejo
            await redis_client.zadd(ACTIVE_CONTACTS_ZSET, {new_member: score})
            await redis_client.zrem(ACTIVE_CONTACTS_ZSET, old_member)
            result["zset_migrated"] = True
            print(f"  Redis ZSET: '{old_member}' → '{new_member}' (score={score:.0f})")
        except Exception as e:
            msg = f"Error migrando ZSET Redis: {e}"
            result["errors"].append(msg)
            print(f"  ❌ {msg}")

    return result


# ============================================================
# VERIFICACIÓN POST-MIGRACIÓN
# ============================================================
async def verify_contact(db, redis_client, contact: dict):
    """Verifica que la migración quedó correcta."""
    phone         = contact["phone"]
    wrong_canal   = contact["wrong_canal"]
    correct_canal = contact["correct_canal"]

    remaining_wrong = await db.messages.count_documents({
        "phone": phone, "channel": wrong_canal
    })
    correct_count = await db.messages.count_documents({
        "phone": phone, "channel": correct_canal
    })

    old_member = f"{phone}:{wrong_canal}"
    new_member = f"{phone}:{correct_canal}"
    old_score  = await redis_client.zscore(ACTIVE_CONTACTS_ZSET, old_member)
    new_score  = await redis_client.zscore(ACTIVE_CONTACTS_ZSET, new_member)

    print(f"\n  Verificación post-migración:")
    mongo_ok = remaining_wrong == 0
    zset_ok  = old_score is None  # el miembro incorrecto debe haber desaparecido

    print(f"    MongoDB mensajes '{wrong_canal}' restantes:  {remaining_wrong}  "
          f"{'✅' if mongo_ok else '❌ ERROR — quedan mensajes sin migrar'}")
    print(f"    MongoDB mensajes '{correct_canal}': {correct_count}  ✅")
    print(f"    ZSET miembro incorrecto '{old_member}': "
          f"{'❌ AÚN EXISTE' if old_score else '✅ eliminado'}")
    print(f"    ZSET miembro correcto   '{new_member}': "
          f"{'✅ existe (score=' + str(int(new_score)) + ')' if new_score else '⚠️  no existe (contacto no activo)'}")


# ============================================================
# MAIN
# ============================================================
async def main(dry_run: bool):
    # Validar variables de entorno
    missing = []
    if not MONGO_URL:  missing.append("MONGO_URL / MONGO_PUBLIC_URL")
    if not REDIS_URL:  missing.append("REDIS_URL / REDIS_PUBLIC_URL")
    if missing:
        print(f"\n❌ ERROR: Faltan variables de entorno:\n   {', '.join(missing)}")
        sys.exit(1)

    mode_label = "DRY RUN (vista previa, NO modifica nada)" if dry_run else "⚠️  EJECUCIÓN REAL"

    print("\n" + "=" * 64)
    print("  FIX CANAL MISMATCH — MongoDB + Redis")
    print("=" * 64)
    print(f"  Modo:      {mode_label}")
    print(f"  MongoDB:   {MONGO_DB_NAME}.messages")
    print(f"  Redis:     active_conversations_sorted (ZSET)")
    print(f"  Contactos: {len(TARGET_CONTACTS)}")
    print("=" * 64)

    # Conectar
    mongo_client  = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=8000)
    db            = mongo_client[MONGO_DB_NAME]
    redis_client  = await aioredis.from_url(REDIS_URL, decode_responses=True)

    total_mongo   = 0
    total_zset    = 0
    total_errors  = 0

    try:
        for contact in TARGET_CONTACTS:
            print(f"\n{'─' * 56}")
            print(f"  Contacto: {contact['phone']}")
            if contact.get("note"):
                print(f"  Nota:     {contact['note']}")
            print(f"{'─' * 56}")

            # Diagnóstico previo (siempre)
            diag = await diagnose_contact(db, redis_client, contact)
            print_diagnosis(diag)

            if diag["mongo_wrong"] == 0 and diag["zset_old_score"] is None:
                print("\n  ✅ Sin cambios necesarios para este contacto.")
                continue

            print()
            result = await migrate_contact(db, redis_client, contact, dry_run)
            total_mongo  += result["mongo_updated"]
            if result["zset_migrated"]:
                total_zset += 1
            total_errors += len(result["errors"])

            # Verificación solo en ejecución real
            if not dry_run and not result["errors"]:
                await verify_contact(db, redis_client, contact)

    finally:
        await redis_client.aclose()
        mongo_client.close()

    # Resumen global
    print(f"\n{'=' * 64}")
    label = "VISTA PREVIA" if dry_run else "MIGRACIÓN COMPLETADA"
    print(f"  {label}")
    print(f"  Mensajes MongoDB {'a actualizar' if dry_run else 'actualizados'}: {total_mongo}")
    print(f"  Entradas ZSET Redis {'a migrar' if dry_run else 'migradas'}:      {total_zset}")
    if total_errors:
        print(f"  ❌ Errores: {total_errors}")

    if dry_run and (total_mongo > 0 or total_zset > 0):
        print()
        print("  Para aplicar los cambios, ejecuta:")
        print("  python scripts/fix_canal_mismatch.py --execute")

    print("=" * 64 + "\n")


if __name__ == "__main__":
    dry_run = "--execute" not in sys.argv
    asyncio.run(main(dry_run))
