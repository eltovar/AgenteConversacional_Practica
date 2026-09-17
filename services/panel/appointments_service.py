"""Appointments service for the advisor panel.

Moved verbatim from `middleware.outbound_panel` — no behavior changes.
`_update_contact_to_visita_agendada` stays in the legacy module, where
jobs and tests use it; it is resolved via `_legacy` so those patches apply.
"""

import os
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

from logging_config import logger
from middleware import outbound_panel as _legacy
from middleware.confirmacion_cita import (
    DatosCita,
    IDENTIFICADOR_PLANTILLA as PLANTILLA_CONFIRMACION_CITA,
    MOTIVO_SIN_TELEFONO_CLIENTE,
    componer as componer_confirmacion,
    hace_falta_reenviar,
)
from middleware.outbound_panel import (
    ws_manager,
)
from middleware.phone_normalizer import PhoneNormalizer
from utils.date_parser import (
    DIAS_SEMANA_CAP,
    MESES_ABREV_CAP,
    hora_12h,
)
from utils.safe_logging import safe_error, safe_id, safe_phone


MOTIVO_SIN_CAMBIOS_PARA_EL_CLIENTE = "sin_cambios_para_el_cliente"
async def _enviar_confirmacion_cita(
    appointment_id: str,
    contact_id: str,
    phone_normalized: str,
    canal: str,
    advisor_id: Optional[str],
    plantilla_id: str,
    datos: DatosCita,
    comunicadas: Optional[Dict[str, str]],
) -> Tuple[bool, str]:
    """
    Escribe al cliente los datos de su cita, si hay algo que contarle.

    Lo usan los DOS caminos: crear la cita (con `comunicadas` vacío, así que
    siempre hay algo nuevo) y editarla (con lo que ya se le dijo, para no
    molestarle si no ha cambiado nada que él vea). Una sola función porque el
    trabajo es idéntico —componer, enviar, dejar rastro— y duplicarlo garantizaba
    que un arreglo en uno de los dos caminos no llegara al otro.

    Devuelve (enviada, motivo). Nunca lanza: la cita ya está guardada y un fallo
    aquí se reporta, no tumba la operación.
    """
    if not phone_normalized:
        return False, MOTIVO_SIN_TELEFONO_CLIENTE

    try:
        # La plantilla se lee de Redis, igual que en el envío manual: si una
        # asesora edita el texto desde el panel, el mensaje lo sigue. Aquí no
        # vive ni una frase.
        await _legacy._init_default_templates()
        plantilla = await _legacy._get_template_by_advisor(advisor_id or "default", plantilla_id)
        decision = componer_confirmacion(plantilla, datos)

        if not decision.enviar:
            motivo = decision.motivo
            if decision.faltantes:
                # Sin esto la asesora lee "datos incompletos" y no sabe cuál de
                # los cinco campos tiene que arreglar.
                motivo += ":" + ",".join(decision.faltantes)
            return False, motivo

        if not hace_falta_reenviar(comunicadas, decision):
            return False, MOTIVO_SIN_CAMBIOS_PARA_EL_CLIENTE

        if not _legacy.twilio_client.is_available:
            return False, "twilio_no_disponible"

        envio = await _legacy.twilio_client.send_whatsapp_message(
            to=phone_normalized,
            body=decision.cuerpo,
            content_sid=decision.content_sid,
            content_variables=decision.content_variables,
        )
        if envio.get("status") != "success":
            return False, f"twilio: {envio.get('message', 'error')}"

        mongo_mgr = _legacy.get_mongo_manager()

        # Sólo tras el OK de Twilio. Guardarlo antes dejaría la cita creyendo que
        # el cliente está avisado, y una edición posterior no le reenviaría nada.
        await mongo_mgr.marcar_confirmacion_enviada(appointment_id, decision.variables)

        # El mensaje tiene que verse en el hilo del panel, o la asesora no sabe
        # qué recibió su cliente y se lo repite a mano.
        try:
            await mongo_mgr.save_message(
                phone=phone_normalized,
                content=decision.cuerpo,
                sender="bot",
                channel=canal,
                hubspot_contact_id=contact_id,
                message_sid=envio.get("message_sid"),
                metadata={
                    "source": "Confirmación automática de cita",
                    "template_id": plantilla_id,
                    "appointment_id": appointment_id,
                },
            )
            await _legacy._get_state_manager().update_activity(phone_normalized, canal=canal)
            rc = await _legacy._get_redis_client()
            await ws_manager.publish_broadcast(rc, {
                "type": "contact_updated",
                "action": "new_message",
                "phone": phone_normalized,
                "canal": canal,
                "sender": "bot",
                "preview": decision.cuerpo[:100],
            })
        except Exception as e_hist:
            logger.warning(
                f"[Panel] Mensaje de cita enviado pero no se reflejó en el panel: {e_hist}"
            )

        return True, "enviado"
    except Exception as e:
        logger.error(f"[Panel] Error enviando mensaje de cita {safe_id(appointment_id, 'appointment')}: {safe_error(e)}")
        return False, f"error: {e}"
def _fecha_larga_nota(momento) -> str:
    """
    "Miercoles, 19 Ago 2026 | 04:00 PM (Hora Colombia)".

    Los nombres de dias y meses salen de utils.date_parser, su fuente unica.
    """
    return (
        f"{DIAS_SEMANA_CAP[momento.weekday()]}, "
        f"{momento.day} {MESES_ABREV_CAP[momento.month - 1]} "
        f"{momento.year} | "
        f"{hora_12h(momento, con_cero=True)} (Hora Colombia)"
    )
def _texto_nota_cita(worker_name: str, momento, direccion: str, notas: str) -> str:
    """
    La nota interna de la cita: la que va al timeline de HubSpot y al hilo del panel.

    Vive en una funcion y no incrustada en el endpoint porque se arma DOS veces:
    al crear la cita y al editarla. Duplicar el texto garantizaba que la nota de
    una cita editada acabara diciendo algo distinto de la de una cita nueva.
    """
    extra = f"\nNotas: {notas}" if (notas or "").strip() else ""
    return (
        f"📅 CITA PROGRAMADA\n"
        f"Encargado: {worker_name}\n"
        f"Fecha: {_fecha_larga_nota(momento)}\n"
        f"Lugar: {direccion}"
        f"{extra}"
    )
class AppointmentCreateRequest(BaseModel):
    worker_id: str = Field(..., description="ID del worker en MongoDB")
    worker_name: str = Field(..., description="Nombre del worker (desnormalizado para rapidez)")
    appointment_dt: str = Field(..., description="Fecha y hora en ISO 8601 (ej. 2026-03-10T10:00:00)")
    # Obligatoria: es el {lugar} de la confirmación que recibe el cliente. Se pide
    # como campo propio y no dentro de las observaciones porque en 90 días y 326
    # citas NINGUNA tenía observaciones escritas — un campo opcional aquí saldría
    # vacío y el cliente recibiría su cita sin dirección.
    direccion: str = Field(..., max_length=200, description="Dirección del inmueble a visitar")
    notes: str = Field("", description="Observaciones de la cita")
    advisor_id: Optional[str] = Field(None, description="HubSpot owner ID de la asesora")
    canal: str = Field("whatsapp", description="Canal de origen del contacto (whatsapp, instagram, etc.)")
async def create_appointment(
    contact_id: str,
    body: AppointmentCreateRequest,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """
    Agenda una cita para un contacto:
    1. Crea nota en HubSpot con formato estructurado
    2. Persiste la cita en MongoDB
    3. Retorna el appointment_id
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    # Se valida ANTES de tocar HubSpot o MongoDB: una cita a medio crear es peor
    # que una cita rechazada, porque nadie se entera de que le falta algo.
    direccion = (body.direccion or "").strip()
    if not direccion:
        raise HTTPException(
            status_code=422,
            detail=(
                "La direccion del inmueble es obligatoria: "
                "es el lugar que se le envia al cliente."
            ),
        )

    from datetime import datetime as dt
    from zoneinfo import ZoneInfo
    BOGOTA_TZ = ZoneInfo("America/Bogota")

    # Parsear fecha y convertir a timezone Colombia
    try:
        appt_dt_utc = dt.fromisoformat(body.appointment_dt)
        if appt_dt_utc.tzinfo is None:
            appt_dt_utc = appt_dt_utc.replace(tzinfo=BOGOTA_TZ)
        appt_dt_bogota = appt_dt_utc.astimezone(BOGOTA_TZ)
    except ValueError:
        raise HTTPException(status_code=422, detail="Formato de fecha inválido. Use ISO 8601.")

    fecha_str = _fecha_larga_nota(appt_dt_bogota)

    # Resolver el encargado para saber que telefono se le da al cliente. Si no
    # tiene ficha o no tiene telefono, la cita sigue adelante: se agenda igual y
    # la confirmacion se omite con motivo. Agendar no puede depender de un campo
    # de configuracion sin llenar.
    mongo_mgr = _legacy.get_mongo_manager()
    telefono_encargado = ""
    try:
        ficha = await mongo_mgr.get_worker(body.worker_id)
        if ficha:
            telefono_encargado = (ficha.get("phone") or "").strip()
    except Exception as e_worker:
        logger.warning(f"[Panel] No se pudo leer la ficha del encargado: {safe_error(e_worker)}")

    note_body = _texto_nota_cita(body.worker_name, appt_dt_bogota, direccion, body.notes)

    # Crear nota en HubSpot (en background para no bloquear)
    hubspot_note_id = None
    try:
        hs_client = _legacy._hs_singleton
        hubspot_note_id = await hs_client.create_note(
            contact_id=contact_id,
            body=note_body,
            owner_id=body.advisor_id
        )
        logger.info(f"[Panel] Nota de cita creada en HubSpot: {hubspot_note_id}")
    except Exception as hs_err:
        logger.warning(f"[Panel] No se pudo crear nota en HubSpot para cita: {hs_err}")
        # No falla el endpoint — la cita se guarda en MongoDB de todas formas

    # Obtener phone y firstname del contacto para guardar en MongoDB y Redis
    phone = ""
    contact_firstname = ""
    try:
        hs_info = await _legacy._get_hubspot_contact_info(contact_id)
        if hs_info:
            phone = hs_info.get("phone", "")
            contact_firstname = (hs_info.get("firstname") or "").strip()
    except Exception:
        pass
    if contact_firstname:
        logger.info(f"[Naming] Nombre resuelto desde HubSpot: {safe_id(contact_firstname, 'firstname')} para contact_id={safe_id(contact_id, 'contact')}")
    else:
        logger.info(f"[Naming] Sin firstname en HubSpot para contact_id={safe_id(contact_id, 'contact')} — se usará fallback en scheduler")

    # Persistir en MongoDB
    appointment_id = await mongo_mgr.create_appointment(
        contact_id=contact_id,
        phone=phone,
        advisor_id=body.advisor_id or "",
        worker_id=body.worker_id,
        worker_name=body.worker_name,
        appointment_dt=appt_dt_utc,
        notes=body.notes,
        hubspot_note_id=hubspot_note_id,
        contact_name=contact_firstname or None,
        canal=body.canal,
        direccion=direccion,
        worker_phone=telefono_encargado,
    )

    if not appointment_id:
        raise HTTPException(status_code=500, detail="Error guardando la cita en base de datos")

    # Insertar nota de cita en el historial de la conversación (colección messages)
    if phone:
        try:
            await mongo_mgr.save_message(
                phone=phone,
                content=note_body,
                sender="system",
                channel=body.canal,
                hubspot_contact_id=contact_id,
                metadata={
                    "type": "appointment_created",
                    "appointment_id": appointment_id,
                    "worker_name": body.worker_name,
                    "fecha_display": fecha_str,
                }
            )
            logger.info(f"[Panel] Nota de cita guardada en historial: contact={safe_id(contact_id, 'contact')}")
        except Exception as msg_err:
            logger.warning(f"[Panel] No se pudo guardar nota de cita en historial: {msg_err}")

    # Se normaliza una sola vez: lo usan el sync a Redis y la confirmacion.
    phone_normalized = ""
    if phone:
        _norm = PhoneNormalizer().normalize(phone)
        phone_normalized = _norm.normalized if _norm.is_valid else phone

    # Sincronizar cita a Redis para que el scheduler de recordatorios la detecte
    if phone_normalized:
        try:
            from middleware.appointment_manager import AppointmentManager
            redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))

            apt_manager = AppointmentManager(redis_url)
            try:
                await apt_manager.create_appointment(
                    phone_normalized=phone_normalized,
                    canal=body.canal,
                    scheduled_datetime=appt_dt_bogota,
                    contact_name=contact_firstname or None,
                    contact_id=contact_id,
                    notes=body.notes or None,
                    advisor_id=body.advisor_id or None,
                )
                logger.info(
                    f"[Panel] Cita sincronizada a Redis: phone={phone_normalized}, canal={body.canal}, "
                    f"dt={appt_dt_bogota.isoformat()}"
                )
            finally:
                await apt_manager.close()
        except Exception as redis_err:
            # No falla el endpoint — MongoDB ya tiene la cita
            logger.warning(f"[Panel] No se pudo sincronizar cita a Redis (recordatorios pueden no funcionar): {redis_err}")

    
    # Embudo y confirmación al cliente
    
    # Van DESPUÉS del guardado y ninguno puede tumbar el endpoint: la cita ya
    # está en firme y es el dato valioso. Un fallo aquí se reporta en la
    # respuesta, no se traga en un log que nadie mira.

    # 1) Embudo → 'Visita agendada'. Automatiza lo que la asesora ya hacía a mano
    #    justo antes de agendar (medido: mediana 12 s antes, 300 de 310 casos).
    etapa_ok, etapa_detalle = await _legacy._update_contact_to_visita_agendada(contact_id)
    if not etapa_ok:
        logger.warning(
            f"[Panel] Cita {appointment_id}: no se movió a 'Visita agendada' — {etapa_detalle}"
        )

    # 2) Confirmación por WhatsApp al cliente. `comunicadas={}` porque a esta cita
    #    todavía no se le ha dicho nada: siempre hay algo nuevo que contar.
    confirmacion_enviada, confirmacion_motivo = await _enviar_confirmacion_cita(
        appointment_id=appointment_id,
        contact_id=contact_id,
        phone_normalized=phone_normalized,
        canal=body.canal or "whatsapp",
        advisor_id=body.advisor_id,
        plantilla_id=PLANTILLA_CONFIRMACION_CITA,
        datos=DatosCita(
            fecha_hora=appt_dt_bogota,
            lugar=direccion,
            encargado=body.worker_name,
            telefono_encargado=telefono_encargado,
            telefono_cliente=phone_normalized,
        ),
        comunicadas={},
    )

    logger.info(
        f"[Panel] Cita agendada: contact={contact_id}, worker={body.worker_name}, "
        f"fecha={fecha_str}, appt_id={appointment_id}, "
        f"etapa={etapa_ok}, confirmacion={confirmacion_enviada} ({confirmacion_motivo})"
    )
    return {
        "appointment_id": appointment_id,
        "hubspot_note_id": hubspot_note_id,
        "worker_name": body.worker_name,
        "appointment_dt": appt_dt_bogota.isoformat(),
        "fecha_display": fecha_str,
        "direccion": direccion,
        "note_body": note_body,
        # El panel usa estos tres para avisar en vez de dar un OK que miente.
        "stage_updated": etapa_ok,
        "stage_detail": etapa_detalle,
        "confirmation_sent": confirmacion_enviada,
        "confirmation_reason": confirmacion_motivo,
    }
async def get_appointments(
    contact_id: str,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Lista las citas de un contacto."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = _legacy.get_mongo_manager()
    appts = await mongo_mgr.get_appointments(contact_id)
    return {"appointments": appts, "total": len(appts)}
async def cancel_appointment(
    appointment_id: str,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Cancela una cita existente y sincroniza Redis para que el scheduler no la envíe."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = _legacy.get_mongo_manager()

    # Leer antes de cancelar para obtener phone (canal no se guarda en MongoDB → default whatsapp)
    apt_doc = await mongo_mgr.get_appointment_by_id(appointment_id)

    ok = await mongo_mgr.cancel_appointment(appointment_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Cita no encontrada")

    # Sincronizar Redis — evita que el scheduler envíe recordatorio de cita cancelada
    if apt_doc and apt_doc.get("phone"):
        try:
            from middleware.appointment_manager import AppointmentManager
            redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
            apt_mgr = AppointmentManager(redis_url)
            try:
                await apt_mgr.cancel_appointment(apt_doc["phone"], apt_doc.get("canal", "whatsapp"))
                logger.info("[Panel] Cita cancelada en Redis: %s", safe_phone(apt_doc["phone"]))
            finally:
                await apt_mgr.close()
        except Exception as redis_err:
            logger.error("[Panel] Error sincronizando cancelación en Redis: %s", redis_err)

    return {"ok": True}
class AppointmentUpdateBody(BaseModel):
    worker_id: str = None
    worker_name: str = None
    appointment_dt: str = None
    direccion: str = None
    notes: str = None
async def update_appointment(
    appointment_id: str,
    body: AppointmentUpdateBody,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Actualiza una cita existente."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    from datetime import datetime as dt
    from zoneinfo import ZoneInfo
    BOGOTA_TZ = ZoneInfo("America/Bogota")

    appt_dt = None
    if body.appointment_dt:
        try:
            appt_dt = dt.fromisoformat(body.appointment_dt)
            if appt_dt.tzinfo is None:
                appt_dt = appt_dt.replace(tzinfo=BOGOTA_TZ)
        except ValueError:
            raise HTTPException(status_code=422, detail="Formato de fecha inválido")

    mongo_mgr = _legacy.get_mongo_manager()

    # Si cambia el encargado, su telefono cambia con el: la copia de la cita
    # tiene que seguirlo o un reenvio daria el numero de otra persona.
    telefono_encargado = None
    if body.worker_id:
        try:
            ficha = await mongo_mgr.get_worker(body.worker_id)
            telefono_encargado = (ficha or {}).get("phone", "")
        except Exception as e_worker:
            logger.warning(f"[Panel] No se pudo releer el encargado al editar cita: {safe_error(e_worker)}")

    ok = await mongo_mgr.update_appointment(
        appointment_id=appointment_id,
        worker_id=body.worker_id,
        worker_name=body.worker_name,
        appointment_dt=appt_dt,
        notes=body.notes,
        direccion=(body.direccion.strip() if body.direccion is not None else None),
        worker_phone=telefono_encargado,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Cita no encontrada")

    # El panel necesita saber si al cliente se le aviso, para no dar un OK que
    # deje a la asesora creyendo que el cliente ya conoce la nueva fecha.
    respuesta = {"ok": True, "client_notified": False, "client_notify_reason": "no_evaluado"}

    # Refrescar la nota interna
    # La nota que se escribió al crear la cita queda mintiendo tras una edición:
    # sigue diciendo la fecha, el encargado y la dirección viejos. La asesora abre
    # la conversación, lee la nota y actúa sobre datos caducados.
    #
    # Se rearma desde el documento YA GUARDADO, no desde `body`: un PATCH puede
    # traer solo un campo, y componer la nota con lo que llegó dejaría el resto
    # vacío. Leer lo persistido es la única fuente completa.
    try:
        cita = await mongo_mgr.get_appointment_by_id(appointment_id)
        if cita:
            momento = cita.get("appointment_dt")
            if momento is not None:
                # Mongo devuelve los datetime en UTC y sin tzinfo. Restarle nada y
                # formatearlo directo desplazaría la nota 5 horas.
                if momento.tzinfo is None:
                    momento = momento.replace(tzinfo=timezone.utc)
                momento = momento.astimezone(BOGOTA_TZ)

                nota = _texto_nota_cita(
                    cita.get("worker_name", ""),
                    momento,
                    cita.get("direccion", ""),
                    cita.get("notes", ""),
                )
                fecha_str = _fecha_larga_nota(momento)

                await mongo_mgr.update_appointment_note_message(
                    appointment_id=appointment_id,
                    contenido=nota,
                    worker_name=cita.get("worker_name", ""),
                    fecha_display=fecha_str,
                )

                # El timeline de HubSpot lo consulta quien no entra al panel, así
                # que no puede quedarse con la versión vieja mientras el panel
                # muestra la nueva.
                nota_hs = cita.get("hubspot_note_id")
                if nota_hs:
                    try:
                        await _legacy._hs_singleton.update_note(nota_hs, nota)
                    except Exception as e_hs:
                        logger.warning(
                            f"[Panel] Nota de HubSpot {nota_hs} no se pudo refrescar: {e_hs}"
                        )

                # Avisar al cliente si cambió algo que él ve. `_enviar_confirmacion_cita`
                # compara los datos con los del último mensaje que recibió, así que
                # editar una observación interna no le cuesta un WhatsApp.
                reenviada, reenvio_motivo = await _enviar_confirmacion_cita(
                    appointment_id=appointment_id,
                    contact_id=cita.get("contact_id", ""),
                    phone_normalized=cita.get("phone", ""),
                    canal=cita.get("canal", "whatsapp"),
                    advisor_id=cita.get("advisor_id"),
                    plantilla_id=PLANTILLA_CONFIRMACION_CITA,
                    datos=DatosCita(
                        fecha_hora=momento,
                        lugar=cita.get("direccion", ""),
                        encargado=cita.get("worker_name", ""),
                        telefono_encargado=cita.get("worker_phone", ""),
                        telefono_cliente=cita.get("phone", ""),
                    ),
                    comunicadas=cita.get("confirmacion_variables") or {},
                )
                respuesta["client_notified"] = reenviada
                respuesta["client_notify_reason"] = reenvio_motivo

                # El panel tiene la nota vieja en memoria hasta que recargue.
                if cita.get("phone"):
                    try:
                        rc = await _legacy._get_redis_client()
                        await ws_manager.publish_broadcast(rc, {
                            "type": "contact_updated",
                            "action": "appointment_updated",
                            "phone": cita["phone"],
                            "canal": cita.get("canal", "whatsapp"),
                            "appointment_id": appointment_id,
                        })
                    except Exception as e_ws:
                        logger.warning(f"[Panel] WS de cita editada falló: {safe_error(e_ws)}")
    except Exception as e_nota:
        # La cita ya está actualizada; que la nota no se refresque no puede
        # convertir una edición correcta en un error para la asesora.
        logger.error(f"[Panel] No se pudo refrescar la nota de la cita {safe_id(appointment_id, 'appointment')}: {safe_error(e_nota)}")

    # Sync Redis si cambió appointment_dt — actualiza ZSET score y resetea flags
    # para que el scheduler use la nueva ventana de recordatorio/seguimiento
    if appt_dt:
        try:
            apt_doc = await mongo_mgr.get_appointment_by_id(appointment_id)
            if apt_doc and apt_doc.get("phone"):
                from middleware.appointment_manager import AppointmentManager
                redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
                apt_mgr = AppointmentManager(redis_url)
                try:
                    rescheduled = await apt_mgr.reschedule_appointment(
                        apt_doc["phone"],
                        apt_doc.get("canal", "whatsapp"),
                        appt_dt
                    )
                finally:
                    await apt_mgr.close()
                if rescheduled:
                    logger.info("[Panel] Cita reprogramada en Redis: %s → %s", safe_phone(apt_doc["phone"]), appt_dt)
                else:
                    logger.warning("[Panel] Cita no encontrada en Redis para reprogramar: %s", safe_phone(apt_doc["phone"]))
        except Exception as redis_err:
            logger.warning("[Panel] Error sync Redis en reprogramación (non-fatal): %s", redis_err)

    return respuesta
async def delete_appointment(
    appointment_id: str,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Elimina una cita permanentemente y limpia Redis para que el scheduler no la envíe."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = _legacy.get_mongo_manager()

    # Leer antes de borrar para obtener phone (canal no se guarda en MongoDB → default whatsapp)
    apt_doc = await mongo_mgr.get_appointment_by_id(appointment_id)

    ok = await mongo_mgr.delete_appointment(appointment_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Cita no encontrada")

    # Limpiar Redis — evita que el scheduler envíe recordatorio de cita eliminada
    if apt_doc and apt_doc.get("phone"):
        try:
            from middleware.appointment_manager import AppointmentManager
            redis_url = os.getenv("REDIS_PUBLIC_URL", os.getenv("REDIS_URL", "redis://localhost:6379"))
            apt_mgr = AppointmentManager(redis_url)
            try:
                await apt_mgr.cancel_appointment(apt_doc["phone"], apt_doc.get("canal", "whatsapp"))
                logger.info("[Panel] Cita eliminada de Redis: %s", safe_phone(apt_doc["phone"]))
            finally:
                await apt_mgr.close()
        except Exception as redis_err:
            logger.error("[Panel] Error limpiando cita eliminada en Redis: %s", redis_err)

    return {"ok": True}
