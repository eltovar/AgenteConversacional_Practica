#!/usr/bin/env python3
"""
patch_redis_deal_ids.py
=======================
Lee migrate_report.xlsx y escribe deal_id + deal_stage en cada conv_meta de Redis.

Con esto el panel lee el deal_id desde Redis meta en lugar de llamar HubSpot,
eliminando los 429 en la carga del panel.

USO:
  python patch_redis_deal_ids.py              # Real
  python patch_redis_deal_ids.py --dry-run   # Simular sin tocar Redis
"""

import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import redis
import pandas as pd
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

REDIS_URL         = os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
REPORT_PATH       = Path(r"C:\Users\Salo\Downloads\migrate_report.xlsx")
REDIS_META_PREFIX = "conv_meta:"
REDIS_CANAL       = "whatsapp"
HUBSPOT_STAGE_NUEVO = "1275156339"   # etapa "Nuevo Lead"

DRY_RUN = "--dry-run" in sys.argv


def main():
    print("=" * 60)
    print("  PATCH REDIS META: agregar deal_id desde migrate_report")
    print(f"  Modo: {'[DRY RUN - no escribe nada]' if DRY_RUN else '[REAL]'}")
    print("=" * 60)

    # ── Leer reporte
    df = pd.read_excel(REPORT_PATH, sheet_name="Migrados")
    df = df[df["deal_id"].notna() & (df["deal_id"] != "")].copy()
    # deal_id puede venir como float (57491013425.0) → convertir a str sin decimales
    df["deal_id"] = df["deal_id"].apply(lambda x: str(int(float(x))))
    print(f"\n  Contactos en el reporte: {len(df)}")

    # ── Conectar Redis
    r = redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=10,
        socket_timeout=10,
    )
    print(f"  Conectando a Redis...")
    r.ping()
    print("  Redis conectado\n")

    ok = 0
    ya_tenia = 0
    sin_meta = 0
    errores = 0

    for idx, row in enumerate(df.itertuples(), start=1):
        phone    = str(row.phone).strip()
        if not phone.startswith("+"):
            phone = f"+{phone}"
        deal_id  = str(row.deal_id)
        meta_key = f"{REDIS_META_PREFIX}{phone}:{REDIS_CANAL}"

        # Leer meta actual
        raw = r.get(meta_key)
        if not raw:
            sin_meta += 1
            if idx <= 5:
                print(f"  [{idx}] SIN META: {phone}")
            continue

        try:
            meta = json.loads(raw)
        except json.JSONDecodeError:
            errores += 1
            continue

        # Si ya tiene el mismo deal_id, saltar
        if meta.get("deal_id") == deal_id:
            ya_tenia += 1
            continue

        # Parchar
        meta["deal_id"]      = deal_id
        meta["deal_stage"]   = HUBSPOT_STAGE_NUEVO
        meta["deal_patched"] = datetime.now(timezone.utc).isoformat()

        if not DRY_RUN:
            ttl = r.ttl(meta_key)
            ex  = ttl if ttl and ttl > 0 else None
            r.set(meta_key, json.dumps(meta, ensure_ascii=False), ex=ex)

        ok += 1

        # Progreso cada 200
        if idx % 200 == 0:
            print(f"  [{idx}/{len(df)}] parcheados: {ok}, sin meta: {sin_meta}")

    print(f"\n{'='*60}")
    print(f"  Parcheados       : {ok}")
    print(f"  Ya tenian deal_id: {ya_tenia}")
    print(f"  Sin meta Redis   : {sin_meta}  (TTL expirado)")
    print(f"  Errores JSON     : {errores}")
    if DRY_RUN:
        print("\n  [DRY RUN] No se escribio nada en Redis.")
    else:
        print("\n  Listo. El panel leeara deal_id desde Redis (sin llamar HubSpot).")


if __name__ == "__main__":
    main()
