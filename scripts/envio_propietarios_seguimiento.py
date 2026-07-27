"""
Envío masivo: Template "Seguimiento Personalizado" a propietarios Envigado.

Usa la misma base de datos Excel que carga_200_propietarios_envigado.py.
NO toca Redis ni panel — el contacto solo aparece en inbox si responde.

Uso:
    python -m scripts.envio_propietarios_seguimiento --preview
    python -m scripts.envio_propietarios_seguimiento --execute
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

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")

TEMPLATE_SID = "HXd1c44acc7571a216729fcb4f778db019"

EXCEL_PATH = "BASE_DATOS_PROPIETARIOS_CELULARES.xlsx"
LOG_PATH = "envio_propietarios_seguimiento.log"

REQUESTS_PER_SECOND = 3
BATCH_SIZE = 10
BATCH_PAUSE = 4
RETRY_MAX = 3
RETRY_BACKOFF = 2.0

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
# LIMPIEZA (reutilizada del script de carga)
# ═══════════════════════════════════════════════════════════════════════════

BAD_KEYWORDS = [
    'fallecio', 'fallecido', 's.a', ' sas', 'ltda', 'inmobiliar',
    'inversiones', 'promotora', 'edificio', 'p.h.', 'pasaje comercial',
]


def _is_contactable(name: str) -> bool:
    n = str(name).lower()
    return not any(k in n for k in BAD_KEYWORDS)


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


@dataclass
class PropietarioRecord:
    firstname: str
    nombre_completo: str
    celular_e164: str
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
        full, first = clean_name(str(row.get('Propietario', '')))
        records.append(PropietarioRecord(
            firstname=first,
            nombre_completo=full,
            celular_e164=normalize_phone(str(row.get('Celular', ''))),
            row_index=int(idx),
        ))
    return records


# ═══════════════════════════════════════════════════════════════════════════
# ENVÍO VÍA TWILIO API (Messages.json con ContentSid)
# ═══════════════════════════════════════════════════════════════════════════

async def send_template(
    client: httpx.AsyncClient,
    to_phone: str,
    firstname: str,
) -> Tuple[bool, str]:
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"

    from_number = TWILIO_PHONE_NUMBER
    if not from_number.startswith("whatsapp:"):
        from_number = f"whatsapp:{from_number}"

    to = to_phone
    if not to.startswith("whatsapp:"):
        to = f"whatsapp:{to}"

    content_variables = {"1": firstname}

    payload = {
        "From": from_number,
        "To": to,
        "ContentSid": TEMPLATE_SID,
        "ContentVariables": json.dumps(content_variables),
    }

    for attempt in range(RETRY_MAX):
        try:
            resp = await client.post(
                url,
                data=payload,
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            )
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "10"))
                logger.warning(f"Rate limited, esperando {wait}s")
                await asyncio.sleep(wait)
                continue
            if resp.status_code in (200, 201):
                data = resp.json()
                return True, data.get("sid", "ok")
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            if attempt < RETRY_MAX - 1:
                await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
            else:
                return False, str(e)

    return False, "Max retries"


# ═══════════════════════════════════════════════════════════════════════════
# PREVIEW
# ═══════════════════════════════════════════════════════════════════════════

def print_preview(records: List[PropietarioRecord]):
    print("\n" + "=" * 80)
    print("  📤 ENVÍO MASIVO — Seguimiento Personalizado a Propietarios")
    print("=" * 80)
    print(f"  Template: {TEMPLATE_SID}")
    print(f"  Desde:    {TWILIO_PHONE_NUMBER}")
    print(f"  Total:    {len(records)} contactos")
    print(f"  {'─' * 76}")
    print(f"  {'#':>3}  {'Nombre ({{1}})':30}  {'Teléfono':16}")
    print(f"  {'─'*3}  {'─'*30}  {'─'*16}")
    for i, r in enumerate(records, 1):
        print(f"  {i:>3}  {r.firstname:30}  {r.celular_e164:16}")
    print(f"\n  {'─' * 76}")
    print(f"  TOTAL: {len(records)} mensajes a enviar")
    print("=" * 80 + "\n")


# ═══════════════════════════════════════════════════════════════════════════
# EJECUCIÓN
# ═══════════════════════════════════════════════════════════════════════════

async def execute_send(records: List[PropietarioRecord]):
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        logger.error("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN no configurados")
        sys.exit(1)
    if not TWILIO_PHONE_NUMBER:
        logger.error("TWILIO_PHONE_NUMBER no configurado")
        sys.exit(1)

    sent = errors = 0
    start_time = time.time()

    logger.info(f"\n{'='*60}\nENVÍO: {len(records)} propietarios — Seguimiento Personalizado\n{'='*60}")

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
    ) as client:
        for i, record in enumerate(records):
            label = f"[{i+1}/{len(records)}]"
            try:
                ok, result = await send_template(client, record.celular_e164, record.firstname)
                if ok:
                    sent += 1
                    logger.info(f"{label} ENVIADO: {record.firstname} ({record.celular_e164}) → {result}")
                else:
                    errors += 1
                    logger.error(f"{label} ERROR: {record.firstname} ({record.celular_e164}) → {result}")

                await asyncio.sleep(1.0 / REQUESTS_PER_SECOND)

                if (i + 1) % BATCH_SIZE == 0:
                    logger.info(
                        f"  ── Lote {(i+1)//BATCH_SIZE}/{len(records)//BATCH_SIZE} | "
                        f"Enviados:{sent} Errores:{errors} | {time.time()-start_time:.0f}s ──"
                    )
                    await asyncio.sleep(BATCH_PAUSE)

            except KeyboardInterrupt:
                logger.info("\nInterrumpido")
                break
            except Exception as exc:
                errors += 1
                logger.error(f"{label} EXCEPCIÓN: {exc}")

    elapsed = time.time() - start_time
    logger.info(f"\n{'='*60}\nRESUMEN ENVÍO\n{'='*60}")
    logger.info(f"Enviados:  {sent}")
    logger.info(f"Errores:   {errors}")
    logger.info(f"Total:     {len(records)}")
    logger.info(f"Tiempo:    {elapsed:.0f}s")
    logger.info(f"{'='*60}")


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Envío masivo Seguimiento Personalizado a propietarios")
    parser.add_argument("--preview", action="store_true", help="Ver lista sin enviar")
    parser.add_argument("--execute", action="store_true", help="Enviar mensajes")
    parser.add_argument("--excel", default=EXCEL_PATH)
    args = parser.parse_args()

    if not Path(args.excel).exists():
        print(f"No se encontró: {args.excel}")
        sys.exit(1)

    records = load_propietarios(args.excel)

    if not args.preview and not args.execute:
        print_preview(records)
        print("Opciones:")
        print("  --preview   Ver lista sin enviar")
        print("  --execute   Enviar mensajes a todos")
        sys.exit(0)

    if args.preview:
        print_preview(records)
        sys.exit(0)

    if args.execute:
        print_preview(records)
        print(f"\n⚠  Enviar template a {len(records)} propietarios vía WhatsApp")
        print(f"   Template: Seguimiento Personalizado ({TEMPLATE_SID})")
        print(f"   Desde:    {TWILIO_PHONE_NUMBER}")
        confirm = input("\n   ¿Continuar? (SI): ")
        if confirm.strip().upper() != "SI":
            print("Cancelado.")
            sys.exit(0)
        asyncio.run(execute_send(records))


if __name__ == "__main__":
    main()
