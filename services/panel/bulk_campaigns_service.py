"""Bulk campaign service for the advisor panel.

Moved verbatim from `middleware.outbound_panel` — no behavior changes.
The APScheduler worker (`_process_bulk_campaign_tick_safe`) stays in the
legacy module, where `app.py` imports it.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Body, Header, Query, HTTPException

from logging_config import logger
from middleware import outbound_panel as _legacy
from middleware.outbound_panel import (
    BULK_ALLOWED_TEMPLATES,
    BULK_EXCLUDED_STAGES,
    LAST_CLIENT_MESSAGE_PREFIX,
    PIPELINE_STAGES,
    _get_redis_client,
    get_mongo_manager,
)
from utils.safe_logging import safe_error


def _validate_bulk_request(
    stage_id: str,
    advisor_id: Optional[str],
    template_sid: str,
    template_variables: Optional[Dict[str, str]],
) -> tuple[Optional[dict], Optional[str]]:
    """
    Valida un request de bulk:
      - advisor_id presente
      - stage no excluido
      - template_sid en BULK_ALLOWED_TEMPLATES
      - template_variables incluye texto para todas las vars NO auto-fill

    Retorna (template_meta, error_msg). Si error_msg != None, hubo fallo.
    """
    if not advisor_id:
        return None, "advisor_id requerido"
    if stage_id in BULK_EXCLUDED_STAGES:
        return None, f"stage_id '{stage_id}' no permite envío masivo"
    if stage_id not in PIPELINE_STAGES:
        return None, f"stage_id '{stage_id}' inválido"
    if not template_sid or not template_sid.startswith("HX"):
        return None, "template_sid inválido (debe comenzar con HX)"
    template_meta = BULK_ALLOWED_TEMPLATES.get(template_sid)
    if not template_meta:
        return None, f"template_sid '{template_sid}' no permitido para envío masivo"

    # Verificar que las variables NO auto-fill traigan texto
    template_variables = template_variables or {}
    missing = []
    for var in template_meta["vars"]:
        if var.get("auto_fill"):
            continue  # se resuelve per-contacto en el worker
        key = var["key"]
        val = (template_variables.get(key) or "").strip()
        if not val:
            missing.append(f"{{{key}}} ({var['label']})")
    if missing:
        return None, f"Faltan valores para: {', '.join(missing)}"
    return template_meta, None
async def _resolve_advisor_name(advisor_id: str) -> str:
    """
    Resuelve display name de la asesora desde MongoDB panel_advisors.
    Fallback: 'tu asesora'.
    """
    try:
        mongo_mgr = get_mongo_manager()
        if not await mongo_mgr.connect():
            return "tu asesora"
        doc = await mongo_mgr.db.panel_advisors.find_one({"advisor_id": advisor_id})
        if doc:
            return doc.get("name") or doc.get("display_name") or "tu asesora"
    except Exception as e:
        logger.debug(f"[BulkCampaign] No se pudo resolver advisor_name: {safe_error(e)}")
    return "tu asesora"


async def _hubspot_search_contacts_for_bulk(
    stage_id: str,
    advisor_id: str,
) -> List[Dict[str, Any]]:
    """
    Busca todos los contactos del embudo asignados a la asesora vía HubSpot search.
    Pagina 100/page con sleep 0.15s. Cache Redis 30 min para no saturar HubSpot.

    Retorna lista de contactos con {contact_id, phone, firstname, owner_id}.
    """
    cache_key = f"{HUBSPOT_BULK_SEARCH_CACHE_PREFIX}{stage_id}:{advisor_id}"
    try:
        r = await _get_redis_client()
        cached = await r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        logger.debug(f"[BulkCampaign] cache miss/error: {safe_error(e)}")

    all_contacts: List[Dict[str, Any]] = []
    after: Optional[str] = None
    pages = 0
    max_pages = 50  # 5000 contactos máx — más que suficiente
    while pages < max_pages:
        try:
            resp = await _hs_singleton.search_contacts_by_lifecyclestage_and_owner(
                stage_id=stage_id,
                owner_id=advisor_id,
                limit=100,
                after=after,
            )
        except Exception as e:
            logger.error(f"[BulkCampaign] Error HubSpot search: {safe_error(e)}")
            break
        for r_item in resp.get("results", []):
            props = r_item.get("properties") or {}
            phone_val = (props.get("whatsapp_id") or props.get("phone") or "").strip()
            if not phone_val:
                continue
            all_contacts.append({
                "contact_id": str(r_item.get("id")),
                "phone": phone_val,
                "firstname": (props.get("firstname") or "").strip(),
                "owner_id": props.get("hubspot_owner_id") or advisor_id,
            })
        after = resp.get("next_after")
        if not after:
            break
        pages += 1
        await asyncio.sleep(0.15)

    try:
        r = await _get_redis_client()
        await r.set(cache_key, json.dumps(all_contacts), ex=HUBSPOT_BULK_SEARCH_CACHE_TTL)
    except Exception as e:
        logger.debug(f"[BulkCampaign] Error guardando cache: {safe_error(e)}")

    return all_contacts


async def _filter_contacts_by_last_message_range(
    contacts: List[Dict[str, Any]],
    date_from: Optional[str],
    date_to: Optional[str],
) -> List[Dict[str, Any]]:
    """
    Filtra contactos cuyo LAST_CLIENT_MESSAGE esté en el rango [date_from, date_to].
    Si no hay date_from ni date_to, retorna todos los contactos sin filtrar.
    Si un contacto no tiene last_client_message en Redis, se INCLUYE igual
    (cubre contactos importados sin historial reciente).
    """
    if not date_from and not date_to:
        return contacts
    try:
        from_dt = datetime.fromisoformat(date_from + "T00:00:00+00:00") if date_from else None
        to_dt = datetime.fromisoformat(date_to + "T23:59:59+00:00") if date_to else None
    except Exception as e:
        logger.warning(f"[BulkCampaign] date parse error: {safe_error(e)}")
        return contacts

    r = await _get_redis_client()
    keys = [f"{LAST_CLIENT_MESSAGE_PREFIX}{c['phone']}" for c in contacts]
    if not keys:
        return contacts
    pipe = r.pipeline()
    for k in keys:
        pipe.get(k)
    raw_values = await pipe.execute()

    filtered = []
    for contact, raw in zip(contacts, raw_values):
        if not raw:
            # Sin historial conocido → se incluye
            filtered.append(contact)
            continue
        try:
            ts_str = raw if isinstance(raw, str) else raw.decode()
            ts_str = ts_str.replace("Z", "+00:00")
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if from_dt and ts < from_dt:
                continue
            if to_dt and ts > to_dt:
                continue
            filtered.append(contact)
        except Exception:
            filtered.append(contact)
    return filtered


# ----------------------------------------------------------------------------
# Endpoints Bulk Campaigns
    # ----------------------------------------------------------------------------

async def preview_bulk_campaign(
    stage_id: str = Body(...),
    advisor_id: str = Body(...),
    template_sid: str = Body(...),
    template_variables: Optional[Dict[str, str]] = Body(None),
    date_from: Optional[str] = Body(None),
    date_to: Optional[str] = Body(None),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Preview: cuántos contactos recibirían el bulk (sin crear campaña)."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    template_meta, err = _validate_bulk_request(stage_id, advisor_id, template_sid, template_variables)
    if err:
        raise HTTPException(status_code=400, detail=err)

    contacts = await _hubspot_search_contacts_for_bulk(stage_id, advisor_id)
    filtered = await _filter_contacts_by_last_message_range(contacts, date_from, date_to)

    sample = [{"firstname": c.get("firstname"), "phone": c.get("phone")[-4:]} for c in filtered[:5]]
    return {
        "status": "success",
        "total": len(filtered),
        "total_in_stage": len(contacts),
        "sample": sample,
        "stage_id": stage_id,
        "stage_name": PIPELINE_STAGES.get(stage_id, stage_id),
        "template_name": template_meta["name"],
    }


async def create_bulk_campaign(
    stage_id: str = Body(...),
    advisor_id: str = Body(...),
    template_sid: str = Body(...),
    template_variables: Optional[Dict[str, str]] = Body(None),
    date_from: Optional[str] = Body(None),
    date_to: Optional[str] = Body(None),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Crea una campaña masiva. El processor APScheduler la procesa async."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    template_meta, err = _validate_bulk_request(stage_id, advisor_id, template_sid, template_variables)
    if err:
        raise HTTPException(status_code=400, detail=err)

    contacts = await _hubspot_search_contacts_for_bulk(stage_id, advisor_id)
    filtered = await _filter_contacts_by_last_message_range(contacts, date_from, date_to)
    if not filtered:
        raise HTTPException(status_code=400, detail="No hay contactos en el rango especificado")

    advisor_name = await _resolve_advisor_name(advisor_id)
    auto_fill_keys = [v["key"] for v in template_meta["vars"] if v.get("auto_fill")]
    literal_vars = {
        v["key"]: (template_variables or {}).get(v["key"], "").strip()
        for v in template_meta["vars"] if not v.get("auto_fill")
    }

    mongo_mgr = get_mongo_manager()
    campaign_id = await mongo_mgr.create_bulk_campaign(
        stage_id=stage_id,
        stage_name=PIPELINE_STAGES.get(stage_id, stage_id),
        template_id=template_meta["name"],
        template_content_sid=template_sid,
        date_from=date_from,
        date_to=date_to,
        contacts=filtered,
        creator_advisor_id=advisor_id,
        creator_advisor_name=advisor_name,
        template_variables=literal_vars,
        auto_fill_keys=auto_fill_keys,
    )
    if not campaign_id:
        raise HTTPException(status_code=500, detail="Error creando campaña")

    logger.info(
        f"[BulkCampaign] Creada {campaign_id} stage={stage_id} advisor={advisor_id} "
        f"total={len(filtered)} template={template_meta['name']}"
    )
    return {
        "status": "success",
        "campaign_id": campaign_id,
        "total": len(filtered),
        "stage_name": PIPELINE_STAGES.get(stage_id, stage_id),
        "template_name": template_meta["name"],
    }


async def get_bulk_campaign_status(
    campaign_id: str,
    advisor_id: str = Query(...),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Polling endpoint — solo retorna progreso si el advisor es el creador."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    doc = await mongo_mgr.get_bulk_campaign(campaign_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Campaña no encontrada")
    if doc.get("creator_advisor_id") != advisor_id:
        raise HTTPException(status_code=403, detail="Sin acceso a esta campaña")
    return {
        "campaign_id": doc["_id"],
        "status": doc.get("status"),
        "total_contacts": doc.get("total_contacts"),
        "sent_count": doc.get("sent_count", 0),
        "failed_count": doc.get("failed_count", 0),
        "stage_name": doc.get("stage_name"),
        "template_id": doc.get("template_id"),
        "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
        "completed_at": doc.get("completed_at").isoformat() if doc.get("completed_at") else None,
    }


async def get_last_bulk_campaign(
    stage_id: str,
    advisor_id: str = Query(...),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Última campaña de esta asesora en este embudo (para el banner del modal)."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    doc = await mongo_mgr.get_last_bulk_campaign_by_stage_and_advisor(stage_id, advisor_id)
    if not doc:
        return {"status": "success", "last": None}
    return {
        "status": "success",
        "last": {
            "campaign_id": doc["_id"],
            "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
            "date_from": doc.get("date_from"),
            "date_to": doc.get("date_to"),
            "total_contacts": doc.get("total_contacts"),
            "sent_count": doc.get("sent_count", 0),
            "failed_count": doc.get("failed_count", 0),
            "status": doc.get("status"),
            "template_id": doc.get("template_id"),
        },
    }
