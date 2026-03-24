#!/usr/bin/env python3
"""
Script de diagnóstico: Rate Limits HubSpot + Redis
Ejecutar en Railway:  railway run python scripts/diagnose_rate_limits.py
"""

import os
import asyncio
import json
from datetime import datetime, timezone

import redis.asyncio as aioredis


# ─────────────────────────────────────────
# Conexión Redis (Railway usa REDIS_URL)
# ─────────────────────────────────────────
# railway run inyecta REDIS_URL con hostname interno (redis.railway.internal)
# que solo resuelve DENTRO de Railway. Desde local usar REDIS_PUBLIC_URL o
# pasar la URL directamente: DIAGNOSE_REDIS_URL="redis://..." railway run python ...
REDIS_URL = (
    os.getenv("DIAGNOSE_REDIS_URL")      # override explícito para diagnóstico local
    or os.getenv("REDIS_PUBLIC_URL")     # URL pública de Railway
    or os.getenv("REDIS_URL", "redis://localhost:6379")
)

print(f"  Conectando a: {REDIS_URL[:40]}...")

SEP = "─" * 60


async def main():
    r = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)

    print(f"\n{'═'*60}")
    print("  DIAGNÓSTICO: Rate Limits HubSpot + Redis")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═'*60}\n")

    # ──────────────────────────────────────────────────────────
    # 1. CONTACTOS ACTIVOS EN EL PANEL
    # ──────────────────────────────────────────────────────────
    print(f"[1] CONTACTOS ACTIVOS (active_conversations_sorted)")
    print(SEP)

    total_active = await r.zcard("active_conversations_sorted")
    print(f"  Total en ZSET:          {total_active}")

    # Obtener todos con sus scores (timestamps)
    all_entries = await r.zrange("active_conversations_sorted", 0, -1, withscores=True)

    status_counts = {"BOT_ACTIVE": 0, "HUMAN_ACTIVE": 0, "IN_CONVERSATION": 0,
                     "PENDING_HANDOFF": 0, "UNKNOWN": 0}
    phones_with_contact_id = 0
    phones_without_contact_id = 0

    for phone, score in all_entries:
        meta_raw = await r.get(f"conv_meta:{phone}")
        if meta_raw:
            try:
                meta = json.loads(meta_raw)
                status = meta.get("status", "UNKNOWN")
                status_counts[status] = status_counts.get(status, 0) + 1
                if meta.get("contact_id"):
                    phones_with_contact_id += 1
                else:
                    phones_without_contact_id += 1
            except Exception:
                status_counts["UNKNOWN"] += 1
        else:
            status_counts["UNKNOWN"] += 1

    print(f"\n  Por estado:")
    for st, cnt in status_counts.items():
        if cnt > 0:
            bar = "█" * cnt
            print(f"    {st:<22} {cnt:>4}  {bar}")

    print(f"\n  Con contact_id (HubSpot):  {phones_with_contact_id}")
    print(f"  Sin contact_id:            {phones_without_contact_id}")

    # ──────────────────────────────────────────────────────────
    # 2. CACHÉ DE LIFECYCLESTAGE
    # ──────────────────────────────────────────────────────────
    print(f"\n[2] CACHÉ HUBSPOT EN REDIS")
    print(SEP)

    stage_keys = []
    async for key in r.scan_iter("contact_stage:*"):
        stage_keys.append(key)
    print(f"  contact_stage:* (lifecyclestage cache):  {len(stage_keys)}")

    name_keys = []
    async for key in r.scan_iter("contact_name:*"):
        name_keys.append(key)
    print(f"  contact_name:*  (nombre cache):          {len(name_keys)}")

    assoc_keys = []
    async for key in r.scan_iter("hs_assoc:*"):
        assoc_keys.append(key)
    print(f"  hs_assoc:*      (asociaciones notas):    {len(assoc_keys)}")

    idempotency_keys = []
    async for key in r.scan_iter("hs_note_processed:*"):
        idempotency_keys.append(key)
    print(f"  hs_note_processed:* (idempotencia):      {len(idempotency_keys)}")

    phone_cache_keys = []
    async for key in r.scan_iter("phone_cache:*"):
        phone_cache_keys.append(key)
    print(f"  phone_cache:*   (inverso contactId→tel): {len(phone_cache_keys)}")

    # ──────────────────────────────────────────────────────────
    # 3. ESTIMACIÓN DE LLAMADAS HUBSPOT POR CARGA DEL PANEL
    # ──────────────────────────────────────────────────────────
    print(f"\n[3] ESTIMACIÓN CARGA HUBSPOT (por carga del panel)")
    print(SEP)

    human_active = status_counts.get("HUMAN_ACTIVE", 0) + status_counts.get("IN_CONVERSATION", 0)
    bot_active   = status_counts.get("BOT_ACTIVE", 0)

    # Contactos sin nombre cacheado → batch request
    sin_nombre_cache = phones_with_contact_id - len(name_keys)
    sin_nombre_cache = max(0, sin_nombre_cache)

    # Contactos sin lifecyclestage → llamada individual
    sin_stage_cache  = phones_with_contact_id - len(stage_keys)
    sin_stage_cache  = max(0, sin_stage_cache)

    print(f"  Contactos prioritarios (HUMAN/IN_CONV): {human_active}")
    print(f"  Contactos bot:                          {bot_active}")
    print(f"  Sin nombre cacheado → batch HubSpot:   {sin_nombre_cache}")
    print(f"  Sin stage cacheado  → GET individual:  {sin_stage_cache}")

    # Estimación por carga de historial (get_notes_for_contact)
    # Cada apertura de chat = 1 GET asociaciones + 1 batch notas
    # Con TTL=10s el caché expira cada 10 segundos
    print(f"\n  ⚠️  ANÁLISIS CRÍTICO:")
    print(f"  - Cada carga del panel con {total_active} contactos puede generar:")
    if sin_nombre_cache > 0:
        print(f"    → 1 batch GET nombres       ({sin_nombre_cache} contactos en 1 llamada)")
    print(f"    → hasta {sin_stage_cache} GETs lifecyclestage  (contactos sin cache)")
    print(f"    → hasta {total_active} GETs asociaciones (si TTL 10s expiró)")
    print(f"    → hasta {total_active} POSTs batch notas")
    total_worst = 1 + sin_stage_cache + total_active + total_active
    print(f"    TOTAL worst case: ~{total_worst} llamadas/carga")
    print(f"    HubSpot permite:  50 llamadas/10s")
    if total_worst > 50:
        print(f"    ❌ EXCEDE el límite por {total_worst - 50} llamadas — STORM GARANTIZADO")
    else:
        print(f"    ✅ Dentro del límite (margen: {50 - total_worst})")

    # ──────────────────────────────────────────────────────────
    # 4. REDIS: CONEXIONES ACTIVAS
    # ──────────────────────────────────────────────────────────
    print(f"\n[4] REDIS: INFO DE CONEXIONES")
    print(SEP)

    info = await r.info("clients")
    print(f"  connected_clients:          {info.get('connected_clients', '?')}")
    print(f"  blocked_clients:            {info.get('blocked_clients', '?')}")
    print(f"  tracking_clients:           {info.get('tracking_clients', '?')}")

    info_mem = await r.info("memory")
    used_mb = info_mem.get("used_memory_human", "?")
    print(f"  Memoria usada:              {used_mb}")

    info_stats = await r.info("stats")
    rejected = info_stats.get("rejected_connections", 0)
    print(f"  Conexiones rechazadas ever: {rejected}")
    if rejected > 0:
        print(f"  ⚠️  Redis rechazó conexiones → pool exhausto en algún momento")

    # ──────────────────────────────────────────────────────────
    # 5. ÚLTIMAS ACTIVIDADES (contactos más recientes)
    # ──────────────────────────────────────────────────────────
    print(f"\n[5] CONTACTOS MÁS ACTIVOS (últimos 10 por timestamp)")
    print(SEP)

    recent = await r.zrange("active_conversations_sorted", -10, -1, withscores=True)
    recent.reverse()
    for phone, score in recent:
        ts = datetime.fromtimestamp(score, tz=timezone.utc).strftime("%m/%d %H:%M")
        meta_raw = await r.get(f"conv_meta:{phone}")
        status = "?"
        cid = "?"
        if meta_raw:
            try:
                meta = json.loads(meta_raw)
                status = meta.get("status", "?")[:12]
                cid = meta.get("contact_id", "sin_id")
            except Exception:
                pass
        print(f"  {ts}  {phone[-10:]}  {status:<14}  id={cid}")

    # ──────────────────────────────────────────────────────────
    # 6. RESUMEN FINAL
    # ──────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print("  RESUMEN EJECUTIVO")
    print(f"{'═'*60}")

    issues = []
    if total_active > 30:
        issues.append(f"🔴 {total_active} contactos activos — carga extrema al abrir panel")
    elif total_active > 15:
        issues.append(f"🟡 {total_active} contactos activos — carga moderada-alta")
    else:
        issues.append(f"🟢 {total_active} contactos activos — carga manejable")

    if len(assoc_keys) == 0 and total_active > 0:
        issues.append("🔴 Cache hs_assoc vacío — cada apertura de chat = 2 llamadas HubSpot")
    elif len(assoc_keys) < total_active // 2:
        issues.append(f"🟡 Cache hs_assoc bajo ({len(assoc_keys)}/{total_active}) — TTL 10s demasiado corto")

    if sin_stage_cache > 5:
        issues.append(f"🟡 {sin_stage_cache} contactos sin lifecyclestage cacheado")

    if info.get("connected_clients", 0) > 20:
        issues.append(f"🔴 {info.get('connected_clients')} conexiones Redis activas — riesgo de exhaustion")

    for issue in issues:
        print(f"  {issue}")

    print(f"\n  FIXES PRIORITARIOS:")
    print(f"  1. ASSOCIATIONS_CACHE_TTL: 10s → 300s (timeline_logger.py:51)")
    print(f"  2. Global HubSpot semaphore: coordinación Panel + TimelineLogger")
    print(f"  3. conversation_state.py: agregar max_connections=20 al Redis pool")
    print(f"  4. timeline_logger.py: reusar httpx.AsyncClient global (no crear por llamada)")
    print()

    await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())
