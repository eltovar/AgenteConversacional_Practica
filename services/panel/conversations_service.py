"""Conversation history service for the advisor panel.

Moved verbatim from `middleware.outbound_panel` — no behavior changes.
Rate limiting decorators travel with the endpoints.
"""

import asyncio
from datetime import datetime
from typing import Optional

from fastapi import Header, Query, Request, HTTPException
from fastapi.responses import JSONResponse

from logging_config import logger
from middleware import outbound_panel as _legacy
from middleware.outbound_panel import (
    _assert_advisor_can_open_chat,
    _get_contact_manager,
    _resolve_panel_target,
    get_mongo_manager,
    get_timeline_logger,
    limiter,
)
from utils.safe_logging import safe_error, safe_id


@limiter.limit("60/minute")
async def get_conversation_history(
    request: Request,
    phone: str,
    # ⚠️ 2026-06-02: default bajado de 500 → 100 (carga inicial rápida).
    # Frontend usa scroll infinito con before_ts para cargar páginas siguientes.
    limit: int = Query(100, ge=1, le=500),
    canal: Optional[str] = Query(None, description="Canal para filtrar mensajes"),
    advisor_id: Optional[str] = Query(None, description="ID de la asesora que abre el chat"),
    before_ts: Optional[str] = Query(
        None,
        description="Cursor ISO 8601 — retorna mensajes con timestamp < before_ts (paginación)"
    ),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Obtiene el historial de conversación de un contacto por teléfono.

    ARQUITECTURA v2.0 + paginación cursor-based (2026-06-02):
    1. Consultar MongoDB con filtro before_ts si presente (paginación).
    2. Si MongoDB vacío Y NO es página paginada → Fallback a HubSpot.
       En páginas paginadas (before_ts presente) NUNCA llamar HubSpot
       para evitar 25+ requests externos en un scroll.
    3. Retornar `has_more` y `oldest_ts` para que frontend pagine.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    target = _resolve_panel_target(phone)
    if target.error:
        raise HTTPException(status_code=400, detail=target.error)

    phone_normalized = target.key
    await _assert_advisor_can_open_chat(
        advisor_id=advisor_id,
        phone=phone_normalized,
        canal=canal,
    )

    messages = []
    source = "none"

    # ⚠️ Parse del cursor before_ts → datetime
    before_ts_dt = None
    if before_ts:
        try:
            before_ts_dt = datetime.fromisoformat(before_ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail=f"before_ts inválido: {before_ts}")

    # ⚠️ Guard HubSpot fallback: solo permitido en primera página (sin cursor).
    hs_fallback_allowed = (before_ts_dt is None)

    try:
        # =====================================================================
        # PASO 1: MongoDB - Fuente de verdad en tiempo real
        # =====================================================================
        mongo_manager = get_mongo_manager()
        messages = await mongo_manager.get_history(
            phone=phone_normalized,
            limit=limit,
            channel=canal,
            before_ts=before_ts_dt,
        )

        if messages:
            source = "mongodb"
            logger.debug(f"[Panel] Historial desde MongoDB: {len(messages)} mensajes")

        # Fallback sin canal: lead de portal (mercado_libre/ciencuadras) → mensajes en seguimiento_transfer()channel=whatsapp
        if not messages and canal and canal not in ("whatsapp", "instagram"):
            messages = await mongo_manager.get_history(
                phone=phone_normalized,
                limit=limit,
                channel=None,
                before_ts=before_ts_dt,
            )
            if messages:
                source = "mongodb"

        # =====================================================================
        # PASO 2: Si MongoDB vacío Y es primera página → Fallback HubSpot
        # ⚠️ 2026-06-02: hs_fallback_allowed bloquea fallback en paginación
        # =====================================================================
        if not messages and hs_fallback_allowed:
            contact_manager = _get_contact_manager()
            contact_id = await contact_manager._search_contact(phone_normalized)

            if contact_id:
                timeline_logger = get_timeline_logger()
                messages = await timeline_logger.get_notes_for_contact(
                    contact_id=contact_id,
                    limit=limit
                )
                source = "hubspot"
                logger.debug(f"[Panel] Historial desde HubSpot (fallback): {len(messages)} mensajes")

        # ⚠️ Metadata de paginación
        has_more = len(messages) == limit  # si recibimos exactamente `limit`, puede haber más
        oldest_ts = None
        if messages:
            # mensajes vienen ordenados cronológicamente (más antiguo primero)
            oldest_ts = messages[0].get("timestamp")

        return {
            "phone": phone_normalized,
            "messages": messages,
            "count": len(messages),
            "source": source,
            "canal": canal,
            "has_more": has_more,
            "oldest_ts": oldest_ts,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Panel] Error obteniendo historial: {safe_error(e)}")
        raise HTTPException(status_code=500, detail=str(e))
@limiter.limit("60/minute")
async def get_history_by_contact_id(
    request: Request,
    contact_id: str,
    # ⚠️ 2026-06-02: default 100 (carga inicial rápida). Frontend pagina con before_ts.
    limit: int = Query(100, ge=1, le=500),
    canal: Optional[str] = Query(None, description="Canal de origen para filtrar mensajes"),
    phone: Optional[str] = Query(None, description="Teléfono para buscar historial"),
    advisor_id: Optional[str] = Query(None, description="ID de la asesora que abre el chat"),
    before_ts: Optional[str] = Query(
        None,
        description="Cursor ISO 8601 — retorna mensajes con timestamp < before_ts (paginación)"
    ),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Obtiene el historial de conversación por contact_id.

    Paginación cursor-based (2026-06-02):
    - Sin before_ts: trae últimos N mensajes (página inicial). HubSpot fallback
      activo si MongoDB tiene ≤20 mensajes.
    - Con before_ts: trae N mensajes con timestamp < before_ts. HubSpot fallback
      BLOQUEADO para evitar requests externos en cada scroll.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # Validar que contact_id sea numérico (ID de HubSpot)
    if not contact_id or not contact_id.isdigit():
        logger.warning(f"[Panel] contact_id inválido recibido: {safe_id(contact_id, 'contact')}")
        return JSONResponse(
            status_code=200,
            content={
                "contact_id": contact_id,
                "messages": [],
                "count": 0,
                "canal": canal,
                "error": "ID de contacto inválido (debe ser numérico)"
            }
        )

    await _assert_advisor_can_open_chat(
        advisor_id=advisor_id,
        phone=phone,
        contact_id=contact_id,
        canal=canal,
    )

    messages = []
    source = "none"

    # ⚠️ 2026-06-02: Parse del cursor before_ts
    before_ts_dt = None
    if before_ts:
        try:
            before_ts_dt = datetime.fromisoformat(before_ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return JSONResponse(
                status_code=400,
                content={"error": f"before_ts inválido: {before_ts}"}
            )

    # Guard HubSpot fallback: solo permitido en primera página (sin cursor).
    # En páginas paginadas evita 25+ requests a HubSpot durante un scroll continuo.
    hs_fallback_allowed = (before_ts_dt is None)

    try:
        mongo_manager = get_mongo_manager()

        # ⚠️ 2026-06-02: Lanzar HubSpot en background SOLO en primera página.
        # En páginas paginadas no llamamos HubSpot, así que no creamos la task.
        _hs_task = None
        if hs_fallback_allowed:
            _tl = get_timeline_logger()
            _hs_task = asyncio.create_task(
                _tl.get_notes_for_contact(contact_id=contact_id, limit=limit)
            )

        # =====================================================================
        # PASO 1: MongoDB por teléfono (preferido - más eficiente)
        # =====================================================================
        if phone:
            target = _resolve_panel_target(phone)
            if not target.error:
                # Sin filtro de canal: el canal con que el panel abre el chat
                # (canal_origen de HubSpot) puede diferir del canal con que se
                # guardan los mensajes (historical_channel de Redis).
                messages = await mongo_manager.get_history(
                    phone=target.key,
                    limit=limit,
                    channel=None,
                    before_ts=before_ts_dt,
                )
                if messages:
                    source = "mongodb"
                    logger.debug(f"[Panel] Historial desde MongoDB (phone): {len(messages)} msgs")

        # =====================================================================
        # PASO 2: MongoDB por contact_id (si no hay phone o no hay resultados)
        # =====================================================================
        if not messages:
            messages = await mongo_manager.get_history_by_contact_id(
                hubspot_contact_id=contact_id,
                limit=limit,
                before_ts=before_ts_dt,
            )
            if messages:
                source = "mongodb"
                logger.debug(f"[Panel] Historial desde MongoDB (contact_id): {len(messages)} msgs")

        # =====================================================================
        # PASO 3: HubSpot fallback — SOLO si es primera página (hs_fallback_allowed).
        # ⚠️ 2026-06-02: en paginación (before_ts presente) nunca tocamos HubSpot
        # para evitar 25+ requests externos durante scroll continuo.
        # =====================================================================
        hs_timeout = False  # P2-B: flag para indicar al frontend que HubSpot no respondió a tiempo
        if hs_fallback_allowed and _hs_task is not None and len(messages) <= 20:
            try:
                hs_messages = await asyncio.wait_for(_hs_task, timeout=15.0)
            except asyncio.TimeoutError:
                logger.warning(f"[Panel] HubSpot timeout (>15s) para contact_id={safe_id(contact_id, 'contact')} — historial puede ser parcial")
                hs_messages = []
                hs_timeout = True
            except Exception as hs_err:
                logger.warning(f"[Panel] HubSpot task falló: {hs_err}")
                hs_messages = []
            if len(hs_messages) > len(messages):
                logger.info(
                    f"[Panel] HubSpot supera MongoDB ({len(hs_messages)} vs "
                    f"{len(messages)} msgs) → usando HubSpot para contact_id={contact_id}"
                )
                messages = hs_messages
                source = "hubspot"
        elif _hs_task is not None:
            # MongoDB ya tiene suficientes o es paginación → cancelar HubSpot para ahorrar recursos
            _hs_task.cancel()

        # Asegurar que messages sea una lista válida
        if messages is None:
            messages = []

        # ⚠️ Metadata de paginación
        has_more = len(messages) == limit
        oldest_ts = None
        if messages:
            # messages viene ordenado cronológicamente (más antiguo primero) tras formatBubbles
            # → primer elemento es el más antiguo de la página actual.
            oldest_ts = messages[0].get("timestamp")

        canal_info = f", canal={canal}" if canal else ""
        page_info = f", before_ts={before_ts}" if before_ts else ""
        logger.info(
            f"[Panel] Historial cargado: {len(messages)} msgs para contact_id={contact_id}"
            f"{canal_info}{page_info} (source={source}, has_more={has_more})"
        )

        return {
            "contact_id": contact_id,
            "messages": messages,
            "count": len(messages),
            "canal": canal,
            "phone": phone,
            "source": source,
            "hs_timeout": hs_timeout,
            "has_more": has_more,
            "oldest_ts": oldest_ts,
        }

    except Exception as e:
        logger.error(f"[Panel] Error obteniendo historial para {safe_id(contact_id, 'contact')}: {safe_error(e)}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "contact_id": contact_id,
                "messages": [],
                "count": 0,
                "canal": canal,
                "source": "error",
                "error": f"Error interno: {str(e)}"
            }
        )
