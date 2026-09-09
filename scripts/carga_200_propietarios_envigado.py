"""
FIX: lifecyclestage usa two-step update (limpiar → setear)
     exactamente como outbound_panel.py, porque HubSpot ignora
     custom stages en el create si están "atrás" en el orden.

Uso:
    python -m scripts.carga_200_propietarios_envigado --preview
    python -m scripts.carga_200_propietarios_envigado --execute

    # Solo corregir lifecyclestage de contactos ya creados (sin recrear)
    python -m scripts.carga_200_propietarios_envigado --fix-stages
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

import httpx
import pandas as pd
import redis.asyncio as aioredis

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ═══════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════════════════

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
HUBSPOT_BASE_URL = "https://api.hubapi.com"
REDIS_URL = os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL") or ""

JUBENY_OWNER_ID = "89096378"
JUBENY_NAME = "Jubeny"
LUISA_OWNER_ID = "89096380"
LUISA_NAME = "Luisa"
PROPIETARIOS_STAGE = "1326623069"

STATE_PREFIX = "conv_state:"
META_PREFIX = "conv_meta:"
ACTIVE_CONTACTS_ZSET = "active_conversations_sorted"
BOT_CONTROLLED_SET = "bot_controlled_conversations"
HUMAN_PANEL_STATE_TTL = 7 * 86400
PANEL_TTL_SECONDS = 365 * 86400
CANAL = "whatsapp"

REQUESTS_PER_SECOND = 4
BATCH_SIZE = 10
BATCH_PAUSE = 3
RETRY_MAX = 3
RETRY_BACKOFF = 2.0

EXCEL_PATH = "BASE_DATOS_PROPIETARIOS_CELULARES.xlsx"
LOG_PATH = "envigado_200_carga.log"
BOGOTA_TZ = timezone(timedelta(hours=-5))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class PropietarioRecord:
    firstname: str
    lastname: str
    nombre_completo: str
    celular: str
    celular_e164: str
    email: Optional[str]
    tipo_inmueble: str
    direccion: str
    destinacion: str
    municipio: str
    barrio: str
    area: str
    owner_id: str
    owner_name: str
    row_index: int


# ═══════════════════════════════════════════════════════════════════════════
# LIMPIEZA
# ═══════════════════════════════════════════════════════════════════════════

def clean_name(raw: str) -> Tuple[str, str, str]:
    name = re.sub(r'\d+', '', raw.strip()).strip()
    if not name or len(name) < 3:
        return ("Propietario", "Propietario", "")
    parts = name.split()
    if len(parts) >= 3:
        reordered = parts[2:] + parts[:2]
    else:
        reordered = parts
    full = " ".join(p.capitalize() for p in reordered)
    first = reordered[0].capitalize() if reordered else "Propietario"
    last = " ".join(p.capitalize() for p in reordered[1:]) if len(reordered) > 1 else ""
    return (full, first, last)


def normalize_phone(celular: str) -> str:
    digits = re.sub(r'\D', '', str(celular))
    if len(digits) == 10 and digits.startswith('3'):
        return f"+57{digits}"
    if len(digits) == 12 and digits.startswith('57'):
        return f"+{digits}"
    return f"+57{digits}"


def clean_area(raw: str) -> str:
    if not raw or raw in ('.', '0', ''):
        return "No especificada"
    match = re.search(r'(\d+)', str(raw))
    return f"{match.group(1)} m²" if match else "No especificada"


def clean_email(raw: str) -> Optional[str]:
    if not raw:
        return None
    email = raw.strip()
    if '@' in email and '.' in email and email not in ('0', '.', ''):
        return email.lower()
    return None


def build_note_html(r: PropietarioRecord) -> str:
    return (
        f"<h3>🏠 Inmueble del Propietario</h3>"
        f"<table>"
        f"<tr><td><strong>Tipo de Inmueble:</strong></td><td>{r.tipo_inmueble}</td></tr>"
        f"<tr><td><strong>Dirección:</strong></td><td>{r.direccion}</td></tr>"
        f"<tr><td><strong>Destinación:</strong></td><td>{r.destinacion}</td></tr>"
        f"<tr><td><strong>Municipio:</strong></td><td>{r.municipio}</td></tr>"
        f"<tr><td><strong>Barrio:</strong></td><td>{r.barrio}</td></tr>"
        f"<tr><td><strong>Área:</strong></td><td>{r.area}</td></tr>"
        f"</table>"
        f"<br><small>Fuente: Base de datos propietarios — "
        f"Cargado {datetime.now().strftime('%d/%m/%Y %H:%M')}</small>"
    )


# ═══════════════════════════════════════════════════════════════════════════
# LECTURA DEL EXCEL
# ═══════════════════════════════════════════════════════════════════════════

BAD_KEYWORDS = [
    'fallecio', 'fallecido', 's.a', ' sas', 'ltda', 'inmobiliar',
    'inversiones', 'promotora', 'edificio', 'p.h.', 'pasaje comercial'
]


def _is_contactable(name: str) -> bool:
    n = str(name).lower()
    return not any(k in n for k in BAD_KEYWORDS)


def load_200_envigado(excel_path: str) -> List[PropietarioRecord]:
    logger.info(f"Leyendo: {excel_path}")
    df = pd.read_excel(excel_path, engine='openpyxl')
    for col in df.columns:
        if df[col].dtype == 'string' or df[col].dtype == object:
            df[col] = df[col].fillna('').astype(str).str.strip()

    envigado = df[df['Municipio'].str.upper() == 'ENVIGADO'].copy()
    envigado = envigado[envigado['Propietario'].apply(_is_contactable)]
    logger.info(f"Envigado limpios: {len(envigado)}")
    envigado = envigado.head(200)
    logger.info(f"Seleccionados: {len(envigado)}")

    records = []
    for idx, row in envigado.iterrows():
        if len(records) % 2 == 0:
            owner_id, owner_name = JUBENY_OWNER_ID, JUBENY_NAME
        else:
            owner_id, owner_name = LUISA_OWNER_ID, LUISA_NAME
        full, first, last = clean_name(str(row.get('Propietario', '')))
        records.append(PropietarioRecord(
            firstname=first, lastname=last, nombre_completo=full,
            celular=str(row.get('Celular', '')).strip(),
            celular_e164=normalize_phone(str(row.get('Celular', ''))),
            email=clean_email(str(row.get('Email', ''))),
            tipo_inmueble=str(row.get('Tipo Inmueble', '')).strip().capitalize(),
            direccion=str(row.get('Direccion', '')).strip(),
            destinacion=str(row.get('Destinación', '')).strip().capitalize(),
            municipio=str(row.get('Municipio', '')).strip().title(),
            barrio=str(row.get('Barrio', '')).strip().title(),
            area=clean_area(str(row.get('Area', ''))),
            owner_id=owner_id, owner_name=owner_name, row_index=int(idx),
        ))
    logger.info(f"Jubeny: {sum(1 for r in records if r.owner_id == JUBENY_OWNER_ID)} | "
                f"Luisa: {sum(1 for r in records if r.owner_id == LUISA_OWNER_ID)}")
    return records


# ═══════════════════════════════════════════════════════════════════════════
# HUBSPOT CLIENT — con two-step lifecycle stage
# ═══════════════════════════════════════════════════════════════════════════

class HubSpotClient:
    def __init__(self, api_key: str):
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self.client: Optional[httpx.AsyncClient] = None
        self._last = 0.0

    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )
        return self

    async def __aexit__(self, *a):
        if self.client:
            await self.client.aclose()

    async def _throttle(self):
        elapsed = time.time() - self._last
        interval = 1.0 / REQUESTS_PER_SECOND
        if elapsed < interval:
            await asyncio.sleep(interval - elapsed)
        self._last = time.time()

    async def _req(self, method, endpoint, json_data=None) -> Tuple[Optional[dict], Optional[str]]:
        url = f"{HUBSPOT_BASE_URL}{endpoint}"
        for attempt in range(RETRY_MAX):
            await self._throttle()
            try:
                resp = await self.client.request(method, url, headers=self.headers, json=json_data)
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", "10"))
                    logger.warning(f"Rate limited {wait}s")
                    await asyncio.sleep(wait)
                    continue
                if resp.status_code in (200, 201):
                    return resp.json(), None
                if resp.status_code == 409:
                    return None, "DUPLICATE"
                return None, f"HTTP {resp.status_code}: {resp.text[:200]}"
            except Exception as e:
                if attempt < RETRY_MAX - 1:
                    await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
                else:
                    return None, str(e)
        return None, "Max retries"

    async def search_by_phone(self, phone: str) -> Optional[str]:
        for prop in ("phone", "whatsapp_id"):
            data, _ = await self._req("POST", "/crm/v3/objects/contacts/search", {
                "filterGroups": [{"filters": [
                    {"propertyName": prop, "operator": "EQ", "value": phone}
                ]}],
                "properties": ["phone", "firstname", "lifecyclestage"], "limit": 1
            })
            if data and data.get("total", 0) > 0:
                return data["results"][0]["id"]
        return None

    async def create_contact(self, r: PropietarioRecord) -> Tuple[Optional[str], Optional[str]]:
        """Crea contacto SIN lifecyclestage (se aplica después con two-step)."""
        props = {
            "firstname": r.firstname, "lastname": r.lastname,
            "phone": r.celular_e164, "whatsapp_id": r.celular_e164,
            "hubspot_owner_id": r.owner_id,
            # NO incluir lifecyclestage aquí — se aplica con two-step después
        }
        if r.email:
            props["email"] = r.email
        data, err = await self._req("POST", "/crm/v3/objects/contacts", {"properties": props})
        return (data.get("id"), None) if data else (None, err)

    async def set_lifecycle_stage_twostep(self, contact_id: str, stage: str) -> Tuple[bool, Optional[str]]:
        """
        Two-step lifecycle stage update (igual que outbound_panel.py):
        1. PATCH con lifecyclestage = "" (limpiar restricción unidireccional)
        2. PATCH con lifecyclestage = stage_id deseado
        """
        endpoint = f"/crm/v3/objects/contacts/{contact_id}"

        # Step 1: Limpiar
        _, err1 = await self._req("PATCH", endpoint, {
            "properties": {"lifecyclestage": ""}
        })
        if err1:
            return False, f"Step1 clear failed: {err1}"

        # Step 2: Setear
        _, err2 = await self._req("PATCH", endpoint, {
            "properties": {"lifecyclestage": stage}
        })
        if err2:
            return False, f"Step2 set failed: {err2}"

        return True, None

    async def create_note(self, contact_id: str, body: str, owner_id: str) -> Tuple[Optional[str], Optional[str]]:
        data, err = await self._req("POST", "/crm/v3/objects/notes", {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": datetime.now(timezone.utc).isoformat(),
                "hubspot_owner_id": owner_id,
            },
            "associations": [{
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}]
            }]
        })
        return (data.get("id"), None) if data else (None, err)


# ═══════════════════════════════════════════════════════════════════════════
# REDIS — REGISTRO EN PANEL (replica activate_human)
# ═══════════════════════════════════════════════════════════════════════════

async def register_in_redis(redis_client, phone_e164, contact_id, owner_id, display_name):
    now = datetime.now(BOGOTA_TZ)
    now_iso = now.isoformat()
    meta = {
        "phone_normalized": phone_e164, "contact_id": contact_id,
        "status": "HUMAN_ACTIVE", "last_activity": now_iso,
        "handoff_reason": "Carga masiva propietarios Envigado",
        "assigned_owner_id": owner_id, "canal_origen": CANAL,
        "display_name": display_name, "created_at": now_iso,
    }
    member = f"{phone_e164}:{CANAL}"
    pipe = redis_client.pipeline(transaction=False)
    pipe.set(f"{STATE_PREFIX}{phone_e164}:{CANAL}", "HUMAN_ACTIVE", ex=HUMAN_PANEL_STATE_TTL)
    pipe.set(f"{META_PREFIX}{phone_e164}:{CANAL}", json.dumps(meta, ensure_ascii=False), ex=PANEL_TTL_SECONDS)
    pipe.set(f"conv_was_panel:{phone_e164}:{CANAL}", "1", ex=PANEL_TTL_SECONDS)
    pipe.zadd(ACTIVE_CONTACTS_ZSET, {member: now.timestamp()})
    pipe.srem(BOT_CONTROLLED_SET, member)
    await pipe.execute()


# ═══════════════════════════════════════════════════════════════════════════
# PREVIEW
# ═══════════════════════════════════════════════════════════════════════════

def print_preview(records):
    print("\n" + "=" * 100)
    print("  📋 LISTA DE 200 PROPIETARIOS — ENVIGADO (PILOTO)")
    print("=" * 100)
    print(f"  {'#':>3}  {'Asesora':<8} {'Nombre':<30} {'Celular':<14} {'Tipo':<14} {'Barrio':<20} {'Área':<10}")
    print(f"  {'─'*3}  {'─'*8} {'─'*30} {'─'*14} {'─'*14} {'─'*20} {'─'*10}")
    for i, r in enumerate(records, 1):
        print(f"  {i:>3}  {r.owner_name:<8} {r.nombre_completo[:30]:<30} {r.celular:<14} {r.tipo_inmueble[:14]:<14} {r.barrio[:20]:<20} {r.area:<10}")
    print(f"\n  {'─'*96}")
    print(f"  TOTAL: {len(records)} | Jubeny: {sum(1 for r in records if r.owner_id == JUBENY_OWNER_ID)} | Luisa: {sum(1 for r in records if r.owner_id == LUISA_OWNER_ID)}")
    print("=" * 100 + "\n")


# ═══════════════════════════════════════════════════════════════════════════
# EJECUCIÓN COMPLETA (create + two-step stage + note + redis)
# ═══════════════════════════════════════════════════════════════════════════

async def execute_load(records):
    if not HUBSPOT_API_KEY:
        logger.error("HUBSPOT_API_KEY no configurada"); sys.exit(1)
    if not REDIS_URL:
        logger.error("REDIS_PUBLIC_URL o REDIS_URL no configurada"); sys.exit(1)

    hs_created = hs_duplicates = hs_errors = 0
    stages_ok = stages_err = notes_ok = redis_ok = redis_err = 0
    start_time = time.time()

    redis_client = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    try:
        await redis_client.ping()
        logger.info("✓ Redis conectado")
    except Exception as e:
        logger.error(f"✗ Redis: {e}"); sys.exit(1)

    logger.info(f"\n{'='*60}\nCARGA: 200 Propietarios Envigado → HubSpot + Redis\n{'='*60}")

    async with HubSpotClient(HUBSPOT_API_KEY) as hs:
        for i, record in enumerate(records):
            label = f"[{i+1}/200]"
            try:
                # ── PASO 1: Deduplicación ──
                existing_id = await hs.search_by_phone(record.celular_e164)
                if existing_id:
                    hs_duplicates += 1
                    contact_id = existing_id
                    logger.info(f"{label} DUPLICADO: {record.firstname} ({existing_id})")
                    # Aún así aplicar two-step stage y nota
                else:
                    # ── PASO 2: Crear contacto (SIN lifecyclestage) ──
                    contact_id, err = await hs.create_contact(record)
                    if err == "DUPLICATE":
                        hs_duplicates += 1
                        logger.info(f"{label} DUP(409): {record.firstname}")
                        continue
                    if err:
                        hs_errors += 1
                        logger.error(f"{label} ERROR: {err}")
                        continue
                    hs_created += 1
                    logger.info(f"{label} CREADO: {record.firstname} {record.lastname} → {contact_id} [{record.owner_name}]")

                # ── PASO 3: Two-step lifecycle stage ──
                stage_ok, stage_err = await hs.set_lifecycle_stage_twostep(
                    contact_id, PROPIETARIOS_STAGE
                )
                if stage_ok:
                    stages_ok += 1
                    logger.info(f"{label} STAGE OK: → Propietarios ({PROPIETARIOS_STAGE})")
                else:
                    stages_err += 1
                    logger.error(f"{label} STAGE ERROR: {stage_err}")

                # ── PASO 4: Crear nota ──
                nid, _ = await hs.create_note(contact_id, build_note_html(record), record.owner_id)
                if nid:
                    notes_ok += 1

                # ── PASO 5: Redis ──
                try:
                    await register_in_redis(redis_client, record.celular_e164, contact_id, record.owner_id, record.nombre_completo)
                    redis_ok += 1
                    logger.info(f"{label} REDIS OK: {record.celular_e164}:{CANAL} → HUMAN_ACTIVE")
                except Exception as re_err:
                    redis_err += 1
                    logger.error(f"{label} REDIS ERR: {re_err}")

                # ── Pausa entre lotes ──
                if (i + 1) % BATCH_SIZE == 0:
                    logger.info(
                        f"  ── Lote {(i+1)//BATCH_SIZE}/20 | "
                        f"HS:{hs_created} Dup:{hs_duplicates} Err:{hs_errors} | "
                        f"Stage:{stages_ok}/{stages_ok+stages_err} | "
                        f"Redis:{redis_ok} | {time.time()-start_time:.0f}s ──"
                    )
                    await asyncio.sleep(BATCH_PAUSE)

            except KeyboardInterrupt:
                logger.info("\nInterrumpido"); break
            except Exception as exc:
                hs_errors += 1
                logger.error(f"{label} EXCEPCIÓN: {exc}")

    await redis_client.aclose()
    elapsed = time.time() - start_time
    logger.info(f"\n{'='*60}\nRESUMEN\n{'='*60}")
    logger.info(f"HS creados:     {hs_created}")
    logger.info(f"HS duplicados:  {hs_duplicates}")
    logger.info(f"HS errores:     {hs_errors}")
    logger.info(f"Stage OK:       {stages_ok}")
    logger.info(f"Stage errores:  {stages_err}")
    logger.info(f"Notas:          {notes_ok}")
    logger.info(f"Redis OK:       {redis_ok}")
    logger.info(f"Redis errores:  {redis_err}")
    logger.info(f"Tiempo:         {elapsed:.0f}s")
    logger.info(f"{'='*60}")


# ═══════════════════════════════════════════════════════════════════════════
# FIX-STAGES — Corrige SOLO el lifecyclestage de contactos ya creados
# ═══════════════════════════════════════════════════════════════════════════

async def fix_stages_only(records):
    """
    Para los 193 contactos ya creados: busca por teléfono y aplica
    two-step lifecycle stage. No crea nada nuevo ni toca Redis.
    """
    if not HUBSPOT_API_KEY:
        logger.error("HUBSPOT_API_KEY no configurada"); sys.exit(1)

    fixed = not_found = errors = already_ok = 0
    start_time = time.time()

    logger.info(f"\n{'='*60}\nFIX STAGES: Aplicar Propietarios a contactos existentes\n{'='*60}")

    async with HubSpotClient(HUBSPOT_API_KEY) as hs:
        for i, record in enumerate(records):
            label = f"[{i+1}/{len(records)}]"
            try:
                contact_id = await hs.search_by_phone(record.celular_e164)
                if not contact_id:
                    not_found += 1
                    logger.warning(f"{label} NO ENCONTRADO: {record.firstname} ({record.celular_e164})")
                    continue

                # Two-step update
                ok, err = await hs.set_lifecycle_stage_twostep(contact_id, PROPIETARIOS_STAGE)
                if ok:
                    fixed += 1
                    logger.info(f"{label} FIXED: {record.firstname} ({contact_id}) → Propietarios")
                else:
                    errors += 1
                    logger.error(f"{label} ERROR: {record.firstname} → {err}")

                if (i + 1) % BATCH_SIZE == 0:
                    logger.info(f"  ── Lote {(i+1)//BATCH_SIZE} | Fixed:{fixed} NotFound:{not_found} Err:{errors} | {time.time()-start_time:.0f}s ──")
                    await asyncio.sleep(BATCH_PAUSE)

            except KeyboardInterrupt:
                logger.info("\nInterrumpido"); break
            except Exception as exc:
                errors += 1
                logger.error(f"{label} EXCEPCIÓN: {exc}")

    elapsed = time.time() - start_time
    logger.info(f"\n{'='*60}\nRESUMEN FIX STAGES\n{'='*60}")
    logger.info(f"Corregidos:    {fixed}")
    logger.info(f"No encontrados:{not_found}")
    logger.info(f"Errores:       {errors}")
    logger.info(f"Tiempo:        {elapsed:.0f}s")
    logger.info(f"{'='*60}")


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Carga 200 propietarios Envigado")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--fix-stages", action="store_true",
                        help="Solo corregir lifecyclestage de contactos ya creados")
    parser.add_argument("--excel", default=EXCEL_PATH)
    args = parser.parse_args()

    if not Path(args.excel).exists():
        print(f"No se encontró: {args.excel}"); sys.exit(1)

    records = load_200_envigado(args.excel)

    if args.fix_stages:
        print(f"\n⚠  Corregir lifecyclestage de {len(records)} contactos existentes en HubSpot")
        print(f"   Método: Two-step (limpiar → setear Propietarios)")
        confirm = input("   ¿Continuar? (SI): ")
        if confirm.strip().upper() != "SI":
            print("Cancelado."); sys.exit(0)
        asyncio.run(fix_stages_only(records))
        sys.exit(0)

    if not args.preview and not args.execute:
        print_preview(records)
        print("Opciones:")
        print("  --preview      Ver lista sin ejecutar")
        print("  --execute      Crear contactos + stage + nota + Redis")
        print("  --fix-stages   Solo corregir lifecyclestage de contactos ya creados")
        sys.exit(0)

    if args.preview:
        print_preview(records); sys.exit(0)

    if args.execute:
        print_preview(records)
        print(f"\n⚠  Cargar {len(records)} contactos en HubSpot + Redis")
        print(f"   Jubeny: {sum(1 for r in records if r.owner_id == JUBENY_OWNER_ID)} | Luisa: {sum(1 for r in records if r.owner_id == LUISA_OWNER_ID)}")
        print(f"   Embudo: Propietarios ({PROPIETARIOS_STAGE})")
        print(f"   Método: Create + Two-step stage + Note + Redis")
        confirm = input("\n   ¿Continuar? (SI): ")
        if confirm.strip().upper() != "SI":
            print("Cancelado."); sys.exit(0)
        asyncio.run(execute_load(records))


if __name__ == "__main__":
    main()