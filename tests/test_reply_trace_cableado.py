"""La traza de citaciones está realmente conectada al webhook.

Why: `test_reply_trace.py` prueba que el clasificador acierta en aislamiento.
Eso no sirve de nada si el webhook nunca le pasa el ChannelMetadata. El fallo
que importa aquí es silencioso: la traza emitiría `sin_cita` para siempre y
esperaríamos una semana de datos que no dicen nada.

Se llama al endpoint real con un payload legacy —la rama viva hoy— y se lee el
log. Antes de la traza el endpoint sólo parsea y valida firma, así que no toca
Redis, Mongo ni HubSpot: `background_tasks.add_task` encola sin ejecutar.
"""

import json
import logging
from urllib.parse import urlencode

import pytest
from fastapi import BackgroundTasks
from starlette.requests import Request

from middleware import webhook_handler as wh
from utils import reply_trace as rt

TELEFONO_FALSO = "+573138405930"
WAMID = "wamid.HBgMNTczMDAxMjM0NTY3FQIAEhggQTFCMkMzRDRFNUY2"


def _peticion_legacy(campos: dict) -> Request:
    """Petición form-encoded igual a la que manda Programmable Messaging."""
    cuerpo = urlencode(campos).encode()

    async def receive():
        return {"type": "http.request", "body": cuerpo, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/whatsapp/webhook",
        "query_string": b"",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "scheme": "https",
        "server": ("ejemplo.railway.app", 443),
    }
    return Request(scope, receive)


def _campos_base(**extra) -> dict:
    campos = {
        "From": f"whatsapp:{TELEFONO_FALSO}",
        "Body": "si, ese mismo apartamento",
        "MessageSid": "SM00000000000000000000000000000001",
        "NumMedia": "0",
    }
    campos.update(extra)
    return campos


def _channel_metadata(con_cita: bool) -> str:
    datos = {"author": {"Name": "Cliente"}}
    if con_cita:
        datos["context"] = {"MessageId": WAMID}
    return json.dumps({"type": "whatsapp", "data": datos})


@pytest.fixture(autouse=True)
def _limpiar_memoria_de_formas():
    """El diagnóstico de forma deduplica en un set de módulo: sin limpiarlo,
    el orden de los tests decidiría si emite o no."""
    rt._FORMAS_VISTAS.clear()
    yield
    rt._FORMAS_VISTAS.clear()


async def _lineas_de_traza(caplog, campos: dict) -> list[str]:
    """Sólo las líneas de clasificación.

    `[CitaTrace][forma]` queda fuera a propósito: es el diagnóstico temporal,
    se emite una vez por estructura distinta —también en mensajes normales—
    y su volumen se comprueba en su propio test.
    """
    caplog.set_level(logging.INFO)
    respuesta = await wh.whatsapp_webhook(_peticion_legacy(campos), BackgroundTasks())
    assert respuesta.status_code == 200, "la traza no puede alterar la respuesta al webhook"
    return [
        r.getMessage() for r in caplog.records
        if "[CitaTrace][entrada]" in r.getMessage() or "[CitaTrace][salida]" in r.getMessage()
    ]


# ── El caso que motiva toda la instrumentación ──────────────────────────────

async def test_una_cita_del_cliente_deja_de_ser_invisible(caplog):
    """Legacy no manda OriginalRepliedMessageSid, pero ChannelMetadata trae el
    contexto. Antes de esto el mensaje era indistinguible de uno sin cita."""
    lineas = await _lineas_de_traza(caplog, _campos_base(
        ChannelMetadata=_channel_metadata(con_cita=True),
    ))
    assert lineas, "el webhook no emitió ninguna traza para una cita entrante"
    entrada = [l for l in lineas if "[entrada]" in l]
    assert entrada, f"falta la traza de entrada; se emitió: {lineas}"
    assert rt.PERDIDA_SOLO_WAMID in entrada[0]
    assert "contexto_whatsapp=si" in entrada[0]


async def test_el_channel_metadata_llega_de_verdad_a_la_traza(caplog):
    """Regresión del cableado: si la rama legacy deja de leer ChannelMetadata,
    la traza diría 'no' para siempre y nadie se enteraría."""
    lineas = await _lineas_de_traza(caplog, _campos_base(
        ChannelMetadata=_channel_metadata(con_cita=True),
    ))
    assert any("contexto_whatsapp=si" in l for l in lineas), (
        "la traza no vio el contexto: el webhook no le está pasando ChannelMetadata"
    )


async def test_un_mensaje_normal_no_ensucia_el_log(caplog):
    """Volumen: sólo se registra cuando hay señal de cita."""
    lineas = await _lineas_de_traza(caplog, _campos_base(
        ChannelMetadata=_channel_metadata(con_cita=False),
    ))
    assert lineas == [], f"se registró ruido para un mensaje sin cita: {lineas}"


async def test_sin_channel_metadata_tampoco_hay_ruido(caplog):
    lineas = await _lineas_de_traza(caplog, _campos_base())
    assert lineas == []


# ── No cambia comportamiento ────────────────────────────────────────────────

async def test_el_webhook_sigue_encolando_el_procesamiento(caplog):
    """La traza va antes de add_task: si lanzara, el mensaje no se procesaría."""
    caplog.set_level(logging.INFO)
    tareas = BackgroundTasks()
    await wh.whatsapp_webhook(
        _peticion_legacy(_campos_base(ChannelMetadata=_channel_metadata(True))),
        tareas,
    )
    encoladas = [t.func.__name__ for t in tareas.tasks]
    assert "_process_message_deferred" in encoladas


async def test_un_channel_metadata_corrupto_no_tumba_el_webhook(caplog):
    """El payload lo controla Twilio, no nosotros: la traza falla en silencio."""
    caplog.set_level(logging.INFO)
    tareas = BackgroundTasks()
    respuesta = await wh.whatsapp_webhook(
        _peticion_legacy(_campos_base(ChannelMetadata="{roto{{")), tareas,
    )
    assert respuesta.status_code == 200
    assert "_process_message_deferred" in [t.func.__name__ for t in tareas.tasks]


# ── PII ─────────────────────────────────────────────────────────────────────

async def test_el_diagnostico_de_forma_se_emite_una_sola_vez(caplog):
    """Se registra una vez por estructura, no una vez por mensaje: cinco
    entrantes idénticos dejan una sola línea."""
    caplog.set_level(logging.INFO)
    for _ in range(5):
        await wh.whatsapp_webhook(
            _peticion_legacy(_campos_base(ChannelMetadata=_channel_metadata(False))),
            BackgroundTasks(),
        )
    formas = [r.getMessage() for r in caplog.records if "[CitaTrace][forma]" in r.getMessage()]
    assert len(formas) == 1, f"el diagnostico se repitio {len(formas)} veces"
    assert "meta=[" in formas[0] and "data=[" in formas[0]


async def test_el_diagnostico_de_forma_no_lleva_valores(caplog):
    caplog.set_level(logging.INFO)
    await wh.whatsapp_webhook(
        _peticion_legacy(_campos_base(ChannelMetadata=_channel_metadata(True))),
        BackgroundTasks(),
    )
    formas = [r.getMessage() for r in caplog.records if "[CitaTrace][forma]" in r.getMessage()]
    assert formas, "no se emitio el diagnostico"
    assert "MessageId" in formas[0], "deberia listar la clave"
    assert WAMID not in formas[0], "pero nunca su valor"


async def test_la_traza_no_registra_el_telefono_ni_el_cuerpo(caplog):
    lineas = await _lineas_de_traza(caplog, _campos_base(
        Body="mi numero es 3138405930",
        ChannelMetadata=_channel_metadata(con_cita=True),
    ))
    for linea in lineas:
        assert "3138405930" not in linea
        assert "apartamento" not in linea
        assert WAMID not in linea
