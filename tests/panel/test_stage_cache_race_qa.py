"""
QA Fase 6 — El chip del panel se quedaba pegado en "Nuevo Lead".

CONTEXTO (produccion, 18-ago-2026):
El backend promovia bien — 11 de 11 promociones verificadas en HubSpot, ~1s de
latencia, cero errores. Lo que fallaba era lo que VEIA la asesora: el chip
seguia diciendo "Nuevo Lead" hasta una hora despues de que HubSpot ya decia
"En conversacion".

LA CARRERA:
  t+0s   GET /contacts arranca. El batch de HubSpot devuelve lifecyclestage=Nuevo Lead
  t+2s   la asesora responde -> PATCH -> HubSpot ya dice En conversacion
         _invalidate_contact_stage_cache() BORRABA la key
  t+8s   el enriquecimiento termina y dispara, en background,
         _cache_contact_stage(cid, "Nuevo Lead")  <- valor leido al EMPEZAR
         setex con TTL de 1h sobre la key recien borrada = valor muerto revivido

Solo le pasa a leads nuevos: el batch de HubSpot unicamente pide los contactos
cuyo NOMBRE no esta en cache (4h), y un lead recien creado nunca lo esta.

EL INVARIANTE QUE SE VALIDA:
  Solo el codigo que ACABA de cambiar la etapa escribe de forma autoritativa.
  Todo otro escritor es best-effort (nx=True) y se declina si ya hay valor.

Ejecutar:
    python -m pytest tests/panel/test_stage_cache_race_qa.py -v
"""
import ast
import os
import sys

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

NUEVO_LEAD = "1417459250"
EN_CONVERSACION = "1326623075"
CONTACT_ID = "242360551320"
PHONE = "+573123122957"
KEY = f"contact_stage:{CONTACT_ID}"

PANEL_PY = os.path.join(ROOT, "middleware", "outbound_panel.py")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class FakeRedis:
    """Redis minimo con semantica real de SET NX / EX, GET y DELETE."""

    def __init__(self, initial=None):
        self.store = dict(initial or {})
        self.ttls = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None  # redis-py devuelve None cuando NX no escribe
        self.store[key] = value
        if ex:
            self.ttls[key] = ex
        return True

    async def setex(self, key, ttl, value):
        self.store[key] = value
        self.ttls[key] = ttl
        return True

    async def delete(self, key):
        self.ttls.pop(key, None)
        return 1 if self.store.pop(key, None) is not None else 0


def _resp(status=200, payload=None, text="{}"):
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.json = MagicMock(return_value=payload or {})
    return r


# ══════════════════════════════════════════════════════════════════════
# GRUPO A — La carrera. Esto es lo que veian las asesoras.
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_carrera_el_poll_tardio_no_resucita_nuevo_lead():
    """
    REGRESION PRINCIPAL. Reproduce la secuencia completa de produccion:
    enriquecimiento lento -> promocion -> escritura tardia de fondo.
    El chip debe quedar en 'En conversacion', no volver a 'Nuevo Lead'.
    """
    from middleware import outbound_panel as op

    redis = FakeRedis()

    with patch.object(op, "_get_redis_client", AsyncMock(return_value=redis)):
        # t+0s: GET /contacts lee del batch de HubSpot y agenda la escritura
        etapa_leida_al_empezar = NUEVO_LEAD

        # t+2s: la asesora responde, HubSpot confirma el cambio
        await op._invalidate_contact_stage_cache(CONTACT_ID, EN_CONVERSACION)

        # t+8s: la tarea de fondo del enriquecimiento por fin corre
        await op._cache_contact_stage(CONTACT_ID, etapa_leida_al_empezar)

    assert redis.store.get(KEY) == EN_CONVERSACION, (
        f"la escritura tardia resucito la etapa muerta: {redis.store.get(KEY)!r}. "
        f"El chip volveria a decir 'Nuevo Lead' durante 1 hora."
    )


@pytest.mark.asyncio
async def test_invalidacion_deja_el_valor_nuevo_en_vez_de_borrar():
    """
    Escribir en vez de borrar ahorra el GET a HubSpot que el DELETE forzaba
    en la siguiente lectura. Es el fix que ademas quita carga a HubSpot.
    """
    from middleware import outbound_panel as op

    redis = FakeRedis({KEY: NUEVO_LEAD})
    with patch.object(op, "_get_redis_client", AsyncMock(return_value=redis)):
        await op._invalidate_contact_stage_cache(CONTACT_ID, EN_CONVERSACION)

    assert KEY in redis.store, "borro la key: la siguiente lectura ira a HubSpot"
    assert redis.store[KEY] == EN_CONVERSACION
    assert redis.ttls[KEY] == op.CONTACT_STAGE_CACHE_TTL


@pytest.mark.asyncio
async def test_invalidacion_sin_etapa_sigue_borrando():
    """Compatibilidad: sin new_stage el comportamiento historico se conserva."""
    from middleware import outbound_panel as op

    redis = FakeRedis({KEY: NUEVO_LEAD})
    with patch.object(op, "_get_redis_client", AsyncMock(return_value=redis)):
        await op._invalidate_contact_stage_cache(CONTACT_ID)

    assert KEY not in redis.store


@pytest.mark.asyncio
async def test_cache_contact_stage_es_best_effort():
    """El escritor de fondo nunca pisa un valor ya presente."""
    from middleware import outbound_panel as op

    redis = FakeRedis({KEY: EN_CONVERSACION})
    with patch.object(op, "_get_redis_client", AsyncMock(return_value=redis)):
        await op._cache_contact_stage(CONTACT_ID, NUEVO_LEAD)

    assert redis.store[KEY] == EN_CONVERSACION, "escribio sin nx"


@pytest.mark.asyncio
async def test_cache_contact_stage_si_puebla_cuando_no_hay_nada():
    """nx no debe romper el caso normal: sin valor previo, si escribe."""
    from middleware import outbound_panel as op

    redis = FakeRedis()
    with patch.object(op, "_get_redis_client", AsyncMock(return_value=redis)):
        await op._cache_contact_stage(CONTACT_ID, NUEVO_LEAD)

    assert redis.store[KEY] == NUEVO_LEAD
    assert redis.ttls[KEY] == op.CONTACT_STAGE_CACHE_TTL


@pytest.mark.asyncio
async def test_la_lectura_de_hubspot_tampoco_pisa_el_valor_autoritativo():
    """
    Ventana corta pero real: _get_contact_lifecyclestage lee de HubSpot y
    escribe ~80ms despues. Si la invalidacion cae en medio, su escritura
    tambien debe declinarse.
    """
    from middleware import outbound_panel as op

    redis = FakeRedis()
    hubspot = AsyncMock(return_value=_resp(
        200, {"properties": {"lifecyclestage": NUEVO_LEAD}}
    ))

    with patch.object(op, "_get_redis_client", AsyncMock(return_value=redis)), \
         patch.object(op, "_hubspot_get", hubspot), \
         patch.object(op, "HUBSPOT_API_KEY", "test-key"):
        # La invalidacion gana la carrera y deja el valor bueno...
        await op._invalidate_contact_stage_cache(CONTACT_ID, EN_CONVERSACION)
        # ...y la lectura que ya venia en vuelo intenta escribir el viejo.
        redis_get_original = redis.get
        redis.get = AsyncMock(return_value=None)  # simula el cache miss previo
        await op._get_contact_lifecyclestage(CONTACT_ID)
        redis.get = redis_get_original

    assert redis.store[KEY] == EN_CONVERSACION, "la lectura en vuelo piso el valor bueno"


# ══════════════════════════════════════════════════════════════════════
# GRUPO B — strict: distinguir "es otra etapa" de "no pude leerla"
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_strict_devuelve_none_cuando_hubspot_falla():
    from middleware import outbound_panel as op

    redis = FakeRedis()
    with patch.object(op, "_get_redis_client", AsyncMock(return_value=redis)), \
         patch.object(op, "_hubspot_get", AsyncMock(return_value=_resp(429))), \
         patch.object(op, "HUBSPOT_API_KEY", "test-key"):
        etapa = await op._get_contact_lifecyclestage(CONTACT_ID, strict=True)

    assert etapa is None


@pytest.mark.asyncio
async def test_sin_strict_se_conserva_el_fail_open():
    """
    Los otros 5 llamantes dependen de este fallback. STAGES_VISIBLES_WORKER
    contiene 'En conversacion': cambiarlo BORRARIA citas del reporte por asesor
    cada vez que HubSpot devuelva 429.
    """
    from middleware import outbound_panel as op

    redis = FakeRedis()
    with patch.object(op, "_get_redis_client", AsyncMock(return_value=redis)), \
         patch.object(op, "_hubspot_get", AsyncMock(return_value=_resp(429))), \
         patch.object(op, "HUBSPOT_API_KEY", "test-key"):
        etapa = await op._get_contact_lifecyclestage(CONTACT_ID)

    assert etapa == op.HUBSPOT_STAGE_EN_CONVERSACION, (
        "se rompio el fail-open del que dependen worker report, hydrate y panel"
    )


@pytest.mark.asyncio
async def test_promocion_no_da_el_lead_por_promovido_si_no_puede_leer_la_etapa():
    """
    El fallo silencioso que motivo `strict`: un 429 devolvia 'En conversacion',
    el guard cortaba, y el lead se quedaba en 'Nuevo Lead' sin dejar ni un log.
    """
    from middleware import outbound_panel as op

    client = MagicMock()
    client.patch = AsyncMock(return_value=_resp(200))
    redis = FakeRedis()

    with patch.object(op, "_get_redis_client", AsyncMock(return_value=redis)), \
         patch.object(op, "_hubspot_get", AsyncMock(return_value=_resp(429))), \
         patch.object(op, "get_httpx_client", lambda: client), \
         patch.object(op, "HUBSPOT_API_KEY", "test-key"), \
         patch.object(op.logger, "warning") as warn:
        ok = await op._promote_nuevo_lead_to_en_conversacion(CONTACT_ID, PHONE)

    assert ok is False
    client.patch.assert_not_awaited()  # no promover a ciegas
    assert any("[NuevoLead]" in str(c) for c in warn.call_args_list), (
        "el fallo de lectura se trago sin dejar rastro en los logs"
    )


@pytest.mark.asyncio
async def test_promocion_sigue_funcionando_de_punta_a_punta():
    """El camino feliz no se toco: Nuevo Lead -> En conversacion, y cache al dia."""
    from middleware import outbound_panel as op

    client = MagicMock()
    client.patch = AsyncMock(return_value=_resp(200))
    redis = FakeRedis({KEY: NUEVO_LEAD})

    with patch.object(op, "_get_redis_client", AsyncMock(return_value=redis)), \
         patch.object(op, "get_httpx_client", lambda: client), \
         patch.object(op, "HUBSPOT_API_KEY", "test-key"):
        ok = await op._promote_nuevo_lead_to_en_conversacion(CONTACT_ID, PHONE)

    assert ok is True
    enviado = client.patch.await_args.kwargs["json"]["properties"]["lifecyclestage"]
    assert enviado == EN_CONVERSACION
    assert redis.store[KEY] == EN_CONVERSACION, "el cache quedo desincronizado del PATCH"


# ══════════════════════════════════════════════════════════════════════
# GRUPO C — Conectores: quien se une con quien
# ══════════════════════════════════════════════════════════════════════

def _llamadas_a(nombre):
    """Todas las llamadas a `nombre` en outbound_panel.py, via AST."""
    arbol = ast.parse(_read(PANEL_PY))
    encontradas = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name):
            if nodo.func.id == nombre:
                encontradas.append(nodo)
    return encontradas


def test_conector_los_cuatro_invalidadores_pasan_la_etapa_nueva():
    """
    _invalidate_contact_stage_cache() <- 4 productores de cambio de etapa:
      visita realizada, bulk no-responde, promocion Nuevo Lead, PATCH /stage.
    Los cuatro estan dentro de un `if status_code == 200`, asi que los cuatro
    conocen la etapa nueva. Si alguno vuelve a llamar con un solo argumento,
    reabre la carrera para ese flujo.
    """
    llamadas = _llamadas_a("_invalidate_contact_stage_cache")
    assert len(llamadas) == 4, f"cambio el numero de invalidadores: {len(llamadas)}"

    sin_etapa = [c.lineno for c in llamadas if len(c.args) < 2]
    assert not sin_etapa, (
        f"invalidacion sin etapa nueva en las lineas {sin_etapa} — "
        f"esos flujos vuelven a borrar la key y quedan expuestos a la carrera"
    )


def test_conector_solo_la_promocion_usa_strict():
    """
    _get_contact_lifecyclestage() <- 6 consumidores. Solo la promocion puede
    pedir strict; los otros 5 dependen del fail-open.
    """
    llamadas = _llamadas_a("_get_contact_lifecyclestage")
    assert len(llamadas) == 6, f"cambio el numero de consumidores: {len(llamadas)}"

    con_strict = [
        c for c in llamadas
        if any(k.arg == "strict" for k in c.keywords)
    ]
    assert len(con_strict) == 1, (
        f"{len(con_strict)} llamantes usan strict; solo la promocion debe hacerlo"
    )


def test_conector_el_reporte_por_asesor_depende_del_fail_open():
    """
    Documenta por que el fallback no es neutro: si dejara de ser
    'En conversacion', un 429 sacaria citas del reporte por asesor.
    """
    from middleware import outbound_panel as op

    src = _read(PANEL_PY)
    assert '"1326623075",              # En Conversación' in src, (
        "STAGES_VISIBLES_WORKER cambio; revisar el fallback de "
        "_get_contact_lifecyclestage antes de tocar nada"
    )
    assert op.HUBSPOT_STAGE_EN_CONVERSACION == EN_CONVERSACION


# ══════════════════════════════════════════════════════════════════════
# GRUPO D — El camino mudo que quedaba sin diagnosticar
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_meta_sin_contact_id_deja_aviso():
    """
    GET /contacts resuelve el contact_id por telefono cuando falta en el meta,
    pero solo en memoria. Un contacto asi se ve perfecto en el panel y jamas
    se promueve: sin este log no hay forma de detectarlo.
    """
    from middleware import outbound_panel as op

    meta = MagicMock()
    meta.assigned_owner_id = "89096378"
    meta.contact_id = None

    sm = MagicMock()
    sm.update_advisor_message_timestamp = AsyncMock()
    sm.get_meta = AsyncMock(return_value=meta)
    sm.remove_from_advisor_inbox = AsyncMock()
    sm.clear_inactivity_notifications_for_contact = AsyncMock()

    with patch.object(op, "_get_state_manager", lambda: sm), \
         patch.object(op.logger, "warning") as warn:
        await op._update_advisor_timestamp(PHONE, "pagina_web")

    avisos = [str(c) for c in warn.call_args_list if "[NuevoLead]" in str(c)]
    assert avisos, "no se avisa de un meta sin contact_id: el lead se pierde en silencio"


@pytest.mark.asyncio
async def test_el_aviso_no_expone_el_telefono_completo():
    """CLAUDE.md: nunca loggear telefonos completos en produccion."""
    from middleware import outbound_panel as op

    meta = MagicMock()
    meta.assigned_owner_id = "89096378"
    meta.contact_id = None

    sm = MagicMock()
    sm.update_advisor_message_timestamp = AsyncMock()
    sm.get_meta = AsyncMock(return_value=meta)
    sm.remove_from_advisor_inbox = AsyncMock()
    sm.clear_inactivity_notifications_for_contact = AsyncMock()

    with patch.object(op, "_get_state_manager", lambda: sm), \
         patch.object(op.logger, "warning") as warn:
        await op._update_advisor_timestamp(PHONE, "pagina_web")

    emitido = " ".join(str(c) for c in warn.call_args_list)
    assert PHONE not in emitido, "fuga de PII: el telefono completo esta en el log"
    assert PHONE[-4:] in emitido, "sin los ultimos 4 digitos el log no sirve para nada"
