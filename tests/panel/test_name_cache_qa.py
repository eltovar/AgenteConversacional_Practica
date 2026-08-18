"""
QA Fase 6 — Cache de nombres del panel: dos bugs distintos.

BUG A — LA CARRERA (misma clase que la de contact_stage, TTL de 4h en vez de 1h)
  GET /contacts tarda segundos. Al terminar dispara _cache_contact_name() con el
  nombre leido AL EMPEZAR. Si en medio cambia el nombre, la escritura tardia
  resucita el viejo durante 4 horas.

BUG B — LA INVALIDACION QUE FALTABA (el que mas dolia)
  Tres escritores tocan el nombre en HubSpot:
    - agents/CRMAgent/crm_agent.py:615      (search-before-create)
    - middleware/contact_manager.py:914     (update_contact_info, 5 llamantes
                                             en webhook_handler)
    - el renombrado manual del panel
  Los dos primeros NUNCA refrescaban el cache. Sofia captura el nombre real a
  mitad de conversacion, HubSpot ya lo tiene, y el panel seguia mostrando el
  valor viejo —normalmente el propio telefono, que es como se crean los leads—
  hasta 4 horas.

EL DISENO
  Los tres desembocan en hubspot_client.update_contact(). Alli se dispara un
  hook registrado por el panel. La capa de integracion NO conoce Redis, ni
  claves, ni TTLs: solo avisa. Asi no hay import circular (outbound_panel ya
  importa integrations.hubspot) ni un pool de Redis mas.

Ejecutar:
    python -m pytest tests/panel/test_name_cache_qa.py -v
"""
import json
import os
import sys

import pytest
from unittest.mock import AsyncMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

CONTACT_ID = "242360551320"
PHONE = "+573123122957"

PANEL_PY = os.path.join(ROOT, "middleware", "outbound_panel.py")
HS_CLIENT_PY = os.path.join(ROOT, "integrations", "hubspot", "hubspot_client.py")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class FakeRedis:
    """Redis minimo con semantica real de SET NX / EX, GET, MGET y DELETE."""

    def __init__(self, initial=None):
        self.store = dict(initial or {})
        self.ttls = {}

    async def get(self, key):
        return self.store.get(key)

    async def mget(self, keys):
        return [self.store.get(k) for k in keys]

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex:
            self.ttls[key] = ex
        return True

    async def delete(self, key):
        self.ttls.pop(key, None)
        return 1 if self.store.pop(key, None) is not None else 0


def _key(cid=CONTACT_ID):
    from middleware import outbound_panel as op
    return f"{op.CONTACT_NAME_CACHE_PREFIX}{cid}"


def _nombre(redis, cid=CONTACT_ID):
    raw = redis.store.get(_key(cid))
    return json.loads(raw) if raw else None


# ══════════════════════════════════════════════════════════════════════
# BUG B — El chokepoint. Sofia captura el nombre y el panel se entera.
# ══════════════════════════════════════════════════════════════════════

def test_el_hook_se_registra_al_importar_el_panel():
    """
    Modo de fallo silencioso: si el registro no ocurre, la invalidacion
    simplemente no pasa y nada falla a la vista. app.py:72 importa middleware,
    asi que el panel siempre esta cargado; este test lo blinda.
    """
    from integrations.hubspot.hubspot_client import _contact_update_hooks
    from middleware import outbound_panel as op  # noqa: F401  (el import registra)

    nombres = [h.__name__ for h in _contact_update_hooks]
    assert "_on_hubspot_contact_updated" in nombres, (
        f"el panel no registro su hook; hooks presentes: {nombres}"
    )


def test_registrar_dos_veces_no_duplica():
    """Una reimportacion no debe hacer que el hook corra por partida doble."""
    from integrations.hubspot import register_contact_update_hook
    from integrations.hubspot.hubspot_client import _contact_update_hooks
    from middleware import outbound_panel as op

    antes = len(_contact_update_hooks)
    register_contact_update_hook(op._on_hubspot_contact_updated)
    assert len(_contact_update_hooks) == antes


@pytest.mark.asyncio
async def test_sofia_captura_el_nombre_y_el_panel_lo_ve():
    """
    REGRESION PRINCIPAL DEL BUG B. Reproduce el caso de produccion:
    el lead se creo con el telefono como nombre, Sofia captura el real,
    y el panel debe verlo ya — no dentro de 4 horas.
    """
    from integrations.hubspot import hubspot_client as cliente
    from middleware import outbound_panel as op

    redis = FakeRedis({_key(): json.dumps({"firstname": PHONE, "lastname": ""})})

    with patch.object(op, "_get_redis_client", AsyncMock(return_value=redis)), \
         patch.object(cliente, "_request", AsyncMock(return_value={"id": CONTACT_ID})):
        await cliente.update_contact(
            CONTACT_ID, {"firstname": "Juan", "lastname": "Salas"}
        )

    assert _nombre(redis) == {"firstname": "Juan", "lastname": "Salas"}, (
        f"el panel seguiria mostrando {_nombre(redis)!r} durante 4 horas"
    )


@pytest.mark.asyncio
async def test_los_hooks_corren_antes_de_que_update_contact_retorne():
    """
    Requisito de diseno: `await`, nunca create_task. Una escritura en background
    llegaria con datos previos al PATCH y reintroduciria la carrera del Bug A.
    Sin ceder el control al loop, el cache ya debe estar al dia.
    """
    from integrations.hubspot import hubspot_client as cliente
    from middleware import outbound_panel as op

    redis = FakeRedis()
    with patch.object(op, "_get_redis_client", AsyncMock(return_value=redis)), \
         patch.object(cliente, "_request", AsyncMock(return_value={"id": CONTACT_ID})):
        await cliente.update_contact(CONTACT_ID, {"firstname": "Ana", "lastname": "Ruiz"})
        # Ni un solo await extra entre el update y la comprobacion.
        assert _nombre(redis) == {"firstname": "Ana", "lastname": "Ruiz"}


@pytest.mark.asyncio
async def test_un_cambio_que_no_toca_el_nombre_no_escribe_en_redis():
    """Coste cero para los updates que no tienen nada que ver con el nombre."""
    from integrations.hubspot import hubspot_client as cliente
    from middleware import outbound_panel as op

    redis = FakeRedis()
    obtener_redis = AsyncMock(return_value=redis)
    with patch.object(op, "_get_redis_client", obtener_redis), \
         patch.object(cliente, "_request", AsyncMock(return_value={"id": CONTACT_ID})):
        await cliente.update_contact(CONTACT_ID, {"chatbot_location": "Envigado"})

    # Ni siquiera debe pedir el cliente de Redis: un DELETE sobre una key
    # ausente dejaria el store igual y el test no se enteraria.
    obtener_redis.assert_not_awaited()
    assert redis.store == {}, "toco Redis por un cambio ajeno al nombre"


@pytest.mark.asyncio
async def test_update_parcial_invalida_en_vez_de_escribir_medio_nombre():
    """
    Si solo llega firstname, escribir el payload completo borraria el apellido
    cacheado. Hay que invalidar y dejar que HubSpot mande.
    """
    from integrations.hubspot import hubspot_client as cliente
    from middleware import outbound_panel as op

    redis = FakeRedis({_key(): json.dumps({"firstname": "Juan", "lastname": "Salas"})})

    with patch.object(op, "_get_redis_client", AsyncMock(return_value=redis)), \
         patch.object(cliente, "_request", AsyncMock(return_value={"id": CONTACT_ID})):
        await cliente.update_contact(CONTACT_ID, {"firstname": "Juan Carlos"})

    assert _key() not in redis.store, (
        f"escribio un nombre a medias y perdio el apellido: {_nombre(redis)!r}"
    )


@pytest.mark.asyncio
async def test_un_hook_roto_no_tumba_la_sincronizacion_con_hubspot():
    """Un Redis caido nunca puede impedir que el dato llegue al CRM."""
    from integrations.hubspot import hubspot_client as cliente
    from integrations.hubspot.hubspot_client import (
        _contact_update_hooks, register_contact_update_hook,
    )

    async def _hook_roto(cid, props):
        raise RuntimeError("Redis caido")

    peticion = AsyncMock(return_value={"id": CONTACT_ID})
    register_contact_update_hook(_hook_roto)
    try:
        with patch.object(cliente, "_request", peticion), \
             patch("middleware.outbound_panel._get_redis_client",
                   AsyncMock(side_effect=RuntimeError("Redis caido"))):
            await cliente.update_contact(CONTACT_ID, {"firstname": "Ana", "lastname": "Ruiz"})
    finally:
        _contact_update_hooks.remove(_hook_roto)

    peticion.assert_awaited_once()  # el PATCH a HubSpot si ocurrio


@pytest.mark.asyncio
async def test_sin_propiedades_validas_no_se_avisa_a_nadie():
    """
    update_contact() aborta antes del PATCH si el validador filtra todo.
    En ese caso HubSpot no cambio nada y el cache no debe tocarse.
    """
    from integrations.hubspot import hubspot_client as cliente
    from middleware import outbound_panel as op

    redis = FakeRedis({_key(): json.dumps({"firstname": "Juan", "lastname": "Salas"})})
    peticion = AsyncMock(return_value={"id": CONTACT_ID})

    with patch.object(op, "_get_redis_client", AsyncMock(return_value=redis)), \
         patch.object(cliente, "_request", peticion):
        # chatbot_summary esta en KNOWN_MISSING_PROPERTIES: el validador la
        # filtra y update_contact aborta antes del PATCH.
        await cliente.update_contact(CONTACT_ID, {"chatbot_summary": "x"})

    peticion.assert_not_awaited()
    assert _nombre(redis) == {"firstname": "Juan", "lastname": "Salas"}


# ══════════════════════════════════════════════════════════════════════
# BUG A — La carrera del escritor de fondo
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_carrera_el_poll_tardio_no_resucita_el_nombre_viejo():
    """
    REGRESION PRINCIPAL DEL BUG A. GET /contacts leyo el telefono como nombre
    al empezar; en medio el nombre real se fija; la tarea de fondo aterriza
    despues y no debe revivir el viejo.
    """
    from middleware import outbound_panel as op

    redis = FakeRedis()
    nombre_leido_al_empezar = (PHONE, "")

    with patch.object(op, "_get_redis_client", AsyncMock(return_value=redis)):
        await op._invalidate_contact_name_cache(
            CONTACT_ID, firstname="Juan", lastname="Salas"
        )
        await op._cache_contact_name(CONTACT_ID, *nombre_leido_al_empezar)

    assert _nombre(redis) == {"firstname": "Juan", "lastname": "Salas"}, (
        f"la escritura tardia resucito el nombre muerto: {_nombre(redis)!r}"
    )


@pytest.mark.asyncio
async def test_cache_contact_name_si_puebla_cuando_no_hay_nada():
    """nx no puede romper el camino normal."""
    from middleware import outbound_panel as op

    redis = FakeRedis()
    with patch.object(op, "_get_redis_client", AsyncMock(return_value=redis)):
        await op._cache_contact_name(CONTACT_ID, "Juan", "Salas")

    assert _nombre(redis) == {"firstname": "Juan", "lastname": "Salas"}
    assert redis.ttls[_key()] == op.CONTACT_NAME_CACHE_TTL


@pytest.mark.asyncio
async def test_el_renombrado_deja_el_nombre_nuevo_en_vez_de_borrar():
    """Ahorra la ida a HubSpot que el DELETE forzaba en el siguiente poll."""
    from middleware import outbound_panel as op

    redis = FakeRedis({_key(): json.dumps({"firstname": "viejo", "lastname": ""})})
    with patch.object(op, "_get_redis_client", AsyncMock(return_value=redis)):
        await op._invalidate_contact_name_cache(CONTACT_ID, "Nuevo", "Nombre")

    assert _key() in redis.store, "borro la key: el siguiente poll ira a HubSpot"
    assert _nombre(redis) == {"firstname": "Nuevo", "lastname": "Nombre"}


@pytest.mark.asyncio
async def test_invalidar_sin_nombre_borra():
    from middleware import outbound_panel as op

    redis = FakeRedis({_key(): json.dumps({"firstname": "Juan", "lastname": "Salas"})})
    with patch.object(op, "_get_redis_client", AsyncMock(return_value=redis)):
        await op._invalidate_contact_name_cache(CONTACT_ID)

    assert _key() not in redis.store


# ══════════════════════════════════════════════════════════════════════
# ANTI-HARDCODEO — cada valor vive en un unico sitio
# ══════════════════════════════════════════════════════════════════════

def test_el_prefijo_de_la_key_no_se_repite_en_ningun_sitio():
    """
    El literal "contact_name:" solo puede aparecer en la definicion de la
    constante. Todo lo demas pasa por _contact_name_cache_key().
    """
    import ast as _ast

    src = _read(PANEL_PY)

    # AST, no regex: ast.walk desciende dentro de los f-strings (JoinedStr), asi
    # que un f"contact_name:{cid}" escrito a mano se detecta igual que un
    # literal suelto — su trozo constante es exactamente el prefijo. Con regex
    # sobre '"contact_name:"' se colaba.
    #
    # "empieza por el prefijo" y no "lo contiene": hay variables y parametros
    # llamados contact_name (el nombre a mostrar) y campos como
    # contact_name_keys que no tienen nada que ver con la key del cache.
    constantes = [
        n.value for n in _ast.walk(_ast.parse(src))
        if isinstance(n, _ast.Constant) and isinstance(n.value, str)
    ]
    for prefijo, constante in (
        ("contact_name:", "CONTACT_NAME_CACHE_PREFIX"),
        ("contact_stage:", "CONTACT_STAGE_CACHE_PREFIX"),
    ):
        apariciones = [c for c in constantes if c.startswith(prefijo)]
        assert len(apariciones) == 1, (
            f"'{prefijo}' aparece en {len(apariciones)} literales; solo puede "
            f"estar en la definicion de {constante} — el resto debe pasar por "
            f"su constructor de key"
        )


def test_los_nombres_de_propiedad_salen_de_la_constante():
    """El hook no puede llevar 'firstname'/'lastname' escritos como filtro."""
    from middleware import outbound_panel as op

    assert op.CONTACT_NAME_PROPERTIES == ("firstname", "lastname")

    src = _read(PANEL_PY)
    cuerpo = src[src.index("async def _on_hubspot_contact_updated"):]
    cuerpo = cuerpo[:cuerpo.index("_register_contact_update_hook(")]
    assert "CONTACT_NAME_PROPERTIES" in cuerpo, (
        "el hook filtra sin usar la constante"
    )


def test_la_capa_de_integracion_no_conoce_redis_ni_claves_de_cache():
    """
    Guard arquitectonico: hubspot_client solo avisa. Si algun dia alguien mete
    Redis ahi, crea el pool numero once y reabre la crisis de memoria que
    CLAUDE.md documenta.
    """
    import ast as _ast

    arbol = _ast.parse(_read(HS_CLIENT_PY))

    importados = []
    for nodo in _ast.walk(arbol):
        if isinstance(nodo, _ast.Import):
            importados += [a.name for a in nodo.names]
        elif isinstance(nodo, _ast.ImportFrom):
            importados.append(nodo.module or "")
    assert not any("redis" in m.lower() for m in importados), (
        f"la capa de integracion importo Redis: {importados}"
    )

    literales = [
        n.value for n in _ast.walk(arbol)
        if isinstance(n, _ast.Constant) and isinstance(n.value, str)
    ]
    # Se excluyen docstrings: lo que importa es que no haya claves ni TTLs
    # operativos, no que la prosa explique por que no los hay.
    docstrings = set()
    for nodo in _ast.walk(arbol):
        if isinstance(nodo, (_ast.Module, _ast.ClassDef, _ast.FunctionDef, _ast.AsyncFunctionDef)):
            d = _ast.get_docstring(nodo, clean=False)
            if d:
                docstrings.add(d)
    operativos = [s for s in literales if s not in docstrings]
    assert not any("contact_name" in s for s in operativos), (
        "la capa de integracion construye una key de cache"
    )

    nombres = [n.id for n in _ast.walk(arbol) if isinstance(n, _ast.Name)]
    nombres += [n.attr for n in _ast.walk(arbol) if isinstance(n, _ast.Attribute)]
    assert not any("CACHE_TTL" in n for n in nombres), (
        "la capa de integracion conoce un TTL de cache"
    )


def test_conector_update_contact_es_el_unico_disparador():
    """
    Documenta el conector: los tres escritores pasan por update_contact(), y
    update_contact() avisa una sola vez, despues del PATCH.
    """
    src = _read(HS_CLIENT_PY)
    assert src.count("_run_contact_update_hooks(") == 2, (
        "cambio el numero de puntos que disparan los hooks "
        "(esperado: la definicion y la llamada en update_contact)"
    )
    cuerpo = src[src.index("async def update_contact"):]
    cuerpo = cuerpo[:cuerpo.index("async def create_deal")]
    assert cuerpo.index('await self._request("PATCH"') < cuerpo.index("_run_contact_update_hooks("), (
        "los hooks se disparan antes del PATCH: avisarian de un cambio no confirmado"
    )
