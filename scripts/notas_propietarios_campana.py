"""
Post-campaña propietarios Envigado:
  1. Elimina las notas HTML crudas ("Inmueble del Propietario") de cada contacto
  2. Crea una nueva nota registrando el envío de la campaña

Uso:
    python -m scripts.notas_propietarios_campana --preview
    python -m scripts.notas_propietarios_campana --execute
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

EXCEL_PATH = "BASE_DATOS_PROPIETARIOS_CELULARES.xlsx"
LOG_PATH = "notas_propietarios_campana.log"

REQUESTS_PER_SECOND = 4
BATCH_SIZE = 10
BATCH_PAUSE = 3
RETRY_MAX = 3
RETRY_BACKOFF = 2.0

NOTA_CAMPANA = (
    "Campaña Propietarios — Seguimiento Personalizado\n\n"
    "Se envió mensaje masivo vía WhatsApp el {fecha}.\n"
    "Contenido: Oferta de servicios de administración y arrendamiento de inmuebles.\n\n"
    "— Campaña Propietarios Envigado Jul 2026"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# LECTURA EXCEL (reutilizada)
# ═══════════════════════════════════════════════════════════════════════════

BAD_KEYWORDS = [
    'fallecio', 'fallecido', 's.a', ' sas', 'ltda', 'inmobiliar',
    'inversiones', 'promotora', 'edificio', 'p.h.', 'pasaje comercial',
]


def _is_contactable(name: str) -> bool:
    return not any(k in str(name).lower() for k in BAD_KEYWORDS)


def clean_name(raw: str) -> Tuple[str, str]:
    name = re.sub(r'\d+', '', raw.strip()).strip()
    if not name or len(name) < 3:
        return ("Propietario", "Propietario")
    parts = name.split()
    if len(parts) >= 3:
        reordered = parts[2:] + parts[:2]
    else:
        reordered = parts
    full = " ".join(p.capitalize() for p in reordered)
    first = reordered[0].capitalize() if reordered else "Propietario"
    return (full, first)


def normalize_phone(celular: str) -> str:
    digits = re.sub(r'\D', '', str(celular))
    if len(digits) == 10 and digits.startswith('3'):
        return f"+57{digits}"
    if len(digits) == 12 and digits.startswith('57'):
        return f"+{digits}"
    return f"+57{digits}"


JUBENY_OWNER_ID = "89096378"
LUISA_OWNER_ID = "89096380"


@dataclass
class PropietarioRecord:
    firstname: str
    nombre_completo: str
    celular_e164: str
    owner_id: str
    row_index: int


def load_propietarios(excel_path: str) -> List[PropietarioRecord]:
    logger.info(f"Leyendo: {excel_path}")
    df = pd.read_excel(excel_path, engine='openpyxl')
    for col in df.columns:
        if df[col].dtype == 'string' or df[col].dtype == object:
            df[col] = df[col].fillna('').astype(str).str.strip()

    envigado = df[df['Municipio'].str.upper() == 'ENVIGADO'].copy()
    envigado = envigado[envigado['Propietario'].apply(_is_contactable)]
    envigado = envigado.head(200)
    logger.info(f"Propietarios seleccionados: {len(envigado)}")

    records = []
    for idx, row in envigado.iterrows():
        if len(records) % 2 == 0:
            owner_id = JUBENY_OWNER_ID
        else:
            owner_id = LUISA_OWNER_ID
        full, first = clean_name(str(row.get('Propietario', '')))
        records.append(PropietarioRecord(
            firstname=first,
            nombre_completo=full,
            celular_e164=normalize_phone(str(row.get('Celular', ''))),
            owner_id=owner_id,
            row_index=int(idx),
        ))
    return records


# ═══════════════════════════════════════════════════════════════════════════
# HUBSPOT CLIENT
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
                if resp.status_code == 204:
                    return {"deleted": True}, None
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
                "properties": ["phone", "firstname"], "limit": 1
            })
            if data and data.get("total", 0) > 0:
                return data["results"][0]["id"]
        return None

    async def get_associated_notes(self, contact_id: str) -> List[str]:
        data, err = await self._req(
            "GET", f"/crm/v3/objects/contacts/{contact_id}/associations/notes"
        )
        if not data:
            return []
        return [r["id"] for r in data.get("results", [])]

    async def get_note_body(self, note_id: str) -> Optional[str]:
        data, err = await self._req(
            "GET", f"/crm/v3/objects/notes/{note_id}?properties=hs_note_body"
        )
        if data:
            return data.get("properties", {}).get("hs_note_body", "")
        return None

    async def delete_note(self, note_id: str) -> Tuple[bool, Optional[str]]:
        data, err = await self._req("DELETE", f"/crm/v3/objects/notes/{note_id}")
        return (err is None, err)

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
# EJECUCIÓN
# ═══════════════════════════════════════════════════════════════════════════

async def execute(records: List[PropietarioRecord]):
    if not HUBSPOT_API_KEY:
        logger.error("HUBSPOT_API_KEY no configurada")
        sys.exit(1)

    not_found = 0
    notes_deleted = notes_delete_err = 0
    notes_created = notes_create_err = 0
    start_time = time.time()
    fecha_envio = "23 de julio de 2026"

    logger.info(f"\n{'='*60}\nNOTAS CAMPAÑA: {len(records)} propietarios\n{'='*60}")

    async with HubSpotClient(HUBSPOT_API_KEY) as hs:
        for i, record in enumerate(records):
            label = f"[{i+1}/{len(records)}]"
            try:
                contact_id = await hs.search_by_phone(record.celular_e164)
                if not contact_id:
                    not_found += 1
                    logger.warning(f"{label} NO ENCONTRADO: {record.firstname} ({record.celular_e164})")
                    continue

                # ── PASO 1: Eliminar notas "Inmueble del Propietario" ──
                note_ids = await hs.get_associated_notes(contact_id)
                for note_id in note_ids:
                    body = await hs.get_note_body(note_id)
                    if body and ("Inmueble del Propietario" in body or "Campaña Propietarios" in body):
                        ok, err = await hs.delete_note(note_id)
                        if ok:
                            notes_deleted += 1
                            logger.info(f"{label} NOTA ELIMINADA: {note_id} ({record.firstname})")
                        else:
                            notes_delete_err += 1
                            logger.error(f"{label} ERROR ELIMINAR NOTA {note_id}: {err}")

                # ── PASO 2: Crear nota de campaña ──
                nota_body = NOTA_CAMPANA.format(fecha=fecha_envio)
                nid, err = await hs.create_note(contact_id, nota_body, record.owner_id)
                if nid:
                    notes_created += 1
                    logger.info(f"{label} NOTA CREADA: {nid} ({record.firstname})")
                else:
                    notes_create_err += 1
                    logger.error(f"{label} ERROR CREAR NOTA: {err}")

                if (i + 1) % BATCH_SIZE == 0:
                    logger.info(
                        f"  ── Lote {(i+1)//BATCH_SIZE}/{len(records)//BATCH_SIZE} | "
                        f"Del:{notes_deleted} CreNew:{notes_created} NotFound:{not_found} | "
                        f"{time.time()-start_time:.0f}s ──"
                    )
                    await asyncio.sleep(BATCH_PAUSE)

            except KeyboardInterrupt:
                logger.info("\nInterrumpido")
                break
            except Exception as exc:
                logger.error(f"{label} EXCEPCIÓN: {exc}")

    elapsed = time.time() - start_time
    logger.info(f"\n{'='*60}\nRESUMEN\n{'='*60}")
    logger.info(f"No encontrados:       {not_found}")
    logger.info(f"Notas eliminadas:     {notes_deleted}")
    logger.info(f"Notas elim. errores:  {notes_delete_err}")
    logger.info(f"Notas campaña creadas:{notes_created}")
    logger.info(f"Notas crea errores:   {notes_create_err}")
    logger.info(f"Tiempo:               {elapsed:.0f}s")
    logger.info(f"{'='*60}")


# ═══════════════════════════════════════════════════════════════════════════
# PREVIEW
# ═══════════════════════════════════════════════════════════════════════════

def print_preview(records: List[PropietarioRecord]):
    print("\n" + "=" * 80)
    print("  📝 NOTAS CAMPAÑA — Propietarios Envigado")
    print("=" * 80)
    print(f"  Acción 1: ELIMINAR notas 'Inmueble del Propietario' (HTML crudo)")
    print(f"  Acción 2: CREAR nota de campaña (registro envío seguimiento)")
    print(f"  Total contactos: {len(records)}")
    print(f"  {'─' * 76}")
    print(f"  {'#':>3}  {'Nombre':30}  {'Teléfono':16}")
    print(f"  {'─'*3}  {'─'*30}  {'─'*16}")
    for i, r in enumerate(records, 1):
        print(f"  {i:>3}  {r.firstname:30}  {r.celular_e164:16}")
    print(f"\n  {'─' * 76}")
    print(f"  TOTAL: {len(records)} contactos")
    print("=" * 80 + "\n")


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Notas campaña propietarios Envigado")
    parser.add_argument("--preview", action="store_true", help="Ver lista sin ejecutar")
    parser.add_argument("--execute", action="store_true", help="Eliminar notas viejas + crear nuevas")
    parser.add_argument("--excel", default=EXCEL_PATH)
    args = parser.parse_args()

    if not Path(args.excel).exists():
        print(f"No se encontró: {args.excel}")
        sys.exit(1)

    records = load_propietarios(args.excel)

    if not args.preview and not args.execute:
        print_preview(records)
        print("Opciones:")
        print("  --preview   Ver lista sin ejecutar")
        print("  --execute   Eliminar notas HTML + crear notas campaña")
        sys.exit(0)

    if args.preview:
        print_preview(records)
        sys.exit(0)

    if args.execute:
        print_preview(records)
        print(f"\n⚠  Operaciones sobre {len(records)} contactos en HubSpot:")
        print(f"   1. ELIMINAR notas con 'Inmueble del Propietario'")
        print(f"   2. CREAR nota de campaña (registro envío seguimiento)")
        confirm = input("\n   ¿Continuar? (SI): ")
        if confirm.strip().upper() != "SI":
            print("Cancelado.")
            sys.exit(0)
        asyncio.run(execute(records))


if __name__ == "__main__":
    main()
