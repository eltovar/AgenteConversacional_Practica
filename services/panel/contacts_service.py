"""Contacts service for the advisor panel.

Moved verbatim from `middleware.outbound_panel` — no behavior changes.
Rate limiting decorators travel with the endpoints.
Shared helpers (close/transfer/stage internals, hydrate, visibility cuts)
stay in the legacy module, where jobs, webhook and tests use them.
"""

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import Body, Form, Header, Query, Request, HTTPException
from fastapi.responses import JSONResponse

from logging_config import logger
from middleware import outbound_panel as _legacy
from middleware.outbound_panel import (
    CONTACTS_INFLIGHT_TTL,
    CONTACTS_PENDING_MAX,
    CONTACTS_RESPONSE_CACHE_TTL,
    HUBSPOT_STAGE_EN_CONVERSACION,
    PIPELINE_STAGES,
    STAGES_AUTO_CLOSE,
    STAGES_TRANSFER_TO_LUISA,
    _ContactsPerfTrace,
    _assert_advisor_can_open_chat,
    _auto_close_by_stage,
    _await_contacts_inflight,
    _build_contacts_page_after_dedupe,
    _build_zset_phone_index,
    _cache_contact_name,
    _cache_contact_stage,
    _contact_visible_for_advisor,
    _cuenta_como_prioridad,
    _dedupe_panel_contacts,
    _deshacer_etapa,
    _ensure_bsuid_hubspot_contact,
    _get_cached_contact_names_batch,
    _get_contact_lifecyclestage,
    _get_contact_manager,
    _get_contacts_by_worker_filter,
    _get_hubspot_contact_info,
    _get_redis_client,
    _get_state_manager,
    _hubspot_batch_get_contacts,
    _hubspot_post,
    _hydrate_contact,
    _hydrate_contact_and_ensure_panel,
    _invalidate_contact_name_cache,
    _invalidate_contacts_response_cache,
    _normalize_contact_phone_key,
    _nunca_se_corta,
    _publish_panel_contact_update,
    _release_contacts_inflight,
    _resolve_outbound_address,
    _resolve_panel_target,
    _resolve_pending_reply_phones,
    _split_always_visible,
    _transfer_to_luisa,
    _update_contact_to_visita_agendada,
    check_24h_window,
    get_bogota_now,
    get_httpx_client,
    get_mongo_manager,
    get_timeline_logger,
    limiter,
    twilio_client,
    ws_manager,
)
from middleware.phone_normalizer import PhoneNormalizer
from utils.safe_logging import safe_error, safe_id, safe_phone, safe_text


async def create_manual_contact(
    firstname: str = Form(..., description="Nombre del contacto"),
    phone: str = Form(..., description="Teléfono del contacto"),
    lastname: str = Form("", description="Apellido (opcional)"),
    property_type: Optional[str] = Form(None, description="Tipo de inmueble"),
    operation_type: Optional[str] = Form(None, description="Tipo de operación (compra/arriendo)"),
    budget: Optional[str] = Form(None, description="Presupuesto"),
    characteristics: Optional[str] = Form(None, description="Características adicionales"),
    canal: str = Form("whatsapp_directo", description="Canal de origen para asignación"),
    advisor_id: Optional[str] = Form(None, description="ID del asesor que crea el contacto (tiene prioridad sobre round-robin)"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Crea un contacto manualmente desde el panel de asesores.
    """
    logger.info(
        f"[Panel] POST /contacts/create - phone={safe_phone(phone)}, "
        f"firstname={safe_id(firstname, 'firstname')}, canal={canal}, "
        f"advisor_id={safe_id(advisor_id, 'advisor')}"
    )

    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # === 1. Normalizar teléfono ===
    normalizer = PhoneNormalizer()
    validation = normalizer.normalize(phone)
    if not validation.is_valid:
        raise HTTPException(
            status_code=400,
            detail=f"Número de teléfono inválido: {phone}"
        )
    phone_normalized = validation.normalized

    logger.info(f"[Panel] Teléfono normalizado: {safe_phone(phone_normalized)}")

    # === 2. Verificar si el contacto ya existe (deduplicación) ===
    import httpx
    hubspot_api_key = os.getenv("HUBSPOT_API_KEY")
    if not hubspot_api_key:
        raise HTTPException(status_code=500, detail="HUBSPOT_API_KEY no configurada")

    # Buscar por whatsapp_id (identificador único)
    search_url = "https://api.hubapi.com/crm/v3/objects/contacts/batch/read"
    search_payload = {
        "properties": ["id", "firstname", "lastname", "phone", "hubspot_owner_id"],
        "idProperty": "whatsapp_id",
        "inputs": [{"id": phone_normalized}]
    }

    try:
        client = get_httpx_client()
        response = await _hubspot_post(client, search_url, search_payload, hubspot_api_key)

        if response.status_code == 200:
            results = response.json().get("results", [])
            if results:
                # Contacto ya existe — enriquecer respuesta con owner, historial y estado Redis
                existing = results[0]
                existing_id = existing.get("id")
                existing_props = existing.get("properties", {})
                _fn = existing_props.get('firstname') or ''
                _ln = existing_props.get('lastname') or ''
                existing_name = f"{_fn} {_ln}".strip()
                existing_owner_id = existing_props.get("hubspot_owner_id") or ""

                logger.warning(f"[Panel] Contacto ya existe: {safe_id(existing_id, 'contact')} ({safe_text(existing_name, 40)})")

                # Resolver nombre del asesor desde OWNERS_CONFIG (lookup local, sin IO)
                from integrations.hubspot.lead_assigner import LeadAssigner
                existing_owner_name = "Sin asignar"
                if existing_owner_id:
                    for _team_members in LeadAssigner.OWNERS_CONFIG.values():
                        for _m in _team_members:
                            if str(_m.get("id")) == existing_owner_id:
                                existing_owner_name = _m.get("name", "Sin asignar")
                                break

                # Contar mensajes en MongoDB (sin bloquear si falla)
                try:
                    message_count = await get_mongo_manager().get_message_count(phone_normalized)
                except Exception:
                    message_count = 0

                # Detectar canal activo en Redis (si el contacto está en el panel)
                redis_canal = None
                try:
                    _rc = await _get_redis_client()
                    _meta_keys = await _rc.keys(f"conv_meta:{phone_normalized}:*")
                    if _meta_keys:
                        redis_canal = _meta_keys[0].split(":")[-1]
                except Exception:
                    pass

                # "Tomar Control" directo si el contacto NO está activo en ningún panel (redis_canal=None).
                # Solo se pide permiso ("Solicitar Transferencia") si alguien lo tiene activo en el panel.
                can_take_control = not (existing_owner_id and message_count > 0 and redis_canal)

                return JSONResponse(
                    status_code=409,
                    content={
                        "status": "exists",
                        "contact_id": existing_id,
                        "phone": phone_normalized,
                        "display_name": existing_name or "Sin nombre",
                        "owner_id": existing_owner_id,
                        "owner_name": existing_owner_name,
                        "message_count": message_count,
                        "redis_canal": redis_canal,
                        "can_take_control": can_take_control,
                    }
                )
    except Exception as e:
        logger.error(f"[Panel] Error buscando contacto existente: {safe_error(e)}")
        # Continuar con la creación si falla la búsqueda

    # === 3. Determinar owner_id ===
    # Si el asesor crea el contacto manualmente desde SU panel, usarlo directamente.
    # Solo se usa round-robin (LeadAssigner) cuando la creación es automática/API.
    if advisor_id:
        owner_id = advisor_id
        logger.info(f"[Panel] Contacto asignado directamente al asesor creador: {safe_id(advisor_id, 'advisor')}")
    else:
        from integrations.hubspot.lead_assigner import lead_assigner
        owner_id = await asyncio.to_thread(lead_assigner.get_next_owner, canal)
        if not owner_id:
            logger.warning(f"[Panel] No se pudo asignar owner para canal: {canal}")
        else:
            logger.info(f"[Panel] Contacto asignado por round-robin (canal={canal}): {safe_id(owner_id, 'owner')}")

    # === 4. Crear contacto en HubSpot ===
    from datetime import timezone as tz
    midnight_utc = datetime.now(tz.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Mapeo de canales internos → valores permitidos por HubSpot (enum fijo)
    _HUBSPOT_CANAL_MAP = {
        "charly": "pagina_web",  # Chatling se registra como pagina_web en HubSpot
    }
    canal_hs = _HUBSPOT_CANAL_MAP.get(canal, canal)

    # Solo propiedades estándar de HubSpot (siempre existen)
    contact_properties = {
        "whatsapp_id": phone_normalized,
        "phone": phone_normalized,
        "firstname": firstname.strip(),
        "lastname": lastname.strip() if lastname else "",
        "canal_origen": canal_hs,
        "chatbot_timestamp": str(int(midnight_utc.timestamp() * 1000)),
        "lifecyclestage": HUBSPOT_STAGE_EN_CONVERSACION,
    }

    # Agregar owner si está disponible
    if owner_id:
        contact_properties["hubspot_owner_id"] = owner_id

    # NOTA: tipo_inmueble, tipo_operacion, presupuesto, caracteristicas
    # se guardan en el Deal (description) en lugar del contacto
    # porque son propiedades custom que pueden no existir en HubSpot

    create_url = "https://api.hubapi.com/crm/v3/objects/contacts"

    try:
        client = get_httpx_client()
        response = await _hubspot_post(
            client, create_url, {"properties": contact_properties}, hubspot_api_key
        )

        if response.status_code in [200, 201]:
            contact_data = response.json()
            contact_id = contact_data.get("id")
            logger.info(f"[Panel] Contacto creado exitosamente: {safe_id(contact_id, 'contact')}")
        elif response.status_code == 409:
            # Conflicto - contacto ya existe (race condition)
            logger.warning(f"[Panel] Conflicto 409 al crear contacto: {safe_error(response.text, 200)}")
            raise HTTPException(
                status_code=409,
                detail="El contacto ya existe. Por favor busca en el panel."
            )
        else:
            logger.error(f"[Panel] Error creando contacto: {response.status_code} - {safe_error(response.text, 200)}")
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Error de HubSpot: {response.text}"
            )

    except httpx.HTTPError as e:
        logger.error(f"[Panel] Error HTTP creando contacto: {safe_error(e)}")
        raise HTTPException(status_code=500, detail=f"Error de conexión: {str(e)}")

    # === 5.5. Escribir url_chat en Contacto ===
    try:
        panel_base_url = os.getenv("PANEL_BASE_URL", "").rstrip("/")
        admin_api_key = os.getenv("ADMIN_API_KEY", "")
        if panel_base_url and owner_id and admin_api_key:
            from urllib.parse import quote
            phone_encoded = quote(phone_normalized, safe='')
            url_chat = f"{panel_base_url}/whatsapp/panel/?key={admin_api_key}&advisor={owner_id}&phone={phone_encoded}"
            
            # Escribir en contacto
            client = get_httpx_client()
            contact_patch_url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
            await client.patch(
                contact_patch_url,
                headers={"Authorization": f"Bearer {hubspot_api_key}", "Content-Type": "application/json"},
                json={"properties": {"url_chat": url_chat}}
            )
            logger.info(f"[Panel] url_chat escrito en contacto {safe_id(contact_id, 'contact')}")
    except Exception as e:
        logger.warning(f"[Panel] Error escribiendo url_chat (no crítico): {safe_error(e)}")

    # === 6. Activar HUMAN_ACTIVE en Redis ===
    try:
        state_manager = _get_state_manager()

        display_name = f"{firstname} {lastname}".strip()

        await state_manager.activate_human(
            phone_normalized=phone_normalized,
            canal_origen=canal,
            owner_id=owner_id,
            reason="Creado manualmente desde panel",
            display_name=display_name,
            contact_id=contact_id
        )

        logger.info(f"[Panel] HUMAN_ACTIVE activado para {safe_phone(phone_normalized)}")

        # Escribir clave inversa phone_cache:{contact_id} → phone para que
        # update_contact_name pueda resolver el teléfono desde el contact_id
        try:
            _rc = await _get_redis_client()
            await _rc.set(f"phone_cache:{contact_id}", phone_normalized, ex=86400)
        except Exception:
            pass

    except Exception as e:
        logger.error(f"[Panel] Error activando HUMAN_ACTIVE: {safe_error(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=(
                f"Contacto creado en HubSpot (ID: {contact_id}) pero no se pudo registrar "
                f"en el panel. Usa restore-panel o vuelve a intentarlo. Error: {e}"
            )
        )

    return {
        "status": "success",
        "message": f"Contacto '{firstname}' creado exitosamente",
        "contact_id": contact_id,
        "phone": phone_normalized,
        "display_name": f"{firstname} {lastname}".strip(),
        "owner_id": owner_id
    }


# ============================================================================
# Endpoint para TRANSFERIR contacto a otra asesora
# ============================================================================

# TRANSFER — ver services/panel/transfer_service.py


# ============================================================================
# Helper para resolver nombre de asesor desde OWNERS_CONFIG
# ============================================================================
async def update_contact_name(
    contact_id: str,
    firstname: str = Form(..., description="Nombre del contacto"),
    lastname: str = Form("", description="Apellido del contacto (opcional)"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Actualiza el nombre del contacto en HubSpot.

    Permite a los asesores corregir nombres de contactos directamente
    desde el panel sin ir a HubSpot.
    """
    logger.info(f"[Panel] PATCH nombre - contact_id={safe_id(contact_id, 'contact')}, firstname={safe_id(firstname, 'firstname')}, lastname={safe_id(lastname, 'lastname')}")

    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # Validar contact_id
    if not contact_id or contact_id == "null" or contact_id == "undefined":
        logger.error(f"[Panel] contact_id inválido: {safe_id(contact_id, 'contact')}")
        raise HTTPException(status_code=400, detail="ID de contacto inválido")

    # Validar que sea numérico (IDs de HubSpot son numéricos)
    try:
        int(contact_id)
    except ValueError:
        logger.error(f"[Panel] contact_id no es numérico: {safe_id(contact_id, 'contact')}")
        raise HTTPException(status_code=400, detail="ID de contacto debe ser numérico")

    hubspot_api_key = os.getenv("HUBSPOT_API_KEY")
    if not hubspot_api_key:
        logger.error("[Panel] HUBSPOT_API_KEY no configurada")
        raise HTTPException(status_code=500, detail="HUBSPOT_API_KEY no configurada")

    url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
    payload = {
        "properties": {
            "firstname": firstname.strip(),
            "lastname": lastname.strip()
        }
    }

    logger.debug(f"[Panel] Enviando PATCH a HubSpot: {safe_url(url)}")

    try:
        response = await _legacy._hubspot_patch(url, payload, hubspot_api_key)

        logger.info(f"[Panel] Respuesta HubSpot: {response.status_code}")

        if response.status_code == 200:
            logger.info(f"[Panel] Nombre actualizado para contacto {safe_id(contact_id, 'contact')}: {safe_id(firstname, 'firstname')} {safe_id(lastname, 'lastname')}")
            # Sincronizar display_name en Redis para todos los canales del contacto
            try:
                _rc = await _get_redis_client()
                _phone = await _rc.get(f"phone_cache:{contact_id}")
                if _phone:
                    _display = f"{firstname} {lastname}".strip()
                    _meta_keys = await _rc.keys(f"conv_meta:{_phone}:*")
                    for _meta_key in _meta_keys:
                        _raw = await _rc.get(_meta_key)
                        if _raw:
                            try:
                                _meta = json.loads(_raw)
                                _meta["display_name"] = _display
                                await _rc.set(_meta_key, json.dumps(_meta))
                            except (json.JSONDecodeError, Exception):
                                pass
                    logger.info(f"[Panel] display_name {safe_id(_display, 'display')} sincronizado en Redis para {safe_phone(_phone)}")
            except Exception as redis_err:
                logger.warning(f"[Panel] No se pudo actualizar display_name en Redis: {redis_err}")
            # El renombrado va por _hubspot_patch, no por hubspot_client.update_contact,
            # así que no lo cubre el hook: se refresca el caché aquí con el mismo
            # escritor autoritativo. Deja el nombre nuevo en vez de borrar, de modo
            # que el siguiente poll no tenga que preguntárselo a HubSpot.
            await _invalidate_contact_name_cache(
                contact_id, firstname=firstname.strip(), lastname=lastname.strip()
            )
            # Notificar a todos los paneles para que actualicen el nombre sin esperar poll
            try:
                _rc_ws = await _get_redis_client()
                _phone_ws = await _rc_ws.get(f"phone_cache:{contact_id}")
                await ws_manager.publish_broadcast(_rc_ws, {
                    "type": "contact_updated",
                    "phone": str(_phone_ws or ""),
                    "action": "name_updated",
                    "display_name": f"{firstname} {lastname}".strip()
                })
            except Exception:
                pass
            return {
                "status": "success",
                "message": "Nombre actualizado correctamente",
                "contact_id": contact_id,
                "firstname": firstname,
                "lastname": lastname,
                "display_name": f"{firstname} {lastname}".strip()
            }
        elif response.status_code == 404:
            logger.warning(f"[Panel] Contacto no encontrado en HubSpot: {safe_id(contact_id, 'contact')}")
            raise HTTPException(
                status_code=404,
                detail="Contacto no encontrado en HubSpot"
            )
        else:
            logger.error(f"[Panel] Error actualizando nombre: {response.status_code} - {safe_error(response.text, 200)}")
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Error de HubSpot: {response.text[:200]}"
            )

    except httpx.TimeoutException:
        logger.error(f"[Panel] Timeout actualizando nombre para {safe_id(contact_id, 'contact')}")
        raise HTTPException(status_code=504, detail="Timeout conectando con HubSpot")
    except HTTPException:
        raise  # Re-raise HTTPExceptions sin modificar
    except Exception as e:
        logger.error(f"[Panel] Error inesperado actualizando nombre: {safe_error(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")


async def close_conversation(
    phone: str,
    canal: Optional[str] = None,
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Cierra una conversación transicionando a BOT_ACTIVE.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        result = await _legacy._close_conversation_internal(phone, canal)
        await _publish_panel_contact_update(
            phone=result.get("phone"),
            canal=result.get("canal") or canal,
            action="closed",
            advisor_id=result.get("advisor_id"),
            new_status=result.get("new_status"),
        )
        return {
            "status": "success",
            "message": "Conversación cerrada - Sofía retomará automáticamente",
            **result,
        }
    except Exception as e:
        logger.error(f"[Panel] Error cerrando conversación: {safe_error(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Endpoint: Marcar contacto como leído (borra del advisor_inbox en Redis)
# ============================================================================
async def mark_contact_read(
    phone: str,
    advisor_id: Optional[str] = Query(None, description="ID del asesor que leyó el contacto (se infiere del meta si no se provee)"),
    canal: Optional[str] = Query(None, description="Canal del contacto (opcional)"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Registra que el asesor abrió un contacto, removiéndolo de su advisor_inbox en Redis.
    Llamado desde el frontend al hacer click en un contacto (fire-and-forget).
    Si advisor_id no se provee, se infiere de ConversationMeta.assigned_owner_id.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    try:
        normalizer = PhoneNormalizer()
        validation = normalizer.normalize(phone)
        phone_norm = validation.normalized if validation.is_valid else phone
        sm = _get_state_manager()
        # Si no se recibe advisor_id, inferirlo del meta del contacto
        if not advisor_id:
            _meta = await sm.get_meta(phone_norm, canal or "whatsapp")
            if _meta:
                advisor_id = _meta.assigned_owner_id
        if advisor_id:
            await sm.remove_from_advisor_inbox(advisor_id, phone_norm, canal)
        return {"status": "ok", "phone": phone_norm}
    except Exception as e:
        logger.warning(f"[Panel][Inbox] mark_contact_read error (non-fatal): {safe_error(e)}")
        return {"status": "error"}


# ============================================================================
# Helper: Transferencia automática a Luisa por embudo
# Soporta: Seguimiento, Hasta 1.5M, Hasta 2M, Hasta 2.5M
# ============================================================================
async def update_contact_stage(
    contact_id: str,
    stage_id: str = Body(..., embed=True),
    phone: Optional[str] = Body(None, embed=True),
    canal: Optional[str] = Body(None, embed=True),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Actualiza la etapa (lifecyclestage) del Contact en HubSpot desde el panel de asesores.
    Arquitectura Contact-Centric: no depende de objetos Deal.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    if stage_id not in PIPELINE_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"stage_id inválido. Valores permitidos: {list(PIPELINE_STAGES.keys())}"
        )

    hubspot_token = os.getenv("HUBSPOT_API_KEY")
    if not hubspot_token:
        raise HTTPException(status_code=500, detail="HUBSPOT_API_KEY no configurada")

    try:
        url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"

        # Two-step update: HubSpot lifecyclestage es unidireccional — solo permite avanzar.
        # Limpiar primero elimina esa restricción y permite mover el stage en cualquier dirección.
        # Ambos steps usan _hubspot_patch() que reintenta automáticamente en 429.
        # Step 1: Clear
        await _legacy._hubspot_patch(url, {"properties": {"lifecyclestage": ""}}, hubspot_token)
        # Step 2: Set
        response = await _legacy._hubspot_patch(url, {"properties": {"lifecyclestage": stage_id}}, hubspot_token)

        if response.status_code == 200:
            stage_name = PIPELINE_STAGES.get(stage_id, stage_id)
            logger.info(f"[Panel] Contacto {safe_id(contact_id, 'contact')} actualizado a etapa '{stage_name}'")
            await _legacy._invalidate_contact_stage_cache(contact_id, stage_id)
            await _invalidate_contacts_response_cache("stage_update")

            payload = {
                "status": "success",
                "message": f"Etapa actualizada a '{stage_name}'",
                "contact_id": contact_id,
                "stage_id": stage_id,
                "stage_name": stage_name,
                "closed": False,
            }

            if stage_id in STAGES_TRANSFER_TO_LUISA and phone:
                try:
                    transfer_result = await _transfer_to_luisa(phone, canal, contact_id, stage_id)
                    payload["closed"] = transfer_result.get("closed", False)
                    payload["transferred"] = transfer_result.get("transferred", False)
                    payload["to_owner"] = transfer_result.get("to_owner")
                    if transfer_result.get("transfer_error"):
                        payload["transfer_error"] = transfer_result["transfer_error"]
                    stage_label = STAGES_TRANSFER_TO_LUISA.get(stage_id, stage_id)
                    logger.info(
                        f"[Panel] Transfer {stage_label} — contact={contact_id} phone={phone} "
                        f"closed={payload['closed']} transferred={payload['transferred']}"
                    )
                except Exception as e_seg:
                    logger.error(
                        f"[Panel] Transfer falló para contact={contact_id} stage={stage_id}: {e_seg}"
                    )
                    payload["transfer_error"] = str(e_seg)

            elif stage_id in STAGES_AUTO_CLOSE and phone:
                try:
                    close_result = await _auto_close_by_stage(phone, canal, stage_id)
                    payload["closed"] = close_result.get("closed", False)
                    if close_result.get("close_error"):
                        payload["close_error"] = close_result["close_error"]
                    logger.info(
                        f"[Panel] AutoClose {STAGES_AUTO_CLOSE[stage_id]} — "
                        f"contact={contact_id} phone={phone} closed={payload['closed']}"
                    )
                except Exception as e_close:
                    logger.error(
                        f"[Panel] AutoClose falló para contact={contact_id} stage={stage_id}: {e_close}"
                    )
                    payload["close_error"] = str(e_close)

            if phone and not payload.get("closed") and not payload.get("transferred"):
                _stage_owner_id = None
                try:
                    _stage_meta = await _get_state_manager().get_meta(phone, canal or "whatsapp")
                    _stage_owner_id = _stage_meta.assigned_owner_id if _stage_meta else None
                except Exception:
                    _stage_owner_id = None
                await _publish_panel_contact_update(
                    phone=phone,
                    canal=canal,
                    action="stage_updated",
                    advisor_id=_stage_owner_id,
                    contact_id=contact_id,
                    stage_id=stage_id,
                    stage_name=stage_name,
                    current_stage=stage_id,
                )

            return payload
        else:
            logger.error(f"[Panel] Error actualizando lifecyclestage: {response.status_code} - {safe_error(response.text, 200)}")
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Error actualizando etapa: {response.text[:200]}"
            )

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Timeout conectando con HubSpot")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Panel] Error inesperado actualizando etapa: {safe_error(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")


# ─── Canales válidos para edición manual de canal_display ────────────────────
_CANALES_DISPLAY_VALIDOS = [
    "whatsapp", "whatsapp_directo",
    "instagram", "facebook", "linkedin", "youtube", "tiktok",
    "finca_raiz", "metrocuadrado", "mercado_libre", "ciencuadras",
    "pagina_web", "desconocido",
]


async def update_contact_canal_display(
    phone: str,
    canal_display: str = Body(..., embed=True),
    canal: Optional[str] = Body(None, embed=True),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Actualiza el canal visible (canal_display) de un contacto.
    canal_display es solo para UI y métricas — NO cambia canal_origen ni el routing.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    canal_display_clean = canal_display.lower().strip()
    if canal_display_clean not in _CANALES_DISPLAY_VALIDOS:
        raise HTTPException(
            status_code=400,
            detail=f"canal_display inválido. Valores permitidos: {_CANALES_DISPLAY_VALIDOS}"
        )

    try:
        normalizer = PhoneNormalizer()
        validation = normalizer.normalize(phone)
        if not validation.is_valid:
            raise HTTPException(status_code=400, detail=f"Número inválido: {validation.error_message}")
        phone_normalized = validation.normalized

        state_manager = _get_state_manager()
        canal_meta = (canal or "whatsapp").lower().strip()
        meta_key = f"{state_manager.META_PREFIX}{phone_normalized}:{canal_meta}"

        raw = await state_manager.redis.get(meta_key)
        if not raw:
            raise HTTPException(status_code=404, detail="Contacto no encontrado en Redis")

        meta_dict = json.loads(raw)
        meta_dict["canal_display"] = canal_display_clean
        await state_manager.redis.set(meta_key, json.dumps(meta_dict))

        logger.info(f"[Panel] canal_display actualizado: {safe_phone(phone_normalized)}:{canal_meta} → {canal_display_clean}")
        return {"status": "success", "phone": phone_normalized, "canal_display": canal_display_clean}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Panel] Error actualizando canal_display: {safe_error(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")


# ─── Notificaciones del asesor ────────────────────────────────────────────────

# NOTIFICATIONS — ver services/panel/notifications_service.py


# ver services/panel/ (extraido sin cambios)


# ver services/panel/ (extraido sin cambios)
async def get_contact_detail(
    phone: str,
    contact_id: Optional[str] = Query(None, description="ID del contacto en HubSpot"),
    canal: Optional[str] = Query(None, description="Canal de origen para filtrar mensajes"),
    advisor_id: Optional[str] = Query(None, description="ID de la asesora que abre el chat"),
    limit: int = Query(50, ge=1, le=100),
    # ⚠️ 2026-06-05: cursor para que el panel pueda paginar con scroll infinito.
    # Si llega, NO se consulta HubSpot (mismo guard que /history/{cid}).
    before_ts: Optional[str] = Query(
        None,
        description="Cursor ISO 8601 — retorna mensajes con timestamp < before_ts"
    ),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Endpoint combinado: devuelve en un solo round-trip los datos necesarios para
    abrir el chat de un contacto — historial de mensajes + estado de ventana 24h.

    Reemplaza las 2 llamadas paralelas GET /history/{id} + GET /window-status/{phone}
    que se hacen en selectContact().
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    target = _resolve_panel_target(phone)
    if target.error:
        raise HTTPException(status_code=400, detail=target.error)

    phone_normalized = target.key
    if target.is_bsuid and not contact_id:
        try:
            contact_id = await asyncio.wait_for(
                _ensure_bsuid_hubspot_contact(target, canal or "whatsapp"),
                timeout=3.0,
            )
        except Exception as e:
            logger.warning(f"[Panel][Detail][BSUID] No se pudo asegurar contacto HubSpot: {safe_error(e)}")

    await _assert_advisor_can_open_chat(
        advisor_id=advisor_id,
        phone=phone_normalized,
        contact_id=contact_id,
        canal=canal,
    )

    mongo_manager = get_mongo_manager()

    # ⚠️ 2026-06-05: Parse del cursor before_ts (igual que /history/{cid})
    before_ts_dt = None
    if before_ts:
        try:
            before_ts_dt = datetime.fromisoformat(before_ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail=f"before_ts inválido: {before_ts}")
    # En paginación nunca consultar HubSpot — solo Mongo (evita N requests externos)
    hs_fallback_allowed = (before_ts_dt is None)

    async def _get_messages():
        try:
            # Paso 1: MongoDB por teléfono (sin filtro de canal).
            # Antes filtraba por canal, pero el canal con que el panel abre el chat
            # (canal_origen de HubSpot) puede diferir del canal con que se guardan los
            # mensajes (historical_channel de Redis), causando mensajes invisibles.
            mongo_msgs = await mongo_manager.get_history(
                phone=phone_normalized,
                limit=limit,
                channel=None,
                before_ts=before_ts_dt,
            )

            # Si MongoDB ya tiene historial completo (>20) → retornar sin consultar HubSpot
            if len(mongo_msgs) > 20:
                return mongo_msgs, "mongodb"

            # Pasos 2 + 3 en PARALELO: MongoDB por contact_id y HubSpot simultáneamente.
            # SIEMPRE se ejecuta cuando mongo_msgs ≤ 20, incluso si Paso 1 devolvió algo,
            # para evitar mostrar historial parcial (ej: 1 mensaje outbound vs 15 en HubSpot).
            # ⚠️ Pero NO en paginación (hs_fallback_allowed=False) — solo Mongo.
            if contact_id and contact_id.isdigit() and hs_fallback_allowed:
                _tl = get_timeline_logger()
                mongo2_msgs, hs_messages = await asyncio.gather(
                    mongo_manager.get_history_by_contact_id(
                        hubspot_contact_id=contact_id,
                        limit=limit,
                        before_ts=before_ts_dt,
                    ),
                    _tl.get_notes_for_contact(
                        contact_id=contact_id,
                        limit=limit
                    ),
                    return_exceptions=True,
                )
                if isinstance(mongo2_msgs, Exception):
                    logger.warning(f"[Panel] MongoDB contact_id falló: {safe_error(mongo2_msgs)}")
                    mongo2_msgs = []
                if isinstance(hs_messages, Exception):
                    logger.warning(f"[Panel] HubSpot falló: {hs_messages}")
                    hs_messages = []

                # Mejor resultado de MongoDB entre Paso 1 y Paso 2
                best_mongo = mongo_msgs if len(mongo_msgs) >= len(mongo2_msgs) else mongo2_msgs

                if len(hs_messages) > len(best_mongo):
                    logger.info(
                        f"[Panel] HubSpot supera MongoDB ({len(hs_messages)} vs "
                        f"{len(best_mongo)} msgs) → usando HubSpot para {phone_normalized}"
                    )
                    return hs_messages, "hubspot"

                if best_mongo:
                    return best_mongo, "mongodb"

            elif mongo_msgs:
                return mongo_msgs, "mongodb"

        except Exception as e:
            logger.error(f"[Panel] Error obteniendo mensajes en detail: {safe_error(e)}")
        return [], "none"

    # Ejecutar historial + window-status en paralelo (1 round-trip combinado)
    (messages, source), window_status = await asyncio.gather(
        _get_messages(),
        check_24h_window(phone_normalized),
    )

    # ⚠️ 2026-06-05: Metadata para que el panel pueda paginar con scroll infinito.
    # has_more=True si la página llegó al límite (puede haber más antes).
    # oldest_ts = timestamp del primer mensaje (cronológicamente más antiguo).
    # Si source='hubspot', has_more=False — HubSpot ya retorna todo lo disponible
    # y la paginación cursor solo funciona contra MongoDB.
    has_more = len(messages) >= limit and source == "mongodb"
    oldest_ts = messages[0].get("timestamp") if messages else None

    # Identidad: el panel necesita saber si este contacto tiene teléfono real
    # para decidir si muestra el número o el botón de agregarlo.
    _detail_meta = None
    try:
        _detail_meta = await _get_state_manager().get_meta(
            phone_normalized, canal or "whatsapp"
        )
    except Exception as _det_err:
        logger.debug(f"[Panel][Detail] meta de identidad no legible: {safe_error(_det_err)}")

    return {
        "phone": phone_normalized,
        "contact_id": contact_id,
        "canal": canal,
        # Identidad
        "identity_type": getattr(_detail_meta, "identity_type", None) or "phone",
        "has_phone": (
            bool(getattr(_detail_meta, "has_phone", True))
            if _detail_meta else not target.is_bsuid
        ),
        "routing_address": getattr(_detail_meta, "routing_address", None),
        # Historial
        "messages": messages,
        "message_count": len(messages),
        "message_source": source,
        "has_more": has_more,
        "oldest_ts": oldest_ts,
        # Ventana 24h
        "window_open": window_status.is_open,
        "last_message_time": window_status.last_message_time.isoformat() if window_status.last_message_time else None,
        "time_remaining_seconds": window_status.time_remaining_seconds,
        "requires_template": window_status.requires_template,
        "window_message": window_status.message,
    }
async def hydrate_contact_endpoint(
    phone: str,
    canal: Optional[str] = Query(None, description="Canal preferido (hint) si se conoce"),
    ensure_panel: bool = Query(False, description="Si True, re-inyecta el contacto al panel (ZSET)"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Retorna un contacto completamente hidratado desde Redis + HubSpot + MongoDB.

    Complementa /contacts/{phone}/detail (historial + ventana 24h) — este endpoint
    retorna SOLO metadatos completos del contacto (nombre, owner, stage, canal, etc.)
    y NO filtra por advisor (flujo cross-advisor correcto).

    Usado por el frontend en deep links de HubSpot y recuperación de estado ghost.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    hydrated = await _hydrate_contact(
        phone=phone,
        canal_hint=canal,
        add_to_zset=bool(ensure_panel),
    )
    if hydrated is None:
        raise HTTPException(status_code=404, detail="contacto no encontrado")
    return hydrated


# CONVERSATIONS — ver services/panel/conversations_service.py


# CONVERSATIONS — ver services/panel/conversations_service.py
async def take_control_of_conversation(
    phone: str,
    canal: Optional[str] = Query(None, description="Canal de origen"),
    contact_id: Optional[str] = Query(None, description="ID del contacto en HubSpot"),
    advisor_id: Optional[str] = Query(None, description="ID de la asesora que toma control"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Activa HUMAN_ACTIVE cuando la asesora hace click en un contacto.

    Este endpoint debe llamarse ANTES de cargar el historial para asegurar
    que Sofía no responda mientras la asesora está revisando la conversación.

    IMPORTANTE: Resuelve el bug donde Sofía seguía respondiendo porque
    HUMAN_ACTIVE solo se activaba al ENVIAR un mensaje, no al seleccionar.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    target = _resolve_panel_target(phone)
    phone_normalized = target.key if not target.error else phone

    try:
        state_manager = _get_state_manager()

        # Verificar estado actual
        current_status = await state_manager.get_status(phone_normalized, canal or "whatsapp")

        # Si ya está en HUMAN_ACTIVE o IN_CONVERSATION, solo refrescar TTL
        if current_status in [ConversationStatus.HUMAN_ACTIVE, ConversationStatus.IN_CONVERSATION]:
            # Refrescar TTL sin cambiar estado
            await state_manager.set_status(
                phone_normalized,
                current_status,
                canal=canal or "whatsapp",
                ttl=state_manager.HUMAN_PANEL_STATE_TTL
            )
            logger.info(
                f"[Panel] Take Control: TTL refrescado para {phone_normalized}:{canal} "
                f"(estado: {current_status.value})"
            )
            return {
                "status": "success",
                "action": "ttl_refreshed",
                "phone": phone_normalized,
                "canal": canal,
                "current_status": current_status.value,
                "message": "TTL de sesión refrescado"
            }

        # Si está en PENDING_HANDOFF, cambiar a HUMAN_ACTIVE
        if current_status == ConversationStatus.PENDING_HANDOFF:
            await state_manager.activate_human(
                phone_normalized=phone_normalized,
                canal_origen=canal or "whatsapp",
                owner_id=advisor_id,
                contact_id=contact_id,
                reason="Asesora tomó control desde panel"
            )
            logger.info(
                f"[Panel] Take Control: PENDING_HANDOFF -> HUMAN_ACTIVE para {phone_normalized}:{canal}"
            )
            return {
                "status": "success",
                "action": "human_activated",
                "phone": phone_normalized,
                "canal": canal,
                "previous_status": "PENDING_HANDOFF",
                "new_status": "HUMAN_ACTIVE",
                "message": "Control tomado - Sofía pausada"
            }

        # Si era BOT_ACTIVE o no existía, activar HUMAN_ACTIVE
        # Preservar owner existente: take-control pausa SofIA, NO reasigna propiedad.
        # Sin este guard, click de asesora X en contacto de asesora Y sobrescribe el owner.
        effective_owner = advisor_id
        meta_key = f"conv_meta:{phone_normalized}:{canal or 'whatsapp'}"
        try:
            _existing_raw = await state_manager.redis.get(meta_key)
            if _existing_raw:
                _existing_owner = json.loads(_existing_raw).get("assigned_owner_id")
                if _existing_owner and _existing_owner != advisor_id:
                    effective_owner = _existing_owner
                    logger.info(
                        f"[Panel] Take Control: preservando owner existente "
                        f"{_existing_owner} (click de {advisor_id}) para {phone_normalized}"
                    )
        except Exception:
            pass

        await state_manager.activate_human(
            phone_normalized=phone_normalized,
            canal_origen=canal or "whatsapp",
            owner_id=effective_owner,
            contact_id=contact_id,
            reason="Asesora seleccionó contacto en panel"
        )

        previous_status = current_status.value if current_status else "BOT_ACTIVE"
        logger.info(
            f"[Panel] Take Control: {previous_status} -> HUMAN_ACTIVE para {phone_normalized}:{canal}"
        )

        return {
            "status": "success",
            "action": "human_activated",
            "phone": phone_normalized,
            "canal": canal,
            "advisor_id": advisor_id,
            "previous_status": previous_status,
            "new_status": "HUMAN_ACTIVE",
            "message": "Control tomado - Sofía pausada"
        }

    except Exception as e:
        logger.error(f"[Panel] Error en take-control: {safe_error(e)}")
        raise HTTPException(status_code=500, detail=str(e))
async def search_contacts_by_keyword(
    q: str = Query(..., min_length=2, max_length=100, description="Palabra clave a buscar"),
    limit: int = Query(20, ge=1, le=50),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Busca contactos por palabra clave en el historial de mensajes.
    
    Usa búsqueda fulltext de MongoDB para encontrar mensajes que contengan
    la palabra buscada y retorna los contactos asociados.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    try:
        logger.info(f"[Panel] Búsqueda por palabra clave: '{q}'")

        # ⚠️ Usar SIEMPRE el singleton. Instanciar MongoDBManager() aquí creaba un
        # AsyncIOMotorClient nuevo (maxPoolSize=20) por cada request de búsqueda,
        # que además re-ejecutaba _ensure_indexes() (24 createIndex) y nunca se
        # cerraba → memory leak + latencia de handshake en cada tecleo.
        mongo_manager = get_mongo_manager()
        matching_phones = await mongo_manager.search_messages_fulltext(q, limit=limit)

        logger.info(f"[Panel] Búsqueda {safe_text(q, 60)}: {len(matching_phones)} contactos encontrados")

        # Enriquecimiento UNIFICADO via _hydrate_contact — garantiza que cada contacto
        # venga con owner_id, owner_name, canal, current_stage, etc. (mismos campos que
        # GET /contacts). Rate-limit con Semaphore(3) para no saturar HubSpot.
        enriched_contacts = []
        if matching_phones:
            _sem = asyncio.Semaphore(3)

            # ⚠️ Índice del ZSET UNA vez para todos los teléfonos. Sin esto cada
            # _hydrate_contact hacía su propio ZSCAN con cursor sobre el ZSET
            # completo: medido en producción, 39,5s de los 41,6s del endpoint.
            _zset_index = await _build_zset_phone_index()

            async def _h(_p: str):
                async with _sem:
                    try:
                        return await _hydrate_contact(
                            _p,
                            canal_hint=None,
                            add_to_zset=False,
                            zset_index=_zset_index,
                        )
                    except Exception as _he:
                        logger.debug(f"[Panel][Search] Hydrate {_p} falló: {_he}")
                        return None

            hydrated_list = await asyncio.gather(
                *[_h(p) for p in matching_phones[:limit]],
                return_exceptions=False,
            )
            enriched_contacts = [c for c in hydrated_list if c]

        return {
            "query": q,
            "count": len(matching_phones),
            "phones": matching_phones,
            "contacts": enriched_contacts,
        }

    except Exception as e:
        logger.error(f"[Panel] Error en búsqueda por palabra clave: {safe_error(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
@limiter.limit("30/minute")
async def get_active_contacts(
    request: Request,
    filter_time: str = Query("24h", description="Filtro de tiempo: 24h, 48h, 1week, custom"),
    date_from: Optional[str] = Query(None, description="Fecha desde (ISO) para filtro custom"),
    date_to: Optional[str] = Query(None, description="Fecha hasta (ISO) para filtro custom"),
    advisor: Optional[str] = Query(None, description="Owner ID para filtrar contactos por asesora"),
    limit: int = Query(30, ge=1, le=100),
    page: int = Query(1, ge=1, description="Página (1-based) para paginación del ZSET"),
    worker_id: Optional[str] = Query(None, description="Worker ID para filtrar contactos por encargado de cita"),
    include_phone: Optional[str] = Query(None, description="Teléfono a incluir aunque no pertenezca al advisor (deep link cross-advisor)"),
    date_field: str = Query("last_activity", description="Campo de fecha para filtro custom: last_activity | created_at"),
    stage: Optional[str] = Query(None, description="Filtro por lifecyclestage — trae TODOS los contactos del owner en esa etapa (cap 500). Ignora limit/page."),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Retorna lista de contactos combinando dos fuentes:
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # ── Branch: filtro por etapa → delega a módulo aislado stage_filter ──────
    if stage and advisor:
        try:
            from middleware.stage_filter import get_contacts_by_stage_full
            _sm = _get_state_manager()
            _stage_result = await get_contacts_by_stage_full(stage, advisor, _sm)
            if worker_id:
                logger.info(f"[StageFilter] worker_id={safe_id(worker_id, 'worker')} ignorado al combinar con stage={safe_id(stage, 'stage')}")
            return {
                "contacts": _stage_result["contacts"],
                "total": _stage_result["total"],
                "page": 1,
                "limit": _stage_result["total"],
                "filter_mode": "stage_full",
                "max_reached": _stage_result["max_reached"],
                "from_cache": _stage_result["from_cache"],
            }
        except Exception as _stage_err:
            logger.error(
                f"[StageFilter] Error en branch stage={stage}, fallback al flujo ZSET: {_stage_err}",
                exc_info=True,
            )
            # Cae al flujo normal abajo

    # Fuera del try: el manejador de errores lo consulta para soltar el turno de
    # single-flight, y si fallara antes de asignarlo daría NameError.
    _inflight_key = None
    _perf_trace: Optional[_ContactsPerfTrace] = None

    try:
        from zoneinfo import ZoneInfo
        TIMEZONE = ZoneInfo("America/Bogota")

        now = datetime.now(TIMEZONE)
        _perf_trace = _ContactsPerfTrace(advisor, filter_time, page, limit)

        # Helpers para time_ago en español (locale-agnostic)
        _DIAS_CORTOS  = ['lun','mar','mié','jue','vie','sáb','dom']
        _MESES_CORTOS = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic']

        def _fmt_time(dt_local):
            """'4:00 p.m.' en español sin depender del locale del sistema."""
            h = dt_local.hour % 12 or 12
            return f"{h}:{dt_local.strftime('%M')} {'a.m.' if dt_local.hour < 12 else 'p.m.'}"

        def _fmt_time_ago(ref_dt, delta):
            """Retorna string time_ago en español preciso."""
            secs = delta.total_seconds()
            local = ref_dt.astimezone(TIMEZONE)
            if secs < 3600:
                return f"hace {int(secs // 60)} min"
            if secs < 86400:
                return f"hoy {_fmt_time(local)}"
            if secs < 172800:
                return f"ayer {_fmt_time(local)}"
            if delta.days < 7:
                return f"{_DIAS_CORTOS[local.weekday()]} {_fmt_time(local)}"
            return f"{local.day} {_MESES_CORTOS[local.month - 1]}"

        # === BRANCH: Filtro por worker_id (citas) ===
        # Cuando worker_id está activo, el pipeline normal se omite.
        # Se buscan contactos desde MongoDB appointments y se filtran por etapa HubSpot.
        if worker_id:
            _wf_date_from: Optional[datetime] = None
            _wf_date_to: Optional[datetime] = None
            if date_from:
                try:
                    _wf_date_from = datetime.fromisoformat(date_from)
                    if _wf_date_from.tzinfo is None:
                        _wf_date_from = _wf_date_from.replace(tzinfo=TIMEZONE)
                except ValueError:
                    pass
            if date_to:
                try:
                    _wf_date_to = datetime.fromisoformat(date_to)
                    if _wf_date_to.tzinfo is None:
                        _wf_date_to = _wf_date_to.replace(tzinfo=TIMEZONE)
                except ValueError:
                    pass
            return await _get_contacts_by_worker_filter(
                worker_id=worker_id,
                advisor=advisor,
                limit=limit,
                date_from=_wf_date_from,
                date_to=_wf_date_to,
            )

        # === PASO 1: Obtener contactos ACTIVOS de Redis ===
        state_manager = _get_state_manager()

        # === CACHE: Respuesta completa en Redis (TTL 5s) para colapsar concurrent requests ===
        # include_phone DEBE formar parte de la key: una request con deep link inyecta
        # un contacto cross-advisor en la respuesta, y sin él en la key esa respuesta
        # contaminada se servía durante 5s a cualquier otra petición con los mismos
        # parámetros (fuga de la segregación por equipo).
        _cache_params = (
            f"{advisor or ''}:{filter_time}:{date_from or ''}:{date_to or ''}:"
            f"{date_field}:{page}:{limit}:{include_phone or ''}"
        )
        _contacts_cache_key = f"contacts_resp:{hashlib.md5(_cache_params.encode()).hexdigest()}"
        try:
            _cached = await state_manager.redis.get(_contacts_cache_key)
            if _cached:
                logger.debug(f"[Panel] GET /contacts cache HIT para advisor={safe_id(advisor, 'advisor') if advisor else 'all'}")
                return json.loads(_cached)
        except Exception:
            pass  # Cache miss o error Redis → continuar con lógica normal
        _perf_trace.mark("cache_lookup")

        # Cache miss. Single-flight: solo una petición reconstruye; las demás
        # esperan su resultado. Sin esto, con la lista lenta se apilan decenas de
        # reconstrucciones simultáneas y cada una dispara cientos de llamadas a
        # HubSpot, que responde 429 y ralentiza aún más — el bucle que llevó
        # GET /contacts a 508s el 10-ago-2026.
        try:
            _candidate_key = f"contacts_inflight:{hashlib.md5(_cache_params.encode()).hexdigest()}"
            _got_slot = await state_manager.redis.set(
                _candidate_key, "1", nx=True, ex=CONTACTS_INFLIGHT_TTL
            )
            if _got_slot:
                _inflight_key = _candidate_key
            else:
                _shared = await _await_contacts_inflight(
                    state_manager.redis, _contacts_cache_key
                )
                if _shared is not None:
                    logger.debug("[Panel] GET /contacts servido por single-flight")
                    return _shared
                # Se agotó la espera: reconstruir igual (fail-open).
        except Exception:
            pass  # Si Redis falla aquí, seguir sin single-flight.
        _perf_trace.mark("singleflight")

        if advisor:
            # Cuando hay filtro de advisor: escanear todo el ZSET para no perder contactos
            # del advisor en páginas posteriores (problema cuando todos tienen el mismo score,
            # e.g. tras restore-panel — Redis ordena lex inverso y los del advisor quedan en p2+).
            _all = await state_manager.get_all_human_active_contacts(limit=300, offset=0)
            advisor_contacts = [
                c for c in _all
                if c.get("owner_id") == advisor
                or advisor in (c.get("assigned_owner_ids") or [])
            ]
            logger.info(f"[Panel] Pre-filtrado por advisor {safe_id(advisor, 'advisor')}: {len(advisor_contacts)} contactos")

            # ⚠️ 2026-05-30: Fallback MongoDB — recupera conversaciones del owner que
            # ya no estén en el ZSET Redis (TTL expirado, ghost cleanup histórico,
            # eviction de Redis, etc.). Garantiza que el panel muestre TODAS las
            # conversaciones del asesor aunque tengan semanas/meses.
            try:
                _zset_phones = {c.get("phone") for c in advisor_contacts if c.get("phone")}
                _mongo_extra = await state_manager.get_archived_conversations_from_mongo(
                    owner_id=advisor,
                    limit=200,  # cap para no inflar la página
                    exclude_phones=_zset_phones,
                )
                if _mongo_extra:
                    advisor_contacts = advisor_contacts + _mongo_extra
                    _huerfanas = sum(
                        1 for c in _mongo_extra if c.get("_orphan_attributed")
                    )
                    logger.info(
                        f"[Panel][MongoFallback] +{len(_mongo_extra)} conversaciones desde MongoDB "
                        f"({_huerfanas} sin dueño atribuidas por canal, "
                        f"owner={advisor}, total ahora {len(advisor_contacts)})"
                    )
            except Exception as _fb_err:
                logger.warning(f"[Panel] Fallback MongoDB falló (non-fatal): {_fb_err}")

            # Límite dinámico: entran SIEMPRE los que tienen mensajes sin leer y los
            # que esperan respuesta del cliente; del resto, solo los `limit` más
            # recientes. El corte era lo que sepultaba conversaciones con el cliente
            # esperando, porque abrir un chat quita el no-leído pero no la deuda.
            # Nota: el PASO 1.5 vuelve a consultar `conversations` para el texto
            # del preview. Se dejan separadas a proposito — fusionarlas ahorraria
            # ~20ms sobre un endpoint de ~3000ms y a cambio acoplaria dos bloques
            # distantes de este archivo.
            _unread_phones = await state_manager.get_all_inbox_phones(advisor)
            _pending_phones = await _resolve_pending_reply_phones(advisor_contacts)

            active_contacts, _cut_stats = _split_always_visible(
                advisor_contacts=advisor_contacts,
                unread_phones=_unread_phones,
                pending_phones=_pending_phones,
                page=page,
                limit=limit,
            )
            total_for_advisor = len(advisor_contacts)

            if _cut_stats["inbox_down"]:
                # El inbox no respondió. Sin saber quién tiene mensajes sin leer no
                # se puede aplicar el corte normal: dejaría fuera precisamente a los
                # contactos nuevos, que es lo que paso el 10-ago-2026. Se amplía el
                # corte — mostrar de más es recuperable, ocultar un lead no.
                logger.warning(
                    "[Panel] Inbox no disponible para advisor=%s — corte ampliado a %d "
                    "(en vez de %d) para no ocultar contactos con mensajes nuevos",
                    advisor, _cut_stats["cut"], limit
                )
            logger.info(
                f"[Panel] Límite dinámico: {_cut_stats['unread']} unread + "
                f"{_cut_stats['pending']} esperando respuesta (sin límite) + "
                f"{_cut_stats['otros']} otros = {_cut_stats['total']} total"
            )
            if _cut_stats["pending_truncated"]:
                logger.warning(
                    "[Panel] Tope de pendientes alcanzado para advisor=%s: %d por "
                    "encima de %d quedaron sujetos al corte normal",
                    advisor, _cut_stats["pending_truncated"], CONTACTS_PENDING_MAX
                )
        else:
            zset_offset = (page - 1) * limit
            active_contacts = await state_manager.get_all_human_active_contacts(limit=limit, offset=zset_offset)
            total_for_advisor = None
            logger.info(f"[Panel] Encontrados {len(active_contacts)} contactos activos en Redis")
        _perf_trace.mark("load_active")

        # ── Pre-calcular has_unread ANTES del sort para usarlo en el ordenamiento.
        # get_inbox_unread_map() es O(N) con 1 round-trip Redis (ZRANGE) → <5ms.
        # Fail-safe: si falla, _unread_map={} → sort cae al comportamiento por status.
        _unread_map = {}
        if advisor and active_contacts:
            try:
                _inbox_pre = [
                    {"phone": c.get("phone", ""), "canal": c.get("canal") or "whatsapp"}
                    for c in active_contacts if c.get("phone")
                ]
                _unread_map = await state_manager.get_inbox_unread_map(advisor, _inbox_pre)
                for c in active_contacts:
                    c["has_unread"] = _unread_map.get(c.get("phone", ""), False)
            except Exception as _pre_unread_err:
                logger.warning(f"[Panel] Error pre-calculando has_unread (non-fatal): {_pre_unread_err}")
        _perf_trace.mark("unread_precalc")

        # ── Pre-limitar ANTES del enriquecimiento con HubSpot.
        # Los contactos de prioridad (ZSET) siempre van; del resto solo los más recientes.
        # Sin esto, 534 contactos × waits de 429 (36s c/u) = timeout de Railway.
        # El criterio vive en _cuenta_como_prioridad() — ver ahí por qué los
        # contactos con el cliente esperando tienen que pasar este corte.
        # El coste de los contactos extra ya no es el de 2026-05: _hubspot_batch_get_contacts
        # agrupa las consultas y el cache Redis de nombres absorbe la mayoría.
        priority_contacts = [c for c in active_contacts if _cuenta_como_prioridad(c)]
        bot_contacts = [c for c in active_contacts if not _cuenta_como_prioridad(c)]

        # Ordenar con 3 tiers (estilo WhatsApp):
        #   Tier 0: has_unread=True → prioridad máxima (necesitan atención)
        #   Tier 1: IN_CONVERSATION / HUMAN_ACTIVE / PENDING_HANDOFF sin unread
        #   Tier 2: BOT_ACTIVE sin unread
        # Dentro de cada tier: más reciente primero (ISO string lexicográfico-correcto)
        def _status_priority(c):
            if c.get("has_unread", False):
                return 0
            s = c.get("status") or c.get("conversation_status") or ""
            return 1 if s in ("IN_CONVERSATION", "HUMAN_ACTIVE", "PENDING_HANDOFF") else 2

        # Stable sort: primero por last_activity desc, luego por tier asc
        # (Python sort es estable → dentro del mismo tier queda orden por actividad)
        priority_contacts.sort(key=lambda c: c.get("last_activity") or "", reverse=True)
        priority_contacts.sort(key=_status_priority)

        # Ordenar bot_contacts por última actividad (más reciente primero)
        def _sort_key(c):
            ts = c.get("last_activity") or ""
            return ts

        bot_contacts.sort(key=_sort_key, reverse=True)

        # Solo enriquecer los slots restantes hasta `limit`.
        # Los contactos con has_unread NO cuentan contra el límite (dynamic limit).
        _priority_no_unread = sum(1 for c in priority_contacts if not c.get("has_unread", False))
        remaining_slots = max(0, (limit * 2) - _priority_no_unread)
        active_contacts = priority_contacts + bot_contacts[:remaining_slots]
        logger.info(
            f"[Panel] Pre-limitado a {len(active_contacts)} contactos "
            f"({len(priority_contacts)} prioridad [{_priority_no_unread} sin unread] + "
            f"{len(bot_contacts[:remaining_slots])} bot) — pre-segregación"
        )
        _perf_trace.mark("prelimit_sort")

        # === PASO 6 (MOVIDO ANTES de enriquecimiento): SEGREGACIÓN ESTRICTA por equipo/portal ===
        # Filtrar ANTES de llamar a HubSpot para reducir llamadas API y evitar 429.
        # Los campos necesarios (canal_origen, owner_id, assigned_owner_ids) ya están desde Redis/MongoDB.
        if advisor:
            if advisor.lower() == "admin":
                logger.info(f"[Panel] Modo ADMIN: mostrando todos los {len(active_contacts)} contactos (sin segregación)")
            else:
                from integrations.hubspot.lead_assigner import LeadAssigner

                advisor_str = str(advisor)

                advisor_team = None
                advisor_name = "Desconocido"
                for team_name, team_members in LeadAssigner.OWNERS_CONFIG.items():
                    for member in team_members:
                        if str(member.get("id")) == advisor_str:
                            advisor_team = team_name
                            advisor_name = member.get("name", "Desconocido")
                            break
                    if advisor_team:
                        break

                if not advisor_team:
                    logger.warning(
                        f"[Panel] Advisor ID {advisor} no encontrado en OWNERS_CONFIG. "
                        f"Usando equipo 'default' (acceso restringido)"
                    )
                    advisor_team = "default"

                allowed_channels = set()
                for canal, team in LeadAssigner.CHANNEL_TO_TEAM.items():
                    if team == advisor_team:
                        allowed_channels.add(canal)
                if advisor_team == "default":
                    for canal, team in LeadAssigner.CHANNEL_TO_TEAM.items():
                        if team == "default":
                            allowed_channels.add(canal)

                all_advisor_ids = set()
                for team_members in LeadAssigner.OWNERS_CONFIG.values():
                    for member in team_members:
                        member_id = member.get("id")
                        if member_id:
                            all_advisor_ids.add(str(member_id))
                other_advisor_ids = all_advisor_ids - {advisor_str}

                filtered_contacts = []
                excluded_count = 0
                for contact in active_contacts:
                    if _contact_visible_for_advisor(
                        contact,
                        advisor_id=advisor_str,
                        allowed_channels=allowed_channels,
                        other_advisor_ids=other_advisor_ids,
                    ):
                        filtered_contacts.append(contact)
                    else:
                        excluded_count += 1

                active_contacts = filtered_contacts
                logger.info(
                    f"[Panel] Segregación pre-enriquecimiento '{advisor_name}' ({advisor_team}): "
                    f"{len(active_contacts)} visibles, {excluded_count} excluidos. "
                    f"Canales: {sorted(allowed_channels)}"
                )
        _perf_trace.mark("segregation")

        # === PASO 1.5: Batch MongoDB — inyectar preview del último mensaje ===
        _phones_need_preview = [
            c.get("phone") for c in active_contacts
            if c.get("phone") and not c.get("last_message_preview")
        ]
        if _phones_need_preview:
            try:
                _previews = await get_mongo_manager().get_message_previews_batch(_phones_need_preview)
                for c in active_contacts:
                    _p = _previews.get(c.get("phone"))
                    if _p:
                        c.setdefault("last_message_preview", _p.get("last_message_preview", ""))
                        c.setdefault("last_message_sender", _p.get("last_message_sender", ""))
            except Exception as _prev_err:
                logger.warning(f"[Panel] Batch previews falló (non-fatal): {_prev_err}")
        _perf_trace.mark("previews")

        # === PASO 2: Enriquecer contactos activos con HubSpot (OPTIMIZADO CON BATCH) ===
        contact_manager = _get_contact_manager()
        
        # ── PASO 2.1: Batch request para obtener nombres de contactos ──
        # Pre-check Redis cache (4h TTL) antes de llamar HubSpot — reduce 429s aún más
        all_contact_ids = [
            c.get("contact_id") for c in active_contacts
            if c.get("contact_id")
        ]

        # ⚠️ 1 round-trip (MGET) en vez de uno por contacto. El bucle secuencial
        # anterior costaba ~5s con ~300 contactos — medido en produccion con el
        # profiler, era el 69% del tiempo de este endpoint.
        redis_name_cache: Dict[str, Dict[str, Any]] = await _get_cached_contact_names_batch(
            all_contact_ids
        )
        ids_needing_fetch: list = [
            cid for cid in all_contact_ids if cid not in redis_name_cache
        ]

        batch_contact_data = {}
        if ids_needing_fetch:
            batch_contact_data = await _hubspot_batch_get_contacts(ids_needing_fetch)
            logger.info(
                f"[Panel] Batch pre-fetch: {len(batch_contact_data)} contactos de HubSpot "
                f"(cache hits: {len(redis_name_cache)})"
            )
        elif redis_name_cache:
            logger.debug(f"[Panel] Todos los nombres desde cache Redis ({len(redis_name_cache)} contactos)")

        # Merge cache en batch_contact_data para uso uniforme en _enrich_single_contact
        for cid, nd in redis_name_cache.items():
            if cid not in batch_contact_data:
                batch_contact_data[cid] = {
                    "firstname": nd.get("firstname", ""),
                    "lastname": nd.get("lastname", ""),
                    "email": None,
                    "phone": None,
                    "lifecyclestage": "",
                    "hubspot_owner_id": "",
                }

        async def _enrich_single_contact(contact: dict) -> dict:
            """Enriquece un contacto individual con datos de HubSpot."""
            phone = contact.get("phone", "")

            # Buscar contact_id si no lo tenemos
            if phone and not contact.get("contact_id"):
                try:
                    contact_id = await contact_manager._search_contact(phone)
                    if contact_id:
                        contact["contact_id"] = contact_id
                except Exception:
                    pass

            # Si tenemos contact_id, obtener nombre de HubSpot y deal info
            if contact.get("contact_id"):
                cid = contact["contact_id"]

                # ✅ HubSpot es source of truth para nombres cuando está disponible en batch
                if cid in batch_contact_data:
                    hs_info = batch_contact_data[cid]
                    hs_name = f"{hs_info.get('firstname', '')} {hs_info.get('lastname', '')}".strip()
                    if hs_name:
                        contact["display_name"] = hs_name
                        # Solo cachear si vino de HubSpot (no del cache propio)
                        if cid not in redis_name_cache:
                            asyncio.create_task(_cache_contact_name(
                                cid, hs_info.get("firstname", ""), hs_info.get("lastname", "")
                            ))
                    elif not contact.get("display_name") or contact.get("display_name") in ("Cliente Nuevo", "Sin nombre"):
                        contact["display_name"] = "Sin nombre"
                    contact["email"] = hs_info.get("email")
                # Si no estaba en batch, conservar display_name de Redis

                # Leer lifecyclestage — preferir batch (0 llamadas extra) → fallback individual
                if not contact.get("current_stage"):
                    batch_stage = batch_contact_data.get(cid, {}).get("lifecyclestage")
                    if batch_stage:
                        contact["current_stage"] = batch_stage
                        asyncio.create_task(_cache_contact_stage(cid, batch_stage))
                    else:
                        try:
                            contact["current_stage"] = await _get_contact_lifecyclestage(cid)
                        except Exception as e:
                            logger.debug(f"[Panel] No se pudo obtener lifecyclestage: {safe_error(e)}")
                            contact["current_stage"] = HUBSPOT_STAGE_EN_CONVERSACION

            # Si aún no tenemos nombre, usar teléfono
            if not contact.get("display_name"):
                contact["display_name"] = phone or "Sin nombre"

            # Formatear TTL para mostrar
            ttl = contact.get("ttl_remaining")
            if ttl and ttl > 0:
                hours = ttl // 3600
                minutes = (ttl % 3600) // 60
                contact["ttl_display"] = f"Expira en {hours}h {minutes}m"

            return contact

        # ✅ FIX: Semáforo REDUCIDO para limitar llamadas concurrentes (deal lookups)
        hubspot_semaphore = asyncio.Semaphore(2)  # Reducido de 3 a 2 para evitar 429
        
        async def _enrich_with_rate_limit(contact: dict) -> dict:
            async with hubspot_semaphore:
                return await _enrich_single_contact(contact)

        # Ejecutar enriquecimiento en paralelo (limitado por semáforo)
        if active_contacts:
            enriched_contacts = await asyncio.gather(
                *[_enrich_with_rate_limit(contact) for contact in active_contacts],
                return_exceptions=True
            )
            # Filtrar excepciones y mantener contactos válidos
            active_contacts = [
                c for c in enriched_contacts
                if isinstance(c, dict)
            ]
            logger.info(f"[Panel] Enriquecimiento paralelo completado: {len(active_contacts)} contactos")

            # Batch write phone_cache inverso en 1 pipeline (en lugar de 30 writes concurrentes)
            try:
                _rc_batch = await _get_redis_client()
                _pipe = _rc_batch.pipeline(transaction=False)
                for _c in active_contacts:
                    _cid = _c.get("contact_id")
                    _ph  = _c.get("phone")
                    if _cid and _ph:
                        _pipe.set(f"phone_cache:{_cid}", _ph, ex=86400, nx=True)
                await _pipe.execute()
            except Exception:
                pass
        _perf_trace.mark("hubspot_enrichment")

        # === PASO 3: Calcular rango de tiempo para historial ===
        if filter_time == "24h":
            since = now - timedelta(hours=24)
            until = now
        elif filter_time == "48h":
            since = now - timedelta(hours=48)
            until = now
        elif filter_time == "1week":
            since = now - timedelta(weeks=1)
            until = now
        elif filter_time == "custom" and date_from:
            since = datetime.fromisoformat(date_from)
            if since.tzinfo is None:
                since = since.replace(tzinfo=TIMEZONE)
            until = datetime.fromisoformat(date_to) if date_to else now
            if until.tzinfo is None:
                until = until.replace(tzinfo=TIMEZONE)
        else:
            since = now - timedelta(hours=24)
            until = now

        # === PASO 3.5: Filtrar contactos por fecha, PERO siempre incluir los activos ===
        # REGLA: En modos de ventana deslizante (24h/48h/1week), los contactos en
        # HUMAN_ACTIVE/PENDING_HANDOFF/IN_CONVERSATION siempre se muestran (bypass PRIORIDAD 1).
        # En modo fecha específica (custom), TODOS los contactos se filtran por fecha — sin bypass.
        if filter_time != "all":
            filtered_active = []
            for contact in active_contacts:
                # PRIORIDAD 1: Bypass solo en modos de ventana deslizante (no "custom").
                # Nota: is_active=True está hardcoded para TODOS los contactos de Redis
                # (conversation_state.py:316,377) — no puede usarse como condición de bypass
                # porque haría que PRIORIDAD 2 (filtro por fecha) NUNCA se ejecute.
                status = contact.get("conversation_status") or contact.get("status") or ""
                is_human_active = status in ["HUMAN_ACTIVE", "PENDING_HANDOFF", "IN_CONVERSATION"]

                if is_human_active and filter_time != "custom":
                    # Calcular time_ago para mostrar, pero NO filtrar
                    # Usar last_activity (último mensaje) como referencia de tiempo
                    ref_time = contact.get("last_activity") or contact.get("activated_at")
                    if ref_time:
                        try:
                            if isinstance(ref_time, str):
                                ref_dt = datetime.fromisoformat(ref_time.replace('+00:00Z', '+00:00').replace("Z", "+00:00"))
                            else:
                                ref_dt = ref_time
                            if ref_dt.tzinfo is None:
                                ref_dt = ref_dt.replace(tzinfo=TIMEZONE)
                            time_ago = now - ref_dt.astimezone(TIMEZONE)
                            contact["time_ago"] = _fmt_time_ago(ref_dt, time_ago)
                        except (ValueError, TypeError):
                            contact["time_ago"] = "en espera"
                    else:
                        contact["time_ago"] = "en espera"

                    filtered_active.append(contact)
                    logger.debug(f"[Panel] Contacto {safe_phone(contact.get('phone'))} incluido (activo/en espera)")
                    continue

                # PRIORIDAD 2: Para contactos no activos, filtrar por el campo elegido
                # date_field=created_at → filtrar por fecha de llegada; default → last_activity
                if date_field == "created_at":
                    ref_time = contact.get("activated_at") or contact.get("last_activity")
                else:
                    ref_time = contact.get("last_activity") or contact.get("activated_at")
                if ref_time:
                    try:
                        if isinstance(ref_time, str):
                            ref_dt = datetime.fromisoformat(ref_time.replace("Z", "+00:00"))
                        else:
                            ref_dt = ref_time

                        if ref_dt.tzinfo is None:
                            ref_dt = ref_dt.replace(tzinfo=TIMEZONE)

                        if since <= ref_dt <= until:
                            time_ago = now - ref_dt.astimezone(TIMEZONE)
                            contact["time_ago"] = _fmt_time_ago(ref_dt, time_ago)
                            filtered_active.append(contact)
                        else:
                            logger.debug(
                                f"[Panel] Contacto histórico {contact.get('phone')} excluido por filtro de tiempo"
                            )
                    except (ValueError, TypeError) as e:
                        logger.debug(f"[Panel] Error parseando fecha: {safe_error(e)}")
                        filtered_active.append(contact)
                else:
                    contact["time_ago"] = "reciente"
                    filtered_active.append(contact)

            logger.info(
                f"[Panel] Contactos después de filtro de tiempo: "
                f"{len(filtered_active)}/{len(active_contacts)} (activos siempre incluidos)"
            )
            active_contacts = filtered_active
        _perf_trace.mark("time_filter")

        # === PASO 4: Obtener historial de HubSpot (si hay espacio) ===
        remaining_slots = limit - len(active_contacts)
        historical_contacts = []

        if remaining_slots > 0:
            try:
                timeline_logger = get_timeline_logger()
                result = await timeline_logger.get_contacts_with_advisor_activity(
                    since=since,
                    until=until,
                    limit=remaining_slots
                )

                # Extraer contactos del resultado (nuevo formato con paginación)
                historical_contacts = result.get("contacts", []) if isinstance(result, dict) else result

                # Marcar como no activos y enriquecer
                for contact in historical_contacts:
                    contact["is_active"] = False
                    contact["conversation_status"] = "historical"

                    # Formatear nombre
                    firstname = contact.get("firstname", "")
                    lastname = contact.get("lastname", "")
                    contact["display_name"] = f"{firstname} {lastname}".strip() or "Sin nombre"

            except Exception as e:
                    logger.warning(f"[Panel] Error obteniendo historial de HubSpot: {safe_error(e)}")
        _perf_trace.mark("historical")

        # === PASO 5: Combinar y deduplicar ===
        active_contacts = _dedupe_panel_contacts(active_contacts)
        seen_phones = {
            _normalize_contact_phone_key(c.get("phone"))
            for c in active_contacts if c.get("phone")
        }
        seen_contact_ids = {
            str(c.get("contact_id") or c.get("id"))
            for c in active_contacts if c.get("contact_id") or c.get("id")
        }

        for contact in historical_contacts:
            phone = contact.get("phone")
            contact_id = contact.get("id") or contact.get("contact_id")
            phone_key = _normalize_contact_phone_key(phone)
            contact_id_key = str(contact_id) if contact_id else ""

            # Evitar duplicados
            if phone_key and phone_key in seen_phones:
                continue
            if contact_id_key and contact_id_key in seen_contact_ids:
                continue

            # Filtrar por advisor: solo incluir históricos que pertenecen al advisor actual
            if advisor:
                hs_owner = contact.get("hubspot_owner_id") or ""
                if hs_owner and str(hs_owner) != str(advisor):
                    continue

            active_contacts.append(contact)
            if phone_key:
                seen_phones.add(phone_key)
            if contact_id_key:
                seen_contact_ids.add(contact_id_key)

        # === [Sync] Deep link cross-advisor: incluir contacto aunque no pertenezca al advisor ===
        # Hidratación completa via _hydrate_contact — garantiza que el frontend reciba
        # display_name, contact_id, canal, owner, stage reales (NO skeleton parcial).
        if include_phone:
            _ip_norm = include_phone.strip()
            if not any(c.get("phone") == _ip_norm for c in active_contacts):
                try:
                    hydrated = await _hydrate_contact(
                        _ip_norm, canal_hint=None, add_to_zset=False
                    )
                    if hydrated:
                        # Marcar como cross_advisor si el owner real no coincide con el advisor actual
                        hydrated["cross_advisor"] = bool(
                            advisor
                            and hydrated.get("owner_id")
                            and str(hydrated.get("owner_id")) != str(advisor)
                        )
                        active_contacts.insert(0, hydrated)
                        logger.info(
                            f"[Sync] include_phone={_ip_norm} hidratado "
                            f"(owner={hydrated.get('owner_id')}, "
                            f"name='{hydrated.get('display_name')}', "
                            f"cross_advisor={hydrated.get('cross_advisor')})"
                        )
                    else:
                        logger.warning(
                            f"[Sync] _hydrate_contact({safe_phone(_ip_norm)}) retornó None "
                            f"— contacto no existe en Redis/HubSpot"
                        )
                except Exception as _he:
                    logger.warning(f"[Sync] Error hidratando include_phone={safe_phone(_ip_norm)}: {safe_error(_he)}")
        _perf_trace.mark("include_phone")

        # === PASO 7: El orden ya viene correcto del ZSET (por last_activity descendente) ===
        # NO reordenar por activated_at porque destruye el orden de "actividad reciente primero"
        contacts_sorted = active_contacts  # Mantener orden del backend

        # === PASO 7.5: Marcar contactos con citas activas (una sola query MongoDB) ===
        # FIX Bug5: se consultan TODOS los contact_ids, no solo los de la primera página.
        # Antes: contacts_sorted[:limit] → contactos en posición >limit nunca obtenían badge.
        try:
            mongo_mgr = get_mongo_manager()
            all_contact_ids = [
                c.get("contact_id", "") for c in contacts_sorted
                if c.get("contact_id")
            ]
            contacts_with_appts = await mongo_mgr.get_contacts_with_appointments(all_contact_ids)
            logger.debug(
                f"[Badge] Appointment query: {len(all_contact_ids)} contactos revisados, "
                f"{len(contacts_with_appts)} con cita activa"
            )
            for c in contacts_sorted:
                c["has_appointment"] = c.get("contact_id", "") in contacts_with_appts
        except Exception as appt_err:
            logger.warning(f"[Badge] Error verificando citas activas: {appt_err}")
            for c in contacts_sorted:
                c["has_appointment"] = False
        _perf_trace.mark("appointments")

        # === PASO 7.6: Calcular has_unread desde advisor_inbox ===
        # Reutiliza _unread_map pre-calculado antes del sort (evita 2do round-trip Redis).
        # Si _unread_map está vacío (error en pre-cálculo o branch sin advisor), recalcular.
        if advisor and contacts_sorted:
            try:
                if not _unread_map:
                    _inbox_contacts = [
                        {
                            "phone": c.get("phone", ""),
                            "canal": c.get("canal") or "whatsapp",
                        }
                        for c in contacts_sorted if c.get("phone")
                    ]
                    _unread_map = await state_manager.get_inbox_unread_map(advisor, _inbox_contacts)
                for c in contacts_sorted:
                    c["has_unread"] = _unread_map.get(c.get("phone", ""), False)
                logger.info(
                    f"[Panel] has_unread calculado para {advisor}: "
                    f"{sum(1 for c in contacts_sorted if c.get('has_unread'))} no leídos"
                )
            except Exception as _ue:
                logger.warning(f"[Panel] Error calculando has_unread (non-fatal): {_ue}")
                for c in contacts_sorted:
                    c["has_unread"] = False
        _perf_trace.mark("unread_final")

        # active_count = solo contactos ESPERANDO respuesta (HUMAN_ACTIVE / PENDING_HANDOFF)
        # IN_CONVERSATION no cuenta: ya están siendo atendidos
        waiting_statuses = {"HUMAN_ACTIVE", "PENDING_HANDOFF"}
        active_count = len([
            c for c in contacts_sorted
            if c.get("conversation_status") in waiting_statuses
        ])

        # Corte final, el que arma la respuesta. Es el tercero de la cadena
        # (selección → pre-límite de HubSpot → este) y los tres tienen que
        # respetar lo mismo: ver _nunca_se_corta().
        _always_in_final = [c for c in contacts_sorted if _nunca_se_corta(c)]
        _rest_in_final = [c for c in contacts_sorted if not _nunca_se_corta(c)]
        _dynamic_result = _build_contacts_page_after_dedupe(_always_in_final, _rest_in_final, limit)

        _pending_in_final = sum(
            1 for c in _always_in_final
            if c.get("pending_reply") and not c.get("has_unread", False)
        )
        logger.info(
            f"[Panel] Retornando {len(_dynamic_result)} contactos "
            f"({len(_always_in_final) - _pending_in_final} unread + "
            f"{_pending_in_final} esperando respuesta + "
            f"{len(_rest_in_final[:limit])} leidos, "
            f"activos: {active_count}, advisor: {advisor})"
        )

        _response_data = {
            "contacts": _dynamic_result,
            "filter": filter_time,
            "advisor": advisor,
            "active_count": active_count,
            "historical_count": len(_dynamic_result) - active_count,
            "total_count": total_for_advisor if total_for_advisor is not None else len(contacts_sorted),
            "page": page,
            "limit": limit,
            "since": since.isoformat(),
            "until": until.isoformat()
        }
        try:
            await state_manager.redis.set(
                _contacts_cache_key, json.dumps(_response_data, default=str),
                ex=CONTACTS_RESPONSE_CACHE_TTL
            )
        except Exception:
            pass  # No bloquear la respuesta si el cache write falla
        finally:
            await _release_contacts_inflight(state_manager.redis, _inflight_key)
        _perf_trace.mark("cache_write_release")
        _perf_trace.emit(status="ok", contact_count=len(_dynamic_result))
        return _response_data

    except Exception as e:
        # Soltar el turno aunque la reconstrucción falle: si no, los polls
        # siguientes esperarían hasta que expire el TTL sin que nadie reconstruya.
        try:
            await _release_contacts_inflight(_get_state_manager().redis, _inflight_key)
        except Exception:
            pass
        if _perf_trace is not None:
            _perf_trace.emit(status="error")
        logger.error(f"[Panel] Error obteniendo contactos: {safe_error(e)}")
        raise HTTPException(status_code=500, detail=str(e))
