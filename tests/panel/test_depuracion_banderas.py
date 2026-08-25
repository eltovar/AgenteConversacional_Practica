"""
QA Fase 6 — Las tres banderas nuevas del runner de depuración (24-ago-2026).

QUÉ SE VALIDA
    El motor tiene sus 73 tests y no se ha tocado. Aquí se prueban las piezas
    que se le añadieron al runner para la limpieza grande:

      --umbral N        cuántos masivos hacen falta (1 en vez de 2)
      --owner ID        añade los de un propietario aunque no cumplan la regla
      --excluir CSV     lista de contactos intocables, por encima de todo
      --corte-fecha     el corte del 18-ago, ahora desactivable

POR QUÉ EXISTE `--corte-fecha`
    `EXCLUIR_RACHA_DESDE = 2026-06-01` se puso el 18-ago con umbral 2: no cerrar
    a quien empezaba su racha en la campaña de julio porque solo llevaba UN
    intento. Con `--umbral 1` ese razonamiento se cae solo — un intento basta por
    decisión del usuario. La corrida en seco del 24-ago lo destapó: con el corte
    puesto salían 419 en vez de 1.031, y los 886 de un solo masivo quedaban
    tapados. Se hace configurable, no se borra: sigue siendo el defecto.

DATOS REALES (24-ago-2026, producción)
    1.148 contactos en "No responde"
      886 con exactamente 1 masivo   145 con 2 o más   117 sin ninguno
      453 del propietario 89096378   (68 de ellos sin masivo)
       96 contestaron tras su último masivo → 57 dijeron "sí", 11 piden datos
    1.031 seleccionados por la combinación aprobada, 68 excluidos

Ejecutar:
    python -m pytest tests/panel/test_depuracion_banderas.py -v
"""
import csv
import importlib.util
import io
import json
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SCRIPT = os.path.join(ROOT, "scripts", "depurar_no_responde.py")


def _cargar():
    spec = importlib.util.spec_from_file_location("_depurar_banderas", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


S = _cargar()


class _RitmoFalso:
    def esperar(self):
        pass


# ═══════════════════════════════════════════════════════════════════════════
# 1. La lista de exclusión
# ═══════════════════════════════════════════════════════════════════════════

def _csv_exclusion(tmp_path, filas):
    ruta = tmp_path / "exclusion.csv"
    with io.open(ruta, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["contact_id", "motivo"])
        w.writeheader()
        w.writerows(filas)
    return str(ruta)


def test_sin_fichero_no_hay_exclusiones():
    assert S.leer_excluidos(None) == {}


def test_la_exclusion_trae_el_motivo_de_cada_uno(tmp_path):
    ruta = _csv_exclusion(tmp_path, [
        {"contact_id": "c1", "motivo": "dijo_si"},
        {"contact_id": "c2", "motivo": "pide_datos_de_un_inmueble"},
    ])
    assert S.leer_excluidos(ruta) == {
        "c1": "dijo_si", "c2": "pide_datos_de_un_inmueble"}


def test_un_fichero_que_no_existe_corta_la_corrida(tmp_path):
    """
    Callar aqui seria lo peor: se lanzaria la depuracion SIN protecciones y se
    cerrarian los 68 que contestaron, creyendo que estaban a salvo.
    """
    with pytest.raises(SystemExit):
        S.leer_excluidos(str(tmp_path / "no_existe.csv"))


def test_un_fichero_vacio_tambien_corta(tmp_path):
    """Mismo razonamiento: una exclusion vacia no es 'no excluir nada'."""
    ruta = _csv_exclusion(tmp_path, [])
    with pytest.raises(SystemExit):
        S.leer_excluidos(ruta)


def test_las_lineas_sin_contact_id_se_ignoran(tmp_path):
    ruta = _csv_exclusion(tmp_path, [
        {"contact_id": "", "motivo": "ruido"},
        {"contact_id": "  ", "motivo": "ruido"},
        {"contact_id": "c9", "motivo": "dijo_si"},
    ])
    assert S.leer_excluidos(ruta) == {"c9": "dijo_si"}


def test_sin_motivo_queda_dicho_que_no_lo_hay(tmp_path):
    ruta = tmp_path / "x.csv"
    io.open(ruta, "w", encoding="utf-8-sig").write("contact_id\nc1\n")
    assert S.leer_excluidos(str(ruta)) == {"c1": "sin_motivo"}


def test_la_lista_real_tiene_los_68_aprobados():
    """
    Regresion sobre la decision del 24-ago: 57 que dijeron "si" mas 11 que piden
    datos de un inmueble. Si el fichero cambia de tamano, algo se genero mal.
    """
    ruta = os.path.join(ROOT, "scripts", "salida", "_PRIVADO_exclusion_depuracion.csv")
    if not os.path.exists(ruta):
        pytest.skip("la lista de exclusion no esta generada en esta maquina")
    excluidos = S.leer_excluidos(ruta)
    assert len(excluidos) == 68
    from collections import Counter
    motivos = Counter(excluidos.values())
    assert motivos["dijo_si"] == 57
    assert motivos["pide_datos_de_un_inmueble"] == 11


# ═══════════════════════════════════════════════════════════════════════════
# 2. La búsqueda por propietario
# ═══════════════════════════════════════════════════════════════════════════

def _respuesta_json(payload):
    r = MagicMock()
    r.__enter__ = lambda s: s
    r.__exit__ = lambda s, *a: False
    r.read = lambda: json.dumps(payload).encode()
    return r


def _con_urlopen(paginas):
    return patch.object(S.urllib.request, "urlopen",
                        side_effect=[_respuesta_json(p) for p in paginas])


def test_la_busqueda_por_owner_filtra_por_owner_Y_por_embudo():
    """
    Pedir solo por propietario traeria sus contactos de TODO el CRM: 1.240 en vez
    de los 453 del embudo. Los dos filtros van en la misma consulta.
    """
    capturado = {}

    def _falso(peticion, timeout=None):
        capturado["cuerpo"] = json.loads(peticion.data.decode())
        return _respuesta_json({"results": []})

    with patch.object(S.urllib.request, "urlopen", side_effect=_falso):
        S.leer_por_owner("89096378", "other", _RitmoFalso())

    filtros = capturado["cuerpo"]["filterGroups"][0]["filters"]
    propiedades = {f["propertyName"]: f["value"] for f in filtros}
    assert propiedades == {"hubspot_owner_id": "89096378", "lifecyclestage": "other"}


def test_la_busqueda_por_owner_pagina_hasta_el_final():
    paginas = [
        {"results": [{"id": "c1", "properties": {"phone": "+571"}}],
         "paging": {"next": {"after": "100"}}},
        {"results": [{"id": "c2", "properties": {"phone": "+572"}}],
         "paging": {"next": {"after": "200"}}},
        {"results": [{"id": "c3", "properties": {"phone": "+573"}}]},
    ]
    with _con_urlopen(paginas):
        salida = S.leer_por_owner("89096378", "other", _RitmoFalso())
    assert salida == {"c1": "+571", "c2": "+572", "c3": "+573"}


def test_si_la_busqueda_se_corta_devuelve_lo_que_llevaba():
    """
    Media lista es mejor que ninguna: los que se lean se mueven y el resto los
    coge la corrida siguiente. Reventar aqui tiraria tambien a los de la regla.
    """
    r1 = _respuesta_json({"results": [{"id": "c1", "properties": {"phone": "+571"}}],
                          "paging": {"next": {"after": "100"}}})
    with patch.object(S.urllib.request, "urlopen",
                      side_effect=[r1, OSError("se cayo la red")]):
        salida = S.leer_por_owner("89096378", "other", _RitmoFalso())
    assert salida == {"c1": "+571"}


def test_un_contacto_sin_telefono_no_rompe_la_busqueda():
    """Sin telefono no se puede cerrar en el panel, pero si mover de etapa."""
    with _con_urlopen([{"results": [{"id": "c1", "properties": {}}]}]):
        assert S.leer_por_owner("x", "other", _RitmoFalso()) == {"c1": ""}


# ═══════════════════════════════════════════════════════════════════════════
# 3. El corte por fecha
# ═══════════════════════════════════════════════════════════════════════════

def test_el_corte_sigue_siendo_el_defecto():
    """
    Nace del 18-ago y protege a quien solo llevaba un intento. Quitarlo tiene que
    ser un acto deliberado, no el comportamiento normal del script.
    """
    parser = _parser()
    args = parser.parse_args([])
    assert args.corte_fecha == S.EXCLUIR_RACHA_DESDE.strftime("%Y-%m-%d")


def _parser():
    """Reconstruye el parser del script sin ejecutar main."""
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--aplicar", action="store_true")
    p.add_argument("--limite", type=int, default=None)
    p.add_argument("--umbral", type=int, default=S.UMBRAL_MASIVOS_SIN_RESPUESTA)
    p.add_argument("--owner", default=None)
    p.add_argument("--excluir", default=None)
    p.add_argument("--corte-fecha", default=S.EXCLUIR_RACHA_DESDE.strftime("%Y-%m-%d"))
    p.add_argument("--modo", default=S.MODO_RACHA_SEGUIDA)
    return p


def test_el_umbral_por_defecto_no_cambia():
    """Los dos masivos siguen siendo la regla normal; el 1 es para esta limpieza."""
    assert _parser().parse_args([]).umbral == 2
    assert S.UMBRAL_MASIVOS_SIN_RESPUESTA == 2


def test_el_corte_de_fecha_tapaba_a_los_de_un_solo_masivo():
    """
    LA REGRESION del 24-ago. Con el corte puesto y umbral 1, quien recibio su
    unico masivo despues del 1-jun sale por `racha_iniciada_en_periodo_excluido`
    y NO se depura: en produccion eran 419 en vez de 1.031.
    """
    from middleware.depuracion_no_responde import (
        MODO_HISTORIAL_SIN_EXCEPCION, MOTIVO_DEPURAR, MOTIVO_RACHA_EXCLUIDA,
        Historial, Regla, evaluar,
    )
    unico_masivo = datetime(2026, 8, 19, 17, 0)
    ahora = datetime(2026, 8, 24, 10, 0)
    historial = Historial("c1", [unico_masivo], None, "other")

    con_corte = Regla(etapa_origen="other", etapa_destino="evangelist", umbral=1,
                      excluir_racha_desde=datetime(2026, 6, 1),
                      modo=MODO_HISTORIAL_SIN_EXCEPCION)
    sin_corte = Regla(etapa_origen="other", etapa_destino="evangelist", umbral=1,
                      excluir_racha_desde=None, modo=MODO_HISTORIAL_SIN_EXCEPCION)

    assert evaluar(historial, con_corte, ahora).motivo == MOTIVO_RACHA_EXCLUIDA
    assert evaluar(historial, sin_corte, ahora).motivo == MOTIVO_DEPURAR


def test_el_umbral_uno_alcanza_a_quien_recibio_un_solo_masivo():
    from middleware.depuracion_no_responde import (
        MODO_HISTORIAL_SIN_EXCEPCION, MOTIVO_DEPURAR, MOTIVO_RACHA_CORTA,
        Historial, Regla, evaluar,
    )
    historial = Historial("c1", [datetime(2026, 8, 19, 17, 0)], None, "other")
    ahora = datetime(2026, 8, 24, 10, 0)
    base = dict(etapa_origen="other", etapa_destino="evangelist",
                excluir_racha_desde=None, modo=MODO_HISTORIAL_SIN_EXCEPCION)

    assert evaluar(historial, Regla(umbral=2, **base), ahora).motivo == MOTIVO_RACHA_CORTA
    assert evaluar(historial, Regla(umbral=1, **base), ahora).motivo == MOTIVO_DEPURAR


# ═══════════════════════════════════════════════════════════════════════════
# 4. Conectores: quién decide qué
# ═══════════════════════════════════════════════════════════════════════════

def test_el_motor_no_sabe_nada_de_propietarios():
    """
    El propietario es un criterio administrativo ("se gestionan por otro lado"),
    no una regla de negocio sobre leads perdidos. Si aparece en el motor, la
    regla que tiene 73 tests deja de significar lo que dice.
    """
    import middleware.depuracion_no_responde as motor
    fuente = io.open(motor.__file__, encoding="utf-8").read()
    for prohibido in ("owner", "propietario", "hubspot_owner_id"):
        assert prohibido not in fuente.lower(), f"el motor se entero de: {prohibido}"


def test_el_motor_no_sabe_nada_de_listas_de_exclusion():
    """La anulacion humana vive en el runner, no dentro de la regla."""
    import middleware.depuracion_no_responde as motor
    fuente = io.open(motor.__file__, encoding="utf-8").read()
    for prohibido in ("excluidos", "leer_excluidos", "exclusion"):
        assert prohibido not in fuente.lower()


def test_la_exclusion_manda_por_encima_del_propietario():
    """
    28 de los que dijeron "si" son del propietario 89096378. Si ganara el
    propietario se cerrarian igual. Se comprueba en el fuente porque la
    comprobacion ocurre ANTES de mirar nada mas del contacto.
    """
    fuente = io.open(SCRIPT, encoding="utf-8").read()
    cuerpo = fuente[fuente.index("for telefono, cid, envios in preseleccion:"):]
    pos_exclusion = cuerpo.index("if cid in excluidos:")
    pos_owner = cuerpo.index("if envios is None:")
    assert pos_exclusion < pos_owner, (
        "la exclusion tiene que comprobarse antes que el criterio del propietario"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 5. El cableado: que la bandera llegue de verdad a la regla
# ═══════════════════════════════════════════════════════════════════════════
# Dos mutaciones sobrevivieron a los 18 tests anteriores: ignorar el --umbral
# recibido y volver al corte de fecha fijo. Los tests probaban el motor con esos
# valores y el parser por separado, pero nadie miraba la tubería del medio. Una
# bandera documentada que no se usa es peor que no tenerla.

import asyncio  # noqa: E402


async def _historial_vacio(etapa_origen):
    return {}, {}, {}


def _principal_con(**kwargs):
    """
    Corre principal() sin red y devuelve los argumentos de la Regla.

    El informe se desvia a un temporal. `principal()` escribe en
    `scripts/salida/` con modo "w", asi que sin este desvio cada corrida de la
    suite dejaba a CERO el informe de la depuracion real — que es la unica
    evidencia de a quien se movio. Paso el 24-ago-2026: truncaron uno de 1.031
    filas recien generado.
    """
    import tempfile

    capturado = {}
    Regla_real = S.Regla

    def _espia(**kw):
        capturado.update(kw)
        return Regla_real(**kw)

    argumentos = dict(aplicar=False, limite=None, modo=S.MODO_RACHA_SEGUIDA,
                      umbral=2, owner=None, ruta_exclusion=None,
                      corte_fecha=S.EXCLUIR_RACHA_DESDE)
    argumentos.update(kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(S, "Regla", _espia), \
             patch.object(S, "_ruta_informe",
                          side_effect=lambda modo: os.path.join(tmp, f"{modo}.csv")), \
             patch.object(S, "CARPETA_SALIDA", tmp), \
             patch.object(S, "leer_historial", new=_historial_vacio), \
             patch.object(S, "leer_etapas", return_value={}):
            asyncio.run(S.principal(**argumentos))
    return capturado


def test_los_tests_no_escriben_en_la_carpeta_de_salida_real():
    """
    Regresion del 24-ago: `principal()` abre el informe con modo "w" y los tests
    lo llamaban de verdad, dejando a cero el informe de la depuracion real.
    """
    real = os.path.join(ROOT, "scripts", "salida")
    antes = {}
    if os.path.isdir(real):
        antes = {n: os.path.getsize(os.path.join(real, n))
                 for n in os.listdir(real) if n.endswith(".csv")}
    _principal_con(umbral=3)
    despues = {}
    if os.path.isdir(real):
        despues = {n: os.path.getsize(os.path.join(real, n))
                   for n in os.listdir(real) if n.endswith(".csv")}
    assert antes == despues, "la suite toco ficheros de la carpeta de salida real"


def test_el_umbral_de_la_bandera_llega_a_la_regla():
    assert _principal_con(umbral=7)["umbral"] == 7


def test_el_corte_de_fecha_de_la_bandera_llega_a_la_regla():
    corte = datetime(2030, 1, 1)
    assert _principal_con(corte_fecha=corte)["excluir_racha_desde"] == corte


def test_quitar_el_corte_llega_como_None_y_no_como_el_defecto():
    """Si llegara el defecto, `--corte-fecha ninguno` no haria nada."""
    assert _principal_con(corte_fecha=None)["excluir_racha_desde"] is None


def test_el_modo_de_la_bandera_llega_a_la_regla():
    from middleware.depuracion_no_responde import MODO_HISTORIAL_SIN_EXCEPCION
    capturado = _principal_con(modo=MODO_HISTORIAL_SIN_EXCEPCION)
    assert capturado["modo"] == MODO_HISTORIAL_SIN_EXCEPCION
