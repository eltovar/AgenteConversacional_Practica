"""
QA Fase 6 — Motor de depuración del embudo "No responde".

REGLA QUE SE VALIDA (fijada con el usuario el 18-ago-2026):
  Un contacto pasa a "Cerrado perdido" si, y solo si:
    · sigue en el embudo "No responde"
    · acumula 2 o más masivos SEGUIDOS sin responder
    · han pasado >= 48h desde el último de esos masivos
    · su racha no arranca en el periodo excluido (julio en la corrida histórica)

  La racha se cuenta DESDE la última respuesta del cliente: contestar reinicia
  la cuenta; una respuesta de hace seis meses no la rompe.

Datos reales sobre los que se dimensionó (produccion, 18-ago-2026):
  1387 telefonos con masivos al embudo · 373 respondieron · 378 con racha 2
  · 636 con racha 1 · 1763 contactos en el embudo

Ejecutar:
    python -m pytest tests/panel/test_depuracion_no_responde.py -v
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from middleware.depuracion_no_responde import (  # noqa: E402
    ESPERA_MINIMA_HORAS,
    MOTIVO_DEPURAR,
    MOTIVO_ESPERA_NO_CUMPLIDA,
    MOTIVO_FUERA_DEL_EMBUDO,
    MOTIVO_RACHA_CORTA,
    MOTIVO_RACHA_EXCLUIDA,
    MOTIVO_RESPONDIO,
    MOTIVO_SIN_MASIVOS,
    UMBRAL_MASIVOS_SIN_RESPUESTA,
    Decision,
    Historial,
    Regla,
    evaluar,
    racha_sin_respuesta,
)

# Fechas reales de las campañas al embudo "No responde"
MAYO = datetime(2026, 5, 6, 14, 32)
MAYO_2 = datetime(2026, 5, 6, 14, 52)
JULIO = datetime(2026, 7, 15, 14, 42)
JULIO_2 = datetime(2026, 7, 15, 14, 50)
AHORA = datetime(2026, 8, 18, 19, 0)

NO_RESPONDE = "other"
CERRADO_PERDIDO = "evangelist"
CORTE_JULIO = datetime(2026, 6, 1)

REGLA = Regla(
    etapa_origen=NO_RESPONDE,
    etapa_destino=CERRADO_PERDIDO,
    excluir_racha_desde=CORTE_JULIO,
)


def _hist(envios, respuesta=None, etapa=NO_RESPONDE, cid="242000000001"):
    return Historial(
        contact_id=cid,
        envios_masivos=envios,
        ultima_respuesta=respuesta,
        etapa_actual=etapa,
    )


# ══════════════════════════════════════════════════════════════════════
# El caso que motiva todo: 2 masivos, ninguna respuesta
# ══════════════════════════════════════════════════════════════════════

def test_dos_masivos_sin_responder_se_depura():
    """El caso mayoritario en produccion: 378 contactos, mayo + julio."""
    d = evaluar(_hist([MAYO, JULIO]), REGLA, AHORA)
    assert d.depurar is True
    assert d.motivo == MOTIVO_DEPURAR
    assert d.racha == 2
    assert d.primer_masivo_racha == MAYO
    assert d.ultimo_masivo == JULIO


def test_un_solo_masivo_no_basta():
    """636 contactos en produccion. Merecen otro intento, no el cierre."""
    d = evaluar(_hist([MAYO]), REGLA, AHORA)
    assert d.depurar is False
    assert d.motivo == MOTIVO_RACHA_CORTA
    assert d.racha == 1


def test_sin_masivos_no_se_toca():
    d = evaluar(_hist([]), REGLA, AHORA)
    assert d.depurar is False
    assert d.motivo == MOTIVO_SIN_MASIVOS


# ══════════════════════════════════════════════════════════════════════
# La respuesta del cliente manda
# ══════════════════════════════════════════════════════════════════════

def test_responder_despues_del_ultimo_masivo_salva_al_contacto():
    """373 contactos reales. Si contestó, no está perdido."""
    d = evaluar(_hist([MAYO, JULIO], respuesta=JULIO + timedelta(days=1)), REGLA, AHORA)
    assert d.depurar is False
    assert d.motivo == MOTIVO_RESPONDIO
    assert d.racha == 0


def test_responder_entre_los_dos_masivos_reinicia_la_cuenta():
    """
    Contestó al primero: la racha vuelve a empezar y solo cuenta el segundo.
    Es la mitad no obvia de la pregunta 8.
    """
    entre = MAYO + timedelta(days=3)
    d = evaluar(_hist([MAYO, JULIO], respuesta=entre), REGLA, AHORA)
    assert d.depurar is False
    assert d.motivo == MOTIVO_RACHA_CORTA
    assert d.racha == 1
    assert d.primer_masivo_racha == JULIO


def test_una_respuesta_muy_vieja_no_rompe_la_racha():
    """
    Pregunta 8, respondida "Sí": contestó hace seis meses y luego encajó dos
    masivos sin responder -> racha de 2, se depura.
    """
    hace_seis_meses = MAYO - timedelta(days=180)
    d = evaluar(_hist([MAYO, JULIO], respuesta=hace_seis_meses), REGLA, AHORA)
    assert d.depurar is True
    assert d.racha == 2


# ══════════════════════════════════════════════════════════════════════
# La etapa actual manda sobre todo lo demás
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("etapa", [
    "evangelist",            # ya cerrado perdido — 58 casos reales
    "1326623075",            # En conversacion
    "marketingqualifiedlead",  # Visita agendada
    "customer",              # Cerrado ganado
    None,                    # sin etapa legible
])
def test_fuera_del_embudo_no_se_toca(etapa):
    """La depuración nunca revierte una decisión de la asesora."""
    d = evaluar(_hist([MAYO, JULIO], etapa=etapa), REGLA, AHORA)
    assert d.depurar is False
    assert d.motivo == MOTIVO_FUERA_DEL_EMBUDO


# ══════════════════════════════════════════════════════════════════════
# La espera de 48h
# ══════════════════════════════════════════════════════════════════════

def test_no_se_depura_antes_de_las_48h():
    """Sin la espera se cerraría a quien aún no ha tenido ocasión de contestar."""
    recien = AHORA - timedelta(hours=47, minutes=59)
    d = evaluar(_hist([MAYO, recien]), REGLA, AHORA)
    assert d.depurar is False
    assert d.motivo == MOTIVO_ESPERA_NO_CUMPLIDA


def test_a_las_48h_exactas_ya_se_depura():
    justo = AHORA - timedelta(hours=ESPERA_MINIMA_HORAS)
    d = evaluar(_hist([MAYO, justo]), REGLA, AHORA)
    assert d.depurar is True


# ══════════════════════════════════════════════════════════════════════
# La exclusión por periodo — la decisión del usuario
# ══════════════════════════════════════════════════════════════════════

def test_racha_que_arranca_en_julio_queda_fuera():
    """
    Decision explicita: "NO se depuren contactos donde el primer masivo que
    recibieron fue en Julio". Dos masivos el mismo dia de julio no bastan.
    """
    d = evaluar(_hist([JULIO, JULIO_2]), REGLA, AHORA)
    assert d.depurar is False
    assert d.motivo == MOTIVO_RACHA_EXCLUIDA
    assert d.racha == 2


def test_racha_que_arranca_en_mayo_si_entra():
    d = evaluar(_hist([MAYO, MAYO_2]), REGLA, AHORA)
    assert d.depurar is True


def test_sin_exclusion_configurada_la_racha_de_julio_entraria():
    """La exclusión es de la corrida histórica, no de la regla permanente."""
    sin_corte = Regla(etapa_origen=NO_RESPONDE, etapa_destino=CERRADO_PERDIDO)
    d = evaluar(_hist([JULIO, JULIO_2]), sin_corte, AHORA)
    assert d.depurar is True


# ══════════════════════════════════════════════════════════════════════
# Detalles del cálculo de la racha
# ══════════════════════════════════════════════════════════════════════

def test_la_racha_no_depende_del_orden_de_entrada():
    """Mongo no garantiza orden; el motor ordena por su cuenta."""
    desordenado = evaluar(_hist([JULIO, MAYO]), REGLA, AHORA)
    ordenado = evaluar(_hist([MAYO, JULIO]), REGLA, AHORA)
    assert desordenado == ordenado
    assert desordenado.primer_masivo_racha == MAYO


def test_racha_sin_respuesta_devuelve_todo_si_nunca_contesto():
    assert racha_sin_respuesta([JULIO, MAYO], None) == (MAYO, JULIO)


def test_racha_sin_respuesta_excluye_los_anteriores_a_la_respuesta():
    assert racha_sin_respuesta([MAYO, JULIO], MAYO + timedelta(days=1)) == (JULIO,)


def test_tres_masivos_sin_responder_tambien_se_depura():
    """El umbral es "2 o más", no "exactamente 2"."""
    d = evaluar(_hist([MAYO, MAYO_2, JULIO]), REGLA, AHORA)
    assert d.depurar is True
    assert d.racha == 3


# ══════════════════════════════════════════════════════════════════════
# Contrato del motor
# ══════════════════════════════════════════════════════════════════════

def test_el_umbral_por_defecto_es_dos():
    assert UMBRAL_MASIVOS_SIN_RESPUESTA == 2
    assert Regla(etapa_origen="a", etapa_destino="b").umbral == 2


def test_la_espera_por_defecto_es_48h():
    assert ESPERA_MINIMA_HORAS == 48
    assert Regla(etapa_origen="a", etapa_destino="b").espera_horas == 48


def test_el_motor_no_toca_la_red():
    """
    Guard arquitectónico: si alguien mete Redis, Mongo o HubSpot aquí, el módulo
    deja de ser probable sin red y la regla vuelve a ser imposible de verificar.
    """
    import ast

    ruta = os.path.join(ROOT, "middleware", "depuracion_no_responde.py")
    with open(ruta, "r", encoding="utf-8") as f:
        arbol = ast.parse(f.read())

    importados = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            importados += [a.name for a in nodo.names]
        elif isinstance(nodo, ast.ImportFrom):
            importados.append(nodo.module or "")
    prohibidos = [
        m for m in importados
        if any(p in m.lower() for p in ("redis", "motor", "httpx", "hubspot", "outbound_panel"))
    ]
    assert not prohibidos, f"el motor dejó de ser puro: importa {prohibidos}"


def test_los_embudos_no_estan_escritos_en_el_motor():
    """Los IDs viven en outbound_panel; aquí solo llegan por parámetro."""
    ruta = os.path.join(ROOT, "middleware", "depuracion_no_responde.py")
    with open(ruta, "r", encoding="utf-8") as f:
        fuente = f.read()
    for literal in ('"other"', '"evangelist"'):
        assert literal not in fuente, (
            f"el motor hardcodea el embudo {literal}; debe llegar en Regla"
        )


def test_la_decision_es_inmutable():
    """Un informe que se puede mutar después de generado no es auditable."""
    d = evaluar(_hist([MAYO, JULIO]), REGLA, AHORA)
    with pytest.raises(Exception):
        d.depurar = False  # type: ignore[misc]
    assert isinstance(d, Decision)


# ══════════════════════════════════════════════════════════════════════
# Conectores — el script runner no puede desviarse del motor
# ══════════════════════════════════════════════════════════════════════

SCRIPT = os.path.join(ROOT, "scripts", "depurar_no_responde.py")


def _fuente_script():
    with open(SCRIPT, "r", encoding="utf-8") as f:
        return f.read()


def test_el_script_usa_el_motor_y_no_reimplementa_la_regla():
    """
    Si el script decidiera por su cuenta habria dos verdades: la probada aqui y
    la que de verdad se ejecuta. El job periodico usara este mismo motor.
    """
    src = _fuente_script()
    assert "from middleware.depuracion_no_responde import" in src
    assert "evaluar(" in src
    for inventado in ("racha >=", "timedelta(hours=48)", "len(post) >= 2"):
        assert inventado not in src, f"el script reimplementa la regla: {inventado!r}"


def test_el_script_no_hardcodea_los_embudos():
    """Los IDs salen de outbound_panel, su fuente unica."""
    src = _fuente_script()
    assert "HUBSPOT_STAGE_NO_RESPONDE" in src and "HUBSPOT_STAGE_CERRADO_PERDIDO" in src
    for literal in ('"other"', "'other'", '"evangelist"', "'evangelist'"):
        assert literal not in src, f"embudo escrito a mano: {literal}"


def test_el_script_no_escribe_sin_el_flag():
    """dry-run por defecto: --aplicar tiene que ser explicito."""
    src = _fuente_script()
    assert '"--aplicar", action="store_true"' in src
    assert 'if not aplicar:' in src


def test_el_checkpoint_solo_registra_exitos():
    """
    Anotar los fallidos haria que el relanzamiento los diera por hechos y
    quedaran sin depurar para siempre.
    """
    src = _fuente_script()
    assert "if not error:\n            anotar_checkpoint(cid)" in src, (
        "el checkpoint volvio a registrar fallos"
    )


def test_el_cambio_de_etapa_es_en_dos_pasos():
    """
    lifecyclestage en HubSpot solo avanza: sin limpiar primero, mover hacia
    atras devuelve 200 y no cambia nada. Es lo que hace el panel.
    """
    src = _fuente_script()
    assert 'for valor in ("", etapa_destino):' in src, (
        "se perdio el clear+set; el movimiento puede fallar en silencio"
    )


def test_hay_corte_por_errores_seguidos():
    """Sin freno, una tormenta de 429 se agrava sola."""
    src = _fuente_script()
    assert "ERRORES_SEGUIDOS_PARA_ABORTAR" in src
    assert "errores_seguidos >= ERRORES_SEGUIDOS_PARA_ABORTAR" in src


def test_el_ultimo_masivo_reportado_es_correcto_aunque_llegue_desordenado():
    """
    En las salidas que NO depuran, `ultimo_masivo` sale de envios[-1]. Si el
    motor no ordena primero, el informe atribuye al contacto una fecha que no
    es la suya. Mongo no garantiza el orden de lectura.
    """
    d = evaluar(_hist([JULIO, MAYO], etapa="customer"), REGLA, AHORA)
    assert d.motivo == MOTIVO_FUERA_DEL_EMBUDO
    assert d.ultimo_masivo == JULIO, "reporto un masivo que no es el ultimo"

    d2 = evaluar(
        _hist([JULIO, MAYO], respuesta=JULIO + timedelta(days=1)), REGLA, AHORA
    )
    assert d2.motivo == MOTIVO_RESPONDIO
    assert d2.ultimo_masivo == JULIO


def test_un_masivo_en_el_mismo_instante_que_la_respuesta_no_cuenta():
    """
    Borde teorico (mismo milisegundo) que fija la semantica: la respuesta gana.
    Ante la duda, no se cierra al contacto.
    """
    assert racha_sin_respuesta([MAYO], MAYO) == ()
    d = evaluar(_hist([MAYO, JULIO], respuesta=JULIO), REGLA, AHORA)
    assert d.depurar is False
    assert d.racha == 0
