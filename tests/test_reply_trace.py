"""Traza de citaciones entrantes — `utils/reply_trace.py`.

Why: la traza existe para contar un fallo que hoy no deja huella. Si ella misma
se equivoca al clasificar, el conteo que salga guiará mal la decisión de qué
arreglar. Estos tests fijan la tabla de verdad completa y el caso concreto que
se observó en producción.

No tocan red ni base de datos: el módulo bajo prueba es puro.
"""

import json

import pytest

from utils import reply_trace as rt

# ── Payloads reales ─────────────────────────────────────────────────────────
# Forma observada en los webhooks form-encoded de Programmable Messaging, que
# es la rama viva hoy. El identificador citado viaja como WAMid en PascalCase.
WAMID = "wamid.HBgMNTczMDAxMjM0NTY3FQIAEhggQTFCMkMzRDRFNUY2"

CM_CON_CITA = json.dumps({
    "type": "whatsapp",
    "data": {
        "context": {"MessageId": WAMID},
        "author": {"Name": "Cliente"},
    },
})

# Forma REAL de un mensaje normal, capturada en produccion el 29-ago-2026:
#   [CitaTrace][forma] meta=[data,type] data=[context] context=[ProfileName,WaId]
# El `context` existe SIEMPRE y describe al remitente. El fixture original de
# este fichero lo omitia porque lo escribi con la misma suposicion que el
# codigo, asi que la prueba confirmaba la hipotesis en vez de atacarla.
CM_SIN_CITA = json.dumps({
    "type": "whatsapp",
    "data": {"context": {"ProfileName": "Cliente", "WaId": "573001234567"}},
})

# Metadata sin bloque `context` en absoluto (no toda variante lo trae).
CM_SIN_CONTEXTO = json.dumps({
    "type": "whatsapp",
    "data": {"author": {"Name": "Cliente"}},
})

# Cliente que cito pero cuyo contexto llego sin identificador utilizable.
CM_CITA_SIN_ID = json.dumps({
    "type": "whatsapp",
    "data": {"context": {"ProfileName": "Cliente", "WaId": "573001234567",
                         "MessageId": ""}},
})


# ── contexto_del_mensaje ────────────────────────────────────────────────────────

def test_detecta_el_contexto_cuando_el_cliente_cito():
    assert rt.contexto_del_mensaje(CM_CON_CITA) == {"MessageId": WAMID}


def test_sin_bloque_context_devuelve_none():
    assert rt.contexto_del_mensaje(CM_SIN_CONTEXTO) is None


def test_el_contexto_de_un_mensaje_normal_no_es_una_cita():
    """REGRESION del falso positivo del 89%: `context` viene en todos los
    entrantes con la identidad de quien escribe. Que exista no prueba nada."""
    ctx = rt.contexto_del_mensaje(CM_SIN_CITA)
    assert ctx is not None, "el contexto existe..."
    assert rt.es_contexto_de_respuesta(ctx) is False, "...pero no es una cita"


def test_una_clave_de_referencia_si_marca_una_cita():
    ctx = rt.contexto_del_mensaje(CM_CON_CITA)
    assert rt.es_contexto_de_respuesta(ctx) is True


def test_un_contexto_con_referencia_vacia_sigue_siendo_una_cita():
    """La clave esta pero el valor no sirve: el cliente cito y nos quedamos
    sin con que buscar."""
    ctx = rt.contexto_del_mensaje(CM_CITA_SIN_ID)
    assert rt.es_contexto_de_respuesta(ctx) is True
    assert rt.referencia_de_contexto(ctx) is None


def test_acepta_dict_ya_parseado_igual_que_json():
    assert rt.contexto_del_mensaje(json.loads(CM_CON_CITA)) == {"MessageId": WAMID}


@pytest.mark.parametrize("basura", [
    None, "", "no-es-json", b"\xff\xfe", "[]", '"cadena"', 42,
    json.dumps({"data": "no-es-dict"}),
    json.dumps({"data": {"context": "no-es-dict"}}),
])
def test_nunca_revienta_con_payloads_malformados(basura):
    """La traza corre en el camino del webhook: no puede lanzar nunca."""
    assert rt.contexto_del_mensaje(basura) is None


# ── referencia_de_contexto ──────────────────────────────────────────────────

@pytest.mark.parametrize("clave", ["MessageId", "message_id", "quoted_message_id", "id"])
def test_extrae_la_referencia_en_todas_las_variantes_de_clave(clave):
    assert rt.referencia_de_contexto({clave: WAMID}) == WAMID


def test_prefiere_pascalcase_que_es_el_campo_real_de_twilio():
    ctx = {"id": "secundario", "MessageId": WAMID}
    assert rt.referencia_de_contexto(ctx) == WAMID


@pytest.mark.parametrize("ctx", [None, {}, {"otra": "cosa"}, "no-dict"])
def test_sin_identificador_devuelve_none(ctx):
    assert rt.referencia_de_contexto(ctx) is None


# ── forma_de_referencia ─────────────────────────────────────────────────────

@pytest.mark.parametrize("ref,esperado", [
    (WAMID, "wamid"),
    ("IM" + "a" * 32, "im_sid"),
    ("SM" + "b" * 32, "sm_sid"),
    ("MM" + "c" * 32, "sm_sid"),
    ("107762041448", "otra"),   # id de nota de HubSpot — las 34 anclas rotas
    (None, "ausente"),
    ("", "ausente"),
])
def test_clasifica_la_forma_de_la_referencia(ref, esperado):
    assert rt.forma_de_referencia(ref) == esperado


# ── diagnosticar: tabla de verdad completa ──────────────────────────────────

def test_resuelta_gana_sobre_cualquier_otra_condicion():
    assert rt.diagnosticar(WAMID, hubo_contexto=True, resuelto=True) == rt.RESUELTA
    assert rt.diagnosticar(None, hubo_contexto=False, resuelto=True) == rt.RESUELTA


def test_sin_referencia_y_sin_contexto_no_hubo_cita():
    assert rt.diagnosticar(None, hubo_contexto=False, resuelto=False) == rt.SIN_CITA


def test_contexto_sin_referencia_es_perdida():
    """El cliente citó y nos quedamos sin identificador con el que buscar."""
    assert rt.diagnosticar(None, hubo_contexto=True, resuelto=False) == rt.PERDIDA_SIN_REFERENCIA


def test_wamid_sin_resolver_se_marca_aparte():
    """Distinto de 'no está en Mongo': el campo `wamid` no se puebla nunca,
    así que buscar por él no puede acertar ni con el mensaje presente."""
    assert rt.diagnosticar(WAMID, hubo_contexto=True, resuelto=False) == rt.PERDIDA_SOLO_WAMID


def test_sid_resoluble_que_no_casa_es_otra_perdida():
    assert rt.diagnosticar("IM" + "a" * 32, hubo_contexto=True, resuelto=False) == rt.PERDIDA_NO_EN_MONGO


@pytest.mark.parametrize("diag,esperado", [
    (rt.RESUELTA, False),
    (rt.SIN_CITA, False),
    (rt.PERDIDA_SIN_REFERENCIA, True),
    (rt.PERDIDA_SOLO_WAMID, True),
    (rt.PERDIDA_NO_EN_MONGO, True),
])
def test_es_perdida_solo_marca_las_citas_que_se_pierden(diag, esperado):
    assert rt.es_perdida(diag) is esperado


# ── traza: el caso de producción ────────────────────────────────────────────

def test_el_caso_vivo_hoy_queda_contado_como_perdida():
    """Rama legacy: Twilio NO manda OriginalRepliedMessageSid, pero
    ChannelMetadata sí trae el contexto. Antes esto era invisible."""
    linea, diag = rt.traza(
        message_sid="SM" + "f" * 32,
        canal="finca_raiz",
        referencia=None,                      # el parser no extrajo nada
        channel_metadata_bruto=CM_CON_CITA,   # pero el cliente sí citó
        resuelto=False,
    )
    assert diag == rt.PERDIDA_SOLO_WAMID
    assert rt.es_perdida(diag)
    assert "contexto_whatsapp=si" in linea
    assert "forma_ref=wamid" in linea


def test_un_mensaje_normal_no_genera_ruido():
    _, diag = rt.traza(
        message_sid="SM" + "f" * 32,
        canal="whatsapp",
        referencia=None,
        channel_metadata_bruto=CM_SIN_CITA,
        resuelto=False,
    )
    assert diag == rt.SIN_CITA


def test_cita_resuelta_se_reporta_como_tal():
    _, diag = rt.traza(
        message_sid="SM" + "f" * 32,
        canal="whatsapp",
        referencia="SM" + "a" * 32,
        channel_metadata_bruto=CM_CON_CITA,
        resuelto=True,
        etapa="salida",
    )
    assert diag == rt.RESUELTA


def test_la_etapa_aparece_en_la_linea_para_poder_correlacionar():
    linea, _ = rt.traza("SMabc", "whatsapp", None, CM_CON_CITA, etapa="salida")
    assert "[CitaTrace][salida]" in linea
    assert "sid=SMabc" in linea


# ── PII ─────────────────────────────────────────────────────────────────────

def test_la_traza_no_filtra_datos_del_cliente():
    """CLAUDE.md prohíbe loguear teléfonos, nombres y contenido en producción.
    La línea sólo puede llevar identificadores de Twilio y el diagnóstico."""
    cm = json.dumps({
        "type": "whatsapp",
        "data": {
            "context": {"MessageId": WAMID},
            "author": {"Name": "Emperatriz Espinosa", "Phone": "+573105155781"},
            "body": "mi cedula es 1020304050",
        },
    })
    linea, _ = rt.traza("SM" + "f" * 32, "finca_raiz", None, cm)
    for secreto in ("Emperatriz", "Espinosa", "3105155781", "1020304050", "cedula"):
        assert secreto not in linea


def test_la_traza_no_vuelca_el_wamid_completo_del_cliente():
    """El WAMid embebe el teléfono del destinatario en base64: se reporta la
    forma, nunca el valor."""
    linea, _ = rt.traza("SM" + "f" * 32, "finca_raiz", None, CM_CON_CITA)
    assert WAMID not in linea
