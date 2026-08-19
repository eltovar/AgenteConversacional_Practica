"""
QA Fase 6 — El deadlock que congeló las campañas masivas 33 días.

QUÉ PASÓ (produccion, 15-jul → 18-ago-2026)
  claim_next_pending_contacts() marca los contactos "in_progress" ANTES de
  enviar. El 15-jul un proceso murió en ese hueco y dejó 5 contactos
  reclamados. A partir de ahí:

    · no queda nada "pending" que reclamar  -> el tick no hace nada
    · finalize no cierra: hay "in_progress" -> la campaña sigue activa
    · get_active_bulk_campaigns ordena por created_at ASC y el tick tomaba
      SIEMPRE la primera -> esa campaña volvía a salir elegida cada 15s

  Resultado: 3 campañas y 217 envíos congelados 33 días, sin un solo error en
  los logs. El tick registraba "executed successfully" cada 15 segundos.

LO QUE SE VALIDA AQUI
  1. Una reclamación huérfana se detecta (y el caso naive/UTC de Mongo).
  2. Una campaña sin trabajo no puede bloquear a las que van detrás.
  3. Lo huérfano de una campaña vieja se cancela; el de una reciente se
     reintenta.

Ejecutar:
    python -m pytest tests/panel/test_bulk_deadlock_qa.py -v
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from database.mongodb_client import (  # noqa: E402
    _a_utc,
    campana_demasiado_vieja,
    claim_caducado,
)

AHORA = datetime(2026, 8, 18, 22, 0, tzinfo=timezone.utc)
LEASE = 900          # 15 min
MAX_EDAD_H = 24


# ══════════════════════════════════════════════════════════════════════
# La trampa que casi deja el fix en nada
# ══════════════════════════════════════════════════════════════════════

def test_mongo_devuelve_naive_y_hay_que_tratarlo_como_utc():
    """
    REGRESION CRITICA. El cliente de Mongo no usa tz_aware, asi que todo
    datetime vuelve NAIVE y en UTC. Restarle un datetime consciente lanza
    TypeError; envuelto en un try/except, el rescate se vuelve un no-op
    silencioso y el deadlock sigue vivo sin que nadie se entere.
    """
    naive = datetime(2026, 8, 18, 21, 0)          # como lo devuelve Mongo
    normalizado = _a_utc(naive)
    assert normalizado.tzinfo is timezone.utc
    assert (AHORA - normalizado).total_seconds() == 3600  # no revienta

    # y el helper completo lo aguanta
    assert claim_caducado(naive, AHORA, LEASE) is True


def test_un_datetime_ya_consciente_no_se_toca():
    aware = datetime(2026, 8, 18, 21, 0, tzinfo=timezone.utc)
    assert _a_utc(aware) is aware


def test_a_utc_tolera_none():
    assert _a_utc(None) is None


# ══════════════════════════════════════════════════════════════════════
# Detección de la reclamación huérfana
# ══════════════════════════════════════════════════════════════════════

def test_reclamacion_reciente_no_es_huerfana():
    """Un lote en curso no se puede rescatar por debajo."""
    hace_poco = (AHORA - timedelta(seconds=10)).replace(tzinfo=None)
    assert claim_caducado(hace_poco, AHORA, LEASE) is False


def test_reclamacion_vieja_es_huerfana():
    vieja = (AHORA - timedelta(hours=2)).replace(tzinfo=None)
    assert claim_caducado(vieja, AHORA, LEASE) is True


def test_sin_claimed_at_se_da_por_huerfana():
    """
    Los 5 contactos que causaron el atasco no tenian la marca: son anteriores
    a que existiera. Si no se dieran por caducados, el deadlock historico
    seguiria sin resolverse solo.
    """
    assert claim_caducado(None, AHORA, LEASE) is True


def test_el_limite_del_lease_es_exacto():
    justo = (AHORA - timedelta(seconds=LEASE)).replace(tzinfo=None)
    assert claim_caducado(justo, AHORA, LEASE) is False
    un_pelo_mas = (AHORA - timedelta(seconds=LEASE + 1)).replace(tzinfo=None)
    assert claim_caducado(un_pelo_mas, AHORA, LEASE) is True


# ══════════════════════════════════════════════════════════════════════
# Reintentar o cancelar, según la edad de la campaña
# ══════════════════════════════════════════════════════════════════════

def test_campana_reciente_se_reintenta():
    """Un reinicio de despliegue no debe costar envios."""
    creada = (AHORA - timedelta(hours=1)).replace(tzinfo=None)
    assert campana_demasiado_vieja(creada, AHORA, MAX_EDAD_H) is False


def test_campana_de_hace_semanas_se_cancela():
    """
    El caso real: 33 dias. Reintentar significaria mandar un mensaje comercial
    completamente fuera de contexto.
    """
    creada = (AHORA - timedelta(days=33)).replace(tzinfo=None)
    assert campana_demasiado_vieja(creada, AHORA, MAX_EDAD_H) is True


def test_campana_sin_fecha_se_trata_como_vieja():
    """Ante la duda, no enviar."""
    assert campana_demasiado_vieja(None, AHORA, MAX_EDAD_H) is True


# ══════════════════════════════════════════════════════════════════════
# El tick: una campaña atascada no puede bloquear a las demás
# ══════════════════════════════════════════════════════════════════════

class _RedisFalso:
    async def set(self, *a, **k):
        return True

    async def delete(self, *a, **k):
        return 1


def _mongo_falso(campanas, con_trabajo):
    """con_trabajo: id de la campaña que sí devuelve contactos al reclamar."""
    m = MagicMock()
    m.get_active_bulk_campaigns = AsyncMock(return_value=campanas)
    m.recuperar_contactos_caducados = AsyncMock(return_value={"devueltos": 0, "cancelados": 0})

    async def _claim(cid, batch):
        if cid == con_trabajo:
            return [{"contact_id": "c1", "phone": "+573000000000", "firstname": "Ana",
                     "_campaign_stage_id": "other", "_campaign_template_content_sid": "HXsid",
                     "_campaign_template_id": "t", "_campaign_creator_advisor_id": "1",
                     "_campaign_creator_advisor_name": "A"}]
        return []

    m.claim_next_pending_contacts = AsyncMock(side_effect=_claim)
    m.finalize_bulk_campaign_if_done = AsyncMock(return_value=False)
    m.get_bulk_campaign = AsyncMock(return_value=None)
    m.mark_bulk_contact_sent = AsyncMock(return_value=True)
    m.mark_bulk_contact_failed = AsyncMock(return_value=True)
    return m


@pytest.mark.asyncio
async def test_una_campana_sin_trabajo_no_bloquea_a_la_siguiente():
    """
    REGRESION PRINCIPAL DEL DEADLOCK. La primera campaña no tiene nada
    reclamable pero tampoco puede cerrarse. Antes, el tick se iba de vacio y
    la segunda no se procesaba jamas.
    """
    from middleware import outbound_panel as op

    atascada = {"_id": "campanaAtascada", "template_variables": {}}
    siguiente = {"_id": "campanaSiguiente", "template_variables": {}}
    mongo = _mongo_falso([atascada, siguiente], con_trabajo="campanaSiguiente")

    with patch.object(op, "_get_redis_client", AsyncMock(return_value=_RedisFalso())), \
         patch.object(op, "get_mongo_manager", lambda: mongo), \
         patch.object(op, "BULK_ALLOWED_TEMPLATES", {}):
        await op._process_bulk_campaign_tick()

    reclamadas = [c.args[0] for c in mongo.claim_next_pending_contacts.await_args_list]
    assert "campanaAtascada" in reclamadas, "ni siquiera lo intento con la primera"
    assert "campanaSiguiente" in reclamadas, (
        "se rindio en la primera campana: el deadlock sigue vivo"
    )


@pytest.mark.asyncio
async def test_el_tick_rescata_lo_huerfano_antes_de_reclamar():
    """
    El orden importa: si no se rescata primero, no hay nada que reclamar y la
    campaña no puede avanzar ni cerrarse.
    """
    from middleware import outbound_panel as op

    campana = {"_id": "c1", "template_variables": {}}
    mongo = _mongo_falso([campana], con_trabajo=None)

    with patch.object(op, "_get_redis_client", AsyncMock(return_value=_RedisFalso())), \
         patch.object(op, "get_mongo_manager", lambda: mongo), \
         patch.object(op, "BULK_ALLOWED_TEMPLATES", {}):
        await op._process_bulk_campaign_tick()

    mongo.recuperar_contactos_caducados.assert_awaited()
    assert mongo.recuperar_contactos_caducados.await_args.args[0] == "c1"
    # y si sigue sin haber trabajo, intenta cerrarla
    mongo.finalize_bulk_campaign_if_done.assert_awaited_with("c1")


@pytest.mark.asyncio
async def test_sin_campanas_activas_el_tick_no_hace_nada():
    from middleware import outbound_panel as op

    mongo = _mongo_falso([], con_trabajo=None)
    with patch.object(op, "_get_redis_client", AsyncMock(return_value=_RedisFalso())), \
         patch.object(op, "get_mongo_manager", lambda: mongo):
        await op._process_bulk_campaign_tick()

    mongo.claim_next_pending_contacts.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════
# Contratos
# ══════════════════════════════════════════════════════════════════════

def _fuente(ruta):
    with open(os.path.join(ROOT, ruta), "r", encoding="utf-8") as f:
        return f.read()


def test_el_claim_deja_marca_de_tiempo():
    """Sin claimed_at no hay forma de distinguir 'en curso' de 'huerfano'."""
    src = _fuente("database/mongodb_client.py")
    assert '"contacts.$.claimed_at": datetime.now(TIMEZONE)' in src, (
        "el claim dejo de marcar cuando reclama: el deadlock puede volver"
    )


def test_el_tick_mira_mas_de_una_campana():
    src = _fuente("middleware/outbound_panel.py")
    assert "BULK_CAMPAIGNS_PER_TICK" in src
    assert "limit=1" not in src.split("get_active_bulk_campaigns")[1][:120], (
        "el tick volvio a mirar una sola campana"
    )


def test_los_plazos_son_constantes_con_nombre():
    from middleware import outbound_panel as op

    assert op.BULK_CLAIM_LEASE_SECONDS == 900
    assert op.BULK_CAMPAIGN_MAX_AGE_HOURS == 24
    assert op.BULK_CAMPAIGNS_PER_TICK > 1


def test_cancelar_no_marca_los_contactos_como_fallidos():
    """
    "cancelled" y "failed" no son lo mismo: uno es "no se intento" y el otro
    "se intento y no se pudo". Perder la distincion borra la trazabilidad de
    a quien se le llego a escribir.
    """
    import ast as _ast

    arbol = _ast.parse(_fuente("database/mongodb_client.py"))
    funcion = next(
        n for n in _ast.walk(arbol)
        if isinstance(n, _ast.AsyncFunctionDef) and n.name == "cancel_bulk_campaign"
    )
    # Fuera el docstring: explica la diferencia entre "cancelled" y "failed",
    # y mirar la prosa en vez del codigo da un falso positivo.
    cuerpo = list(funcion.body)
    if (cuerpo and isinstance(cuerpo[0], _ast.Expr)
            and isinstance(cuerpo[0].value, _ast.Constant)
            and isinstance(cuerpo[0].value.value, str)):
        cuerpo = cuerpo[1:]
    literales = [
        n.value for c in cuerpo for n in _ast.walk(c)
        if isinstance(n, _ast.Constant) and isinstance(n.value, str)
    ]
    assert "cancelled" in literales, "dejo de marcar los contactos como cancelled"
    assert "failed" not in literales, (
        "cancelar marca contactos como failed: se pierde la distincion entre "
        "'no se intento' y 'se intento y no se pudo'"
    )
