"""
QA Fase 6 — Motor de depuración del embudo "No responde".

REGLA QUE SE VALIDA (fijada con el usuario el 18-ago-2026):
  Un contacto pasa a "Cerrado perdido" si, y solo si:
    · sigue en el embudo "No responde"
    · acumula 2 o más masivos sin responder
    · han pasado >= 48h desde el último de esos masivos
    · el primer masivo contado no cae en el periodo excluido

DOS FORMAS DE CONTAR
  racha_seguida (18-ago)  solo los masivos posteriores a la última respuesta:
                          contestar reinicia la cuenta.
  historial_completo      todos los masivos, estén seguidos o no. Una respuesta
    (19-ago)              intercalada ya no salva al contacto; solo lo salva
                          haber hablado DESPUÉS del último masivo.

Datos reales sobre los que se dimensionó:
  18-ago: 1387 telefonos con masivos al embudo · 373 respondieron · 378 con
          racha 2 · 636 con racha 1 · 1763 contactos en el embudo
  19-ago: 458 cumplen contando el historial completo (+80, y 0 perdidos frente
          a la racha) · 106 con 2+ masivos que SÍ contestaron al último, 39 de
          ellos aún en el embudo — son los que protege el candado

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
    BASES_DE_CONTEO,
    ESPERA_MINIMA_HORAS,
    MODO_HISTORIAL_COMPLETO,
    MODO_RACHA_SEGUIDA,
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
    base_de_conteo,
    evaluar,
    masivos_del_historial,
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
    assert d.masivos_contados == 2
    assert d.primer_masivo == MAYO
    assert d.ultimo_masivo == JULIO


def test_un_solo_masivo_no_basta():
    """636 contactos en produccion. Merecen otro intento, no el cierre."""
    d = evaluar(_hist([MAYO]), REGLA, AHORA)
    assert d.depurar is False
    assert d.motivo == MOTIVO_RACHA_CORTA
    assert d.masivos_contados == 1


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
    assert d.masivos_contados == 0


def test_responder_entre_los_dos_masivos_reinicia_la_cuenta():
    """
    Contestó al primero: la racha vuelve a empezar y solo cuenta el segundo.
    Es la mitad no obvia de la pregunta 8.
    """
    entre = MAYO + timedelta(days=3)
    d = evaluar(_hist([MAYO, JULIO], respuesta=entre), REGLA, AHORA)
    assert d.depurar is False
    assert d.motivo == MOTIVO_RACHA_CORTA
    assert d.masivos_contados == 1
    assert d.primer_masivo == JULIO


def test_una_respuesta_muy_vieja_no_rompe_la_racha():
    """
    Pregunta 8, respondida "Sí": contestó hace seis meses y luego encajó dos
    masivos sin responder -> racha de 2, se depura.
    """
    hace_seis_meses = MAYO - timedelta(days=180)
    d = evaluar(_hist([MAYO, JULIO], respuesta=hace_seis_meses), REGLA, AHORA)
    assert d.depurar is True
    assert d.masivos_contados == 2


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
    assert d.masivos_contados == 2


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
    assert desordenado.primer_masivo == MAYO


def test_racha_sin_respuesta_devuelve_todo_si_nunca_contesto():
    assert racha_sin_respuesta([JULIO, MAYO], None) == (MAYO, JULIO)


def test_racha_sin_respuesta_excluye_los_anteriores_a_la_respuesta():
    assert racha_sin_respuesta([MAYO, JULIO], MAYO + timedelta(days=1)) == (JULIO,)


def test_tres_masivos_sin_responder_tambien_se_depura():
    """El umbral es "2 o más", no "exactamente 2"."""
    d = evaluar(_hist([MAYO, MAYO_2, JULIO]), REGLA, AHORA)
    assert d.depurar is True
    assert d.masivos_contados == 3


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
    assert '_fijar_etapa(contact_id, "", ritmo)' in src, (
        "se perdio el paso de limpieza; mover hacia atras devuelve 200 sin cambiar nada"
    )
    assert "_fijar_etapa(contact_id, etapa_destino, ritmo)" in src, (
        "se perdio el paso de fijado"
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
    assert d.masivos_contados == 0


# ══════════════════════════════════════════════════════════════════════
# Modo historial completo — la ampliacion del 19-ago-2026
# ══════════════════════════════════════════════════════════════════════

REGLA_HISTORIAL = Regla(
    etapa_origen=NO_RESPONDE,
    etapa_destino=CERRADO_PERDIDO,
    excluir_racha_desde=CORTE_JULIO,
    modo=MODO_HISTORIAL_COMPLETO,
)


def test_el_caso_que_motiva_el_modo_nuevo():
    """
    El ejemplo real que dio el usuario: masivo en mayo, contesto "No gracias",
    masivo en julio, silencio. Dos masivos y cero interes, pero la racha valia
    1 y la primera corrida lo dejo dentro del embudo. Son 57 contactos.
    """
    contesto_al_primero = MAYO + timedelta(minutes=1)
    hist = _hist([MAYO, JULIO], respuesta=contesto_al_primero)

    viejo = evaluar(hist, REGLA, AHORA)
    assert viejo.depurar is False, "la regla vieja ya lo depuraba; el modo sobra"
    assert viejo.motivo == MOTIVO_RACHA_CORTA

    nuevo = evaluar(hist, REGLA_HISTORIAL, AHORA)
    assert nuevo.depurar is True
    assert nuevo.motivo == MOTIVO_DEPURAR
    assert nuevo.masivos_contados == 2
    assert nuevo.primer_masivo == MAYO
    assert nuevo.ultimo_masivo == JULIO


def test_hablar_tras_el_ultimo_masivo_sigue_salvando_al_contacto():
    """
    EL CANDADO. En modo racha salia gratis; aqui hay que ponerlo a mano. Sin el,
    la corrida cerraria a 39 personas que si contestaron al ultimo masivo.
    """
    d = evaluar(
        _hist([MAYO, JULIO], respuesta=JULIO + timedelta(days=1)),
        REGLA_HISTORIAL, AHORA,
    )
    assert d.depurar is False
    assert d.motivo == MOTIVO_RESPONDIO
    assert d.masivos_contados == 0


def test_un_solo_masivo_tampoco_basta_contando_el_historial():
    """823 telefonos reales. El umbral no cambia con el modo."""
    d = evaluar(_hist([MAYO]), REGLA_HISTORIAL, AHORA)
    assert d.depurar is False
    assert d.motivo == MOTIVO_RACHA_CORTA


def test_la_espera_de_48h_tambien_rige_en_el_modo_nuevo():
    recien = AHORA - timedelta(hours=1)
    d = evaluar(_hist([MAYO, recien]), REGLA_HISTORIAL, AHORA)
    assert d.depurar is False
    assert d.motivo == MOTIVO_ESPERA_NO_CUMPLIDA


def test_la_exclusion_mira_el_primer_masivo_de_todo_el_historial():
    """
    Contando el historial, "el primero" ya no es el primero de la racha sino el
    primero a secas. Quien empezo a recibir masivos en julio sigue fuera.
    """
    d = evaluar(_hist([JULIO, JULIO_2], respuesta=JULIO + timedelta(minutes=1)),
                REGLA_HISTORIAL, AHORA)
    assert d.depurar is False
    assert d.motivo == MOTIVO_RACHA_EXCLUIDA
    assert d.primer_masivo == JULIO


@pytest.mark.parametrize("envios,respuesta", [
    ([MAYO, JULIO], None),                          # nunca contesto
    ([MAYO, JULIO], MAYO - timedelta(days=180)),    # contesto hace medio ano
    ([MAYO, MAYO_2, JULIO], None),                  # tres masivos
    ([MAYO, MAYO_2], None),                         # los dos en mayo
])
def test_el_modo_nuevo_no_pierde_a_nadie_del_viejo(envios, respuesta):
    """
    Medido en produccion: 378 con la regla vieja, 458 con la nueva, 0 perdidos.
    Si esto se rompe, la ampliacion habria pasado a contradecir a la corrida
    anterior en vez de ampliarla, y habria contactos mal cerrados.
    """
    hist = _hist(envios, respuesta=respuesta)
    if evaluar(hist, REGLA, AHORA).depurar:
        assert evaluar(hist, REGLA_HISTORIAL, AHORA).depurar is True, (
            "la regla nueva deja fuera a alguien que la vieja si depuraba"
        )


# ── La funcion de conteo, aislada ───────────────────────────────────────────

def test_masivos_del_historial_devuelve_todos_y_ordenados():
    assert masivos_del_historial([JULIO, MAYO], None) == (MAYO, JULIO)


def test_masivos_del_historial_ignora_una_respuesta_intercalada():
    entre = MAYO + timedelta(days=3)
    assert masivos_del_historial([MAYO, JULIO], entre) == (MAYO, JULIO)


def test_masivos_del_historial_se_vacia_si_hablo_despues_del_ultimo():
    assert masivos_del_historial([MAYO, JULIO], JULIO + timedelta(seconds=1)) == ()


def test_masivos_del_historial_sin_envios():
    assert masivos_del_historial([], None) == ()


def test_una_respuesta_en_el_mismo_instante_del_ultimo_masivo_no_salva():
    """
    Borde simetrico al del modo racha. Alli la respuesta empataba y ganaba; aqui
    solo salva si es ESTRICTAMENTE posterior, porque el masivo se envio primero.
    """
    assert masivos_del_historial([MAYO, JULIO], JULIO) == (MAYO, JULIO)


# ── Contrato de los modos ───────────────────────────────────────────────────

def test_el_modo_por_defecto_sigue_siendo_la_racha():
    """
    REGRESION. Cambiar el defecto alteraria en silencio a todo llamador que ya
    exista, incluido el job periodico pendiente. Ampliar debe ser explicito.
    """
    assert Regla(etapa_origen="a", etapa_destino="b").modo == MODO_RACHA_SEGUIDA


def test_un_modo_inventado_falla_ruidosamente():
    """
    Caer en un defecto ante un nombre mal escrito depuraria contactos con una
    regla distinta de la pedida, y sin avisar.
    """
    with pytest.raises(ValueError, match="modo de conteo desconocido"):
        evaluar(_hist([MAYO, JULIO]),
                Regla(etapa_origen=NO_RESPONDE, etapa_destino=CERRADO_PERDIDO,
                      modo="racha_segida"),
                AHORA)


def test_cada_modo_apunta_a_su_funcion():
    assert base_de_conteo(MODO_RACHA_SEGUIDA) is racha_sin_respuesta
    assert base_de_conteo(MODO_HISTORIAL_COMPLETO) is masivos_del_historial
    assert set(BASES_DE_CONTEO) == {MODO_RACHA_SEGUIDA, MODO_HISTORIAL_COMPLETO}


# ── El script expone el modo sin forzarlo ───────────────────────────────────

def test_el_script_deja_elegir_el_modo_y_no_lo_impone():
    src = _fuente_script()
    assert '"--modo"' in src, "no se puede pedir la regla ampliada desde la linea de comandos"
    assert "default=MODO_RACHA_SEGUIDA" in src, (
        "el script cambio de regla por defecto: una corrida rutinaria depuraria "
        "de mas sin que nadie lo pidiera"
    )
    assert "choices=sorted(BASES_DE_CONTEO)" in src, (
        "las opciones no salen del motor; pueden desincronizarse"
    )


def test_el_informe_no_pisa_el_de_la_corrida_anterior():
    """
    El CSV es la evidencia de que se movio. Si cada corrida sobrescribe a la
    anterior, se pierde el registro de la depuracion del 18-ago.
    """
    src = _fuente_script()
    assert "depuracion_no_responde_informe_{modo}.csv" in src


def test_el_checkpoint_es_uno_solo_para_todos_los_modos():
    """
    "A este ya se le movio" no depende de con que regla se decidio. Partirlo por
    modo haria que la corrida ampliada reintentara los 368 ya movidos.
    """
    src = _fuente_script()
    assert 'CHECKPOINT = os.path.join(CARPETA_SALIDA, "depuracion_no_responde_checkpoint.jsonl")' in src


# ══════════════════════════════════════════════════════════════════════
# El camino de ESCRITURA — lo que costo un contacto el 19-ago-2026
# ══════════════════════════════════════════════════════════════════════
#
# Mover de embudo son DOS llamadas: limpiar y fijar. Entre una y otra el
# contacto no esta en ningun embudo. Aquel dia un corte de red pasajero
# (WinError 10054) entro entre las dos y dejo a un contacto sin etapa: ni en el
# panel, ni en la regla —que exige el embudo de origen para tocarlo—. Invisible
# para todo el mundo y sin forma de recuperarlo solo.
#
# La causa de fondo era una asimetria: la lectura reintentaba 3 veces, la
# escritura ninguna.

import importlib.util  # noqa: E402
import urllib.error  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402


def _cargar_script():
    spec = importlib.util.spec_from_file_location("_depurar_script", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SCRIPT_MOD = _cargar_script()


class _RitmoFalso:
    def esperar(self):
        pass


def _respuesta(status=200):
    r = MagicMock()
    r.status = status
    r.__enter__ = lambda s: s
    r.__exit__ = lambda s, *a: False
    return r


def _http_error(codigo):
    return urllib.error.HTTPError("u", codigo, "no", {}, None)


def _con_urlopen(efectos):
    """Sustituye urlopen y el sleep de los reintentos (los tests no esperan)."""
    return (
        patch.object(SCRIPT_MOD.urllib.request, "urlopen", side_effect=efectos),
        patch.object(SCRIPT_MOD, "time", MagicMock(sleep=lambda s: None,
                                                   monotonic=lambda: 0.0)),
        patch.object(SCRIPT_MOD, "_cabeceras", lambda: {}),
    )


def _valores_enviados(mock_urlopen):
    """Los lifecyclestage que se llegaron a mandar, en orden."""
    import json as _json
    return [
        _json.loads(c.args[0].data)["properties"]["lifecyclestage"]
        for c in mock_urlopen.call_args_list
    ]


def _ejecutar(efectos, fn):
    p1, p2, p3 = _con_urlopen(efectos)
    with p1 as mock_urlopen, p2, p3:
        return fn(), mock_urlopen


# ── Reintentos ──────────────────────────────────────────────────────────────

def test_un_corte_de_red_pasajero_ya_no_pierde_la_escritura():
    """El fallo real: WinError 10054 a la primera, bien a la segunda."""
    efectos = [OSError("[WinError 10054] connection forcibly closed"), _respuesta(200)]
    error, mock = _ejecutar(
        efectos, lambda: SCRIPT_MOD._fijar_etapa("1", "evangelist", _RitmoFalso())
    )
    assert error is None, "se rindio al primer corte de red"
    assert mock.call_count == 2


def test_se_rinde_tras_agotar_los_reintentos():
    efectos = [OSError("red caida")] * SCRIPT_MOD.REINTENTOS_ESCRITURA
    error, mock = _ejecutar(
        efectos, lambda: SCRIPT_MOD._fijar_etapa("1", "evangelist", _RitmoFalso())
    )
    assert error is not None
    assert mock.call_count == SCRIPT_MOD.REINTENTOS_ESCRITURA


def test_un_400_no_se_reintenta():
    """Insistir ante un rechazo del propio HubSpot solo gasta presupuesto."""
    efectos = [_http_error(400)] * 5
    error, mock = _ejecutar(
        efectos, lambda: SCRIPT_MOD._fijar_etapa("1", "evangelist", _RitmoFalso())
    )
    assert "400" in error
    assert mock.call_count == 1, "reintento un error que no se arregla reintentando"


def test_un_429_si_se_reintenta():
    """El rate limit es justo lo contrario: pasajero por definicion."""
    efectos = [_http_error(429), _respuesta(200)]
    error, mock = _ejecutar(
        efectos, lambda: SCRIPT_MOD._fijar_etapa("1", "evangelist", _RitmoFalso())
    )
    assert error is None
    assert mock.call_count == 2


# ── El movimiento completo, y el deshacer ───────────────────────────────────

def test_el_movimiento_limpia_y_luego_fija():
    efectos = [_respuesta(200), _respuesta(200)]
    error, mock = _ejecutar(
        efectos,
        lambda: SCRIPT_MOD.mover_etapa("1", "other", "evangelist", _RitmoFalso()),
    )
    assert error is None
    assert _valores_enviados(mock) == ["", "evangelist"]


def test_si_falla_la_limpieza_no_se_toca_nada():
    """El contacto sigue en su embudo: no hay nada que deshacer."""
    efectos = [_http_error(400)]
    error, mock = _ejecutar(
        efectos,
        lambda: SCRIPT_MOD.mover_etapa("1", "other", "evangelist", _RitmoFalso()),
    )
    assert error is not None
    assert _valores_enviados(mock) == [""], "escribio despues de fallar la limpieza"


def test_si_falla_el_fijado_el_contacto_vuelve_a_su_embudo():
    """
    LA REGRESION. Sin esto el contacto queda sin etapa: fuera del panel y fuera
    del alcance de la regla, que exige el embudo de origen. Devolverlo lo deja
    donde estaba y la siguiente pasada lo reintenta sola.
    """
    efectos = [
        _respuesta(200),   # limpiar: bien
        _http_error(400),  # fijar: no (un 4xx no se reintenta)
        _respuesta(200),   # deshacer: bien
    ]
    error, mock = _ejecutar(
        efectos,
        lambda: SCRIPT_MOD.mover_etapa("1", "other", "evangelist", _RitmoFalso()),
    )
    enviados = _valores_enviados(mock)
    assert enviados[-1] == "other", (
        f"no se devolvio al embudo de origen; quedo sin etapa. Enviado: {enviados}"
    )
    assert error is not None and "deshecho" in error


def test_si_tampoco_se_puede_deshacer_el_error_lo_dice_a_gritos():
    """
    Queda un contacto sin etapa de verdad. Tiene que verse en el informe, porque
    es el unico estado del que no se sale solo.
    """
    efectos = [_respuesta(200)] + [_http_error(400)] * 2
    error, _ = _ejecutar(
        efectos,
        lambda: SCRIPT_MOD.mover_etapa("1", "other", "evangelist", _RitmoFalso()),
    )
    assert "SIN ETAPA" in error


# ── El rescate ──────────────────────────────────────────────────────────────

def test_el_rescate_devuelve_al_origen_no_empuja_al_destino():
    """
    De un contacto sin etapa no se sabe que decidio nadie. Se le devuelve a su
    embudo y pasa por el mismo camino auditado que los demas.
    """
    efectos = [_respuesta(200), _respuesta(200)]
    n, mock = _ejecutar(
        efectos,
        lambda: SCRIPT_MOD.rescatar_sin_etapa(["1", "2"], "other", _RitmoFalso()),
    )
    assert n == 2
    assert _valores_enviados(mock) == ["other", "other"]
    assert "evangelist" not in _valores_enviados(mock)


def test_el_runner_recoge_a_los_que_quedaron_sin_etapa():
    """Si no se apartan aparte, la regla los descarta y no vuelven nunca."""
    src = _fuente_script()
    assert 'if not props.get("lifecyclestage"):' in src
    assert "sin_etapa.append(cid)" in src
    assert "rescatar_sin_etapa(sin_etapa, regla.etapa_origen, ritmo)" in src


def test_la_escritura_reintenta_igual_que_la_lectura():
    """La asimetria entre leer y escribir es lo que causo el incidente."""
    assert SCRIPT_MOD.REINTENTOS_ESCRITURA >= 3
