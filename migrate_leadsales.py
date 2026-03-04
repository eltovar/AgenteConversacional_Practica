#!/usr/bin/env python3
"""
migrate_leadsales.py
====================
Migración de contactos Leadsales → HubSpot + MongoDB

QUÉ HACE:
  - Lee C:\\Users\\Salo\\Downloads\\contactos_limpios.xlsx
  - Filtra los 1,602 contactos con teléfono real (descarta Messenger/Facebook IDs)
  - Por cada contacto:
      1. Crea el Contacto en HubSpot (con owner asignado por Funnel Name)
      2. Crea el Deal en HubSpot pipeline 854756009 → etapa "Nuevo Lead"
      3. Escribe url_chat (deep link CRM → Panel) en contacto y deal
      4. Guarda el cache phone→hubspot_id en MongoDB.contacts
      5. Escribe estado BOT_ACTIVE en Redis → contacto visible en el panel
  - Checkpoint cada 50 contactos (permite reanudar si se interrumpe)
  - Genera reporte Excel al finalizar

USO:
  python migrate_leadsales.py              # Migración real
  python migrate_leadsales.py --dry-run   # Simular sin tocar ninguna API
  python migrate_leadsales.py --resume    # Continuar desde checkpoint
"""

import asyncio
import io
import json
import os
import re
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

# Forzar UTF-8 en stdout para que los logs con tildes/emojis no fallen en Windows
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import httpx
import pandas as pd
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv(Path(__file__).parent / ".env")

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
MONGO_URL       = os.getenv("MONGO_URL") or os.getenv("MONGO_PUBLIC_URL", "")
REDIS_URL       = os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
PANEL_BASE_URL  = os.getenv("PANEL_BASE_URL", "").rstrip("/")
ADMIN_API_KEY   = os.getenv("ADMIN_API_KEY", "")

# Redis: mismos prefijos y constantes que ConversationStateManager
REDIS_STATE_PREFIX      = "conv_state:"
REDIS_META_PREFIX       = "conv_meta:"
REDIS_ZSET              = "active_conversations_sorted"
REDIS_SET_LEGACY        = "active_conversations_index"
REDIS_BOT_CONTROLLED    = "bot_controlled_conversations"  # Contactos BOT sin prioridad
REDIS_CANAL             = "whatsapp"   # canal canónico para todos los migrados
REDIS_MIGRATION_TTL     = 604800       # 7 días — los migrados no son urgentes,
                                       # se les da más tiempo antes de expirar del panel

HUBSPOT_PIPELINE_ID      = "854756009"
HUBSPOT_STAGE_NUEVO_LEAD = "1275156339"

FUNNEL_TO_OWNER = {
    "LUISA":  "89096380",
    "MONICA": "89096379",
    "JUBENY": "89096378",
}

EXCEL_PATH      = Path(r"C:\Users\Salo\Downloads\contactos_limpios.xlsx")
CHECKPOINT_FILE = Path(__file__).parent / "migrate_checkpoint.json"
REPORT_FILE     = Path(r"C:\Users\Salo\Downloads\migrate_report.xlsx")

# Rate limiting: HubSpot permite 100 req/10s.
# Cada contacto usa hasta 4 llamadas → ~0.4s mínimo/contacto.
# Usamos 0.12s de pausa entre llamadas individuales para ser conservadores.
SLEEP_BETWEEN_CALLS    = 0.12   # segundos entre cada llamada HTTP
SLEEP_BETWEEN_CONTACTS = 0.10   # pausa extra entre contactos
MAX_RETRIES            = 4
RETRY_WAIT_BASE        = 2.0    # segundos (exponential backoff en 429)

DRY_RUN = "--dry-run" in sys.argv
RESUME  = "--resume"  in sys.argv

HUBSPOT_BASE = "https://api.hubapi.com"


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS: NORMALIZACIÓN Y FORMATO
# ─────────────────────────────────────────────────────────────────────────────

def normalize_phone(lead) -> Optional[str]:
    """
    Convierte el campo Lead a formato E.164 (+57XXXXXXXXXX).
    Retorna None si no es un teléfono válido.
    """
    if pd.isna(lead):
        return None
    s = str(lead).strip()
    if "@" in s:
        return None  # Facebook LID
    try:
        ns = str(int(float(s)))
        if len(ns) > 15:
            return None  # Messenger ID (muy largo)
        if len(ns) >= 10:
            return f"+{ns}"
    except (ValueError, OverflowError):
        pass
    return None


def strip_emojis(text: str) -> str:
    """Elimina emojis y caracteres de control del texto."""
    return "".join(
        ch for ch in text
        if unicodedata.category(ch) not in ("So", "Cs", "Cc", "Cf")
    ).strip()


def parse_name(name) -> tuple[str, str]:
    """Parsea el campo Name → (firstname, lastname)."""
    if pd.isna(name) or not str(name).strip():
        return ("Contacto", "Migrado")
    clean = strip_emojis(str(name))
    if not clean:
        return ("Contacto", "Migrado")
    parts = clean.split(None, 1)
    firstname = parts[0][:100]
    lastname  = parts[1][:100] if len(parts) > 1 else ""
    return (firstname, lastname)


def build_url_chat(owner_id: str, phone: str) -> str:
    """Construye el deep link CRM → Panel (mismo formato que contact_manager.py)."""
    if not PANEL_BASE_URL or not ADMIN_API_KEY or not owner_id:
        return ""
    phone_encoded = quote(phone, safe="")
    return (
        f"{PANEL_BASE_URL}/whatsapp/panel/"
        f"?key={ADMIN_API_KEY}&advisor={owner_id}&phone={phone_encoded}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# CHECKPOINT
# ─────────────────────────────────────────────────────────────────────────────

def load_checkpoint() -> set:
    if CHECKPOINT_FILE.exists():
        data = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
        return set(data.get("done", []))
    return set()


def save_checkpoint(done: set):
    CHECKPOINT_FILE.write_text(
        json.dumps(
            {"done": list(done), "updated_at": datetime.now().isoformat()},
            ensure_ascii=False, indent=2
        ),
        encoding="utf-8"
    )


# ─────────────────────────────────────────────────────────────────────────────
# HUBSPOT API
# ─────────────────────────────────────────────────────────────────────────────

async def hs_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    payload: dict = None
) -> Optional[httpx.Response]:
    """
    Realiza una llamada a la API de HubSpot con retry y backoff en 429.
    path puede ser una URL completa o un path relativo a HUBSPOT_BASE.
    """
    url = path if path.startswith("http") else f"{HUBSPOT_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = await client.request(method, url, json=payload, headers=headers)
            if r.status_code == 429:
                wait = RETRY_WAIT_BASE * (2 ** attempt)
                print(f"    ⚠ Rate limit (429) — esperando {wait:.1f}s …")
                await asyncio.sleep(wait)
                continue
            return r
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_WAIT_BASE * (2 ** attempt))
            else:
                print(f"    ✗ Error de red tras {MAX_RETRIES} intentos: {exc}")
                return None
    return None


async def create_hubspot_contact(
    client: httpx.AsyncClient,
    phone: str,
    firstname: str,
    lastname: str,
    owner_id: str,
    channel: str,
) -> tuple[Optional[str], bool]:
    """
    Crea el contacto en HubSpot.
    Retorna (contact_id, ya_existia).
    Si ya existía (409) devuelve el ID existente y ya_existia=True.
    """
    midnight_utc = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    properties = {
        "whatsapp_id":       phone,
        "phone":             phone,
        "firstname":         firstname,
        "lastname":          lastname,
        "hubspot_owner_id":  owner_id,
        "canal_origen":      channel,
        "lifecyclestage":    "lead",
        "chatbot_timestamp": str(int(midnight_utc.timestamp() * 1000)),
    }
    r = await hs_request(client, "POST", "/crm/v3/objects/contacts", {"properties": properties})
    await asyncio.sleep(SLEEP_BETWEEN_CALLS)

    if r is None:
        return None, False

    if r.status_code in (200, 201):
        return r.json().get("id"), False

    if r.status_code == 409:
        # Contacto duplicado: extraer ID del mensaje de error
        body = r.json()
        msg  = body.get("message", "")
        match = re.search(r"Existing ID: (\d+)", msg)
        if match:
            return match.group(1), True

        # Fallback: buscar por whatsapp_id
        sr = await hs_request(
            client, "POST", "/crm/v3/objects/contacts/search",
            {
                "filterGroups": [{"filters": [
                    {"propertyName": "whatsapp_id", "operator": "EQ", "value": phone}
                ]}],
                "properties": ["hs_object_id"],
                "limit": 1,
            }
        )
        await asyncio.sleep(SLEEP_BETWEEN_CALLS)
        if sr and sr.status_code == 200:
            results = sr.json().get("results", [])
            if results:
                return results[0].get("id"), True

    print(f"    ✗ Error al crear contacto: {r.status_code} — {r.text[:150]}")
    return None, False


async def create_hubspot_deal(
    client: httpx.AsyncClient,
    contact_id: str,
    dealname: str,
    owner_id: str,
    amount: Optional[float],
) -> Optional[str]:
    """
    Crea el deal y lo asocia al contacto en la misma llamada.
    Retorna deal_id o None si falla.
    """
    properties = {
        "dealname":         dealname,
        "pipeline":         HUBSPOT_PIPELINE_ID,
        "dealstage":        HUBSPOT_STAGE_NUEVO_LEAD,
        "hubspot_owner_id": owner_id,
    }
    if amount and amount > 0:
        properties["amount"] = str(int(amount))

    payload = {
        "properties": properties,
        "associations": [{
            "to":    {"id": contact_id},
            "types": [{
                "associationCategory": "HUBSPOT_DEFINED",
                "associationTypeId":   3   # deal_to_contact
            }]
        }]
    }
    r = await hs_request(client, "POST", "/crm/v3/objects/deals", payload)
    await asyncio.sleep(SLEEP_BETWEEN_CALLS)

    if r and r.status_code in (200, 201):
        return r.json().get("id")

    if r:
        print(f"    ⚠ Deal no creado: {r.status_code} — {r.text[:150]}")
    return None


async def write_url_chat(
    client: httpx.AsyncClient,
    contact_id: str,
    deal_id: Optional[str],
    url_chat: str,
):
    """Escribe url_chat en el contacto y en el deal."""
    if not url_chat:
        return
    await hs_request(
        client, "PATCH", f"/crm/v3/objects/contacts/{contact_id}",
        {"properties": {"url_chat": url_chat}}
    )
    await asyncio.sleep(SLEEP_BETWEEN_CALLS)

    if deal_id:
        await hs_request(
            client, "PATCH", f"/crm/v3/objects/deals/{deal_id}",
            {"properties": {"url_chat": url_chat}}
        )
        await asyncio.sleep(SLEEP_BETWEEN_CALLS)


# ─────────────────────────────────────────────────────────────────────────────
# MONGODB
# ─────────────────────────────────────────────────────────────────────────────

async def upsert_mongo_contact(motor_db, phone: str, hubspot_id: str, name: str, channel: str):
    """Actualiza o crea el cache local phone → hubspot_id en MongoDB."""
    try:
        await motor_db.contacts.update_one(
            {"phone": phone},
            {
                "$set": {
                    "phone":        phone,
                    "hubspot_id":   hubspot_id,
                    "name":         name,
                    "last_channel": channel,
                    "updated_at":   datetime.utcnow(),
                },
                "$setOnInsert": {"created_at": datetime.utcnow()},
            },
            upsert=True,
        )
    except Exception as exc:
        print(f"    ⚠ MongoDB error para {phone}: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# REDIS
# ─────────────────────────────────────────────────────────────────────────────

async def write_redis_state(
    redis_client,
    phone: str,
    owner_id: str,
    contact_id: str,
    display_name: str,
    channel: str,
):
    """
    Registra el contacto migrado en Redis con estado BOT_ACTIVE.

    Los contactos migrados van directamente al BOT_CONTROLLED_SET
    (visibles en el panel de la asesora, pero sin notificaciones de prioridad).
    Esto evita inundar el panel con 1,602 alertas prioritarias a la vez.

    Cuando haya interacción real (asesora escribe o cliente responde),
    activate_human() / request_handoff() los moverán al ZSET automáticamente.

    Keys escritos:
      conv_state:{phone}:whatsapp       →  "BOT_ACTIVE"  (TTL 7 días)
      conv_meta:{phone}:whatsapp        →  JSON meta      (TTL 7 días)
      bot_controlled_conversations      →  SADD (sin TTL propio — persiste via meta)
    """
    now_iso   = datetime.now(timezone.utc).astimezone().isoformat()
    member    = f"{phone}:{REDIS_CANAL}"
    state_key = f"{REDIS_STATE_PREFIX}{phone}:{REDIS_CANAL}"
    meta_key  = f"{REDIS_META_PREFIX}{phone}:{REDIS_CANAL}"

    meta = {
        "phone_normalized":   phone,
        "contact_id":         contact_id,
        "status":             "BOT_ACTIVE",
        "last_activity":      now_iso,
        "handoff_reason":     "Migrado desde Leadsales",
        "assigned_owner_id":  owner_id,
        "assigned_owner_ids": [owner_id],
        "primary_owner_id":   owner_id,
        "canal_origen":       channel,
        "display_name":       display_name,
        "created_at":         now_iso,
        "version_id":         str(uuid.uuid4()),
    }

    try:
        pipe = redis_client.pipeline()
        # Estado y meta con TTL de 7 días (leads históricos, no urgentes)
        pipe.set(state_key, "BOT_ACTIVE", ex=REDIS_MIGRATION_TTL)
        pipe.set(meta_key, json.dumps(meta, ensure_ascii=False), ex=REDIS_MIGRATION_TTL)
        # Va a BOT_CONTROLLED_SET (visible en panel, sin prioridad/notificaciones)
        # NO va al ZSET — activate_human() lo mueve ahí cuando haya interacción real
        pipe.sadd(REDIS_BOT_CONTROLLED, member)
        await pipe.execute()
    except Exception as exc:
        print(f"    ⚠ Redis error para {phone}: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 65)
    print("  MIGRACION LEADSALES -> HUBSPOT + MONGODB")
    print(f"  Modo    : {'[DRY RUN - ninguna API sera tocada]' if DRY_RUN else '[REAL]'}")
    print(f"  Reanudar: {'Si (desde checkpoint)' if RESUME else 'No (inicio limpio)'}")
    print("=" * 65)

    # ── Validaciones previas
    if not DRY_RUN and not HUBSPOT_API_KEY:
        print("\n✗ HUBSPOT_API_KEY no está configurada en .env — abortando.")
        return

    # ── Leer Excel
    print("\n▶ Leyendo Excel …")
    df = pd.read_excel(EXCEL_PATH)
    print(f"   Total filas         : {len(df)}")

    # ── Filtrar migrables (solo teléfonos reales)
    df["_phone"] = df["Lead"].apply(normalize_phone)
    migrables  = df[df["_phone"].notna()].copy()
    descartados = df[df["_phone"].isna()].copy()
    print(f"   Migrables (teléfono): {len(migrables)}")
    print(f"   Descartados         : {len(descartados)}")

    # ── Checkpoint
    done_phones: set = load_checkpoint() if RESUME else set()
    pending = migrables[~migrables["_phone"].isin(done_phones)]
    print(f"   Ya procesados       : {len(done_phones)}")
    print(f"   Pendientes          : {len(pending)}")

    # ── Conectar MongoDB (opcional, no bloquea si falla)
    motor_db = None
    if MONGO_URL and not DRY_RUN:
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
            motor_client = AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=5000)
            await motor_client.admin.command("ping")
            motor_db = motor_client.get_database("inmobiliaria_chat")
            print("\n   ✓ MongoDB conectado")
        except Exception as exc:
            print(f"\n   ⚠ MongoDB no disponible (continúa sin él): {exc}")

    # ── Conectar Redis (opcional, no bloquea si falla)
    redis_client = None
    if not DRY_RUN:
        try:
            import redis.asyncio as aioredis
            redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
            await redis_client.ping()
            print("   ✓ Redis conectado")
        except Exception as exc:
            print(f"   ⚠ Redis no disponible (continúa sin él): {exc}")

    # ── Resultados acumulados
    success   = []
    existed   = []
    skipped   = []
    errors    = []

    total    = len(pending)
    start_ts = time.time()

    print(f"\n{'─'*65}")
    print(f"  Iniciando migración de {total} contactos …")
    print(f"{'─'*65}\n")

    async with httpx.AsyncClient(timeout=30.0) as client:
        for idx, (_, row) in enumerate(pending.iterrows(), start=1):

            phone    = row["_phone"]
            funnel   = str(row.get("Funnel Name", "")).strip().upper()
            owner_id = FUNNEL_TO_OWNER.get(funnel)

            # ── Calcular ETA
            elapsed  = time.time() - start_ts
            eta_secs = (elapsed / idx) * (total - idx) if idx > 1 else 0
            eta_str  = f"{eta_secs/60:.1f}m" if eta_secs > 60 else f"{eta_secs:.0f}s"

            firstname, lastname = parse_name(row.get("Name"))
            budget_raw = row.get("Value")
            budget     = float(budget_raw) if pd.notna(budget_raw) and budget_raw != 0 else None
            channel_norm = "whatsapp_directo"  # Todos los contactos de Leadsales son WhatsApp
            url_chat    = build_url_chat(owner_id, phone) if owner_id else ""

            print(
                f"[{idx:>4}/{total}]  {phone}  |  {firstname} {lastname}"
                f"  |  {funnel}  |  ETA {eta_str}"
            )

            # ── Saltar si asesora no mapeada
            if not owner_id:
                print(f"    ⚠ Funnel '{funnel}' sin mapeo — saltado")
                skipped.append({"phone": phone, "funnel": funnel, "razon": "asesora no mapeada"})
                continue

            # ── DRY RUN: solo simular
            if DRY_RUN:
                print(f"    [DRY] Contacto → owner {owner_id}"
                      + (f" | presupuesto ${int(budget):,}" if budget else ""))
                success.append({"phone": phone, "asesora": funnel,
                                 "contact_id": "DRY", "deal_id": "DRY"})
                continue

            # ── 1. Crear contacto en HubSpot
            contact_id, ya_existia = await create_hubspot_contact(
                client, phone, firstname, lastname, owner_id, channel_norm
            )
            if not contact_id:
                print(f"    ✗ Fallo al crear/recuperar contacto — saltando")
                errors.append({"phone": phone, "paso": "create_contact"})
                continue

            status_icon = "⏭ (ya existía)" if ya_existia else "✓ nuevo"
            print(f"    Contacto {status_icon}: {contact_id}")
            if ya_existia:
                existed.append({"phone": phone, "contact_id": contact_id})

            # ── 2. Crear deal (con asociación incluida)
            dealname = f"Lead WA {phone}"
            deal_id  = await create_hubspot_deal(
                client, contact_id, dealname, owner_id, budget
            )
            if deal_id:
                budget_str = f" (${int(budget):,})" if budget else ""
                print(f"    ✓ Deal: {deal_id}{budget_str}")
            else:
                print(f"    ⚠ Deal no creado — el contacto sí quedó registrado")

            # ── 3. Escribir url_chat en contacto y deal
            await write_url_chat(client, contact_id, deal_id, url_chat)
            if url_chat:
                print(f"    ✓ url_chat registrado")

            # ── 4. Actualizar cache MongoDB
            if motor_db:
                full_name = f"{firstname} {lastname}".strip()
                await upsert_mongo_contact(
                    motor_db, phone, contact_id, full_name, channel_norm
                )

            # ── 5. Escribir estado BOT_ACTIVE en Redis → visible en el panel
            if redis_client:
                full_name = f"{firstname} {lastname}".strip()
                await write_redis_state(
                    redis_client, phone, owner_id, contact_id, full_name, channel_norm
                )
                print(f"    ✓ Redis: visible en panel de {funnel}")

            # ── Registrar resultado
            success.append({
                "phone":      phone,
                "nombre":     f"{firstname} {lastname}".strip(),
                "asesora":    funnel,
                "contact_id": contact_id,
                "deal_id":    deal_id or "",
                "presupuesto": budget or 0,
                "ya_existia": ya_existia,
                "url_chat":   url_chat,
            })
            done_phones.add(phone)

            # ── Checkpoint cada 50 contactos
            if idx % 50 == 0:
                save_checkpoint(done_phones)
                print(f"\n    💾 Checkpoint guardado ({len(done_phones)} procesados)\n")

            await asyncio.sleep(SLEEP_BETWEEN_CONTACTS)

    # ── Checkpoint final
    save_checkpoint(done_phones)

    # ── Resumen
    elapsed_total = time.time() - start_ts
    print(f"\n{'=' * 65}")
    print("  RESULTADO FINAL")
    print(f"{'=' * 65}")
    redis_ok = "Si" if redis_client else "No (no disponible)"
    mongo_ok = "Si" if motor_db else "No (no disponible)"
    print(f"  ✅ Migrados exitosamente : {len(success)}")
    print(f"     └ Ya existían en HS   : {len(existed)}")
    print(f"  ⚠  Saltados              : {len(skipped)}")
    print(f"  ✗  Errores               : {len(errors)}")
    print(f"  ⏱  Tiempo total          : {elapsed_total/60:.1f} minutos")
    print(f"  Redis (panel)            : {redis_ok}")
    print(f"  MongoDB (cache)          : {mongo_ok}")

    # ── Generar reporte Excel
    print(f"\n▶ Generando reporte en {REPORT_FILE} …")
    with pd.ExcelWriter(str(REPORT_FILE), engine="openpyxl") as writer:
        if success:
            pd.DataFrame(success).to_excel(
                writer, sheet_name="Migrados", index=False
            )
        if errors:
            pd.DataFrame(errors).to_excel(
                writer, sheet_name="Errores", index=False
            )
        if skipped:
            pd.DataFrame(skipped).to_excel(
                writer, sheet_name="Saltados", index=False
            )
        # Hoja con los descartados originales (Facebook/Instagram)
        if not descartados.empty:
            descartados.drop(columns=["_phone"], errors="ignore").to_excel(
                writer, sheet_name="No_Telefono", index=False
            )

    print(f"   ✓ Reporte guardado: {REPORT_FILE}")

    if errors:
        print(f"\n  ⚠ Hay {len(errors)} errores. Revisa la hoja 'Errores' del reporte.")
        print("     Puedes reejecutar con --resume para reintentar solo los fallidos.")

    print("\n  ✓ Migración completada.\n")


if __name__ == "__main__":
    asyncio.run(main())
