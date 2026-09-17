"""Diagnostics service for the advisor panel.

Pure reads: Redis state, HubSpot cache estimates and WebSocket stats.
Moved verbatim from `middleware.outbound_panel` — no behavior changes.
"""

import os
from datetime import datetime

from fastapi import Header, HTTPException

from logging_config import logger
from middleware import outbound_panel as _legacy
from middleware.outbound_panel import (
    CONTACT_NAME_CACHE_PREFIX,
    CONTACT_STAGE_CACHE_PREFIX,
    _get_redis_client,
    get_bogota_now,
    ws_manager,
)
from utils.safe_logging import safe_error


async def debug_redis(
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Endpoint de diagnóstico para verificar conexión Redis y datos.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        # Redis URL unificado (Railway interna, local pública)
        is_railway = os.getenv("RAILWAY_ENVIRONMENT") is not None
        redis_url = os.getenv("REDIS_URL") if is_railway else (
            os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
        )

        r = await _get_redis_client()

        # Test connection
        pong = await r.ping()

        # Get all conversation state keys
        state_keys = []
        async for key in r.scan_iter(match="conv_state:*"):
            value = await r.get(key)
            ttl = await r.ttl(key)
            state_keys.append({
                "key": key,
                "value": value,
                "ttl": ttl
            })

        # Get all meta keys
        meta_keys = []
        async for key in r.scan_iter(match="conv_meta:*"):
            value = await r.get(key)
            ttl = await r.ttl(key)
            meta_keys.append({
                "key": key,
                "value": value[:100] + "..." if len(value or "") > 100 else value,
                "ttl": ttl
            })

        return {
            "redis_url": redis_url,
            "connection_ok": pong,
            "state_keys_count": len(state_keys),
            "state_keys": state_keys,
            "meta_keys_count": len(meta_keys),
            "meta_keys": meta_keys,
            "env_vars": {
                "REDIS_PUBLIC_URL": os.getenv("REDIS_PUBLIC_URL", "(not set)"),
                "REDIS_URL": os.getenv("REDIS_URL", "(not set)")
            }
        }

    except Exception as e:
        logger.error(f"[Panel] Error en debug Redis: {safe_error(e)}")
        return {
            "error": str(e),
            "redis_url": os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
        }


async def diagnose_system(x_api_key: str = Header(None, alias="X-API-Key")):
    """
    Endpoint de diagnóstico: estado de Redis + estimación de carga HubSpot.
    Solo para administradores. Llamar con:
      curl -H "X-API-Key: <ADMIN_API_KEY>" https://<app>/whatsapp/panel/diagnose
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    import json as _json
    from datetime import timezone as _tz

    result = {}
    try:
        redis_client = await _get_redis_client()

        # ── Contactos activos ──────────────────────────────────────
        total_zset = await redis_client.zcard("active_conversations_sorted")
        all_entries = await redis_client.zrange("active_conversations_sorted", 0, -1, withscores=True)

        status_counts: dict = {}
        with_contact_id = 0
        without_contact_id = 0
        for phone, score in all_entries:
            meta_raw = await redis_client.get(f"conv_meta:{phone}")
            if meta_raw:
                try:
                    meta = _json.loads(meta_raw)
                    st = meta.get("status", "UNKNOWN")
                    status_counts[st] = status_counts.get(st, 0) + 1
                    if meta.get("contact_id"):
                        with_contact_id += 1
                    else:
                        without_contact_id += 1
                except Exception:
                    status_counts["PARSE_ERROR"] = status_counts.get("PARSE_ERROR", 0) + 1
            else:
                status_counts["NO_META"] = status_counts.get("NO_META", 0) + 1

        result["contacts"] = {
            "total_zset": total_zset,
            "by_status": status_counts,
            "with_contact_id": with_contact_id,
            "without_contact_id": without_contact_id,
        }

        # ── Caché HubSpot en Redis ─────────────────────────────────
        stage_keys = len([k async for k in redis_client.scan_iter(f"{CONTACT_STAGE_CACHE_PREFIX}*")])
        name_keys  = len([k async for k in redis_client.scan_iter(f"{CONTACT_NAME_CACHE_PREFIX}*")])
        assoc_keys = len([k async for k in redis_client.scan_iter("hs_assoc:*")])
        idem_keys  = len([k async for k in redis_client.scan_iter("hs_note_processed:*")])

        result["hubspot_cache"] = {
            "contact_stage_keys": stage_keys,
            "contact_name_keys":  name_keys,
            "hs_assoc_keys":      assoc_keys,
            "note_idempotency_keys": idem_keys,
        }

        # ── Estimación de carga HubSpot ────────────────────────────
        contacts_needing_stage = max(0, with_contact_id - stage_keys)
        contacts_needing_assoc = max(0, total_zset - assoc_keys)
        worst_case_calls = 1 + contacts_needing_stage + contacts_needing_assoc * 2
        result["hubspot_load_estimate"] = {
            "contacts_needing_stage_fetch": contacts_needing_stage,
            "contacts_needing_assoc_fetch": contacts_needing_assoc,
            "worst_case_calls_per_panel_load": worst_case_calls,
            "hubspot_limit_per_10s": 50,
            "exceeds_limit": worst_case_calls > 50,
            "over_by": max(0, worst_case_calls - 50),
        }

        # ── Info Redis ─────────────────────────────────────────────
        info_clients = await redis_client.info("clients")
        info_stats   = await redis_client.info("stats")
        info_mem     = await redis_client.info("memory")
        result["redis"] = {
            "connected_clients":   info_clients.get("connected_clients"),
            "blocked_clients":     info_clients.get("blocked_clients"),
            "rejected_connections_ever": info_stats.get("rejected_connections"),
            "memory_used":         info_mem.get("used_memory_human"),
        }

        # ── Últimos 5 contactos activos ────────────────────────────
        recent = await redis_client.zrange("active_conversations_sorted", -5, -1, withscores=True)
        recent_out = []
        for phone, score in reversed(recent):
            meta_raw = await redis_client.get(f"conv_meta:{phone}")
            status, cid = "?", "?"
            if meta_raw:
                try:
                    m = _json.loads(meta_raw)
                    status = m.get("status", "?")
                    cid = m.get("contact_id", "sin_id")
                except Exception:
                    pass
            ts = datetime.fromtimestamp(score, tz=_tz.utc).strftime("%m/%d %H:%M")
            recent_out.append({"ts": ts, "phone_last10": phone[-10:], "status": status, "contact_id": cid})
        result["recent_contacts"] = recent_out

        result["timestamp"] = get_bogota_now().isoformat()
        return result

    except Exception as e:
        logger.error(f"[Panel][Diagnose] Error: {safe_error(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def websocket_stats(x_api_key: str = Header(None, alias="X-API-Key")):
    """
    Retorna estadísticas de conexiones WebSocket activas.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    return ws_manager.get_stats()
