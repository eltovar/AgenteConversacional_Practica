"""Diagnóstico temporal de la forma del ChannelMetadata.

Why: la primera hora en producción marcó el 89% de los entrantes como cita
perdida. La suposición de partida —que `data.context` sólo aparece en
respuestas— era falsa. Este diagnóstico revela qué claves trae el payload real
para poder endurecer el clasificador; estos tests fijan que registre nombres de
clave y nunca valores, y que no inunde el log.

Retirar junto con `forma_del_payload` cuando el clasificador esté calibrado.
"""

import json

import pytest

from utils import reply_trace as rt


@pytest.fixture(autouse=True)
def _limpiar_memoria_de_formas():
    """Cada test parte de cero: el dedupe vive en el módulo."""
    rt._FORMAS_VISTAS.clear()
    yield
    rt._FORMAS_VISTAS.clear()


CM = json.dumps({
    "type": "whatsapp",
    "data": {
        "context": {"Forwarded": False, "FrequentlyForwarded": False},
        "author": {"Name": "Cliente"},
    },
})


def test_describe_los_tres_niveles_de_claves():
    firma = rt.forma_del_payload(CM)
    assert "meta=[data,type]" in firma
    assert "data=[author,context]" in firma
    assert "context=[Forwarded,FrequentlyForwarded]" in firma


def test_las_claves_van_ordenadas_para_que_la_firma_sea_estable():
    """Sin orden, el mismo payload generaría firmas distintas y el dedupe
    dejaría de acotar el volumen."""
    a = rt.forma_del_payload(json.dumps({"data": {"context": {"b": 1, "a": 2}}}))
    b = rt.forma_del_payload(json.dumps({"data": {"context": {"a": 2, "b": 1}}}))
    assert a == b


def test_un_contexto_que_no_es_dict_se_reporta_por_tipo():
    firma = rt.forma_del_payload(json.dumps({"data": {"context": "algo"}}))
    assert "context=<str>" in firma


def test_sin_context_no_inventa_la_seccion():
    firma = rt.forma_del_payload(json.dumps({"data": {"author": {}}}))
    assert "context=" not in firma


@pytest.mark.parametrize("basura", [None, "", "{roto{{", b"\xff", 42, "[]"])
def test_no_revienta_ni_describe_lo_que_no_existe(basura):
    assert rt.forma_del_payload(basura) is None


# ── Volumen ─────────────────────────────────────────────────────────────────

def test_la_misma_forma_solo_se_registra_una_vez():
    firma = rt.forma_del_payload(CM)
    assert rt.forma_es_nueva(firma) is True
    assert rt.forma_es_nueva(firma) is False
    assert rt.forma_es_nueva(firma) is False


def test_una_forma_distinta_salta_de_inmediato():
    """Es el objetivo: una respuesta real traerá claves nuevas y se verá sola
    entre miles de mensajes normales."""
    rt.forma_es_nueva(rt.forma_del_payload(CM))
    con_cita = json.dumps({
        "type": "whatsapp",
        "data": {"context": {"MessageId": "wamid.ABC"}, "author": {}},
    })
    assert rt.forma_es_nueva(rt.forma_del_payload(con_cita)) is True


def test_nunca_se_registra_una_firma_vacia():
    assert rt.forma_es_nueva(None) is False
    assert rt.forma_es_nueva("") is False


def test_el_set_tiene_techo_duro():
    """CLAUDE.md: el watchdog de memoria reinicia el worker por estructuras
    que crecen sin límite."""
    for i in range(rt._MAX_FORMAS):
        assert rt.forma_es_nueva(f"firma-{i}") is True
    assert rt.forma_es_nueva("una-mas") is False
    assert len(rt._FORMAS_VISTAS) == rt._MAX_FORMAS


# ── PII ─────────────────────────────────────────────────────────────────────

def test_registra_nombres_de_clave_pero_jamas_valores():
    cm = json.dumps({
        "type": "whatsapp",
        "data": {
            "context": {"MessageId": "wamid.SECRETO", "From": "+573105155781"},
            "author": {"Name": "Emperatriz Espinosa"},
            "body": "mi cedula es 1020304050",
        },
    })
    firma = rt.forma_del_payload(cm)
    assert "MessageId" in firma and "From" in firma      # las claves sí
    for valor in ("wamid.SECRETO", "3105155781", "Emperatriz", "Espinosa", "1020304050"):
        assert valor not in firma                        # los valores nunca
