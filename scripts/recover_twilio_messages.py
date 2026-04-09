#!/usr/bin/env python3
"""
Recuperación de historial de conversaciones desde Twilio API → MongoDB.

CONTEXTO:
  Los mensajes de +573053316687 y +573013661610 no llegaron a MongoDB debido
  a un OOM kill del worker de Railway que interrumpió las BackgroundTasks antes
  de que mongo_manager.save_message() se ejecutara.

SEGURIDAD:
  - Por defecto corre en DRY RUN: solo muestra la vista previa, NO inserta nada.
  - Para insertar de verdad usar el flag --execute.
  - Deduplicación por message_sid: nunca crea duplicados.

USO (desde la raíz del proyecto):
  python scripts/recover_twilio_messages.py              ← Vista previa
  python scripts/recover_twilio_messages.py --execute    ← Inserción real
"""

import sys
import os
import asyncio
from datetime import datetime, timedelta, timezone

# Agregar raíz del proyecto al path para importar módulos internos
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from twilio.rest import Client as TwilioClient
import motor.motor_asyncio
import pymongo.errors

# ============================================================
# CONFIGURACIÓN
# ============================================================
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WA_NUMBER   = os.getenv("TWILIO_PHONE_NUMBER", "")  # formato: whatsapp:+573123969894

# Para ejecución LOCAL: usar URL pública de Railway (la interna solo es accesible
# desde dentro de Railway). Priorizar MONGO_PUBLIC_URL sobre MONGO_URL.
MONGO_URL = (
    os.getenv("MONGO_PUBLIC_URL") or
    os.getenv("MONGODB_PUBLIC_URL") or
    os.getenv("MONGO_URL") or
    os.getenv("MONGODB_URL")
)
MONGO_DB_NAME = "inmobiliaria_chat"

# Contactos afectados
TARGET_PHONES = [
    "+573053316687",
    "+573013661610",
    "+573113570021",
    "+573044532188",
    "+573014192759",
    "+573016476173",
]

# Rango: últimos 90 días (máximo que conserva Twilio en plan estándar)
END_DATE   = datetime.now(timezone.utc)
START_DATE = END_DATE - timedelta(days=90)


# ============================================================
# HELPERS
# ============================================================
def _normalize(number: str) -> str:
    """Elimina el prefijo 'whatsapp:' y espacios."""
    return number.replace("whatsapp:", "").strip()

def _to_wa(number: str) -> str:
    """Retorna el número con prefijo 'whatsapp:'."""
    return f"whatsapp:{_normalize(number)}"


# ============================================================
# FETCH DESDE TWILIO (SÍNCRONO — Twilio SDK no es async)
# ============================================================
def fetch_twilio_messages(twilio_client: TwilioClient, phone: str) -> list:
    """
    Obtiene todos los mensajes de un contacto: inbound (cliente → nosotros)
    y outbound (nosotros → cliente). Retorna lista ordenada cronológicamente.
    """
    wa_client  = _to_wa(phone)
    wa_us      = _to_wa(TWILIO_WA_NUMBER)  # asegura prefijo whatsapp: siempre

    all_msgs = []

    # 1. INBOUND: cliente envió a nosotros
    print(f"    Buscando inbound  ({wa_client} → {wa_us})...")
    inbound = twilio_client.messages.list(
        from_=wa_client,
        to=wa_us,
        date_sent_after=START_DATE,
        date_sent_before=END_DATE,
        limit=1000
    )
    all_msgs.extend(inbound)
    print(f"    → {len(inbound)} mensajes inbound")

    # 2. OUTBOUND: nosotros enviamos al cliente
    print(f"    Buscando outbound ({wa_us} → {wa_client})...")
    outbound = twilio_client.messages.list(
        from_=wa_us,
        to=wa_client,
        date_sent_after=START_DATE,
        date_sent_before=END_DATE,
        limit=1000
    )
    all_msgs.extend(outbound)
    print(f"    → {len(outbound)} mensajes outbound")

    # Ordenar por fecha ascendente (más antiguo primero)
    all_msgs.sort(
        key=lambda m: m.date_sent or datetime.min.replace(tzinfo=timezone.utc)
    )
    return all_msgs


# ============================================================
# CONSTRUCCIÓN DEL DOCUMENTO MONGODB
# ============================================================
def build_doc(msg, client_phone: str) -> dict:
    """
    Convierte un mensaje de Twilio al formato de documento de la colección
    `messages` en MongoDB, compatible con get_history().
    """
    from_clean  = _normalize(msg.from_ or "")
    client_clean = _normalize(client_phone)

    # Determinar sender: inbound = "client", outbound = "advisor"
    # (no hay forma de saber si fue el bot o un asesor — usamos "advisor")
    sender = "client" if from_clean == client_clean else "advisor"

    # Normalizar timezone del timestamp
    ts = msg.date_sent
    if ts and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    return {
        "phone":              client_phone,
        "content":            msg.body or "[Mensaje vacío]",
        "sender":             sender,
        "channel":            "whatsapp",
        "message_sid":        msg.sid,
        "timestamp":          ts,
        "timestamp_utc":      ts,
        "media":              None,   # Twilio no expone binario de media en historial
        "synced_to_hubspot":  False,
        "metadata": {
            "recovery_source":  "twilio_api",
            "recovered_at":     datetime.now(timezone.utc).isoformat(),
            "twilio_status":    msg.status,
            "twilio_direction": msg.direction,
        },
        "reply_to_id":        None,
        "reply_to_preview":   None,
    }


# ============================================================
# INSERCIÓN EN MONGODB (ASÍNCRONO — Motor)
# ============================================================
async def recover_contact(db, phone: str, messages: list, dry_run: bool) -> int:
    """
    Procesa e inserta los mensajes de un contacto.
    Retorna la cantidad de mensajes insertados (o que se insertarían).
    """
    inserted    = 0
    dup_skipped = 0
    errors      = 0

    for msg in messages:
        if not msg.sid:
            continue

        doc = build_doc(msg, phone)
        ts_str = doc["timestamp"].strftime("%Y-%m-%d %H:%M") if doc["timestamp"] else "?"

        if dry_run:
            arrow = "→" if doc["sender"] == "client" else "←"
            print(f"    {ts_str}  {arrow}  [{doc['sender']:8}]  {doc['content'][:65]!r}")
            inserted += 1
            continue

        # Deduplicación: verificar que no exista antes de insertar
        existing = await db.messages.find_one({"message_sid": msg.sid}, {"_id": 1})
        if existing:
            dup_skipped += 1
            continue

        try:
            await db.messages.insert_one(doc)
            inserted += 1
            print(f"    ✅ {ts_str}  [{doc['sender']}]  {doc['content'][:55]!r}")
        except pymongo.errors.DuplicateKeyError:
            dup_skipped += 1
        except Exception as e:
            print(f"    ❌ Error insertando {msg.sid}: {e}")
            errors += 1

    # Resumen del contacto
    if dry_run:
        print(f"\n    VISTA PREVIA: {inserted} mensajes se insertarían")
    else:
        print(f"\n    Insertados:           {inserted}")
        print(f"    Duplicados (saltados): {dup_skipped}")
        if errors:
            print(f"    Errores:              {errors}")

    return inserted


# ============================================================
# MAIN
# ============================================================
async def main(dry_run: bool):

    # Validar variables de entorno requeridas
    missing = []
    if not TWILIO_ACCOUNT_SID: missing.append("TWILIO_ACCOUNT_SID")
    if not TWILIO_AUTH_TOKEN:  missing.append("TWILIO_AUTH_TOKEN")
    if not TWILIO_WA_NUMBER:   missing.append("TWILIO_PHONE_NUMBER")
    if not MONGO_URL:          missing.append("MONGO_URL / MONGO_PUBLIC_URL")
    if missing:
        print(f"\n❌ ERROR: Faltan variables de entorno:\n   {', '.join(missing)}")
        sys.exit(1)

    # Header
    print("\n" + "=" * 62)
    print("  RECUPERACIÓN DE MENSAJES — TWILIO API → MONGODB")
    print("=" * 62)
    mode_label = "DRY RUN (vista previa, NO inserta)" if dry_run else "⚠️  EJECUCIÓN REAL"
    print(f"  Modo:     {mode_label}")
    print(f"  Período:  {START_DATE.strftime('%Y-%m-%d')} → {END_DATE.strftime('%Y-%m-%d')}")
    print(f"  Twilio #: {TWILIO_WA_NUMBER}")
    print(f"  MongoDB:  {MONGO_DB_NAME}.messages")
    print(f"  Contactos: {', '.join(TARGET_PHONES)}")
    print("=" * 62)

    # Clientes
    twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    mongo  = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=8000)
    db     = mongo[MONGO_DB_NAME]

    total = 0

    for phone in TARGET_PHONES:
        print(f"\n{'─' * 50}")
        print(f"  Contacto: {phone}")
        print(f"{'─' * 50}")

        # Fetch Twilio (síncrono)
        messages = fetch_twilio_messages(twilio, phone)

        if not messages:
            print(f"  ⚠️  Sin mensajes en Twilio para este número en el período.")
            continue

        print(f"\n  Total encontrado en Twilio: {len(messages)} mensajes\n")

        count = await recover_contact(db, phone, messages, dry_run)
        total += count

    # Resumen global
    print(f"\n{'=' * 62}")
    label = "MENSAJES EN VISTA PREVIA" if dry_run else "MENSAJES RECUPERADOS"
    print(f"  TOTAL {label}: {total}")

    if dry_run and total > 0:
        print()
        print("  Para ejecutar la inserción real, corre:")
        print("  python scripts/recover_twilio_messages.py --execute")

    print("=" * 62 + "\n")

    mongo.close()


if __name__ == "__main__":
    dry_run = "--execute" not in sys.argv
    asyncio.run(main(dry_run))
