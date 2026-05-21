"""
Filtro por etapa (lifecyclestage) — Pull completo desde HubSpot.

Cuando el asesor selecciona una etapa específica en el panel, este módulo trae
TODOS los contactos del asesor en esa etapa (cap MAX_STAGE_CONTACTS).

Aislado de outbound_panel.py para mantener cohesión. Reutiliza singletons
existentes (Redis, HubSpot, state_manager). No escribe al ZSET.
"""
import json
import logging
import os
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from middleware.conversation_state import ConversationStateManager

logger = logging.getLogger(__name__)

# ── Configuración ────────────────────────────────────────────────────────────
MAX_STAGE_CONTACTS = int(os.getenv("MAX_STAGE_CONTACTS", "500"))
HS_STAGE_CACHE_TTL = int(os.getenv("HS_STAGE_CACHE_TTL", "60"))
_MAX_PAGES = 10  # safety: 10 páginas × 100 = 1000 ceiling
_BATCH_SIZE = 100  # HubSpot batch read máximo


# ── Cache helpers ────────────────────────────────────────────────────────────
async def _get_cached_stage_results(stage: str, owner_id: str) -> Optional[List[Dict[str, Any]]]:
    """Lee cache Redis hs_stage:{stage}:{owner_id}. None si miss o error."""
    from middleware.outbound_panel import _get_redis_client
    try:
        rc = await _get_redis_client()
        raw = await rc.get(f"hs_stage:{stage}:{owner_id}")
        if raw:
            return json.loads(raw)
    except Exception as e:
        logger.warning(f"[StageFilter] Cache read error: {e}")
    return None


async def _set_cached_stage_results(stage: str, owner_id: str, results: List[Dict[str, Any]]) -> None:
    """Escribe cache Redis con TTL HS_STAGE_CACHE_TTL. Errores no propagan."""
    from middleware.outbound_panel import _get_redis_client
    try:
        rc = await _get_redis_client()
        await rc.setex(
            f"hs_stage:{stage}:{owner_id}",
            HS_STAGE_CACHE_TTL,
            json.dumps(results, default=str),
        )
    except Exception as e:
        logger.warning(f"[StageFilter] Cache write error: {e}")


# ── Paginated HubSpot search ─────────────────────────────────────────────────
async def _paginated_search(stage: str, owner_id: str) -> List[Dict[str, Any]]:
    """
    Pagina HubSpot search hasta MAX_STAGE_CONTACTS o _MAX_PAGES.
    Usa cursor 'after' del response de HubSpot.
    """
    from integrations.hubspot import hubspot_client
    all_results: List[Dict[str, Any]] = []
    after: Optional[str] = None
    pages = 0
    while len(all_results) < MAX_STAGE_CONTACTS and pages < _MAX_PAGES:
        remaining = MAX_STAGE_CONTACTS - len(all_results)
        batch_limit = min(100, remaining)
        result = await hubspot_client.search_contacts_by_lifecyclestage_and_owner(
            stage_id=stage,
            owner_id=owner_id,
            limit=batch_limit,
            after=after,
        )
        all_results.extend(result.get("results", []))
        after = result.get("next_after")
        pages += 1
        if not after:
            break
    logger.info(
        f"[StageFilter] Search stage={stage} owner={owner_id}: "
        f"{len(all_results)} resultados en {pages} páginas"
    )
    return all_results


# ── Batch enrichment (>100 IDs en chunks) ────────────────────────────────────
async def _batch_enrich(contact_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Enriquece contact_ids en chunks de _BATCH_SIZE (HubSpot batch limit).
    Cada chunk usa _hubspot_batch_get_contacts (incluye su cache hs_batch:*).
    """
    from middleware.outbound_panel import _hubspot_batch_get_contacts
    if not contact_ids:
        return {}
    merged: Dict[str, Dict[str, Any]] = {}
    for i in range(0, len(contact_ids), _BATCH_SIZE):
        chunk = contact_ids[i:i + _BATCH_SIZE]
        chunk_data = await _hubspot_batch_get_contacts(chunk)
        merged.update(chunk_data)
    return merged


# ── Merge con Redis ZSET ─────────────────────────────────────────────────────
async def _merge_with_redis(
    contact_ids: List[str],
    enriched: Dict[str, Dict[str, Any]],
    state_manager: "ConversationStateManager",
    owner_id: str,
    stage: str,
) -> List[Dict[str, Any]]:
    """
    Para cada contact_id:
    - Si está en Redis ZSET → usar su meta completa (unread, last_activity, etc.)
    - Si solo está en HubSpot → skeleton con from_hubspot_stage=True
    """
    redis_map: Dict[str, Dict[str, Any]] = {}
    try:
        active = await state_manager.get_all_human_active_contacts(limit=1000, offset=0)
        for c in active:
            cid = c.get("contact_id")
            if cid:
                redis_map[cid] = c
    except Exception as e:
        logger.warning(f"[StageFilter] Redis merge error: {e}")

    final: List[Dict[str, Any]] = []
    for cid in contact_ids:
        if cid in redis_map:
            contact = dict(redis_map[cid])
            contact["from_hubspot_stage"] = False
            # HubSpot es source of truth para current_stage al momento del search:
            # evita que el filtro local del frontend rechace contactos por cache stale.
            contact["current_stage"] = stage
        else:
            hs_data = enriched.get(cid, {})
            firstname = (hs_data.get("firstname") or "").strip()
            lastname = (hs_data.get("lastname") or "").strip()
            full = f"{firstname} {lastname}".strip()
            display = full or hs_data.get("email") or "Sin nombre"
            contact = {
                "contact_id": cid,
                "phone": hs_data.get("phone") or "",
                "display_name": display,
                "current_stage": stage,
                "status": "BOT_ACTIVE",
                "has_unread": False,
                "from_hubspot_stage": True,
                "last_activity": None,
                "assigned_owner_id": owner_id,
            }
        final.append(contact)

    final.sort(
        key=lambda c: (c.get("last_activity") or "", c.get("display_name") or ""),
        reverse=True,
    )
    return final


# ── Función pública ──────────────────────────────────────────────────────────
async def get_contacts_by_stage_full(
    stage: str,
    owner_id: str,
    state_manager: "ConversationStateManager",
) -> Dict[str, Any]:
    """
    Trae todos los contactos del owner en la etapa dada (cap MAX_STAGE_CONTACTS).

    Returns:
        {
            "contacts": list[dict],
            "total": int,
            "max_reached": bool,
            "from_cache": bool,
        }
    """
    # Cache hit
    cached = await _get_cached_stage_results(stage, owner_id)
    if cached is not None:
        logger.info(
            f"[StageFilter] Cache HIT stage={stage} owner={owner_id} count={len(cached)}"
        )
        return {
            "contacts": cached,
            "total": len(cached),
            "max_reached": len(cached) >= MAX_STAGE_CONTACTS,
            "from_cache": True,
        }

    # HubSpot search paginado
    hs_results = await _paginated_search(stage, owner_id)
    contact_ids = [r.get("id") for r in hs_results if r.get("id")]

    # Enriquecer (batches de 100)
    enriched = await _batch_enrich(contact_ids)

    # Merge con Redis ZSET
    final = await _merge_with_redis(contact_ids, enriched, state_manager, owner_id, stage)

    # Cache write — defensivo: nunca propaga (los resultados frescos son lo que importa)
    try:
        await _set_cached_stage_results(stage, owner_id, final)
    except Exception as _cache_err:
        logger.warning(f"[StageFilter] Cache write propagated unexpectedly: {_cache_err}")

    logger.info(
        f"[StageFilter] Pipeline complete stage={stage} owner={owner_id}: "
        f"hs={len(hs_results)} enriched={len(enriched)} final={len(final)}"
    )

    return {
        "contacts": final,
        "total": len(final),
        "max_reached": len(final) >= MAX_STAGE_CONTACTS,
        "from_cache": False,
    }
