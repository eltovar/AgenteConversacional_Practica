#!/usr/bin/env python3
"""
scripts/reconcile_history_from_hubspot.py
==========================================
Reconciliación de historial: HubSpot timeline notes → MongoDB messages.

Caso de uso: cuando el `memory watchdog` mata el worker durante el procesamiento
de un mensaje, el `insert_one` en MongoDB puede no haberse completado mientras
que la nota en HubSpot sí se envió (corre en otro background task con tenacity
retry). Resultado: huecos en MongoDB `messages` que rompen el historial del panel.

Este script lee notas de HubSpot para los contactos seleccionados, las compara
con MongoDB `messages` (dedupe por timestamp + sender + content), e inserta los
faltantes con flag `_restored_from_hubspot=True`. Idempotente: re-ejecutar es
seguro y no duplica.

MODOS DE EJECUCIÓN
------------------
  python scripts/reconcile_history_from_hubspot.py
      → diagnóstico únicamente (default): cuenta huecos, no escribe.

  python scripts/reconcile_history_from_hubspot.py --dry-run
      → simulación: muestra qué insertaría sin escribir.

  python scripts/reconcile_history_from_hubspot.py --confirm
      → ejecuta restauración real en MongoDB.

FILTROS
-------
  --contact-id 12345           → un solo contacto
  --phone +573015396265        → buscar por teléfono normalizado
  --owner-id 89096378          → solo contactos asignados a esa asesora
  --gap-threshold 5            → mín. diferencia (HubSpot - Mongo) para considerar hueco (default 5)
  --since 2026-03-01           → notas desde esta fecha (ISO)
  --limit-contacts 50          → procesar máximo N contactos por ejecución
  --hubspot-notes-per-contact  → cap de notas a traer por contacto (default 500)

SEGURIDAD
---------
- Default es siempre diagnóstico (no escribe nada).
- --confirm requerido explícitamente para mutar MongoDB.
- Reporte final con cantidad de inserts, duplicados omitidos, errores.
- Idempotente: usa hash (timestamp_rounded + sender + first_200_chars) para dedupe.
- Respeta rate limits de HubSpot via _rate_limited_request del timeline_logger.

VARIABLES DE ENTORNO REQUERIDAS
-------------------------------
  MONGO_URL (o MONGO_PUBLIC_URL para local)
  HUBSPOT_ACCESS_TOKEN
  REDIS_URL (opcional, solo si --owner-id y se quiere usar ZSET)

EJEMPLOS
--------
  # 1. Diagnóstico global (qué contactos tienen huecos):
  python scripts/reconcile_history_from_hubspot.py --gap-threshold 5

  # 2. Reconciliar solo a Celia (caso reportado):
  python scripts/reconcile_history_from_hubspot.py --phone +573015396265 --confirm

  # 3. Reconciliar todos los contactos con huecos significativos (cuidado en prod):
  python scripts/reconcile_history_from_hubspot.py --gap-threshold 5 --confirm

  # 4. Solo asesora Jubeny, últimos 30 días:
  python scripts/reconcile_history_from_hubspot.py --owner-id 89096378 --since 2026-04-30 --confirm
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

# Permitir importar desde la raíz del proyecto sin instalar el paquete
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ── Local-execution URL patch ──────────────────────────────────────────────
# Cuando este script corre vía `railway run` desde una máquina externa al
# cluster, Railway inyecta RAILWAY_ENVIRONMENT + URLs *.railway.internal que
# solo resuelven dentro del cluster. Si detectamos URLs públicas disponibles,
# las preferimos y deseteamos RAILWAY_ENVIRONMENT para que el resto del
# código (mongodb_client, redis client, etc.) tome el path "local".
# NO afecta producción: en Railway no hay *_PUBLIC_URL variables expuestas
# al worker, y aunque las hubiera, la URL no contiene "railway.internal".
def _force_public_urls_for_local_execution() -> None:
    swaps: List[str] = []

    # MongoDB
    mongo_current = os.getenv("MONGO_URL") or os.getenv("MONGODB_URL") or ""
    mongo_public = os.getenv("MONGO_PUBLIC_URL") or os.getenv("MONGODB_PUBLIC_URL")
    if mongo_public and "railway.internal" in mongo_current:
        os.environ["MONGO_URL"] = mongo_public
        swaps.append("MONGO_URL→public")

    # Redis (opcional — solo si se usa)
    redis_current = os.getenv("REDIS_URL") or ""
    redis_public = os.getenv("REDIS_PUBLIC_URL")
    if redis_public and "railway.internal" in redis_current:
        os.environ["REDIS_URL"] = redis_public
        swaps.append("REDIS_URL→public")

    # Si hicimos algún swap, desetar RAILWAY_ENVIRONMENT para que el cliente
    # de MongoDB no fuerce el path "internal-only" (líneas 61-72 de
    # mongodb_client.py).
    if swaps and os.getenv("RAILWAY_ENVIRONMENT"):
        os.environ.pop("RAILWAY_ENVIRONMENT", None)
        swaps.append("RAILWAY_ENVIRONMENT=unset")

    if swaps:
        # No imprimir las URLs — solo qué se cambió (las URLs llevan password).
        print(f"[INFO] Local-exec env patch: {', '.join(swaps)}", file=sys.stderr)


_force_public_urls_for_local_execution()
# ────────────────────────────────────────────────────────────────────────────


from logging_config import logger  # noqa: E402

TIMEZONE_BOGOTA = ZoneInfo("America/Bogota")

# Sender map HubSpot → MongoDB
# get_notes_for_contact retorna sender ∈ {"client","bot","advisor","unknown"}
_VALID_SENDERS = {"client", "bot", "advisor", "system"}


def _content_hash(timestamp_iso: str, sender: str, content: str) -> str:
    """
    Hash de dedupe: timestamp redondeado a minuto + sender + primeros 200 chars.

    Redondear a minuto evita falsos negativos por jitter de ms entre Mongo y
    HubSpot. Si dos mensajes idénticos llegan en el mismo minuto se consolidan
    (esquina aceptable; mejor que duplicar).
    """
    try:
        dt = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
        ts_minute = dt.replace(second=0, microsecond=0).isoformat()
    except Exception:
        ts_minute = (timestamp_iso or "")[:16]
    key = f"{ts_minute}|{sender}|{(content or '')[:200]}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _parse_hubspot_timestamp(ts: Optional[str]) -> Optional[datetime]:
    """HubSpot timestamps son ISO con Z. Retorna datetime tz-aware UTC."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


async def _get_contacts_to_process(
    args: argparse.Namespace,
    mongo_manager,
    state_manager_redis,
) -> List[Dict[str, str]]:
    """
    Decide qué contactos procesar según filtros CLI.
    Retorna lista de dicts {phone, contact_id, display_name?, canal?}.
    """
    contacts: List[Dict[str, str]] = []

    # Caso 1: contact_id o phone explícito
    if args.contact_id:
        # Buscar phone para ese contact_id (si existe en conversations)
        doc = await mongo_manager.db.conversations.find_one(
            {"contact_id": args.contact_id}
        )
        if doc:
            contacts.append({
                "phone": doc.get("phone"),
                "contact_id": args.contact_id,
                "display_name": doc.get("display_name"),
                "canal": doc.get("canal") or "whatsapp",
            })
        else:
            contacts.append({
                "phone": None,
                "contact_id": args.contact_id,
                "display_name": None,
                "canal": "whatsapp",
            })
        return contacts

    if args.phone:
        # Buscar contact_id desde conversations o messages
        doc = await mongo_manager.db.conversations.find_one({"phone": args.phone})
        contact_id = doc.get("contact_id") if doc else None
        if not contact_id:
            # Fallback: buscar en messages
            msg = await mongo_manager.db.messages.find_one(
                {"phone": args.phone, "hubspot_contact_id": {"$ne": None}},
                {"hubspot_contact_id": 1},
            )
            contact_id = msg.get("hubspot_contact_id") if msg else None

        if not contact_id:
            print(f"[ERROR] No se encontró contact_id HubSpot para {args.phone}")
            print("        Pasa --contact-id manualmente o verifica que tenga mensajes en MongoDB.")
            return []

        contacts.append({
            "phone": args.phone,
            "contact_id": contact_id,
            "display_name": (doc.get("display_name") if doc else None),
            "canal": (doc.get("canal") if doc else "whatsapp"),
        })
        return contacts

    # Caso 2: barrido — usar colección conversations
    query: Dict[str, Any] = {"contact_id": {"$exists": True, "$ne": None}}
    if args.owner_id:
        query["owner_id"] = args.owner_id

    cursor = mongo_manager.db.conversations.find(query).sort(
        "last_message_at", -1
    ).limit(args.limit_contacts)
    async for doc in cursor:
        contacts.append({
            "phone": doc.get("phone"),
            "contact_id": doc.get("contact_id"),
            "display_name": doc.get("display_name"),
            "canal": doc.get("canal") or "whatsapp",
        })

    if not contacts:
        # Fallback: si conversations está vacía (script corriendo pre-deploy),
        # usar la colección messages para listar phones únicos con contact_id.
        print("[INFO] conversations vacía, fallback a messages para inventario.")
        pipeline = [
            {"$match": {"hubspot_contact_id": {"$ne": None}}},
            {"$group": {
                "_id": "$phone",
                "contact_id": {"$first": "$hubspot_contact_id"},
                "channel": {"$first": "$channel"},
                "last_ts": {"$max": "$timestamp"},
            }},
            {"$sort": {"last_ts": -1}},
            {"$limit": args.limit_contacts},
        ]
        async for d in mongo_manager.db.messages.aggregate(pipeline):
            contacts.append({
                "phone": d["_id"],
                "contact_id": d["contact_id"],
                "display_name": None,
                "canal": d.get("channel") or "whatsapp",
            })

    return contacts


async def _diagnose_contact(
    mongo_manager,
    timeline_logger,
    phone: str,
    contact_id: str,
    canal: str,
    since: Optional[datetime],
    notes_limit: int,
) -> Dict[str, Any]:
    """
    Compara mensajes MongoDB vs notas HubSpot para un contacto.
    Retorna dict con conteos y lista de mensajes faltantes.
    """
    # 1) Notas HubSpot
    try:
        hs_notes = await timeline_logger.get_notes_for_contact(
            contact_id=contact_id,
            limit=notes_limit,
            since=since,
        )
    except Exception as e:
        return {"error": f"HubSpot fetch falló: {e}", "phone": phone, "contact_id": contact_id}

    # 2) Mensajes MongoDB (mismo rango)
    mongo_query: Dict[str, Any] = {"phone": phone} if phone else {"hubspot_contact_id": contact_id}
    if since:
        mongo_query["timestamp"] = {"$gte": since}
    mongo_msgs = []
    async for d in mongo_manager.db.messages.find(mongo_query):
        mongo_msgs.append(d)

    # 3) Construir sets de hashes
    mongo_hashes = set()
    for m in mongo_msgs:
        ts = m.get("timestamp")
        if isinstance(ts, datetime):
            ts_iso = ts.isoformat()
        else:
            ts_iso = str(ts) if ts else ""
        mongo_hashes.add(_content_hash(ts_iso, m.get("sender") or "", m.get("content") or ""))

    # 4) Identificar notas HubSpot no presentes en Mongo
    missing: List[Dict[str, Any]] = []
    for note in hs_notes:
        sender = note.get("sender") or "unknown"
        if sender not in _VALID_SENDERS:
            sender = "client" if note.get("align") == "left" else "advisor"
        h = _content_hash(note.get("timestamp") or "", sender, note.get("message") or "")
        if h not in mongo_hashes:
            missing.append({
                "hubspot_note_id": note.get("id"),
                "timestamp_iso": note.get("timestamp"),
                "sender": sender,
                "content": note.get("message") or "",
            })

    return {
        "phone": phone,
        "contact_id": contact_id,
        "canal": canal,
        "hubspot_notes": len(hs_notes),
        "mongo_messages": len(mongo_msgs),
        "gap": len(hs_notes) - len(mongo_msgs),
        "missing_count": len(missing),
        "missing": missing,
    }


async def _restore_missing(
    mongo_manager,
    phone: str,
    contact_id: str,
    canal: str,
    missing: List[Dict[str, Any]],
    dry_run: bool,
) -> Tuple[int, int, int]:
    """
    Inserta los mensajes faltantes en MongoDB messages + upsert conversations.
    Retorna (inserted, skipped_duplicates, errors).
    """
    inserted = skipped = errored = 0
    for m in missing:
        dt = _parse_hubspot_timestamp(m["timestamp_iso"])
        if not dt:
            errored += 1
            continue
        dt_bogota = dt.astimezone(TIMEZONE_BOGOTA)

        # Documento mongo equivalente al formato de save_message
        doc = {
            "phone": phone,
            "content": m["content"],
            "sender": m["sender"],
            "channel": canal,
            "hubspot_contact_id": contact_id,
            "message_sid": None,  # No conocido; HubSpot no lo expone
            "timestamp": dt_bogota,
            "timestamp_utc": dt.astimezone(timezone.utc).replace(tzinfo=None),
            "metadata": {
                "_restored_from_hubspot": True,
                "hubspot_note_id": m["hubspot_note_id"],
                "restored_at": datetime.now(timezone.utc).isoformat(),
            },
            "synced_to_hubspot": True,  # ya estaba en HubSpot por definición
        }

        if dry_run:
            inserted += 1
            continue

        try:
            await mongo_manager.db.messages.insert_one(doc)
            inserted += 1
            # Upsert paralelo en conversations (no fire-and-forget aquí porque
            # queremos consistencia post-script, no path crítico)
            try:
                await mongo_manager.upsert_conversation_on_message(
                    phone=phone,
                    canal=canal,
                    content=m["content"],
                    sender=m["sender"],
                    hubspot_contact_id=contact_id,
                )
            except Exception:
                pass  # conversations no es crítico para que el mensaje aparezca
        except Exception as e:
            errno = str(e).lower()
            if "duplicate" in errno or "e11000" in errno:
                skipped += 1
            else:
                logger.warning(f"[Reconcile] Insert falló {phone}: {e}")
                errored += 1

    return inserted, skipped, errored


def _print_table(rows: List[Dict[str, Any]]) -> None:
    """Tabla legible en consola."""
    if not rows:
        print("(sin filas)")
        return
    headers = ["phone", "contact_id", "hs_notes", "mongo_msgs", "gap", "missing"]
    widths = {h: max(len(h), max((len(str(r.get(h, ""))) for r in rows), default=0)) for h in headers}
    line = "  ".join(h.ljust(widths[h]) for h in headers)
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(str(r.get(h, "")).ljust(widths[h]) for h in headers))


async def main_async(args: argparse.Namespace) -> int:
    # Imports tardíos para evitar costo si --help
    from database.mongodb_client import get_mongo_manager
    from integrations.hubspot.timeline_logger import get_timeline_logger

    mongo_manager = get_mongo_manager()
    ok = await mongo_manager.connect()
    if not ok:
        print("[FATAL] No se pudo conectar a MongoDB. Verifica MONGO_URL.")
        return 2

    timeline_logger = get_timeline_logger()

    # State manager solo necesario si quieres expandir vía ZSET en el futuro
    state_manager_redis = None

    since: Optional[datetime] = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since)
            if since.tzinfo is None:
                since = since.replace(tzinfo=TIMEZONE_BOGOTA)
        except ValueError:
            print(f"[ERROR] --since inválido: {args.since}. Usa ISO 8601 (YYYY-MM-DD).")
            return 2

    mode = "CONFIRM (escribiendo a MongoDB)" if args.confirm else (
        "DRY-RUN (simulación)" if args.dry_run else "DIAGNÓSTICO (solo lectura)"
    )
    print(f"\n=== Reconcile History from HubSpot ===")
    print(f"Modo:       {mode}")
    print(f"Since:      {since.isoformat() if since else 'todo el historial accesible'}")
    print(f"Gap min:    {args.gap_threshold}")
    print(f"Limit:      {args.limit_contacts} contactos")
    print(f"HS notes/c: {args.hubspot_notes_per_contact}")
    print("=" * 38 + "\n")

    contacts = await _get_contacts_to_process(args, mongo_manager, state_manager_redis)
    if not contacts:
        print("[INFO] Sin contactos para procesar con los filtros dados.")
        return 0
    print(f"[INFO] {len(contacts)} contacto(s) a evaluar.\n")

    diagnoses: List[Dict[str, Any]] = []
    contacts_with_gap: List[Dict[str, Any]] = []

    for i, c in enumerate(contacts, 1):
        phone = c.get("phone") or ""
        contact_id = c.get("contact_id") or ""
        canal = c.get("canal") or "whatsapp"
        if not contact_id:
            print(f"  [{i}/{len(contacts)}] {phone} — sin contact_id HubSpot, skip")
            continue
        print(f"  [{i}/{len(contacts)}] Analizando {phone or '(s/phone)'} cid={contact_id} ...", end=" ", flush=True)
        d = await _diagnose_contact(
            mongo_manager, timeline_logger,
            phone=phone, contact_id=contact_id, canal=canal,
            since=since, notes_limit=args.hubspot_notes_per_contact,
        )
        if "error" in d:
            print(f"ERROR ({d['error']})")
            continue
        print(f"hs={d['hubspot_notes']} mongo={d['mongo_messages']} missing={d['missing_count']}")
        diagnoses.append(d)
        if d["missing_count"] >= args.gap_threshold:
            contacts_with_gap.append(d)

        # Pequeña pausa para no saturar HubSpot
        await asyncio.sleep(0.2)

    print(f"\n--- Resumen ---")
    print(f"Contactos analizados:        {len(diagnoses)}")
    print(f"Con huecos >= {args.gap_threshold}: {len(contacts_with_gap)}")
    print(f"Mensajes faltantes total:    {sum(d['missing_count'] for d in contacts_with_gap)}")

    if contacts_with_gap:
        print("\n--- Top 20 contactos con mayor faltante ---")
        _print_table(sorted(contacts_with_gap, key=lambda x: -x["missing_count"])[:20])

    # Si solo es diagnóstico, salir aquí
    if not args.dry_run and not args.confirm:
        print("\n[INFO] Modo diagnóstico. Re-ejecuta con --dry-run o --confirm para restaurar.")
        return 0

    # Restauración (dry-run o real)
    print(f"\n=== Restaurando ({'DRY-RUN' if args.dry_run else 'REAL'}) ===\n")
    total_ins = total_skip = total_err = 0
    for d in contacts_with_gap:
        if not d["missing"]:
            continue
        ins, skip, err = await _restore_missing(
            mongo_manager,
            phone=d["phone"],
            contact_id=d["contact_id"],
            canal=d["canal"],
            missing=d["missing"],
            dry_run=args.dry_run,
        )
        marker = "[DRY]" if args.dry_run else "[OK ]"
        print(f"  {marker} {d['phone']} cid={d['contact_id']} → ins={ins} skip={skip} err={err}")
        total_ins += ins
        total_skip += skip
        total_err += err

    print("\n--- Totales ---")
    print(f"Insertados: {total_ins} {'(simulados)' if args.dry_run else ''}")
    print(f"Saltados (duplicados): {total_skip}")
    print(f"Errores: {total_err}")

    return 0 if total_err == 0 else 1


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Reconcilia historial HubSpot → MongoDB messages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="Simula la restauración sin escribir a MongoDB.")
    mode.add_argument("--confirm", action="store_true",
                      help="Ejecuta la restauración real en MongoDB.")

    p.add_argument("--contact-id", help="HubSpot contact_id específico.")
    p.add_argument("--phone", help="Teléfono normalizado (ej. +573015396265).")
    p.add_argument("--owner-id", help="Filtrar barrido por owner_id (asesora).")
    p.add_argument("--gap-threshold", type=int, default=5,
                   help="Mín. diferencia HubSpot-Mongo para reconciliar (default 5).")
    p.add_argument("--since", help="Fecha ISO desde la cual reconciliar (YYYY-MM-DD).")
    p.add_argument("--limit-contacts", type=int, default=50,
                   help="Máximo de contactos por ejecución (default 50).")
    p.add_argument("--hubspot-notes-per-contact", type=int, default=500,
                   help="Cap de notas HubSpot por contacto (default 500).")
    return p


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n[ABORTED] interrumpido por usuario.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
