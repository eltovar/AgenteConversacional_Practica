"""Administrative service for the advisor panel (recovery + maintenance).

Moved verbatim from `middleware.outbound_panel` — no behavior changes.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Body, Header, Query, HTTPException

from logging_config import logger
from middleware import outbound_panel as _legacy
from middleware.outbound_panel import (
    HUBSPOT_API_KEY,
    TIMEZONE_BOGOTA,
    _get_redis_client,
    _get_state_manager,
    _hubspot_post,
    get_advisor_names,
    get_bogota_now,
    get_httpx_client,
    get_mongo_manager,
    ws_manager,
)
from utils.safe_logging import safe_error, safe_id, safe_phone


async def restore_panel_from_hubspot(
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Recupera contactos cuyo meta Redis expiró.
    Busca contactos en HubSpot que tengan owner asignado + conversación de chatbot.
    Ejecutar una vez para restaurar contactos desaparecidos del panel.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida o no configurada")

    try:
        # 1. Buscar contactos asignados que tuvieron interacción con el chatbot
        search_url = "https://api.hubapi.com/crm/v3/objects/contacts/search"
        payload = {
            "filterGroups": [{
                "filters": [
                    {"propertyName": "hubspot_owner_id", "operator": "HAS_PROPERTY"}
                ]
            }],
            "properties": [
                "phone", "firstname", "hubspot_owner_id",
                "canal_origen", "whatsapp_id"
            ],
            "sorts": [{"propertyName": "lastmodifieddate", "direction": "DESCENDING"}],
            "limit": 100
        }
        client = get_httpx_client()
        # Paginar hasta obtener todos los contactos (HubSpot devuelve máx 100 por página)
        contacts = []
        after_cursor = None
        MAX_CONTACTS = 500  # límite de seguridad para evitar loops infinitos
        while len(contacts) < MAX_CONTACTS:
            page_payload = {**payload}
            if after_cursor:
                page_payload["after"] = after_cursor
            response = await _hubspot_post(client, search_url, page_payload, HUBSPOT_API_KEY, max_retries=2)
            if response.status_code not in (200, 207):
                raise HTTPException(status_code=502,
                    detail=f"HubSpot devolvió {response.status_code}")
            data = response.json()
            contacts.extend(data.get("results", []))
            after_cursor = data.get("paging", {}).get("next", {}).get("after")
            if not after_cursor:
                break
        logger.info(f"[RestorePanel] HubSpot devolvió {len(contacts)} contactos candidatos (paginado)")

        if not contacts:
            return {
                "restored": 0, "already_active": 0,
                "message": "No se encontraron contactos asignados en HubSpot"
            }

        # 2. Restaurar metas Redis
        state_manager = _get_state_manager()

        restored = 0
        already_active = 0
        skipped_no_phone = 0
        errors = []
        now_ts = datetime.now(timezone.utc).timestamp()
        now_iso = datetime.now(timezone.utc).isoformat() + "Z"

        for contact in contacts:
            try:
                props = contact.get("properties", {})
                # whatsapp_id es el identificador canónico; fallback a phone
                raw_phone = props.get("whatsapp_id") or props.get("phone", "")
                if not raw_phone:
                    skipped_no_phone += 1
                    continue
                validation = PhoneNormalizer().normalize(raw_phone)
                if not validation.is_valid:
                    skipped_no_phone += 1
                    continue
                phone_norm = validation.normalized

                canal_raw = props.get("canal_origen") or "whatsapp_directo"
                canal_safe = canal_raw.lower().replace(" ", "_").strip()
                meta_key = f"{state_manager.META_PREFIX}{phone_norm}:{canal_safe}"

                # Si ya existe meta activo: no sobreescribir meta, pero sí refrescar
                # el ZSET score a now_ts para que no quede enterrado más allá del
                # límite 100 de get_active_contacts() (zrevrange devuelve los más recientes).
                existing = await state_manager.redis.get(meta_key)
                if existing:
                    zset_member = f"{phone_norm}:{canal_safe}"
                    state_key = f"conv_state:{phone_norm}:{canal_safe}"
                    pipe = state_manager.redis.pipeline(transaction=False)
                    # Refrescar ZSET score
                    pipe.zadd(state_manager.ACTIVE_CONTACTS_ZSET, {zset_member: now_ts})
                    pipe.set(state_key, ConversationStatus.HUMAN_ACTIVE.value)
                    pipe.persist(meta_key)
                    await pipe.execute()
                    already_active += 1
                    continue

                # Crear meta restaurado con TTL permanente
                meta = {
                    "phone_normalized": phone_norm,
                    "contact_id": contact.get("id"),
                    "status": ConversationStatus.HUMAN_ACTIVE.value,
                    "last_activity": now_iso,
                    "canal_origen": canal_safe,
                    "display_name": props.get("firstname", ""),
                    "assigned_owner_id": props.get("hubspot_owner_id"),
                    "in_panel": True,
                    "restored": True,
                }
                # state_key es CRÍTICO: get_active_contacts() lo lee primero para el status.
                # Sin él → fuerza BOT_ACTIVE → contacto queda excluido del panel.
                state_key = f"conv_state:{phone_norm}:{canal_safe}"
                pipe = state_manager.redis.pipeline(transaction=False)
                pipe.set(meta_key, json.dumps(meta))
                pipe.set(state_key, ConversationStatus.HUMAN_ACTIVE.value)
                pipe.zadd(state_manager.ACTIVE_CONTACTS_ZSET, {f"{phone_norm}:{canal_safe}": now_ts})
                await pipe.execute()
                restored += 1
                logger.info(f"[RestorePanel] Contacto restaurado: {safe_phone(phone_norm)} ({canal_safe})")

            except Exception as ce:
                errors.append(str(ce))

        return {
            "restored": restored,
            "already_active": already_active,
            "skipped_no_phone": skipped_no_phone,
            "total_from_hubspot": len(contacts),
            "errors": errors[:10]
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[RestorePanel] Error: {safe_error(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# RECOVERY — Restaurar badges/orden tras outage de MongoDB
# =============================================================================

def _parse_iso_to_utc(iso_str: str) -> datetime:
    """Parsea ISO 8601 (con o sin tz) a datetime UTC-aware."""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def recover_outage(
    start_iso: str = Query(..., description="Inicio ventana ISO 8601 ej: 2026-05-20T10:54:00-05:00"),
    end_iso: str = Query(..., description="Fin ventana ISO 8601 ej: 2026-05-20T14:00:00-05:00"),
    dry_run: bool = Query(True, description="True = solo listar candidatos, no modificar Redis"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Recupera badges y orden de contactos cuyo cliente envió mensajes durante un outage
    pero la asesora no ha respondido. Lee Redis (que estuvo UP durante el outage de MongoDB),
    filtra por ventana de tiempo, y restaura advisor_inbox + ZSET + WS broadcast.

    Use cases:
    - Tras outage de MongoDB que pudo desincronizar advisor_inbox
    - Tras crash del worker que pudo dejar contactos sin badge
    - Sweep manual para asegurar que todo contacto esperando respuesta aparece en panel
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida o no configurada")

    try:
        start_dt = _parse_iso_to_utc(start_iso)
        end_dt = _parse_iso_to_utc(end_iso)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=f"Fecha inválida: {ve}")

    if end_dt <= start_dt:
        raise HTTPException(status_code=400, detail="end_iso debe ser mayor que start_iso")

    rc = await _get_redis_client()
    state_manager = _get_state_manager()

    candidates: List[Dict[str, Any]] = []
    scanned = 0
    parse_errors = 0
    BATCH_SIZE = 500  # MGET en lotes de 500 para evitar N+1

    def _process_batch(keys_batch: List[str], values_batch: List[Any]) -> None:
        """Procesa un batch de (key, value) buscando candidatos en la ventana."""
        nonlocal parse_errors
        for key, raw in zip(keys_batch, values_batch):
            if not raw:
                continue
            try:
                meta = json.loads(raw)
                last_client_iso = meta.get("last_client_message")
                if not last_client_iso:
                    continue
                last_client_dt = _parse_iso_to_utc(last_client_iso)

                # Filtro por ventana de outage
                if not (start_dt <= last_client_dt <= end_dt):
                    continue

                # Cliente fue el último en hablar? (gate principal)
                last_advisor_iso = meta.get("last_advisor_message")
                if last_advisor_iso:
                    last_advisor_dt = _parse_iso_to_utc(last_advisor_iso)
                    if last_advisor_dt >= last_client_dt:
                        continue

                phone = meta.get("phone_normalized")
                canal = (meta.get("canal_origen") or "whatsapp").lower()
                owner_id = meta.get("assigned_owner_id")
                display_name = meta.get("display_name")

                if not phone:
                    continue

                candidates.append({
                    "phone": phone,
                    "canal": canal,
                    "owner_id": owner_id,
                    "display_name": display_name,
                    "last_client_message": last_client_iso,
                    "last_advisor_message": last_advisor_iso,
                    "redis_key": key,
                })
            except Exception as pe:
                parse_errors += 1
                if parse_errors <= 5:
                    logger.warning(f"[Recovery] Parse error en {key}: {pe}")

    # 1. Scan + MGET en batches (3-5 ordenes de magnitud más rápido que GET individual)
    logger.info(f"[Recovery] Iniciando scan con ventana {safe_id(start_iso, 'from')} -> {safe_id(end_iso, 'to')}")
    keys_buffer: List[str] = []
    try:
        async for key_bytes in rc.scan_iter(match=f"{state_manager.META_PREFIX}*", count=500):
            key = key_bytes.decode() if isinstance(key_bytes, bytes) else key_bytes
            keys_buffer.append(key)

            # Flush cuando el buffer llegue a BATCH_SIZE
            if len(keys_buffer) >= BATCH_SIZE:
                values = await rc.mget(keys_buffer)
                _process_batch(keys_buffer, values)
                scanned += len(keys_buffer)
                keys_buffer = []
                logger.info(
                    f"[Recovery] Progreso: {scanned} metas escaneadas, "
                    f"{len(candidates)} candidatos hasta ahora"
                )

        # Flush del buffer restante
        if keys_buffer:
            values = await rc.mget(keys_buffer)
            _process_batch(keys_buffer, values)
            scanned += len(keys_buffer)
    except Exception as scan_err:
        logger.error(f"[Recovery] Error escaneando Redis: {scan_err}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error escaneando Redis: {scan_err}")

    logger.info(
        f"[Recovery] Scan completo: {scanned} metas revisadas, "
        f"{len(candidates)} candidatos en ventana, {parse_errors} errores parseo"
    )

    # 2. Aplicar recovery (si no es dry_run)
    applied: List[str] = []
    apply_errors: List[Dict[str, str]] = []
    skipped_no_owner: List[str] = []

    if not dry_run:
        for cand in candidates:
            phone = cand["phone"]
            canal = cand["canal"]
            owner_id = cand["owner_id"]
            try:
                last_client_dt = _parse_iso_to_utc(cand["last_client_message"])
                score = last_client_dt.timestamp()

                # 2a. ZADD active_conversations_sorted (mantiene orden por urgencia)
                await rc.zadd(
                    state_manager.ACTIVE_CONTACTS_ZSET,
                    {f"{phone}:{canal}": score}
                )

                # 2b. advisor_inbox (badge "no leído") — requiere owner_id
                if owner_id:
                    await state_manager.add_to_advisor_inbox(owner_id, phone, canal, score=score)
                else:
                    skipped_no_owner.append(phone)

                # 2c. WS broadcast para refrescar panel en vivo
                try:
                    await ws_manager.publish_broadcast(rc, {
                        "type": "contact_updated",
                        "phone": phone,
                        "canal": canal,
                        "action": "new_message",
                    })
                except Exception as we:
                    logger.warning(f"[Recovery] WS broadcast falló para {safe_phone(phone)}: {safe_error(we)}")

                applied.append(phone)
            except Exception as ae:
                apply_errors.append({"phone": phone, "error": str(ae)})
                logger.error(f"[Recovery] Apply error para {safe_phone(phone)}: {safe_error(ae)}", exc_info=True)

    return {
        "window": {"start": start_iso, "end": end_iso},
        "dry_run": dry_run,
        "scanned_metas": scanned,
        "candidates_found": len(candidates),
        "parse_errors": parse_errors,
        "applied_count": len(applied) if not dry_run else 0,
        "apply_errors": apply_errors,
        "skipped_no_owner": skipped_no_owner,
        # En dry_run devolvemos detalle completo; en aplicado solo lista de phones
        "candidates": candidates if dry_run else None,
        "applied": applied if not dry_run else None,
    }


# =============================================================================
# ONE-SHOT: Limpieza de advisor_inbox stale (BOT_ACTIVE entries)
# =============================================================================

async def cleanup_stale_inbox(
    dry_run: bool = Query(True, description="True = solo listar, no borrar"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Limpia entries de advisor_inbox cuyo contacto está en BOT_ACTIVE o sin estado.
    Estas entries son badges fantasma que no deberían existir.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida o no configurada")

    from middleware.conversation_state import ConversationStatus
    state_mgr = _get_state_manager()
    ACTIVE_ADVISORS = get_panel_advisor_ids()
    results = {}
    total_cleaned = 0

    for adv_id in ACTIVE_ADVISORS:
        inbox_key = f"{state_mgr.ADVISOR_INBOX_PREFIX}{adv_id}"
        members = await state_mgr.redis.zrange(inbox_key, 0, -1, withscores=True)
        if not members:
            results[adv_id] = {"total": 0, "stale": 0, "entries": []}
            continue
        stale_members = []
        stale_details = []
        for m, score in members:
            if isinstance(m, bytes):
                m = m.decode("utf-8", errors="ignore")
            parts = m.rsplit(":", 1)
            if len(parts) != 2:
                continue
            phone, canal = parts
            state_key = f"{state_mgr.STATE_PREFIX}{phone}:{canal}"
            raw_state = await state_mgr.redis.get(state_key)
            if raw_state and isinstance(raw_state, bytes):
                raw_state = raw_state.decode("utf-8", errors="ignore")
            if not raw_state or raw_state == ConversationStatus.BOT_ACTIVE.value:
                stale_members.append(m)
                stale_details.append({
                    "member": m,
                    "state": raw_state or "NO_STATE",
                    "score": score,
                })
        removed = 0
        if stale_members and not dry_run:
            removed = await state_mgr.redis.zrem(inbox_key, *stale_members)
            total_cleaned += removed
        results[adv_id] = {
            "total": len(members),
            "stale": len(stale_members),
            "removed": removed if not dry_run else "dry_run",
            "entries": stale_details[:50],
        }

    return {
        "dry_run": dry_run,
        "total_cleaned": total_cleaned if not dry_run else "dry_run",
        "advisors": results,
    }


async def sync_owners_with_hubspot(
    dry_run: bool = Query(True, description="True = solo listar, no aplicar"),
    limit: int = Query(50, ge=1, le=500, description="Contactos a procesar por lote"),
    offset: int = Query(0, ge=0, description="Posición inicial en el ZSET"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Sincroniza assigned_owner_id en Redis meta con hubspot_owner_id de HubSpot.
    También corrige MongoDB conversations.owner_id y mueve inbox badges.
    Usa limit/offset para procesar en lotes y evitar timeouts.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida o no configurada")

    import json as _json

    state_mgr = _get_state_manager()
    http = get_httpx_client()
    OWNER_NAMES = get_advisor_names()
    hs_token = os.getenv("HUBSPOT_ACCESS_TOKEN") or os.getenv("HUBSPOT_API_KEY")

    total_zset = await state_mgr.redis.zcard(state_mgr.ACTIVE_CONTACTS_ZSET)
    all_members = await state_mgr.redis.zrange(
        state_mgr.ACTIVE_CONTACTS_ZSET, offset, offset + limit - 1, withscores=True,
    )

    mismatches = []
    correct = 0
    skipped = 0
    errors = []

    for raw_member, score in all_members:
        if isinstance(raw_member, bytes):
            raw_member = raw_member.decode("utf-8", errors="ignore")
        parts = raw_member.rsplit(":", 1)
        if len(parts) != 2:
            continue
        phone, canal = parts

        meta_key = f"{state_mgr.META_PREFIX}{phone}:{canal}"
        raw = await state_mgr.redis.get(meta_key)
        if not raw:
            skipped += 1
            continue
        meta = _json.loads(raw)
        redis_owner = meta.get("assigned_owner_id")

        contact_id = meta.get("contact_id")
        if not contact_id:
            cache_key = f"phone_cache:{phone}"
            contact_id = await state_mgr.redis.get(cache_key)
            if isinstance(contact_id, bytes):
                contact_id = contact_id.decode()
        if not contact_id:
            skipped += 1
            continue

        try:
            resp = await http.get(
                f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}",
                params={"properties": "hubspot_owner_id"},
                headers={"Authorization": f"Bearer {hs_token}"},
            )
            if resp.status_code != 200:
                skipped += 1
                continue
            hs_owner = resp.json().get("properties", {}).get("hubspot_owner_id")
        except Exception as e:
            errors.append({"phone": phone, "error": str(e)})
            continue

        if not hs_owner:
            skipped += 1
            continue

        if str(redis_owner) != str(hs_owner):
            mismatches.append({
                "phone": phone,
                "canal": canal,
                "contact_id": contact_id,
                "redis_owner": redis_owner,
                "hubspot_owner": hs_owner,
                "redis_owner_name": OWNER_NAMES.get(str(redis_owner), "?"),
                "hubspot_owner_name": OWNER_NAMES.get(str(hs_owner), "?"),
                "display_name": meta.get("display_name", ""),
                "zset_score": score,
            })
        else:
            correct += 1

        await asyncio.sleep(0.05)

    fixed_redis = 0
    fixed_mongo = 0
    fixed_inbox = 0

    if not dry_run and mismatches:
        mongo_mgr = get_mongo_manager()
        for m in mismatches:
            phone, canal = m["phone"], m["canal"]
            old_owner = str(m["redis_owner"]) if m["redis_owner"] else None
            new_owner = m["hubspot_owner"]
            meta_key = f"{state_mgr.META_PREFIX}{phone}:{canal}"

            try:
                raw = await state_mgr.redis.get(meta_key)
                if raw:
                    meta_data = _json.loads(raw)
                    meta_data["assigned_owner_id"] = new_owner
                    await state_mgr.redis.set(meta_key, _json.dumps(meta_data))
                    fixed_redis += 1
            except Exception as e:
                errors.append({"phone": phone, "error": f"Redis: {e}"})

            try:
                await mongo_mgr.db.conversations.update_one(
                    {"phone": phone, "canal": canal},
                    {"$set": {"owner_id": new_owner}},
                )
                fixed_mongo += 1
            except Exception:
                pass

            member = f"{phone}:{canal}"
            if old_owner:
                old_inbox = f"{state_mgr.ADVISOR_INBOX_PREFIX}{old_owner}"
                new_inbox = f"{state_mgr.ADVISOR_INBOX_PREFIX}{new_owner}"
                try:
                    inbox_score = await state_mgr.redis.zscore(old_inbox, member)
                    if inbox_score is not None:
                        await state_mgr.redis.zrem(old_inbox, member)
                        await state_mgr.redis.zadd(new_inbox, {member: inbox_score})
                        fixed_inbox += 1
                except Exception:
                    pass

    return {
        "dry_run": dry_run,
        "total_checked": correct + len(mismatches) + skipped,
        "correct": correct,
        "skipped": skipped,
        "mismatches_count": len(mismatches),
        "mismatches": [
            {
                "phone": m["phone"],
                "display_name": m["display_name"],
                "from": f"{m['redis_owner_name']} ({m['redis_owner']})",
                "to": f"{m['hubspot_owner_name']} ({m['hubspot_owner']})",
            }
            for m in mismatches
        ],
        "pagination": {
            "total_zset": total_zset,
            "offset": offset,
            "limit": limit,
            "processed": len(all_members),
            "next_offset": offset + len(all_members) if offset + len(all_members) < total_zset else None,
        },
        "applied": {
            "redis_meta": fixed_redis,
            "mongodb": fixed_mongo,
            "inbox_moved": fixed_inbox,
        } if not dry_run else "dry_run",
        "errors": errors[:20],
    }


async def sync_owners_mongo(
    dry_run: bool = Query(True),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    advisor: str = Query(None, description="Solo corregir contactos de este advisor"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Escanea MongoDB conversations directamente y corrige owner_id vs HubSpot.
    Cubre contactos que no están en el ZSET (los que aparecen como 'Historial').
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    import json as _json
    mongo_mgr = get_mongo_manager()
    http = get_httpx_client()
    OWNER_NAMES = get_advisor_names()
    hs_token = os.getenv("HUBSPOT_ACCESS_TOKEN") or os.getenv("HUBSPOT_API_KEY")

    query: dict = {"owner_id": {"$exists": True, "$ne": None}}
    if advisor:
        query["owner_id"] = advisor

    total = await mongo_mgr.db.conversations.count_documents(query)
    cursor = mongo_mgr.db.conversations.find(
        query,
        {"phone": 1, "canal": 1, "owner_id": 1, "display_name": 1, "contact_id": 1, "_id": 0},
    ).sort("last_message_at", -1).skip(offset).limit(limit)
    docs = await cursor.to_list(length=limit)

    mismatches = []
    correct = 0
    skipped = 0
    errors = []

    for d in docs:
        phone = d.get("phone", "")
        if not phone:
            skipped += 1
            continue

        contact_id = d.get("contact_id")
        if not contact_id:
            state_mgr = _get_state_manager()
            cache_key = f"phone_cache:{phone}"
            contact_id = await state_mgr.redis.get(cache_key)
            if isinstance(contact_id, bytes):
                contact_id = contact_id.decode()
        if not contact_id:
            skipped += 1
            continue

        try:
            resp = await http.get(
                f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}",
                params={"properties": "hubspot_owner_id"},
                headers={"Authorization": f"Bearer {hs_token}"},
            )
            if resp.status_code != 200:
                skipped += 1
                continue
            hs_owner = resp.json().get("properties", {}).get("hubspot_owner_id")
        except Exception as e:
            errors.append({"phone": phone, "error": str(e)})
            continue

        if not hs_owner:
            skipped += 1
            continue

        mongo_owner = d.get("owner_id")
        if str(mongo_owner) != str(hs_owner):
            mismatches.append({
                "phone": phone,
                "canal": d.get("canal", "whatsapp"),
                "contact_id": contact_id,
                "mongo_owner": mongo_owner,
                "hubspot_owner": hs_owner,
                "display_name": d.get("display_name", ""),
            })
        else:
            correct += 1

        await asyncio.sleep(0.05)

    fixed_mongo = 0
    fixed_redis = 0

    if not dry_run and mismatches:
        state_mgr = _get_state_manager()
        for m in mismatches:
            phone, canal = m["phone"], m["canal"]
            new_owner = m["hubspot_owner"]

            try:
                await mongo_mgr.db.conversations.update_one(
                    {"phone": phone, "canal": canal},
                    {"$set": {"owner_id": new_owner}},
                )
                fixed_mongo += 1
            except Exception:
                pass

            meta_key = f"{state_mgr.META_PREFIX}{phone}:{canal}"
            try:
                raw = await state_mgr.redis.get(meta_key)
                if raw:
                    meta_data = _json.loads(raw)
                    if str(meta_data.get("assigned_owner_id")) != str(new_owner):
                        meta_data["assigned_owner_id"] = new_owner
                        await state_mgr.redis.set(meta_key, _json.dumps(meta_data))
                        fixed_redis += 1
            except Exception:
                pass

    return {
        "dry_run": dry_run,
        "source": "mongodb",
        "filter_advisor": advisor,
        "pagination": {"total": total, "offset": offset, "limit": limit, "processed": len(docs),
                        "next_offset": offset + len(docs) if offset + len(docs) < total else None},
        "correct": correct,
        "skipped": skipped,
        "mismatches_count": len(mismatches),
        "mismatches": [
            {"phone": m["phone"], "display_name": m["display_name"],
             "from": f"{OWNER_NAMES.get(str(m['mongo_owner']), '?')} ({m['mongo_owner']})",
             "to": f"{OWNER_NAMES.get(str(m['hubspot_owner']), '?')} ({m['hubspot_owner']})"}
            for m in mismatches
        ],
        "applied": {"mongodb": fixed_mongo, "redis_meta": fixed_redis} if not dry_run else "dry_run",
        "errors": errors[:20],
    }


async def recover_lost_conversations(
    dry_run: bool = Query(True),
    hours: int = Query(72, ge=1, le=168, description="Ventana de tiempo en horas"),
    advisor: str = Query(None, description="Solo recuperar para este advisor"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Recupera conversaciones perdidas donde el último mensaje fue del CLIENTE
    y no ha recibido respuesta dentro de la ventana de tiempo.

    Lógica:
    1. Busca en MongoDB `messages` los últimos mensajes por teléfono
    2. Filtra solo aquellos donde el último fue sender='client' (sin respuesta)
    3. Verifica que esté dentro de la ventana de horas
    4. Obtiene el owner correcto de HubSpot
    5. Corrige owner_id en MongoDB/Redis si es necesario
    6. Agrega al inbox del advisor correcto (badge de no-leído)
    7. Envía notificación WebSocket en tiempo real
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    import json as _json
    from datetime import timedelta

    mongo_mgr = get_mongo_manager()
    state_mgr = _get_state_manager()
    http = get_httpx_client()
    OWNER_NAMES = get_advisor_names()
    hs_token = os.getenv("HUBSPOT_ACCESS_TOKEN") or os.getenv("HUBSPOT_API_KEY")

    cutoff = get_bogota_now() - timedelta(hours=hours)

    pipeline = [
        {"$match": {"timestamp": {"$gte": cutoff}}},
        {"$sort": {"phone": 1, "timestamp": -1}},
        {"$group": {
            "_id": "$phone",
            "last_sender": {"$first": "$sender"},
            "last_content": {"$first": "$content"},
            "last_timestamp": {"$first": "$timestamp"},
            "last_channel": {"$first": "$channel"},
            "contact_id": {"$first": "$hubspot_contact_id"},
        }},
        {"$match": {"last_sender": "client"}},
        {"$sort": {"last_timestamp": -1}},
    ]

    try:
        results = await mongo_mgr.db.messages.aggregate(pipeline).to_list(length=500)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Aggregation error: {e}")

    recovered = []
    already_in_inbox = 0
    skipped = 0
    errors = []

    for doc in results:
        phone = doc["_id"]
        if not phone:
            continue

        canal = doc.get("last_channel", "whatsapp")
        contact_id = doc.get("contact_id")
        last_msg = doc.get("last_content", "")
        last_ts = doc.get("last_timestamp")

        if not contact_id:
            cache_key = f"phone_cache:{phone}"
            contact_id = await state_mgr.redis.get(cache_key)
            if isinstance(contact_id, bytes):
                contact_id = contact_id.decode()

        hs_owner = None
        display_name = ""
        if contact_id:
            try:
                resp = await http.get(
                    f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}",
                    params={"properties": "hubspot_owner_id,firstname,lastname"},
                    headers={"Authorization": f"Bearer {hs_token}"},
                )
                if resp.status_code == 200:
                    props = resp.json().get("properties", {})
                    hs_owner = props.get("hubspot_owner_id")
                    display_name = f"{props.get('firstname', '')} {props.get('lastname', '')}".strip()
            except Exception as e:
                errors.append({"phone": phone, "error": str(e)})
                continue
            await asyncio.sleep(0.05)

        if not hs_owner:
            meta_key = f"{state_mgr.META_PREFIX}{phone}:{canal}"
            raw_meta = await state_mgr.redis.get(meta_key)
            if raw_meta:
                m = _json.loads(raw_meta)
                hs_owner = m.get("assigned_owner_id")
                if not display_name:
                    display_name = m.get("display_name", "")

        if not hs_owner:
            skipped += 1
            continue

        if advisor and str(hs_owner) != str(advisor):
            skipped += 1
            continue

        member = f"{phone}:{canal}"
        inbox_key = f"{state_mgr.ADVISOR_INBOX_PREFIX}{hs_owner}"
        existing_score = await state_mgr.redis.zscore(inbox_key, member)
        if existing_score is not None:
            already_in_inbox += 1
            continue

        time_ago = ""
        if last_ts:
            try:
                _ts = last_ts.replace(tzinfo=TIMEZONE_BOGOTA) if last_ts.tzinfo is None else last_ts
                delta = get_bogota_now() - _ts
                hrs = int(delta.total_seconds() / 3600)
                time_ago = f"{hrs}h" if hrs < 48 else f"{hrs // 24}d"
            except Exception:
                time_ago = "?"

        entry = {
            "phone": phone,
            "canal": canal,
            "display_name": display_name or phone,
            "advisor": hs_owner,
            "advisor_name": OWNER_NAMES.get(str(hs_owner), "?"),
            "last_message": last_msg[:80] if last_msg else "",
            "last_timestamp": last_ts.isoformat() if last_ts else None,
            "time_ago": time_ago,
            "contact_id": contact_id,
        }

        if not dry_run:
            meta_key = f"{state_mgr.META_PREFIX}{phone}:{canal}"
            raw_meta = await state_mgr.redis.get(meta_key)
            if raw_meta:
                meta_data = _json.loads(raw_meta)
                if str(meta_data.get("assigned_owner_id")) != str(hs_owner):
                    meta_data["assigned_owner_id"] = hs_owner
                    await state_mgr.redis.set(meta_key, _json.dumps(meta_data))

            zset_member = f"{phone}:{canal}"
            msg_ts = last_ts.timestamp() if last_ts else get_bogota_now().timestamp()
            await state_mgr.redis.zadd(
                state_mgr.ACTIVE_CONTACTS_ZSET, {zset_member: msg_ts}
            )

            await state_mgr.add_to_advisor_inbox(hs_owner, phone, canal, score=msg_ts)

            await mongo_mgr.db.conversations.update_one(
                {"phone": phone, "canal": canal},
                {"$set": {"owner_id": hs_owner}},
                upsert=False,
            )

            try:
                _rc = await _get_redis_client()
                await ws_manager.notify_new_message(
                    phone=phone,
                    canal=canal,
                    message_preview=last_msg[:50] if last_msg else "Mensaje sin respuesta",
                    sender="client",
                    contact_name=display_name or phone,
                    redis_client=_rc,
                    assigned_owner_id=hs_owner,
                    state_manager=state_mgr,
                )
            except Exception:
                pass

        recovered.append(entry)

    by_advisor = {}
    for r in recovered:
        name = r["advisor_name"]
        by_advisor.setdefault(name, []).append(r)

    return {
        "dry_run": dry_run,
        "window_hours": hours,
        "filter_advisor": advisor,
        "total_client_last_message": len(results),
        "already_in_inbox": already_in_inbox,
        "skipped_no_owner": skipped,
        "recovered_count": len(recovered),
        "recovered_by_advisor": {
            name: {
                "count": len(items),
                "contacts": [
                    {"phone": i["phone"], "name": i["display_name"],
                     "last_msg": i["last_message"], "time_ago": i["time_ago"]}
                    for i in items
                ],
            }
            for name, items in by_advisor.items()
        },
        "errors": errors[:20],
    }
