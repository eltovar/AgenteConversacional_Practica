"""Todo mensaje que sale hacia el cliente queda guardado en Mongo.

Why: tres ramas de `_process_message_deferred` enviaban por Twilio y
retornaban sin llamar a `save_message`. El cliente recibía el mensaje, lo leía
y lo citaba, y `get_message_by_sid` no lo encontraba: la cita se caía sin
error, sin log y sin documento. Confirmado en producción el 2-sep-2026 —
enviado 17:37:32, citado 17:37:49, no encontrado 17:37:52. Además esos
mensajes faltaban en el historial del panel, así que la asesora no veía lo que
el bot le acababa de decir al cliente.

El test que de verdad protege esto es el estructural: si alguien añade una
cuarta ruta de envío sin persistencia, falla. Los de comportamiento fijan el
contrato del helper.
"""

import ast
import inspect
from pathlib import Path

import pytest

from middleware import webhook_handler as wh


# ── El guardia estructural ──────────────────────────────────────────────────
# Es el único que detecta una ruta nueva. Los de abajo sólo prueban la que ya
# existe.

RUTA_MODULO = Path(wh.__file__)
VENTANA = 80  # líneas dentro de las que se acepta la persistencia


def _envios_y_persistencias():
    fuente = RUTA_MODULO.read_text(encoding="utf-8").splitlines()
    envios, persis = [], []
    for i, linea in enumerate(fuente, start=1):
        if "send_whatsapp_message(" in linea and "def " not in linea:
            envios.append(i)
        if "_persistir_saliente(" in linea and "async def" not in linea:
            persis.append(i)
        if "save_message(" in linea and "def " not in linea:
            persis.append(i)
    return envios, persis


def test_ninguna_ruta_de_envio_queda_sin_persistir():
    """Cada `send_whatsapp_message` tiene una escritura a Mongo cerca."""
    envios, persis = _envios_y_persistencias()
    assert envios, "no se encontró ningún envío: el guardia se quedó ciego"
    huerfanas = [e for e in envios if not any(0 < p - e < VENTANA for p in persis)]
    assert not huerfanas, (
        f"rutas de envío sin persistencia en las líneas {huerfanas}. "
        "El cliente recibe ese mensaje: si no se guarda, no se puede citar "
        "y no sale en el historial del panel."
    )


def test_el_helper_existe_y_es_asincrono():
    assert inspect.iscoroutinefunction(wh._persistir_saliente)


def test_las_tres_rutas_recuperadas_lo_llaman():
    """Las tres ramas que retornaban sin guardar ahora invocan el helper."""
    arbol = ast.parse(RUTA_MODULO.read_text(encoding="utf-8"))
    llamadas = [
        n for n in ast.walk(arbol)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_persistir_saliente"
    ]
    assert len(llamadas) >= 3, (
        f"se esperaban al menos 3 llamadas (mensaje especial, fuera de "
        f"horario, aviso de error) y hay {len(llamadas)}"
    )


# ── Contrato del helper ─────────────────────────────────────────────────────

class _MongoEspia:
    def __init__(self):
        self.guardados = []

    async def save_message(self, **kwargs):
        self.guardados.append(kwargs)
        return "id-falso"


@pytest.fixture
def mongo(monkeypatch):
    espia = _MongoEspia()
    monkeypatch.setattr(wh, "get_mongo_manager", lambda: espia)
    return espia


@pytest.mark.asyncio
async def test_guarda_el_saliente_con_su_sid(mongo):
    """El SID es lo único que permite resolver una cita después."""
    await wh._persistir_saliente(
        phone="+573138405930",
        contenido="En un momento te contacta una asesora",
        resultado_envio={"status": "success", "message_sid": "SM5f713af5"},
        canal="finca_raiz",
        contact_id="123",
    )
    assert len(mongo.guardados) == 1
    g = mongo.guardados[0]
    assert g["message_sid"] == "SM5f713af5"
    assert g["sender"] == "bot"
    assert g["channel"] == "finca_raiz"
    assert g["content"] == "En un momento te contacta una asesora"


@pytest.mark.asyncio
async def test_no_guarda_si_el_envio_fallo(mongo):
    """Si Twilio no lo entregó, el cliente no lo vio y no hay nada que citar."""
    await wh._persistir_saliente(
        phone="+573138405930",
        contenido="hola",
        resultado_envio={"status": "error", "error_code": 21610},
        canal="whatsapp",
    )
    assert mongo.guardados == []


@pytest.mark.asyncio
async def test_no_guarda_si_no_hubo_resultado(mongo):
    await wh._persistir_saliente(
        phone="+573138405930", contenido="hola",
        resultado_envio=None, canal="whatsapp",
    )
    assert mongo.guardados == []


@pytest.mark.asyncio
async def test_un_fallo_de_mongo_no_tumba_el_procesamiento(monkeypatch):
    """El entrante ya funcionaba antes de esto: no puede romperse por aquí."""
    class _Rota:
        async def save_message(self, **kwargs):
            raise RuntimeError("mongo caido")

    monkeypatch.setattr(wh, "get_mongo_manager", lambda: _Rota())
    await wh._persistir_saliente(          # no debe propagar
        phone="+573138405930", contenido="hola",
        resultado_envio={"status": "success", "message_sid": "SM1"},
        canal="whatsapp",
    )


@pytest.mark.asyncio
async def test_prefiere_el_conversation_sid_del_envio(mongo):
    """El del resultado es el real; el del parámetro es el de la petición."""
    await wh._persistir_saliente(
        phone="+573138405930", contenido="hola",
        resultado_envio={
            "status": "success",
            "message_sid": "SM1",
            "conversation_sid": "CHnuevo",
            "conversations_message_sid": "IM1",
        },
        canal="whatsapp",
        conversation_sid="CHviejo",
    )
    g = mongo.guardados[0]
    assert g["conversation_sid"] == "CHnuevo"
    assert g["conversations_message_sid"] == "IM1"
