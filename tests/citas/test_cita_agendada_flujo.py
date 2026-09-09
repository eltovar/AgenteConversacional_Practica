"""
QA Fase 6 — Orquestación de POST /contacts/{id}/appointments.

El módulo puro (`confirmacion_cita`) ya está probado aparte. Aquí se prueba lo
que NO es puro: el orden de las operaciones y qué sobrevive a cada fallo.

LA PREGUNTA QUE RESPONDE CADA TEST
    Si HubSpot falla, ¿se pierde la cita?          → no
    Si Twilio falla, ¿se pierde la cita?           → no
    Si el encargado no tiene teléfono, ¿se agenda? → sí, sin confirmación
    Si falta la dirección, ¿se escribe algo?       → nada, se rechaza antes
    Si el segundo PATCH de etapa falla, ¿el contacto queda sin etapa? → no

EL FALLO QUE ORIGINA EL DESHACER
    `lifecyclestage` en HubSpot es unidireccional: para mover un contacto hacia
    atrás hay que limpiar el campo y volver a fijarlo. Entre los dos pasos el
    contacto NO TIENE ETAPA: no lo ve el panel, no lo alcanza ninguna regla de
    embudo, y nadie se entera. Pasó de verdad el 18-ago-2026 con la depuración
    de "No responde" y dejó un contacto atrapado hasta que se rescató a mano.

Ejecutar:
    python -m pytest tests/panel/test_cita_agendada_flujo.py -v
"""
import os
import sys
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import middleware.outbound_panel as panel  # noqa: E402
from middleware.outbound_panel import (  # noqa: E402
    HUBSPOT_STAGE_EN_CONVERSACION,
    HUBSPOT_STAGE_VISITA_AGENDADA,
    AppointmentCreateRequest,
    _update_contact_to_visita_agendada,
    create_appointment,
)
from middleware.templates.templates import DEFAULT_TEMPLATES  # noqa: E402

CONTACTO = "216000000001"
CLAVE = "clave-de-prueba"
PLANTILLA = DEFAULT_TEMPLATES["cita_confirmacion"]


def respuesta(codigo=200, texto=""):
    r = MagicMock()
    r.status_code = codigo
    r.text = texto
    return r


# ═══════════════════════════════════════════════════════════════════════════
# 1. El paso de etapa a "Visita agendada"
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sin_contacto_no_se_toca_hubspot():
    with patch.object(panel, "_hubspot_patch", new=AsyncMock()) as parche:
        ok, detalle = await _update_contact_to_visita_agendada("")
        assert ok is False
        parche.assert_not_called()


@pytest.mark.asyncio
async def test_si_ya_estaba_en_visita_agendada_no_se_escribe_nada():
    """Idempotencia: reagendar dos veces no genera dos escrituras en HubSpot."""
    with patch.object(panel, "HUBSPOT_API_KEY", "clave"), \
         patch.object(panel, "_get_contact_lifecyclestage",
                      new=AsyncMock(return_value=HUBSPOT_STAGE_VISITA_AGENDADA)), \
         patch.object(panel, "_hubspot_patch", new=AsyncMock()) as parche:
        ok, detalle = await _update_contact_to_visita_agendada(CONTACTO)
        assert ok is True
        assert detalle == "ya_estaba"
        parche.assert_not_called()


@pytest.mark.asyncio
async def test_el_cambio_es_en_dos_pasos_limpiar_y_fijar():
    """
    Un solo PATCH no bastaría: HubSpot sólo deja avanzar el lifecyclestage, así
    que traer a alguien desde una etapa posterior exige limpiar primero.
    """
    parche = AsyncMock(return_value=respuesta(200))
    with patch.object(panel, "HUBSPOT_API_KEY", "clave"), \
         patch.object(panel, "_get_contact_lifecyclestage",
                      new=AsyncMock(return_value=HUBSPOT_STAGE_EN_CONVERSACION)), \
         patch.object(panel, "_invalidate_contact_stage_cache", new=AsyncMock()), \
         patch.object(panel, "_hubspot_patch", new=parche):
        ok, detalle = await _update_contact_to_visita_agendada(CONTACTO)

    assert (ok, detalle) == (True, "movido")
    assert parche.await_count == 2
    assert parche.await_args_list[0].args[1] == {"properties": {"lifecyclestage": ""}}
    assert parche.await_args_list[1].args[1] == {
        "properties": {"lifecyclestage": HUBSPOT_STAGE_VISITA_AGENDADA}
    }


@pytest.mark.asyncio
async def test_no_hay_guard_de_etapas_protegidas():
    """
    Decisión explícita del usuario (19-ago-2026): agendar pisa cualquier etapa.
    De 310 contactos con cita, 10 venían de una etapa avanzada y la asesora la
    pisó igual a mano. Al contrario que `_update_contact_to_visita_realizada`,
    aquí no se respeta PROTECTED_STAGES_POST_VISITA.
    """
    for protegida in sorted(panel.PROTECTED_STAGES_POST_VISITA):
        parche = AsyncMock(return_value=respuesta(200))
        with patch.object(panel, "HUBSPOT_API_KEY", "clave"), \
             patch.object(panel, "_get_contact_lifecyclestage",
                          new=AsyncMock(return_value=protegida)), \
             patch.object(panel, "_invalidate_contact_stage_cache", new=AsyncMock()), \
             patch.object(panel, "_hubspot_patch", new=parche):
            ok, _ = await _update_contact_to_visita_agendada(CONTACTO)
        assert ok is True, f"{protegida} debería poder pisarse"


@pytest.mark.asyncio
async def test_si_falla_el_limpiado_no_se_toco_nada():
    """El contacto sigue en su etapa: no hay nada que deshacer."""
    parche = AsyncMock(return_value=respuesta(500))
    with patch.object(panel, "HUBSPOT_API_KEY", "clave"), \
         patch.object(panel, "_get_contact_lifecyclestage",
                      new=AsyncMock(return_value=HUBSPOT_STAGE_EN_CONVERSACION)), \
         patch.object(panel, "_hubspot_patch", new=parche):
        ok, detalle = await _update_contact_to_visita_agendada(CONTACTO)

    assert ok is False
    assert detalle == "limpiar_HTTP_500"
    assert parche.await_count == 1  # no se intenta fijar ni deshacer


@pytest.mark.asyncio
async def test_si_falla_el_fijado_el_contacto_vuelve_a_su_etapa():
    """LA REGRESIÓN: nadie puede quedarse sin etapa."""
    parche = AsyncMock(side_effect=[
        respuesta(200),   # limpiar → el contacto queda SIN ETAPA
        respuesta(500),   # fijar   → falla
        respuesta(200),   # deshacer
    ])
    with patch.object(panel, "HUBSPOT_API_KEY", "clave"), \
         patch.object(panel, "_get_contact_lifecyclestage",
                      new=AsyncMock(return_value=HUBSPOT_STAGE_EN_CONVERSACION)), \
         patch.object(panel, "_invalidate_contact_stage_cache", new=AsyncMock()), \
         patch.object(panel, "_hubspot_patch", new=parche):
        ok, detalle = await _update_contact_to_visita_agendada(CONTACTO)

    assert ok is False
    assert "deshecho" in detalle
    assert parche.await_count == 3
    assert parche.await_args_list[2].args[1] == {
        "properties": {"lifecyclestage": HUBSPOT_STAGE_EN_CONVERSACION}
    }


@pytest.mark.asyncio
async def test_un_corte_de_red_a_mitad_tambien_deshace():
    """Un WinError 10054 entre los dos pasos abre el mismo hueco que un HTTP 500."""
    parche = AsyncMock(side_effect=[
        respuesta(200),
        ConnectionResetError("conexión cerrada por el host remoto"),
        respuesta(200),
    ])
    with patch.object(panel, "HUBSPOT_API_KEY", "clave"), \
         patch.object(panel, "_get_contact_lifecyclestage",
                      new=AsyncMock(return_value=HUBSPOT_STAGE_EN_CONVERSACION)), \
         patch.object(panel, "_invalidate_contact_stage_cache", new=AsyncMock()), \
         patch.object(panel, "_hubspot_patch", new=parche):
        ok, detalle = await _update_contact_to_visita_agendada(CONTACTO)

    assert ok is False
    assert "deshecho" in detalle


@pytest.mark.asyncio
async def test_si_el_deshacer_tambien_falla_se_grita():
    """El caso que dejó a un contacto atrapado. Tiene que quedar en el detalle."""
    parche = AsyncMock(side_effect=[respuesta(200), respuesta(500), respuesta(500)])
    with patch.object(panel, "HUBSPOT_API_KEY", "clave"), \
         patch.object(panel, "_get_contact_lifecyclestage",
                      new=AsyncMock(return_value=HUBSPOT_STAGE_EN_CONVERSACION)), \
         patch.object(panel, "_invalidate_contact_stage_cache", new=AsyncMock()), \
         patch.object(panel, "_hubspot_patch", new=parche):
        ok, detalle = await _update_contact_to_visita_agendada(CONTACTO)

    assert ok is False
    assert "SIN ETAPA" in detalle


@pytest.mark.asyncio
async def test_al_moverse_se_invalida_la_cache_de_etapa():
    """Si no, el panel sigue mostrando la etapa vieja hasta que expire el TTL."""
    invalidar = AsyncMock()
    with patch.object(panel, "HUBSPOT_API_KEY", "clave"), \
         patch.object(panel, "_get_contact_lifecyclestage",
                      new=AsyncMock(return_value=HUBSPOT_STAGE_EN_CONVERSACION)), \
         patch.object(panel, "_invalidate_contact_stage_cache", new=invalidar), \
         patch.object(panel, "_hubspot_patch", new=AsyncMock(return_value=respuesta(200))):
        await _update_contact_to_visita_agendada(CONTACTO)
    invalidar.assert_awaited_once_with(CONTACTO, HUBSPOT_STAGE_VISITA_AGENDADA)


# ═══════════════════════════════════════════════════════════════════════════
# 2. El endpoint completo
# ═══════════════════════════════════════════════════════════════════════════

def _mongo(telefono_encargado="+573001234567"):
    m = MagicMock()
    m.get_worker = AsyncMock(return_value={
        "id": "w1", "name": "Mauricio Restrepo",
        "phone": telefono_encargado, "active": True,
    })
    m.create_appointment = AsyncMock(return_value="appt-1")
    m.save_message = AsyncMock(return_value="msg-1")
    m.marcar_confirmacion_enviada = AsyncMock(return_value=True)
    return m


def _peticion(direccion="Calle 10 #43-25, Apto 502, Envigado"):
    return AppointmentCreateRequest(
        worker_id="w1",
        worker_name="Mauricio Restrepo",
        appointment_dt="2026-09-15T15:30:00",
        direccion=direccion,
        notes="",
        advisor_id="89096378",
        canal="whatsapp",
    )


class _EntornoCita(ExitStack):
    """
    Deja el endpoint aislado de la red. Expone los dobles que cada test inspecciona.

    Se monta como context manager y no como fixture porque varios tests necesitan
    cambiar UNA pieza (que Twilio falle, que no haya teléfono) y el resto igual.
    """

    def __init__(self, mongo=None, envio=None, etapa=(True, "movido"), plantilla=PLANTILLA):
        super().__init__()
        self.mongo = mongo or _mongo()
        self.twilio = MagicMock()
        self.twilio.is_available = True
        self.twilio.send_whatsapp_message = AsyncMock(
            return_value=envio or {"status": "success", "message_sid": "SM1"}
        )
        self.etapa = AsyncMock(return_value=etapa)
        self.plantilla = plantilla
        self.notas = AsyncMock(return_value="nota-1")

    def __enter__(self):
        super().__enter__()
        p = self.enter_context
        p(patch.object(panel, "_validate_api_key", return_value=True))
        p(patch.object(panel, "get_mongo_manager", return_value=self.mongo))
        p(patch.object(panel, "_hs_singleton", MagicMock(create_note=self.notas)))
        p(patch.object(panel, "_get_hubspot_contact_info",
                       new=AsyncMock(return_value={"phone": "+573009998877",
                                                   "firstname": "Ana"})))
        p(patch.object(panel, "_update_contact_to_visita_agendada", new=self.etapa))
        p(patch.object(panel, "_init_default_templates", new=AsyncMock()))
        p(patch.object(panel, "_get_template_by_advisor",
                       new=AsyncMock(return_value=self.plantilla)))
        p(patch.object(panel, "twilio_client", self.twilio))
        p(patch.object(panel, "_get_state_manager",
                       return_value=MagicMock(update_activity=AsyncMock())))
        p(patch.object(panel, "_get_redis_client", new=AsyncMock(return_value=MagicMock())))
        p(patch.object(panel, "ws_manager", MagicMock(publish_broadcast=AsyncMock())))
        gestor = MagicMock()
        gestor.create_appointment = AsyncMock()
        gestor.close = AsyncMock()
        p(patch("middleware.appointment_manager.AppointmentManager", return_value=gestor))
        return self


@pytest.mark.asyncio
async def test_el_camino_feliz_agenda_mueve_la_etapa_y_confirma():
    with _EntornoCita() as e:
        r = await create_appointment(CONTACTO, _peticion(), CLAVE)

    assert r["appointment_id"] == "appt-1"
    assert r["stage_updated"] is True
    assert r["confirmation_sent"] is True
    e.twilio.send_whatsapp_message.assert_awaited_once()
    enviado = e.twilio.send_whatsapp_message.await_args.kwargs
    assert enviado["to"] == "+573009998877"
    assert "Calle 10 #43-25, Apto 502, Envigado" in enviado["body"]
    assert "+573001234567" in enviado["body"]


@pytest.mark.asyncio
async def test_la_direccion_se_guarda_en_la_cita_y_en_la_nota():
    with _EntornoCita() as e:
        r = await create_appointment(CONTACTO, _peticion(), CLAVE)

    assert e.mongo.create_appointment.await_args.kwargs["direccion"] == (
        "Calle 10 #43-25, Apto 502, Envigado"
    )
    assert e.mongo.create_appointment.await_args.kwargs["worker_phone"] == "+573001234567"
    assert "Lugar: Calle 10 #43-25, Apto 502, Envigado" in r["note_body"]


@pytest.mark.asyncio
async def test_sin_direccion_no_se_escribe_absolutamente_nada():
    """Se valida antes que todo: una cita a medio crear no la ve nadie."""
    from fastapi import HTTPException

    for vacia in ("", "   "):
        with _EntornoCita() as e:
            with pytest.raises(HTTPException) as fallo:
                await create_appointment(CONTACTO, _peticion(direccion=vacia), CLAVE)
            assert fallo.value.status_code == 422
            e.mongo.create_appointment.assert_not_awaited()
            e.notas.assert_not_awaited()
            e.etapa.assert_not_awaited()
            e.twilio.send_whatsapp_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_encargado_sin_telefono_agenda_igual_pero_avisa():
    """
    Los 6 encargados de producción no tienen teléfono todavía. Bloquear el
    agendamiento por un campo de configuración sin llenar dejaría el panel
    inservible hasta que alguien los escriba.
    """
    with _EntornoCita(mongo=_mongo(telefono_encargado="")) as e:
        r = await create_appointment(CONTACTO, _peticion(), CLAVE)

    assert r["appointment_id"] == "appt-1"          # la cita existe
    assert r["stage_updated"] is True                # el embudo avanzó
    assert r["confirmation_sent"] is False
    assert r["confirmation_reason"] == "datos_incompletos:contacto"
    e.twilio.send_whatsapp_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_si_twilio_falla_la_cita_sobrevive():
    with _EntornoCita(envio={"status": "error", "message": "63049 fuera de ventana"}) as e:
        r = await create_appointment(CONTACTO, _peticion(), CLAVE)

    assert r["appointment_id"] == "appt-1"
    assert r["confirmation_sent"] is False
    assert "63049" in r["confirmation_reason"]
    # save_message se llama una sola vez: la nota de la cita en el historial, que
    # ya existia. La confirmacion NO se guarda, porque no salio: fingirla haria
    # que la asesora creyera que el cliente esta avisado.
    assert e.mongo.save_message.await_count == 1
    assert e.mongo.save_message.await_args.kwargs["sender"] == "system"


@pytest.mark.asyncio
async def test_si_twilio_revienta_la_cita_sobrevive():
    with _EntornoCita() as e:
        e.twilio.send_whatsapp_message.side_effect = RuntimeError("conexión caída")
        r = await create_appointment(CONTACTO, _peticion(), CLAVE)

    assert r["appointment_id"] == "appt-1"
    assert r["confirmation_sent"] is False
    assert r["confirmation_reason"].startswith("error:")


@pytest.mark.asyncio
async def test_si_la_etapa_no_se_mueve_la_cita_sobrevive():
    with _EntornoCita(etapa=(False, "fijar_HTTP_500 (deshecho: sigue en 1326623075)")) as e:
        r = await create_appointment(CONTACTO, _peticion(), CLAVE)

    assert r["appointment_id"] == "appt-1"
    assert r["stage_updated"] is False
    assert "deshecho" in r["stage_detail"]
    assert r["confirmation_sent"] is True   # una cosa no arrastra a la otra


@pytest.mark.asyncio
async def test_la_confirmacion_enviada_aparece_en_el_hilo_del_panel():
    """Si no se guarda, la asesora no sabe qué recibió su cliente y lo repite."""
    with _EntornoCita() as e:
        await create_appointment(CONTACTO, _peticion(), CLAVE)

    # Dos guardados: la nota interna de la cita (ya existia) y la confirmacion.
    assert e.mongo.save_message.await_count == 2
    guardado = e.mongo.save_message.await_args.kwargs  # el ultimo = la confirmacion
    assert guardado["sender"] == "bot"
    assert guardado["metadata"]["appointment_id"] == "appt-1"
    assert guardado["metadata"]["template_id"] == "cita_confirmacion"
    assert "Calle 10" in guardado["content"]


@pytest.mark.asyncio
async def test_el_orden_es_guardar_primero_y_avisar_despues():
    """
    La cita es el dato valioso; la etapa y el mensaje son derivados. Si el orden
    se invierte, un fallo de red deja la cita sin guardar.
    """
    orden = []
    mongo = _mongo()
    mongo.create_appointment = AsyncMock(
        side_effect=lambda **k: orden.append("guardar") or "appt-1")

    with _EntornoCita(mongo=mongo) as e:
        e.etapa.side_effect = lambda *a: orden.append("etapa") or (True, "movido")
        e.twilio.send_whatsapp_message.side_effect = (
            lambda **k: orden.append("enviar") or {"status": "success", "message_sid": "SM1"})
        await create_appointment(CONTACTO, _peticion(), CLAVE)

    assert orden == ["guardar", "etapa", "enviar"]


@pytest.mark.asyncio
async def test_un_contacto_sin_telefono_agenda_sin_confirmacion():
    with _EntornoCita() as e:
        with patch.object(panel, "_get_hubspot_contact_info",
                          new=AsyncMock(return_value={"phone": "", "firstname": "Ana"})):
            r = await create_appointment(CONTACTO, _peticion(), CLAVE)

    assert r["appointment_id"] == "appt-1"
    assert r["confirmation_sent"] is False
    assert r["confirmation_reason"] == "sin_telefono_del_cliente"
    e.twilio.send_whatsapp_message.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# 3. Editar una cita: la nota interna tiene que dejar de mentir
# ═══════════════════════════════════════════════════════════════════════════
# Detectado probando en local el 19-ago-2026: se edita la cita y la nota del
# hilo del panel sigue mostrando la fecha, el encargado y la direccion viejos.
# La asesora abre la conversacion, lee la nota y actua sobre datos caducados.

from datetime import datetime, timezone  # noqa: E402

from middleware.outbound_panel import (  # noqa: E402
    AppointmentUpdateBody,
    _fecha_larga_nota,
    _texto_nota_cita,
    update_appointment,
)

CITA = "cita-1"


def _mongo_edicion(**cambios):
    """Doble de Mongo con una cita ya guardada, como queda TRAS el update."""
    guardada = {
        "_id": CITA,
        "contact_id": CONTACTO,
        "phone": "+573009998877",
        "canal": "whatsapp",
        "worker_id": "w2",
        "worker_name": "Esteban Gomez",
        "worker_phone": "+573001112233",
        # Como lo devuelve Mongo de verdad: UTC y sin tzinfo.
        "appointment_dt": datetime(2026, 8, 20, 21, 0),   # 4:00 PM Bogota
        "direccion": "cra 41 33 b sur 40",
        "notes": "PRUEBA",
        "hubspot_note_id": "nota-hs-1",
    }
    guardada.update(cambios)
    m = MagicMock()
    m.get_worker = AsyncMock(return_value={"id": "w2", "name": "Esteban Gomez",
                                           "phone": "+573001112233", "active": True})
    m.update_appointment = AsyncMock(return_value=True)
    m.get_appointment_by_id = AsyncMock(return_value=guardada)
    m.update_appointment_note_message = AsyncMock(return_value=True)
    m.marcar_confirmacion_enviada = AsyncMock(return_value=True)
    m.save_message = AsyncMock(return_value="msg-2")
    return m


# Crear y reprogramar usan la MISMA plantilla: el cliente recibe siempre el
# mismo formato con los datos vigentes.
PLANTILLA_CITA = DEFAULT_TEMPLATES["cita_confirmacion"]


class _EntornoEdicion(ExitStack):
    def __init__(self, mongo=None):
        super().__init__()
        self.mongo = mongo or _mongo_edicion()
        self.nota_hs = AsyncMock(return_value=True)
        self.twilio = MagicMock()
        self.twilio.is_available = True
        self.twilio.send_whatsapp_message = AsyncMock(
            return_value={"status": "success", "message_sid": "SM2"}
        )

    def __enter__(self):
        super().__enter__()
        p = self.enter_context
        p(patch.object(panel, "_validate_api_key", return_value=True))
        p(patch.object(panel, "get_mongo_manager", return_value=self.mongo))
        p(patch.object(panel, "_hs_singleton", MagicMock(update_note=self.nota_hs)))
        p(patch.object(panel, "_init_default_templates", new=AsyncMock()))
        # Devuelve la plantilla que se PIDE, no una fija: un doble que ignora el
        # identificador no puede detectar que se use la plantilla equivocada.
        # Un doble asi dejo pasar una mutacion durante el desarrollo.
        self.plantillas_pedidas = []

        async def _plantilla(advisor_id, plantilla_id):
            self.plantillas_pedidas.append(plantilla_id)
            return DEFAULT_TEMPLATES.get(plantilla_id)

        p(patch.object(panel, "_get_template_by_advisor", new=_plantilla))
        p(patch.object(panel, "twilio_client", self.twilio))
        p(patch.object(panel, "_get_state_manager",
                       return_value=MagicMock(update_activity=AsyncMock())))
        p(patch.object(panel, "_get_redis_client", new=AsyncMock(return_value=MagicMock())))
        p(patch.object(panel, "ws_manager", MagicMock(publish_broadcast=AsyncMock())))
        gestor = MagicMock()
        gestor.reschedule_appointment = AsyncMock(return_value=True)
        gestor.close = AsyncMock()
        p(patch("middleware.appointment_manager.AppointmentManager", return_value=gestor))
        return self


@pytest.mark.asyncio
async def test_al_editar_la_nota_del_panel_se_reescribe():
    with _EntornoEdicion() as e:
        r = await update_appointment(
            CITA,
            AppointmentUpdateBody(
                worker_id="w2", worker_name="Esteban Gomez",
                appointment_dt="2026-08-20T16:00:00",
                direccion="cra 41 33 b sur 40", notes="PRUEBA",
            ),
            CLAVE,
        )

    assert r["ok"] is True
    e.mongo.update_appointment_note_message.assert_awaited_once()
    escrito = e.mongo.update_appointment_note_message.await_args.kwargs
    assert escrito["appointment_id"] == CITA
    assert "Esteban Gomez" in escrito["contenido"]
    assert "cra 41 33 b sur 40" in escrito["contenido"]
    assert "PRUEBA" in escrito["contenido"]


@pytest.mark.asyncio
async def test_la_nota_editada_lleva_la_hora_de_bogota_no_la_de_mongo():
    """
    Mongo devuelve los datetime en UTC y sin tzinfo. Formatearlos tal cual
    escribiria la nota con 5 horas de mas: una cita de las 4:00 PM saldria a las
    9:00 PM. Es el mismo fallo que ya mordio con los masivos.
    """
    with _EntornoEdicion() as e:
        await update_appointment(
            CITA,
            AppointmentUpdateBody(appointment_dt="2026-08-20T16:00:00"),
            CLAVE,
        )

    contenido = e.mongo.update_appointment_note_message.await_args.kwargs["contenido"]
    assert "04:00 PM" in contenido
    assert "09:00 PM" not in contenido


@pytest.mark.asyncio
async def test_la_nota_se_rearma_desde_lo_guardado_no_desde_el_patch():
    """
    Un PATCH puede traer un solo campo. Componer la nota con lo que llego dejaria
    el resto vacio: cambiar solo la hora borraria la direccion de la nota.
    """
    with _EntornoEdicion() as e:
        await update_appointment(
            CITA,
            AppointmentUpdateBody(appointment_dt="2026-08-20T16:00:00"),
            CLAVE,
        )

    contenido = e.mongo.update_appointment_note_message.await_args.kwargs["contenido"]
    assert "cra 41 33 b sur 40" in contenido      # no llego en el body
    assert "Esteban Gomez" in contenido            # tampoco


@pytest.mark.asyncio
async def test_la_nota_de_hubspot_tambien_se_refresca():
    """El timeline lo consulta quien no entra al panel; no puede quedarse viejo."""
    with _EntornoEdicion() as e:
        await update_appointment(
            CITA, AppointmentUpdateBody(direccion="otra direccion 123"), CLAVE
        )

    e.nota_hs.assert_awaited_once()
    identificador, cuerpo = e.nota_hs.await_args.args
    assert identificador == "nota-hs-1"
    assert cuerpo == e.mongo.update_appointment_note_message.await_args.kwargs["contenido"]


@pytest.mark.asyncio
async def test_una_cita_sin_nota_de_hubspot_no_revienta():
    """Las citas viejas no tienen hubspot_note_id."""
    with _EntornoEdicion(mongo=_mongo_edicion(hubspot_note_id=None)) as e:
        r = await update_appointment(
            CITA, AppointmentUpdateBody(notes="algo"), CLAVE
        )
    assert r["ok"] is True
    e.nota_hs.assert_not_awaited()


@pytest.mark.asyncio
async def test_si_la_nota_falla_la_edicion_sigue_siendo_correcta():
    """La cita ya esta actualizada; un fallo al refrescar no puede volverla un error."""
    mongo = _mongo_edicion()
    mongo.update_appointment_note_message = AsyncMock(side_effect=RuntimeError("mongo caido"))
    with _EntornoEdicion(mongo=mongo):
        r = await update_appointment(
            CITA, AppointmentUpdateBody(notes="algo"), CLAVE
        )
    assert r["ok"] is True


@pytest.mark.asyncio
async def test_una_cita_que_ya_no_existe_no_intenta_reescribir_nada():
    mongo = _mongo_edicion()
    mongo.get_appointment_by_id = AsyncMock(return_value=None)
    with _EntornoEdicion(mongo=mongo) as e:
        r = await update_appointment(
            CITA, AppointmentUpdateBody(notes="algo"), CLAVE
        )
    assert r["ok"] is True
    e.mongo.update_appointment_note_message.assert_not_awaited()


def test_la_nota_de_crear_y_la_de_editar_son_el_mismo_texto():
    """
    Las dos salen de `_texto_nota_cita`. Si alguien duplica el texto en uno de los
    dos caminos, una cita editada acabaria diciendo algo distinto de una nueva.
    """
    momento = datetime(2026, 8, 19, 16, 0)
    nota = _texto_nota_cita("Esteban", momento, "cra 41 33 b sur 40", "PRUEBA")
    assert nota.splitlines() == [
        "\U0001F4C5 CITA PROGRAMADA",
        "Encargado: Esteban",
        f"Fecha: {_fecha_larga_nota(momento)}",
        "Lugar: cra 41 33 b sur 40",
        "Notas: PRUEBA",
    ]


def test_sin_observaciones_la_nota_no_deja_una_linea_de_notas_vacia():
    nota = _texto_nota_cita("Esteban", datetime(2026, 8, 19, 16, 0), "cra 41", "   ")
    assert "Notas:" not in nota
    assert nota.endswith("Lugar: cra 41")


# ═══════════════════════════════════════════════════════════════════════════
# 4. Reprogramar: el cliente tiene que enterarse
# ═══════════════════════════════════════════════════════════════════════════
# Si se mueve la cita del miercoles al viernes, la nota interna se actualizaba
# pero el cliente seguia con la fecha vieja en su WhatsApp. La regla es que se
# le reescribe cuando cambia algo que EL VE; una observacion interna no.

from middleware.confirmacion_cita import hace_falta_reenviar  # noqa: E402

# Lo que se le dijo al cliente cuando se creo la cita del 20 de agosto.
YA_COMUNICADO = {
    "fecha": "jueves 20 de agosto de 2026",
    "hora": "4:00 PM",
    "lugar": "cra 41 33 b sur 40",
    "asesor": "Esteban Gomez",
    "contacto": "+573001112233",
}


@pytest.mark.asyncio
async def test_cambiar_la_fecha_le_reescribe_al_cliente():
    mongo = _mongo_edicion(
        confirmacion_variables=YA_COMUNICADO,
        appointment_dt=datetime(2026, 8, 22, 21, 0),   # ahora es el sabado 22
    )
    with _EntornoEdicion(mongo=mongo) as e:
        r = await update_appointment(
            CITA, AppointmentUpdateBody(appointment_dt="2026-08-22T16:00:00"), CLAVE
        )

    assert r["client_notified"] is True
    e.twilio.send_whatsapp_message.assert_awaited_once()
    enviado = e.twilio.send_whatsapp_message.await_args.kwargs
    assert enviado["to"] == "+573009998877"
    assert "sábado 22 de agosto de 2026" in enviado["body"]
    # Se reenvia la MISMA plantilla de confirmacion con los datos nuevos.
    assert e.plantillas_pedidas == ["cita_confirmacion"]
    assert "confirma la programación de su cita" in enviado["body"]


@pytest.mark.asyncio
async def test_cambiar_solo_una_observacion_interna_no_le_cuesta_un_whatsapp():
    """
    Las observaciones no aparecen en el mensaje: el cliente no tiene de que
    enterarse. Reenviar aqui seria spam con la misma informacion de siempre.
    """
    mongo = _mongo_edicion(confirmacion_variables=YA_COMUNICADO, notes="otra nota interna")
    with _EntornoEdicion(mongo=mongo) as e:
        r = await update_appointment(
            CITA, AppointmentUpdateBody(notes="otra nota interna"), CLAVE
        )

    assert r["client_notified"] is False
    assert r["client_notify_reason"] == "sin_cambios_para_el_cliente"
    e.twilio.send_whatsapp_message.assert_not_awaited()
    # pero la nota interna SI se refresca
    e.mongo.update_appointment_note_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_cambiar_la_direccion_le_reescribe_al_cliente():
    mongo = _mongo_edicion(
        confirmacion_variables=YA_COMUNICADO, direccion="calle 50 #20-10, Sabaneta"
    )
    with _EntornoEdicion(mongo=mongo) as e:
        r = await update_appointment(
            CITA, AppointmentUpdateBody(direccion="calle 50 #20-10, Sabaneta"), CLAVE
        )

    assert r["client_notified"] is True
    assert "calle 50 #20-10, Sabaneta" in e.twilio.send_whatsapp_message.await_args.kwargs["body"]


@pytest.mark.asyncio
async def test_cambiar_de_encargado_le_reescribe_al_cliente():
    """El cliente espera a una persona concreta y con un telefono concreto."""
    mongo = _mongo_edicion(
        confirmacion_variables=YA_COMUNICADO,
        worker_name="Mauricio Restrepo", worker_phone="+573007776655",
    )
    with _EntornoEdicion(mongo=mongo) as e:
        r = await update_appointment(
            CITA, AppointmentUpdateBody(worker_id="w1", worker_name="Mauricio Restrepo"), CLAVE
        )

    assert r["client_notified"] is True
    cuerpo = e.twilio.send_whatsapp_message.await_args.kwargs["body"]
    assert "Mauricio Restrepo" in cuerpo
    assert "+573007776655" in cuerpo


@pytest.mark.asyncio
async def test_una_cita_que_nunca_se_confirmo_se_confirma_al_editarla():
    """
    Caso real: se agendo cuando el encargado no tenia telefono, asi que el
    cliente nunca supo nada. En cuanto ese dato aparece, hay que contarselo.
    """
    mongo = _mongo_edicion(confirmacion_variables={})
    with _EntornoEdicion(mongo=mongo) as e:
        r = await update_appointment(CITA, AppointmentUpdateBody(notes="x"), CLAVE)

    assert r["client_notified"] is True
    e.twilio.send_whatsapp_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_lo_reenviado_queda_registrado_para_no_repetirlo():
    """Sin esto, cada edicion posterior volveria a mandar el mismo mensaje."""
    mongo = _mongo_edicion(
        confirmacion_variables=YA_COMUNICADO,
        appointment_dt=datetime(2026, 8, 22, 21, 0),
    )
    with _EntornoEdicion(mongo=mongo) as e:
        await update_appointment(
            CITA, AppointmentUpdateBody(appointment_dt="2026-08-22T16:00:00"), CLAVE
        )

    e.mongo.marcar_confirmacion_enviada.assert_awaited_once()
    guardado = e.mongo.marcar_confirmacion_enviada.await_args.args[1]
    assert guardado["fecha"] == "sábado 22 de agosto de 2026"


@pytest.mark.asyncio
async def test_si_twilio_falla_al_reenviar_la_edicion_sigue_valiendo():
    mongo = _mongo_edicion(
        confirmacion_variables=YA_COMUNICADO,
        appointment_dt=datetime(2026, 8, 22, 21, 0),
    )
    with _EntornoEdicion(mongo=mongo) as e:
        e.twilio.send_whatsapp_message = AsyncMock(
            return_value={"status": "error", "message": "63049"}
        )
        r = await update_appointment(
            CITA, AppointmentUpdateBody(appointment_dt="2026-08-22T16:00:00"), CLAVE
        )

    assert r["ok"] is True
    assert r["client_notified"] is False
    assert "63049" in r["client_notify_reason"]
    # no se marca como comunicado: la proxima edicion volvera a intentarlo
    e.mongo.marcar_confirmacion_enviada.assert_not_awaited()


def test_la_regla_de_reenvio_compara_datos_y_no_textos():
    """
    Se comparan los VALORES, no el texto. Si alguien edita la plantilla desde el
    panel el cuerpo cambia, y eso no es motivo para volver a escribirle al
    cliente: sus datos siguen siendo los mismos.
    """
    from middleware.confirmacion_cita import DatosCita as _D, componer as _c

    datos = _D(
        fecha_hora=datetime(2026, 8, 20, 16, 0),
        lugar="cra 41 33 b sur 40",
        encargado="Esteban Gomez",
        telefono_encargado="+573001112233",
        telefono_cliente="+573009998877",
    )
    original = _c(DEFAULT_TEMPLATES["cita_confirmacion"], datos)
    editada_a_mano = dict(DEFAULT_TEMPLATES["cita_confirmacion"])
    editada_a_mano["body"] = "Otro texto: {fecha} {hora} {lugar} {asesor} {contacto}"
    retocada = _c(editada_a_mano, datos)

    assert original.cuerpo != retocada.cuerpo        # el texto cambio
    assert original.variables == retocada.variables  # los datos no
    assert hace_falta_reenviar(original.variables, retocada) is False


def test_una_decision_de_no_enviar_nunca_dispara_un_reenvio():
    from middleware.confirmacion_cita import DatosCita as _D, componer as _c

    sin_telefono = _c(PLANTILLA_CITA, _D(
        fecha_hora=datetime(2026, 8, 20, 16, 0), lugar="x", encargado="y",
        telefono_encargado="", telefono_cliente="+573009998877",
    ))
    assert sin_telefono.enviar is False
    assert hace_falta_reenviar({}, sin_telefono) is False
