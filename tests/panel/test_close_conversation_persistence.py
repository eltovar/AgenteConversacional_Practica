"""
QA Fase 6 — Cierre de conversaciones: que se cierren cuando deben y, sobre todo,
que NO reaparezcan solas y que SÍ vuelvan cuando corresponde.

Contexto del bug: _close_conversation_internal() solo tocaba Redis, pero el panel
lee de Redis Y de MongoDB. Las dos rutas que releen `conversations`
(find_conversations_by_owner via el fallback de GET /contacts, y
find_recent_conversations via el rebuild nocturno) filtran por `archived`, que
nunca se escribía en True. Resultado: el contacto reaparecía.

Ejecutar:
    python -m pytest tests/panel/test_close_conversation_persistence.py -v

Esto ejecuta correctamente los tests. El bug donde las conversacion aparecen despues de ser cerrados probablemente es por otro motivo.
"""
import json
import sys
import os
import pytest
import pytest_asyncio
import fakeredis.aioredis
from unittest.mock import AsyncMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PHONE = "+573001234567"
CANAL = "whatsapp"
MEMBER = f"{PHONE}:{CANAL}"
ZSET = "active_conversations_sorted"
JUBENY = "89096378"
LUISA = "89096380"
STAGE_CERRADO_PERDIDO = "evangelist"


# ═══════════════════════════════════════════════════════════════
# Dobles de prueba
# ═══════════════════════════════════════════════════════════════

class FakeConversationsCollection:
    """Doble de la colección MongoDB `conversations` con la semántica real de los
    dos queries que releen: filtran por archived != True."""

    def __init__(self):
        self.docs = {}

    def seed(self, phone, canal, owner_id, archived=False):
        self.docs[(phone, canal)] = {
            "phone": phone, "canal": canal, "owner_id": owner_id,
            "archived": archived, "last_message_at": "2026-08-09T10:00:00",
        }

    def set_archived(self, phone, canal, value):
        doc = self.docs.get((phone, canal))
        if doc is None:
            return False
        doc["archived"] = value
        return True

    def find_by_owner(self, owner_id, include_archived=False):
        """Replica find_conversations_by_owner: archived != True."""
        return [
            d for d in self.docs.values()
            if d["owner_id"] == owner_id
            and (include_archived or d.get("archived") is not True)
        ]

    def find_recent(self):
        """Replica find_recent_conversations: archived: {$ne: True}."""
        return [d for d in self.docs.values() if d.get("archived") is not True]


class FakeMongoManager:
    def __init__(self, collection):
        self._c = collection

    async def update_conversation_meta(self, phone, canal="whatsapp", archived=None, **kwargs):
        if archived is None:
            return False
        return self._c.set_archived(phone, (canal or "whatsapp").lower(), archived)


@pytest_asyncio.fixture
async def redis_client():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest_asyncio.fixture
async def panel_env(redis_client):
    """Monta outbound_panel con Redis falso, CSM falso y Mongo falso."""
    from middleware import outbound_panel as op

    collection = FakeConversationsCollection()
    collection.seed(PHONE, CANAL, JUBENY, archived=False)

    await redis_client.zadd(ZSET, {MEMBER: 1000.0})
    await redis_client.set(
        f"conv_meta:{PHONE}:{CANAL}",
        json.dumps({"phone_normalized": PHONE, "assigned_owner_id": JUBENY,
                    "in_panel": True, "display_name": "Cliente Test"}),
    )

    sm = AsyncMock()
    sm.activate_bot = AsyncMock(return_value=True)
    sm.get_meta = AsyncMock(return_value=None)
    sm.get_status = AsyncMock(return_value=None)
    sm.remove_from_advisor_inbox = AsyncMock(return_value=None)

    with patch.object(op, "_get_redis_client", AsyncMock(return_value=redis_client)), \
         patch.object(op, "_get_state_manager", lambda: sm), \
         patch("database.mongodb_client.get_mongo_manager",
               lambda: FakeMongoManager(collection)):
        yield op, redis_client, collection, sm


# ═══════════════════════════════════════════════════════════════
# ESC 1-3: el cierre debe cerrar de verdad (Redis + MongoDB)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_esc01_cierre_saca_del_zset(panel_env):
    """ESC 1 — Al cerrar, el contacto sale del ZSET."""
    op, r, _, _ = panel_env
    assert await r.zscore(ZSET, MEMBER) is not None, "precondición: estaba en el ZSET"

    await op._close_conversation_internal(PHONE, CANAL)

    assert await r.zscore(ZSET, MEMBER) is None, "sigue en el ZSET tras cerrar"


@pytest.mark.asyncio
async def test_esc02_cierre_marca_in_panel_false(panel_env):
    """ESC 2 — Al cerrar, in_panel queda en False en Redis."""
    op, r, _, _ = panel_env

    await op._close_conversation_internal(PHONE, CANAL)

    meta = json.loads(await r.get(f"conv_meta:{PHONE}:{CANAL}"))
    assert meta["in_panel"] is False


@pytest.mark.asyncio
async def test_esc03_cierre_archiva_en_mongo(panel_env):
    """ESC 3 — EL FIX: al cerrar, archived=True en MongoDB."""
    op, _, coll, _ = panel_env
    assert coll.docs[(PHONE, CANAL)]["archived"] is False

    await op._close_conversation_internal(PHONE, CANAL)

    assert coll.docs[(PHONE, CANAL)]["archived"] is True, \
        "sin archived=True el contacto reaparece por el fallback de MongoDB"


# ═══════════════════════════════════════════════════════════════
# ESC 4-5: las dos fuentes de reaparición quedan selladas
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_esc04_no_reaparece_por_fallback_del_panel(panel_env):
    """ESC 4 — A1: el fallback de GET /contacts ya no lo devuelve."""
    op, _, coll, _ = panel_env
    assert len(coll.find_by_owner(JUBENY)) == 1, "precondición: visible para el fallback"

    await op._close_conversation_internal(PHONE, CANAL)

    assert coll.find_by_owner(JUBENY) == [], \
        "el fallback MongoDB lo devuelve y reaparece en el panel"


@pytest.mark.asyncio
async def test_esc05_no_reaparece_por_rebuild_nocturno(panel_env):
    """ESC 5 — A2: el rebuild de las 3 AM ya no lo re-agrega al ZSET."""
    op, _, coll, _ = panel_env
    assert len(coll.find_recent()) == 1, "precondición: visible para el rebuild"

    await op._close_conversation_internal(PHONE, CANAL)

    assert coll.find_recent() == [], \
        "el rebuild lo re-agregaría al ZSET a las 3 AM"


# ═══════════════════════════════════════════════════════════════
# ESC 6-7: reapertura — el riesgo alto de Fase 3
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_esc06_reapertura_desarchiva(panel_env):
    """
    ESC 6 — EL MÁS IMPORTANTE: tras cerrar, una reactivación debe desarchivar.
    Si esto falla, la conversación queda invisible para siempre.
    """
    op, _, coll, _ = panel_env
    await op._close_conversation_internal(PHONE, CANAL)
    assert coll.docs[(PHONE, CANAL)]["archived"] is True

    # activate_human / request_handoff sincronizan archived=False
    mongo = FakeMongoManager(coll)
    await mongo.update_conversation_meta(phone=PHONE, canal=CANAL, archived=False)

    assert coll.docs[(PHONE, CANAL)]["archived"] is False
    assert len(coll.find_by_owner(JUBENY)) == 1, "no volvió a ser visible en el panel"


@pytest.mark.asyncio
async def test_esc07_activate_human_pasa_archived_false():
    """ESC 7 — activate_human envía archived=False al sincronizar MongoDB."""
    import inspect
    from middleware import conversation_state

    src = inspect.getsource(conversation_state.ConversationStateManager.activate_human)
    assert "archived=False" in src, \
        "activate_human no desarchiva — un contacto reactivado seguiría oculto"

    src_handoff = inspect.getsource(conversation_state.ConversationStateManager.request_handoff)
    assert "archived=False" in src_handoff, "request_handoff no desarchiva"


# ═══════════════════════════════════════════════════════════════
# ESC 8-9: Cerrado perdido
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_esc08_cerrado_perdido_cierra(panel_env):
    """ESC 8 — Mover a 'Cerrado perdido' cierra y saca del panel."""
    op, r, coll, _ = panel_env

    with patch.object(op.ws_manager, "notify_status_change", AsyncMock(return_value=1)):
        result = await op._auto_close_by_stage(PHONE, CANAL, STAGE_CERRADO_PERDIDO)

    assert result["closed"] is True
    assert result["close_error"] is None
    assert await r.zscore(ZSET, MEMBER) is None, "sigue en el ZSET"
    assert coll.docs[(PHONE, CANAL)]["archived"] is True, "no se archivó"


@pytest.mark.asyncio
async def test_esc09_cerrado_perdido_no_transfiere_owner(panel_env):
    """
    ESC 9 — 'Cerrado perdido' es cierre puro: NO debe cambiar de dueño.
    Si se hubiera metido en STAGES_TRANSFER_TO_LUISA, el contacto acabaría
    asignado a Luisa en vez de cerrado.
    """
    op, _, _, sm = panel_env

    with patch.object(op.ws_manager, "notify_status_change", AsyncMock(return_value=1)):
        await op._auto_close_by_stage(PHONE, CANAL, STAGE_CERRADO_PERDIDO)

    sm.transfer_ownership.assert_not_called()
    assert STAGE_CERRADO_PERDIDO not in op.STAGES_TRANSFER_TO_LUISA
    assert STAGE_CERRADO_PERDIDO in op.STAGES_AUTO_CLOSE


# ═══════════════════════════════════════════════════════════════
# ESC 10: bug del `if raw_meta:`
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_esc10_transfiere_aunque_falte_conv_meta(redis_client):
    """
    ESC 10 — Sin conv_meta, transfer_ownership debe ejecutarse igual.
    Antes se saltaba en silencio y HubSpot conservaba al owner anterior.
    """
    from middleware import outbound_panel as op

    coll = FakeConversationsCollection()
    coll.seed(PHONE, CANAL, JUBENY)
    # Sin conv_meta en Redis — el escenario del bug
    await redis_client.zadd(ZSET, {MEMBER: 1000.0})

    sm = AsyncMock()
    sm.activate_bot = AsyncMock(return_value=True)
    sm.get_meta = AsyncMock(return_value=None)
    sm.remove_from_advisor_inbox = AsyncMock(return_value=None)
    sm.ensure_meta_with_channel = AsyncMock(return_value=True)
    sm.transfer_ownership = AsyncMock(return_value={"status": "success"})
    sm.activate_human = AsyncMock(return_value=True)
    sm.add_to_advisor_inbox = AsyncMock(return_value=None)
    sm.redis = redis_client

    with patch.object(op, "_get_redis_client", AsyncMock(return_value=redis_client)), \
         patch.object(op, "_get_state_manager", lambda: sm), \
         patch.object(op.ws_manager, "notify_contact_transferred", AsyncMock(return_value=1)), \
         patch("database.mongodb_client.get_mongo_manager", lambda: FakeMongoManager(coll)):
        await op._transfer_to_luisa(PHONE, CANAL, "contact-123", "1407668893")

    sm.ensure_meta_with_channel.assert_awaited_once()
    sm.transfer_ownership.assert_awaited_once()
    kwargs = sm.transfer_ownership.await_args.kwargs
    assert kwargs["to_owner_id"] == LUISA


# ═══════════════════════════════════════════════════════════════
# ESC 11: simetría del guard in_panel
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_esc11_mensaje_asesor_no_revive_contacto_cerrado(redis_client):
    """
    ESC 11 — A3: un mensaje saliente sobre un contacto cerrado NO debe
    devolverlo al ZSET. Antes update_advisor_message_timestamp hacía zadd
    incondicional, a diferencia de su gemela del cliente.
    """
    from middleware.conversation_state import ConversationStateManager

    csm = ConversationStateManager.__new__(ConversationStateManager)
    csm.redis = redis_client

    await redis_client.set(
        f"{csm.META_PREFIX}{PHONE}:{CANAL}",
        json.dumps({"phone_normalized": PHONE, "in_panel": False}),
    )

    await csm.update_advisor_message_timestamp(PHONE, CANAL)

    assert await redis_client.zscore(csm.ACTIVE_CONTACTS_ZSET, MEMBER) is None, \
        "el contacto cerrado volvió al ZSET por un mensaje del asesor"


@pytest.mark.asyncio
async def test_esc12_mensaje_asesor_si_reordena_contacto_abierto(redis_client):
    """ESC 12 — Contra-prueba: con in_panel=True el reordenamiento sigue vivo."""
    from middleware.conversation_state import ConversationStateManager

    csm = ConversationStateManager.__new__(ConversationStateManager)
    csm.redis = redis_client

    await redis_client.zadd(csm.ACTIVE_CONTACTS_ZSET, {MEMBER: 1.0})
    await redis_client.set(
        f"{csm.META_PREFIX}{PHONE}:{CANAL}",
        json.dumps({"phone_normalized": PHONE, "in_panel": True}),
    )

    await csm.update_advisor_message_timestamp(PHONE, CANAL)

    score = await redis_client.zscore(csm.ACTIVE_CONTACTS_ZSET, MEMBER)
    assert score is not None and score > 1.0, "dejó de reordenar contactos abiertos"
