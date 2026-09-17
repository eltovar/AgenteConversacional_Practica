"""Template service for the advisor panel.

Moved verbatim from `middleware.outbound_panel` — no behavior changes.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import BackgroundTasks, Form, Header, Query, HTTPException
from fastapi.responses import JSONResponse

from logging_config import logger
from middleware import outbound_panel as _legacy
from middleware.outbound_panel import (
    _backfill_im_sid_after_send,
    _content_sids,
    _delete_template_by_advisor,
    _ensure_bsuid_hubspot_contact,
    _get_all_templates_by_advisor,
    _get_template_by_advisor,
    _init_default_templates,
    _log_advisor_message_to_hubspot,
    _picker_visible,
    _plain_template_closed_window_error,
    _resolve_outbound_address,
    _resolve_panel_target,
    _save_template_by_advisor,
    check_24h_window,
    get_mongo_manager,
    get_panel_templates,
    twilio_client,
)
from utils.safe_logging import safe_error, safe_id, safe_phone


async def send_template_message(
    background_tasks: BackgroundTasks,
    to: str = Form(..., description="Número de destino (+573001234567)"),
    template_id: str = Form("reactivacion_general", description="ID del template a usar"),
    variables: str = Form("{}", description="JSON con variables para el template"),
    contact_id: Optional[str] = Form(None, description="ID del contacto en HubSpot"),
    canal: Optional[str] = Form(None, description="Canal de origen para segregación"),
    advisor_id: Optional[str] = Form(None, description="ID del asesor que envía el template"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Envía un mensaje de Template (plantilla) de WhatsApp para reactivar conversación.
    """
    # Validar API Key
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # =========================================================================
    # ASIGNACIÓN DE CANAL POR DEFECTO
    # Si 'canal' es nulo, vacío, o literalmente "null" (que a veces manda JS)
    # =========================================================================
    if not canal or canal.strip() == "" or canal.lower() == "null":
        canal_final = "whatsapp"
    else:
        canal_final = canal.lower().strip()

    target = _resolve_panel_target(to)
    if target.error:
        raise HTTPException(status_code=400, detail=target.error)

    phone_normalized = target.key
    outbound_to, outbound_error = await _resolve_outbound_address(target, canal_final)
    if outbound_error:
        raise HTTPException(status_code=400, detail=outbound_error)

    if not contact_id and target.is_bsuid:
        try:
            contact_id = await asyncio.wait_for(
                _ensure_bsuid_hubspot_contact(target, canal_final),
                timeout=3.0,
            )
        except Exception as e:
            logger.warning(f"[Panel][Template][BSUID] No se pudo asegurar contacto HubSpot: {safe_error(e)}")

    # Verificar disponibilidad de Twilio
    if not twilio_client.is_available:
        raise HTTPException(
            status_code=503,
            detail="Twilio no está configurado correctamente"
        )

    import time
    _t0 = time.monotonic()

    # Inicializar templates predefinidos si es necesario
    await _init_default_templates()
    logger.info(f"[Panel][TIMING] _init_default_templates: {(time.monotonic()-_t0)*1000:.1f}ms")

    # Obtener template de Redis
    _t1 = time.monotonic()
    template = await _get_template_by_advisor(advisor_id or "default", template_id)
    logger.info(f"[Panel][TIMING] _get_template_by_advisor: {(time.monotonic()-_t1)*1000:.1f}ms")
    if not template:
        raise HTTPException(
            status_code=404,
            detail=f"Template '{template_id}' no encontrado"
        )

    # Parsear variables
    try:
        vars_dict = json.loads(variables) if variables else {}
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400,
            detail="Variables JSON inválidas"
        )

    # Safety net: rechazar si template requiere variables pero no se proporcionaron.
    # Previene envío de placeholders crudos ({fecha}, {hora}) por fallo de extracción.
    template_vars = template.get("variables", [])
    if template_vars and not vars_dict:
        logger.warning(
            f"[Panel] Template '{template_id}' requiere {len(template_vars)} variables "
            f"pero vars_dict está vacío — rechazando para evitar placeholders crudos"
        )
        raise HTTPException(
            status_code=400,
            detail=f"Template '{template_id}' requiere variables ({', '.join(template_vars)}) pero no se proporcionaron"
        )

    # Reemplazar variables en el body del template
    template_body = template.get("body", "")
    try:
        # Usar format_map para manejar variables faltantes graciosamente
        class SafeDict(dict):
            def __missing__(self, key):
                return f"{{{key}}}"  # Mantiene {variable} si no se proporciona

        template_message = template_body.format_map(SafeDict(vars_dict))
    except Exception as e:
        logger.warning(f"[Panel] Error formateando template: {safe_error(e)}")
        template_message = template_body  # Usar el body sin formato si hay error

    logger.info(f"[Panel] Enviando template '{template_id}' a {safe_phone(phone_normalized)}")

    # Construir content_variables numeradas para Twilio Content API
    # (solo se usa cuando el template tiene content_sid aprobado por Meta)
    content_sid = template.get("content_sid")
    variables_map = template.get("content_variables_map", [])
    content_variables = None
    if content_sid:
        if isinstance(variables_map, dict):
            # Numeración explícita: {"2": "link_inmueble"} → {"2": valor}
            # Usado cuando el template Meta no empieza en {{1}}
            content_variables = {
                num: vars_dict.get(var_name, "")
                for num, var_name in variables_map.items()
            }
        else:
            # Numeración secuencial: ["nombre", "tema"] → {"1": nombre, "2": tema}
            content_variables = {
                str(i + 1): vars_dict.get(var_name, "")
                for i, var_name in enumerate(variables_map)
            }
        # Validar que ninguna variable esté vacía — evita error 21656 de Twilio
        empty_vars = [k for k, v in content_variables.items() if not v or not str(v).strip()]
        if empty_vars:
            logger.error(
                f"[Panel] content_variables vacíos: keys={empty_vars} "
                f"vars_dict={vars_dict} map={variables_map}"
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Las variables {', '.join(empty_vars)} están vacías. "
                    f"Selecciona la plantilla de nuevo con / y solo reemplaza "
                    f"los campos {{variable}} sin modificar el texto fijo."
                )
            )
        logger.info(
            f"[Panel] Usando ContentSid={content_sid} con variables={content_variables}"
        )
    elif not content_sid:
        logger.warning(
            f"[Panel] Template '{template_id}' sin content_sid — "
            f"se enviará como texto plano solo si la ventana está abierta"
        )

    window_status = await check_24h_window(phone_normalized)
    plain_template_error = _plain_template_closed_window_error(content_sid, window_status)
    if plain_template_error:
        logger.warning(
            f"[Panel] Template '{template_id}' sin content_sid bloqueado para "
            f"{safe_phone(phone_normalized)}: ventana cerrada"
        )
        raise HTTPException(status_code=400, detail=plain_template_error)

    # Enviar mensaje via Twilio
    _t2 = time.monotonic()
    result = await twilio_client.send_whatsapp_message(
        to=outbound_to,
        body=template_message,
        content_sid=content_sid,
        content_variables=content_variables,
    )
    logger.info(
        f"[Panel][TIMING] twilio.send_whatsapp_message: {(time.monotonic()-_t2)*1000:.1f}ms | "
        f"total_hasta_aqui: {(time.monotonic()-_t0)*1000:.1f}ms | "
        f"status: {result.get('status')}"
    )

    if result["status"] == "success":
        message_sid = result.get("message_sid")
        sent_via = result.get("via")
        conv_sid_from_send = result.get("conversation_sid")
        im_sid_from_send = result.get("conversations_message_sid")
        chat_svc_sid_from_send = result.get("chat_service_sid")
        template_content = f"[TEMPLATE: {template.get('name', template_id)}] {template_message}"

        # =====================================================================
        # PASO 1: Guardar en MongoDB INMEDIATAMENTE
        # =====================================================================
        mongo_message_id = None
        try:
            mongo_manager = get_mongo_manager()
            mongo_message_id = await mongo_manager.save_message(
                phone=phone_normalized,
                content=template_content,
                sender="advisor",
                channel=canal_final,
                hubspot_contact_id=contact_id,
                message_sid=message_sid,
                metadata={"source": "Template via Panel", "template_id": template_id, "send_via": sent_via or "legacy"},
                conversation_sid=conv_sid_from_send,
                conversations_message_sid=im_sid_from_send,
                chat_service_sid=chat_svc_sid_from_send,
            )
            if mongo_message_id:
                logger.info(f"[Panel] Template guardado en MongoDB: {mongo_message_id}")
        except Exception as e:
            logger.error(f"[Panel] Error guardando template en MongoDB: {safe_error(e)}")

        if mongo_message_id and not im_sid_from_send:
            background_tasks.add_task(
                _backfill_im_sid_after_send,
                phone_normalized,
                template_message,
                str(mongo_message_id),
                None,
            )

        # =====================================================================
        # PASO 2: Registrar en HubSpot Timeline (BACKGROUND)
        # =====================================================================
        if contact_id:
            background_tasks.add_task(
                _log_advisor_message_to_hubspot,
                contact_id,
                template_content,
                phone_normalized,
                "Template via Panel",
                mongo_message_id
            )

        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message_sid": message_sid,
                "mongo_id": mongo_message_id,
                "to": phone_normalized,
                "contact_id": contact_id,
                "canal": canal_final,
                "template_id": template_id,
                "template_name": template.get("name"),
                "template_sent": True,
                "message": "Template enviado. La conversación se reabrirá cuando el cliente responda."
            }
        )
    else:
        raise HTTPException(
            status_code=500,
            detail=f"Error enviando template: {result.get('message')}"
        )
async def get_template_sids(
    advisor_id: str = Query(...),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Sirve al panel los Content SIDs vigentes de masivos y programados.

    Why: los SIDs estaban duplicados en el frontend. Al migrar de cuenta Twilio
    cambian todos, y un frontend desactualizado envía SIDs que el backend rechaza.
    Sirviéndolos desde aquí, cambiar las env vars de Railway basta para ambos lados.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    return {
        "bulk": _content_sids.BULK_TEMPLATES,
        "schedulable": _content_sids.SCHEDULABLE_TEMPLATES,
    }


async def list_templates(
    advisor_id: str = Query(...),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Lista todos los templates disponibles para un asesor."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    await _init_default_templates()
    templates = await _get_all_templates_by_advisor(advisor_id)
    permitidas = get_panel_templates(advisor_id)
    categories = {}
    for t in templates:
        t["picker_visible"] = _picker_visible(t, permitidas)
        cat = t.get("category", "otros")
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(t)
    return {
        "templates": templates,
        "by_category": categories,
        "total": len(templates)
    }


async def get_template_by_id(
    template_id: str,
    advisor_id: str = Query(...),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Obtiene un template específico para un asesor."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    template = await _get_template_by_advisor(advisor_id, template_id)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' no encontrado")
    return template


async def create_template(
    advisor_id: str = Query(...),
    name: str = Form(..., description="Nombre del template"),
    category: str = Form(..., description="Categoría: reactivacion, cita, seguimiento, recordatorio, promocion"),
    body: str = Form(..., description="Cuerpo del mensaje con variables {nombre}, {fecha}, etc."),
    variables: str = Form("[]", description="JSON array de nombres de variables"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Crea un nuevo template para un asesor."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    # Permitir nombres flexibles, solo bloquear si el nombre ya existe (case-insensitive)
    template_id = name.strip().lower()
    existing_templates = await _get_template_by_advisor(advisor_id, template_id)
    if existing_templates:
        raise HTTPException(
            status_code=409,
            detail=f"Ya existe un template con ese nombre. Elige otro nombre."
        )
    # Variables opcionales y siempre JSON válido
    try:
        vars_list = json.loads(variables) if variables else []
        if not isinstance(vars_list, list):
            vars_list = []
    except Exception:
        vars_list = []
    template_data = {
        "id": template_id,
        "name": name.strip(),
        "category": category.strip(),
        "body": body.strip(),
        "variables": vars_list,
        "is_default": False,
        "created_at": datetime.now(timezone.utc).isoformat() + "Z"
    }
    success = await _save_template_by_advisor(advisor_id, template_data)
    if not success:
        raise HTTPException(status_code=500, detail="Error guardando template")
    return JSONResponse(
        status_code=201,
        content={
            "status": "success",
            "template": template_data,
            "message": f"Template '{name}' creado exitosamente"
        }
    )


async def update_template(
    template_id: str,
    advisor_id: str = Query(...),
    name: str = Form(None, description="Nombre del template"),
    category: str = Form(None, description="Categoría"),
    body: str = Form(None, description="Cuerpo del mensaje"),
    variables: str = Form(None, description="JSON array de variables"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Actualiza un template existente para un asesor."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    template = await _get_template_by_advisor(advisor_id, template_id)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' no encontrado")
    if name is not None:
        template["name"] = name.strip()
    if category is not None:
        template["category"] = category.strip()
    if body is not None:
        template["body"] = body.strip()
    if variables is not None:
        try:
            template["variables"] = json.loads(variables)
        except json.JSONDecodeError:
            pass
    template["updated_at"] = datetime.now(timezone.utc).isoformat() + "Z"
    success = await _save_template_by_advisor(advisor_id, template)
    if not success:
        raise HTTPException(status_code=500, detail="Error actualizando template")
    return {
        "status": "success",
        "template": template,
        "message": f"Template '{template_id}' actualizado"
    }


async def delete_template(
    template_id: str,
    advisor_id: str = Query(...),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Elimina un template de un asesor (no se pueden eliminar templates predefinidos)."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    template = await _get_template_by_advisor(advisor_id, template_id)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' no encontrado")
    if template.get("is_default"):
        raise HTTPException(
            status_code=403,
            detail="No se pueden eliminar templates predefinidos"
        )
    success = await _delete_template_by_advisor(advisor_id, template_id)
    if not success:
        raise HTTPException(status_code=500, detail="Error eliminando template")
    return {
        "status": "success",
        "message": f"Template '{template_id}' eliminado"
    }
