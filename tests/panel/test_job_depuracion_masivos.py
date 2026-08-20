"""
QA Fase 6 — Job periódico de depuración del embudo "No responde".

QUÉ SE VALIDA
    El motor (`depuracion_no_responde`) ya tiene sus 73 tests. Aquí se prueba lo
    que NO es puro: el interruptor, el reloj, el orden de las operaciones y qué
    sobrevive a cada fallo.

LA PREGUNTA QUE RESPONDE CADA TEST
    ¿Un despliegue puede empezar a mover contactos solo?      → no, viene en seco
    ¿La espera de 48h depende de la zona del contenedor?      → no, se fija en UTC
    ¿Si falla el segundo PATCH el contacto queda sin etapa?   → no, se deshace
    ¿Una campaña anómala puede mover miles de golpe?          → no, hay tope
    ¿HubSpot caído produce 200 intentos inútiles?             → no, corta a los 5

EL FALLO DE ZONA HORARIA QUE ORIGINA `_ahora_utc`
    `save_message` guarda `datetime.now(TIMEZONE)` en hora de Bogotá, pero
    PyMongo convierte todo datetime con zona a UTC al escribirlo: lo que vuelve
    de la consulta es naive Y EN UTC. Comprobado el 20-ago-2026 contra
    producción — `timestamp` y `timestamp_utc` del mismo documento coinciden.
    Con `datetime.now()`, la ventana de 48h se corría tantas horas como dijera
    la zona del proceso: 53h desde un portátil en Bogotá.

DATOS REALES (medidos el 19 y 20-ago-2026 en producción)
    1.990 teléfonos con al menos un masivo al embudo
    1.134 siguen en "No responde"
        0 candidatos hoy — las tres corridas manuales ya limpiaron lo que había
      166 cuando venzan las 48h del masivo del 19-ago (787 envíos)
     ~23 s por pasada: 1 s de Mongo + 20 lotes de HubSpot

Ejecutar:
    python -m pytest tests/panel/test_job_depuracion_masivos.py -v
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import middleware.job_depuracion_masivos as job  # noqa: E402
from middleware.depuracion_no_responde import (  # noqa: E402
    MODO_HISTORIAL_COMPLETO,
    MODO_HISTORIAL_SIN_EXCEPCION,
    MODO_RACHA_SEGUIDA,
)

ORIGEN = "other"
DESTINO = "evangelist"


# ═══════════════════════════════════════════════════════════════════════════
# 1. El reloj
# ═══════════════════════════════════════════════════════════════════════════

def test_el_reloj_es_utc_y_no_la_hora_del_contenedor():
    """
    LA REGRESIÓN. En Railway (UTC) `datetime.now()` acertaría por casualidad; el
    día que alguien ponga TZ=America/Bogota se depuraría 5 horas antes de tiempo.
    """
    ahora = job._ahora_utc()
    real = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((ahora - real).total_seconds()) < 5


def test_el_reloj_no_lleva_zona_pegada():
    """Mongo devuelve naive; comparar naive con aware lanza TypeError."""
    assert job._ahora_utc().tzinfo is None


# ═══════════════════════════════════════════════════════════════════════════
# 2. El interruptor y los defectos
# ═══════════════════════════════════════════════════════════════════════════

def test_por_defecto_no_aplica_nada(monkeypatch):
    """Un despliegue no puede empezar a mover contactos por si solo."""
    monkeypatch.delenv("DEPURACION_AUTO_ENABLED", raising=False)
    assert job._activado() is False


@pytest.mark.parametrize("valor", ["true", "TRUE", " True "])
def test_solo_un_true_explicito_lo_activa(monkeypatch, valor):
    monkeypatch.setenv("DEPURACION_AUTO_ENABLED", valor)
    assert job._activado() is True


@pytest.mark.parametrize("valor", ["false", "1", "si", "yes", "", "  "])
def test_cualquier_otra_cosa_lo_deja_en_seco(monkeypatch, valor):
    """Un '1' bienintencionado no puede encender un job que mueve 166 contactos."""
    monkeypatch.setenv("DEPURACION_AUTO_ENABLED", valor)
    assert job._activado() is False


def test_el_modo_por_defecto_es_el_aprobado(monkeypatch):
    monkeypatch.delenv("DEPURACION_AUTO_MODO", raising=False)
    assert job._modo() == MODO_HISTORIAL_COMPLETO


def test_el_modo_agresivo_no_es_el_defecto_de_nada(monkeypatch):
    """
    `historial_sin_excepcion` cierra tambien a quien contesto. Se uso una vez,
    supervisado, con las 37 conversaciones delante. Que corra solo cada madrugada
    es otra cosa.
    """
    for var in ("DEPURACION_AUTO_MODO",):
        monkeypatch.delenv(var, raising=False)
    assert job._modo() != MODO_HISTORIAL_SIN_EXCEPCION


def test_el_tope_por_defecto_cubre_el_lote_previsto(monkeypatch):
    monkeypatch.delenv("DEPURACION_AUTO_TOPE", raising=False)
    assert job._tope() >= 166   # el lote real del masivo del 19-ago


def test_un_tope_ilegible_no_deja_el_job_sin_red(monkeypatch):
    monkeypatch.setenv("DEPURACION_AUTO_TOPE", "doscientos")
    assert job._tope() == 200


def test_un_tope_cero_no_desactiva_el_job_por_la_puerta_de_atras(monkeypatch):
    monkeypatch.setenv("DEPURACION_AUTO_TOPE", "0")
    assert job._tope() >= 1


# ═══════════════════════════════════════════════════════════════════════════
# 3. La lectura de MongoDB
# ═══════════════════════════════════════════════════════════════════════════

class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()


class _Coleccion:
    """Doble de `db.messages` que responde segun el filtro, como el real."""

    def __init__(self, masivos, clientes):
        self.masivos, self.clientes = masivos, clientes
        self.filtros = []

    def find(self, filtro, proyeccion=None):
        self.filtros.append(filtro)
        if filtro.get("sender") == "client":
            return _Cursor(self.clientes)
        return _Cursor(self.masivos)


def _db(masivos, clientes):
    d = MagicMock()
    d.messages = _Coleccion(masivos, clientes)
    return d


T0 = datetime(2026, 8, 1, 12, 0)


@pytest.mark.asyncio
async def test_la_lectura_agrupa_masivos_por_telefono():
    db = _db(
        masivos=[
            {"phone": "+571", "timestamp": T0, "hubspot_contact_id": "c1"},
            {"phone": "+571", "timestamp": T0 + timedelta(days=30), "hubspot_contact_id": "c1"},
            {"phone": "+572", "timestamp": T0, "hubspot_contact_id": "c2"},
        ],
        clientes=[{"phone": "+572", "timestamp": T0 + timedelta(days=1)}],
    )
    masivos, respuestas, ids = await job.leer_historial_masivos(db, ORIGEN)
    assert len(masivos["+571"]) == 2
    assert respuestas == {"+572": T0 + timedelta(days=1)}
    assert ids == {"+571": "c1", "+572": "c2"}


@pytest.mark.asyncio
async def test_la_lectura_se_queda_con_la_respuesta_mas_reciente():
    db = _db(
        masivos=[{"phone": "+571", "timestamp": T0, "hubspot_contact_id": "c1"}],
        clientes=[
            {"phone": "+571", "timestamp": T0},
            {"phone": "+571", "timestamp": T0 + timedelta(days=5)},
            {"phone": "+571", "timestamp": T0 + timedelta(days=2)},
        ],
    )
    _, respuestas, _ = await job.leer_historial_masivos(db, ORIGEN)
    assert respuestas["+571"] == T0 + timedelta(days=5)


@pytest.mark.asyncio
async def test_la_lectura_filtra_por_el_embudo_pedido():
    """Un masivo a otro embudo no cuenta para este."""
    db = _db(masivos=[], clientes=[])
    await job.leer_historial_masivos(db, ORIGEN)
    filtro = db.messages.filtros[0]
    assert filtro["metadata.is_bulk_send"] is True
    assert filtro["metadata.stage_id"] == ORIGEN


@pytest.mark.asyncio
async def test_los_documentos_incompletos_no_rompen_la_lectura():
    db = _db(
        masivos=[
            {"phone": None, "timestamp": T0},
            {"phone": "+571", "timestamp": None},
            {"phone": "+571", "timestamp": T0, "hubspot_contact_id": "c1"},
        ],
        clientes=[{"phone": "+571"}],
    )
    masivos, respuestas, ids = await job.leer_historial_masivos(db, ORIGEN)
    assert masivos == {"+571": [T0]}
    assert respuestas == {}


# ═══════════════════════════════════════════════════════════════════════════
# 4. La pasada completa
# ═══════════════════════════════════════════════════════════════════════════

def _historial(n_contactos, masivos_cada_uno=2, dias_atras=10):
    """n contactos, cada uno con sus masivos, todos fuera de la ventana de 48h."""
    ahora = job._ahora_utc()
    masivos, ids = {}, {}
    for i in range(n_contactos):
        tel = f"+5730000000{i:02d}"
        masivos[tel] = [
            ahora - timedelta(days=dias_atras + k * 30) for k in range(masivos_cada_uno)
        ]
        ids[tel] = f"c{i}"
    return masivos, {}, ids


class _Entorno:
    """Aísla la pasada de la red. Expone los dobles que cada test inspecciona."""

    def __init__(self, n=3, etapas=None, mover=None):
        self.masivos, self.respuestas, self.ids = _historial(n)
        self.etapas = etapas if etapas is not None else {v: ORIGEN for v in self.ids.values()}
        self.mover = mover or AsyncMock(return_value=None)
        self.cerrar = AsyncMock(return_value=0)
        self._parches = []

    def __enter__(self):
        mongo = MagicMock()
        mongo.connect = AsyncMock(return_value=True)
        mongo.db = MagicMock()
        self._parches = [
            patch.object(job, "leer_historial_masivos",
                         new=AsyncMock(return_value=(self.masivos, self.respuestas, self.ids))),
            patch.object(job, "_leer_etapas", new=AsyncMock(return_value=self.etapas)),
            patch.object(job, "_mover_al_embudo_terminal", new=self.mover),
            patch.object(job, "_cerrar_en_panel", new=self.cerrar),
            patch("middleware.outbound_panel.get_mongo_manager", return_value=mongo),
            patch.object(job, "asyncio", MagicMock(sleep=AsyncMock())),
        ]
        for p in self._parches:
            p.start()
        return self

    def __exit__(self, *a):
        for p in reversed(self._parches):
            p.stop()
        return False


@pytest.mark.asyncio
async def test_en_seco_no_toca_hubspot_ni_el_panel():
    """Es el estado en el que se despliega. Tiene que ser inofensivo."""
    with _Entorno(n=3) as e:
        r = await job.ejecutar_pasada(aplicar=False)

    assert r["aplicado"] is False
    assert r["candidatos"] == 3
    assert r["movidos"] == 0
    e.mover.assert_not_awaited()
    e.cerrar.assert_not_awaited()


@pytest.mark.asyncio
async def test_aplicando_mueve_y_cierra():
    with _Entorno(n=3) as e:
        r = await job.ejecutar_pasada(aplicar=True)

    assert r["movidos"] == 3
    assert e.mover.await_count == 3
    # Se cierra en el panel a los movidos, y solo a ellos.
    e.cerrar.assert_awaited_once()
    assert len(e.cerrar.await_args.args[0]) == 3


@pytest.mark.asyncio
async def test_el_destino_es_el_embudo_terminal_y_el_origen_el_de_no_responde():
    with _Entorno(n=1) as e:
        await job.ejecutar_pasada(aplicar=True)
    _contacto, origen, destino = e.mover.await_args.args
    assert origen == ORIGEN
    assert destino == DESTINO


@pytest.mark.asyncio
async def test_quien_ya_salio_del_embudo_no_se_vuelve_a_tocar():
    """
    Es lo que hace al job idempotente sin guardar estado: el motor descarta por
    `fuera_del_embudo` a quien ya se movió.
    """
    with _Entorno(n=3) as e:
        e.etapas["c0"] = DESTINO          # ya depurado en una pasada anterior
        e.etapas["c1"] = "1326623075"      # una asesora lo rescató
        r = await job.ejecutar_pasada(aplicar=True)

    assert r["candidatos"] == 1
    assert e.mover.await_count == 1


@pytest.mark.asyncio
async def test_un_telefono_sin_contact_id_se_ignora():
    """Sin id no hay a quién mover; contarlo como candidato sería mentir."""
    with _Entorno(n=2) as e:
        del e.ids["+573000000001"]
        r = await job.ejecutar_pasada(aplicar=True)

    assert r["candidatos"] == 1


@pytest.mark.asyncio
async def test_el_tope_corta_y_lo_deja_dicho(monkeypatch):
    monkeypatch.setenv("DEPURACION_AUTO_TOPE", "2")
    with _Entorno(n=5) as e:
        r = await job.ejecutar_pasada(aplicar=True)

    assert r["candidatos"] == 5
    assert r["topado"] is True
    assert r["movidos"] == 2


@pytest.mark.asyncio
async def test_si_el_tope_muerde_salen_primero_los_mas_antiguos(monkeypatch):
    """Los que llevan más tiempo esperando, no los que acaban de entrar."""
    monkeypatch.setenv("DEPURACION_AUTO_TOPE", "1")
    ahora = job._ahora_utc()
    with _Entorno(n=2) as e:
        e.masivos["+573000000000"] = [ahora - timedelta(days=200), ahora - timedelta(days=100)]
        e.masivos["+573000000001"] = [ahora - timedelta(days=20), ahora - timedelta(days=10)]
        await job.ejecutar_pasada(aplicar=True)

    assert e.mover.await_args.args[0] == "c0"   # el del masivo mas viejo


@pytest.mark.asyncio
async def test_un_fallo_suelto_no_para_la_pasada():
    mover = AsyncMock(side_effect=["fijar_HTTP_500 (deshecho)", None, None])
    with _Entorno(n=3, mover=mover) as e:
        r = await job.ejecutar_pasada(aplicar=True)

    assert r["fallidos"] == 1
    assert r["movidos"] == 2
    assert len(e.cerrar.await_args.args[0]) == 2   # solo se cierran los que si


@pytest.mark.asyncio
async def test_hubspot_caido_corta_la_pasada_en_seco():
    """Sin esto, un portal caido son 200 intentos inutiles y 200 lineas de error."""
    mover = AsyncMock(return_value="limpiar_HTTP_500")
    with _Entorno(n=50, mover=mover) as e:
        r = await job.ejecutar_pasada(aplicar=True)

    assert r["fallidos"] == job.FALLOS_SEGUIDOS_PARA_ABORTAR
    assert e.mover.await_count == job.FALLOS_SEGUIDOS_PARA_ABORTAR


@pytest.mark.asyncio
async def test_un_exito_reinicia_la_cuenta_de_fallos():
    """4 fallos, un acierto y 4 fallos mas no son 8 seguidos."""
    mover = AsyncMock(side_effect=["e", "e", "e", "e", None, "e", "e", "e", "e", None])
    with _Entorno(n=10, mover=mover) as e:
        r = await job.ejecutar_pasada(aplicar=True)

    assert e.mover.await_count == 10
    assert r["movidos"] == 2


@pytest.mark.asyncio
async def test_sin_mongo_no_se_inventa_una_pasada():
    mongo = MagicMock()
    mongo.connect = AsyncMock(return_value=False)
    with patch("middleware.outbound_panel.get_mongo_manager", return_value=mongo), \
         patch.object(job, "_leer_etapas", new=AsyncMock()) as etapas:
        r = await job.ejecutar_pasada(aplicar=True)

    assert r["movidos"] == 0
    etapas.assert_not_awaited()


@pytest.mark.asyncio
async def test_sin_masivos_no_se_consulta_hubspot():
    """La mayoria de las pasadas: los masivos son 3 dias en 4 meses."""
    mongo = MagicMock()
    mongo.connect = AsyncMock(return_value=True)
    mongo.db = MagicMock()
    with patch("middleware.outbound_panel.get_mongo_manager", return_value=mongo), \
         patch.object(job, "leer_historial_masivos", new=AsyncMock(return_value=({}, {}, {}))), \
         patch.object(job, "_leer_etapas", new=AsyncMock()) as etapas:
        r = await job.ejecutar_pasada(aplicar=True)

    assert r["evaluados"] == 0
    etapas.assert_not_awaited()


@pytest.mark.asyncio
async def test_el_punto_de_entrada_del_scheduler_nunca_lanza():
    """Una excepcion aqui mataria el job para el resto de la vida del worker."""
    with patch.object(job, "ejecutar_pasada", new=AsyncMock(side_effect=RuntimeError("boom"))):
        await job.check_depuracion_masivos()   # no debe propagar


@pytest.mark.asyncio
async def test_el_modo_agresivo_deja_aviso_en_el_log(monkeypatch):
    monkeypatch.setenv("DEPURACION_AUTO_MODO", MODO_HISTORIAL_SIN_EXCEPCION)
    with patch.object(job, "ejecutar_pasada", new=AsyncMock()), \
         patch.object(job.logger, "warning") as aviso:
        await job.check_depuracion_masivos()
    assert any(MODO_HISTORIAL_SIN_EXCEPCION in str(c) for c in aviso.call_args_list)


@pytest.mark.asyncio
async def test_el_modo_se_puede_cambiar_por_entorno(monkeypatch):
    monkeypatch.setenv("DEPURACION_AUTO_MODO", MODO_RACHA_SEGUIDA)
    with _Entorno(n=1):
        r = await job.ejecutar_pasada(aplicar=False)
    assert r["modo"] == MODO_RACHA_SEGUIDA


# ═══════════════════════════════════════════════════════════════════════════
# 5. Conectores: quién se une con quién
# ═══════════════════════════════════════════════════════════════════════════

def test_el_job_no_reescribe_la_regla():
    """
    La decision vive en el motor puro. Si aparece aqui un umbral o una comparacion
    de fechas propia, hay dos reglas y un dia diran cosas distintas.
    """
    fuente = open(job.__file__, encoding="utf-8").read()
    assert "from .depuracion_no_responde import" in fuente
    for prohibido in ("UMBRAL", "timedelta(hours=48)", "len(envios) >="):
        assert prohibido not in fuente, f"el job empezo a decidir por su cuenta: {prohibido}"


def test_el_job_no_abre_conexiones_propias():
    """
    Regla 1 de CLAUDE.md: Redis, httpx y HubSpot solo desde los singletons. Un job
    diario es justo donde una fuga de conexiones acaba en memory watchdog.
    """
    fuente = open(job.__file__, encoding="utf-8").read()
    for prohibido in ("AsyncIOMotorClient(", "redis.from_url(", "httpx.AsyncClient("):
        assert prohibido not in fuente, f"conexion creada fuera del singleton: {prohibido}"


def test_el_job_esta_registrado_en_el_arranque():
    """Un job impecable que nadie registra no corre nunca."""
    ruta = os.path.join(ROOT, "app.py")
    fuente = open(ruta, encoding="utf-8").read()
    assert "check_depuracion_masivos" in fuente
    assert "hour=5" in fuente, "la hora dejo de ser las 5:00 — revisa que no choque con el panel"


def test_el_script_y_el_job_leen_lo_mismo():
    """Duplicar la consulta garantizaba que un dia vieran cosas distintas."""
    ruta = os.path.join(ROOT, "scripts", "depurar_no_responde.py")
    fuente = open(ruta, encoding="utf-8").read()
    assert "from middleware.job_depuracion_masivos import leer_historial_masivos" in fuente


# ═══════════════════════════════════════════════════════════════════════════
# 6. El movimiento real, con su deshacer
# ═══════════════════════════════════════════════════════════════════════════
# Los tests de la pasada parchean `_mover_al_embudo_terminal` entero, asi que
# hasta aqui el deshacer no lo ejecutaba nadie: una mutacion que lo eliminaba
# sobrevivio a las 40 pruebas anteriores. Estas lo ejercitan de verdad.

import middleware.outbound_panel as panel  # noqa: E402


def _respuesta(codigo):
    r = MagicMock()
    r.status_code = codigo
    r.text = ""
    return r


@pytest.mark.asyncio
async def test_mover_es_en_dos_pasos_limpiar_y_fijar():
    """
    Un solo PATCH no bastaria: lifecyclestage en HubSpot es unidireccional y solo
    deja avanzar, asi que traer a alguien a un embudo terminal exige limpiar.
    """
    parche = AsyncMock(return_value=_respuesta(200))
    with patch.object(panel, "HUBSPOT_API_KEY", "clave"), \
         patch.object(panel, "_hubspot_patch", new=parche), \
         patch.object(panel, "_invalidate_contact_stage_cache", new=AsyncMock()):
        error = await job._mover_al_embudo_terminal("c1", ORIGEN, DESTINO)

    assert error is None
    assert parche.await_count == 2
    assert parche.await_args_list[0].args[1] == {"properties": {"lifecyclestage": ""}}
    assert parche.await_args_list[1].args[1] == {"properties": {"lifecyclestage": DESTINO}}


@pytest.mark.asyncio
async def test_al_mover_se_invalida_la_cache_de_etapa():
    """Si no, el panel sigue mostrando 'No responde' hasta que expire el TTL."""
    invalidar = AsyncMock()
    with patch.object(panel, "HUBSPOT_API_KEY", "clave"), \
         patch.object(panel, "_hubspot_patch", new=AsyncMock(return_value=_respuesta(200))), \
         patch.object(panel, "_invalidate_contact_stage_cache", new=invalidar):
        await job._mover_al_embudo_terminal("c1", ORIGEN, DESTINO)
    invalidar.assert_awaited_once_with("c1", DESTINO)


@pytest.mark.asyncio
async def test_si_falla_el_limpiado_no_se_toco_nada():
    """El contacto sigue en su embudo: no hay nada que deshacer."""
    parche = AsyncMock(return_value=_respuesta(500))
    with patch.object(panel, "HUBSPOT_API_KEY", "clave"), \
         patch.object(panel, "_hubspot_patch", new=parche):
        error = await job._mover_al_embudo_terminal("c1", ORIGEN, DESTINO)

    assert error == "limpiar_HTTP_500"
    assert parche.await_count == 1


@pytest.mark.asyncio
async def test_si_falla_el_fijado_el_contacto_vuelve_a_su_embudo():
    """
    LA REGRESION. Entre limpiar y fijar el contacto NO TIENE ETAPA: no lo ve el
    panel y no lo alcanza ninguna regla de embudo. Paso el 18-ago-2026 y dejo un
    contacto atrapado hasta que se rescato a mano.
    """
    parche = AsyncMock(side_effect=[
        _respuesta(200),   # limpiar -> el contacto queda SIN ETAPA
        _respuesta(500),   # fijar   -> falla
        _respuesta(200),   # deshacer
    ])
    with patch.object(panel, "HUBSPOT_API_KEY", "clave"), \
         patch.object(panel, "_hubspot_patch", new=parche), \
         patch.object(panel, "_invalidate_contact_stage_cache", new=AsyncMock()):
        error = await job._mover_al_embudo_terminal("c1", ORIGEN, DESTINO)

    assert error is not None and "deshecho" in error
    assert parche.await_count == 3
    assert parche.await_args_list[2].args[1] == {"properties": {"lifecyclestage": ORIGEN}}


@pytest.mark.asyncio
async def test_un_corte_de_red_a_mitad_tambien_deshace():
    """Un WinError 10054 entre los dos pasos abre el mismo hueco que un HTTP 500."""
    parche = AsyncMock(side_effect=[
        _respuesta(200),
        ConnectionResetError("conexion cerrada por el host remoto"),
        _respuesta(200),
    ])
    with patch.object(panel, "HUBSPOT_API_KEY", "clave"), \
         patch.object(panel, "_hubspot_patch", new=parche), \
         patch.object(panel, "_invalidate_contact_stage_cache", new=AsyncMock()):
        error = await job._mover_al_embudo_terminal("c1", ORIGEN, DESTINO)

    assert error is not None and "deshecho" in error


@pytest.mark.asyncio
async def test_si_el_deshacer_tambien_falla_se_grita():
    """El caso que dejo un contacto atrapado. Tiene que quedar dicho en el motivo."""
    parche = AsyncMock(side_effect=[_respuesta(200), _respuesta(500), _respuesta(500)])
    with patch.object(panel, "HUBSPOT_API_KEY", "clave"), \
         patch.object(panel, "_hubspot_patch", new=parche), \
         patch.object(panel, "_invalidate_contact_stage_cache", new=AsyncMock()):
        error = await job._mover_al_embudo_terminal("c1", ORIGEN, DESTINO)

    assert "SIN ETAPA" in error


@pytest.mark.asyncio
async def test_el_job_reusa_el_deshacer_del_panel_en_vez_de_copiarlo():
    """Una segunda copia del deshacer envejeceria por su cuenta."""
    with patch.object(panel, "HUBSPOT_API_KEY", "clave"), \
         patch.object(panel, "_hubspot_patch",
                      new=AsyncMock(side_effect=[_respuesta(200), _respuesta(500)])), \
         patch.object(panel, "_deshacer_etapa",
                      new=AsyncMock(return_value="deshecho")) as deshacer:
        await job._mover_al_embudo_terminal("c1", ORIGEN, DESTINO)

    deshacer.assert_awaited_once()
    assert deshacer.await_args.args[0] == "c1"
    assert deshacer.await_args.args[1] == ORIGEN
