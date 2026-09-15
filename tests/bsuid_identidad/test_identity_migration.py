"""
Migración de una conversación BSUID a su teléfono real.

Lo que se fija aquí es el contrato que hace la migración segura en producción:

- El **alias se escribe antes** de borrar nada. Si el worker muere a mitad, el
  webhook ya enruta bien y una reejecución termina el trabajo.
- El **TTL de `last_client_msg` se conserva**. Es la ventana de 24 h de WhatsApp:
  perderlo hace que el panel fuerce plantillas sin motivo, o que crea abierta una
  ventana cerrada.
- El **score del ZSET y del inbox se conserva**. El score es el orden del panel.
- El **routing sigue siendo el BSUID**. Migramos el índice, no el canal.
- Un teléfono que ya tiene contacto en HubSpot se **adopta**, no se duplica.
"""
import json

import fakeredis.aioredis
import pytest

from middleware import identity_migration as mig
from middleware.identity_migration import migrate_identity_to_phone, resolver_alias

CLAVE = "bsuid_aaaabbbbccccddddeeeeffff"
BSUID = "whatsapp:CO.ABC123XYZ"
TELEFONO = "+573001234567"
CANAL = "whatsapp"
OWNER = "89096378"


# ═══════════════════════════════════════════════════════════════════════════════
# DOBLES
# ═══════════════════════════════════════════════════════════════════════════════


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return list(self._docs[:length] if length else self._docs)


class _Coleccion:
    """Lo mínimo de una colección Motor que usa el migrador."""

    def __init__(self, docs=None):
        self.docs = docs or []
        self._siguiente_id = 1000

    def _match(self, doc, filtro):
        return all(doc.get(k) == v for k, v in filtro.items())

    def find(self, filtro):
        return _Cursor([d for d in self.docs if self._match(d, filtro)])

    async def find_one(self, filtro, *args, **kwargs):
        for d in self.docs:
            if self._match(d, filtro):
                return d
        return None

    async def update_many(self, filtro, update):
        tocados = [d for d in self.docs if self._match(d, filtro)]
        for d in tocados:
            d.update(update.get("$set", {}))

        class _Res:
            modified_count = len(tocados)

        return _Res()

    async def update_one(self, filtro, update, upsert=False):
        doc = await self.find_one(filtro)
        if doc is None:
            return None
        doc.update(update.get("$set", {}))
        for campo, delta in (update.get("$inc") or {}).items():
            doc[campo] = int(doc.get(campo, 0) or 0) + int(delta)
        return None

    async def delete_one(self, filtro):
        for i, d in enumerate(self.docs):
            if self._match(d, filtro):
                del self.docs[i]
                return None
        return None


class _Db:
    def __init__(self, messages=None, conversations=None):
        self.messages = _Coleccion(messages)
        self.conversations = _Coleccion(conversations)


class _Mongo:
    def __init__(self, db):
        self.db = db

    async def connect(self):
        return True


class _HubSpotFalso:
    def __init__(self, existente=None):
        self.existente = existente
        self.updates = []

    async def search_contact_by_phone(self, phone):
        return self.existente

    async def update_contact(self, contact_id, props):
        self.updates.append((contact_id, props))


class _StateManagerFalso:
    def __init__(self, meta=None):
        self._meta = meta

    async def get_meta(self, phone, canal="whatsapp"):
        return self._meta


class _WsFalso:
    def __init__(self):
        self.enviados = []

    async def publish_to_advisor(self, redis_client, advisor_id, message):
        self.enviados.append(("advisor", advisor_id, message))

    async def publish_broadcast(self, redis_client, message):
        self.enviados.append(("broadcast", None, message))


# ═══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def redis_falso():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def entorno(monkeypatch, redis_falso):
    """Cablea el migrador contra dobles y devuelve las piezas observables."""
    import integrations.hubspot as hs_pkg
    import database.mongodb_client as mongo_mod
    import middleware.outbound_panel as panel
    import middleware.websocket_manager as ws_mod

    hubspot = _HubSpotFalso()
    mongo = _Mongo(_Db())
    ws = _WsFalso()

    async def _get_redis():
        return redis_falso

    monkeypatch.setattr(panel, "_get_redis_client", _get_redis)
    monkeypatch.setattr(panel, "_get_state_manager", lambda: _StateManagerFalso())
    monkeypatch.setattr(hs_pkg, "hubspot_client", hubspot)
    monkeypatch.setattr(mongo_mod, "get_mongo_manager", lambda: mongo)
    monkeypatch.setattr(ws_mod, "ws_manager", ws)

    return {"redis": redis_falso, "hubspot": hubspot, "mongo": mongo, "ws": ws}


async def _sembrar_conversacion_bsuid(r, *, con_estado="HUMAN_ACTIVE"):
    """Deja en Redis una conversación BSUID como la que crea el webhook."""
    meta = {
        "phone_normalized": CLAVE,
        "contact_id": "123456",
        "identity_type": "bsuid",
        "has_phone": False,
        "routing_address": BSUID,
        "display_name": "Cliente WhatsApp sin teléfono",
        "assigned_owner_id": OWNER,
        "canal_origen": "whatsapp",
        "created_at": "2026-09-01T10:00:00-05:00",
        "phone_asked": True,
    }
    await r.set(f"conv_meta:{CLAVE}:{CANAL}", json.dumps(meta))
    await r.set(f"conv_state:{CLAVE}:{CANAL}", con_estado, ex=7 * 86400)
    await r.set(f"conv_was_panel:{CLAVE}:{CANAL}", "1")
    await r.zadd("active_conversations_sorted", {f"{CLAVE}:{CANAL}": 1757000000.0})
    await r.zadd(f"advisor_inbox:{OWNER}", {f"{CLAVE}:{CANAL}": 1757000001.0})
    await r.set(f"last_client_msg:{CLAVE}", "2026-09-07T10:00:00-05:00", ex=3600)
    await r.set(f"phone_cache:{CLAVE}", "123456", ex=86400)
    await r.rpush(f"message_store:{CLAVE}:{CANAL}", "mensaje-viejo")


# ═══════════════════════════════════════════════════════════════════════════════
# VALIDACIÓN DE ENTRADA
# ═══════════════════════════════════════════════════════════════════════════════


async def test_rechaza_clave_que_no_es_bsuid(entorno):
    r = await migrate_identity_to_phone("+573009998877", TELEFONO, CANAL)
    assert r.ok is False
    assert r.outcome == "not_bsuid"


@pytest.mark.parametrize("basura", ["", "300 millones", "abc", "12"])
async def test_rechaza_telefonos_invalidos(entorno, basura):
    """Una alucinación del LLM no puede convertirse en clave de conversación."""
    r = await migrate_identity_to_phone(CLAVE, basura, CANAL)
    assert r.ok is False
    assert r.outcome == "invalid_phone"
    # Y no deja rastro: sin alias, la conversación sigue donde estaba.
    assert await entorno["redis"].get(f"bsuid_alias:{CLAVE}") is None


# ═══════════════════════════════════════════════════════════════════════════════
# MIGRACIÓN COMPLETA
# ═══════════════════════════════════════════════════════════════════════════════


async def test_migra_todas_las_claves_de_redis(entorno):
    r = entorno["redis"]
    await _sembrar_conversacion_bsuid(r)

    res = await migrate_identity_to_phone(CLAVE, TELEFONO, CANAL, source="sofia")
    assert res.ok and res.outcome == "migrated"

    # Destino escrito
    assert await r.get(f"conv_meta:{TELEFONO}:{CANAL}") is not None
    assert await r.get(f"conv_state:{TELEFONO}:{CANAL}") == "HUMAN_ACTIVE"
    assert await r.get(f"conv_was_panel:{TELEFONO}:{CANAL}") == "1"
    assert await r.zscore("active_conversations_sorted", f"{TELEFONO}:{CANAL}") is not None
    assert await r.zscore(f"advisor_inbox:{OWNER}", f"{TELEFONO}:{CANAL}") is not None
    assert await r.get(f"last_client_msg:{TELEFONO}") is not None
    assert await r.lrange(f"message_store:{TELEFONO}:{CANAL}", 0, -1) == ["mensaje-viejo"]

    # Origen retirado
    assert await r.get(f"conv_meta:{CLAVE}:{CANAL}") is None
    assert await r.get(f"conv_state:{CLAVE}:{CANAL}") is None
    assert await r.zscore("active_conversations_sorted", f"{CLAVE}:{CANAL}") is None
    assert await r.zscore(f"advisor_inbox:{OWNER}", f"{CLAVE}:{CANAL}") is None
    assert await r.get(f"last_client_msg:{CLAVE}") is None


async def test_el_ttl_de_la_ventana_de_24h_sobrevive(entorno):
    """
    Sin este TTL el panel miente sobre si puede escribir libre o necesita
    plantilla. Copiar sin TTL la vuelve eterna; copiar con TTL nuevo la reinicia.
    """
    r = entorno["redis"]
    await _sembrar_conversacion_bsuid(r)

    await migrate_identity_to_phone(CLAVE, TELEFONO, CANAL)

    ttl = await r.ttl(f"last_client_msg:{TELEFONO}")
    assert 0 < ttl <= 3600, f"TTL perdido o reiniciado: {ttl}"


async def test_conserva_el_orden_del_panel(entorno):
    """El score del ZSET es el orden de la lista. Si se pierde, el contacto salta."""
    r = entorno["redis"]
    await _sembrar_conversacion_bsuid(r)

    await migrate_identity_to_phone(CLAVE, TELEFONO, CANAL)

    assert await r.zscore("active_conversations_sorted", f"{TELEFONO}:{CANAL}") == 1757000000.0
    assert await r.zscore(f"advisor_inbox:{OWNER}", f"{TELEFONO}:{CANAL}") == 1757000001.0


async def test_el_routing_sigue_siendo_el_bsuid(entorno):
    """
    Migramos el índice, no el canal. Si `routing_address` se perdiera, el panel
    intentaría enviar a whatsapp:+57… y el mensaje no llegaría al cliente.
    """
    r = entorno["redis"]
    await _sembrar_conversacion_bsuid(r)

    await migrate_identity_to_phone(CLAVE, TELEFONO, CANAL, source="sofia")

    meta = json.loads(await r.get(f"conv_meta:{TELEFONO}:{CANAL}"))
    assert meta["routing_address"] == BSUID
    assert meta["identity_type"] == "bsuid"   # el panel lo necesita para resolver el envío
    assert meta["has_phone"] is True          # pero ya NO es un contacto sin teléfono
    assert meta["phone_normalized"] == TELEFONO
    assert meta["phone_source"] == "sofia"
    assert meta["display_name"] == "Cliente WhatsApp sin teléfono"
    assert meta["assigned_owner_id"] == OWNER


async def test_el_alias_se_escribe_antes_de_borrar_el_origen(entorno, monkeypatch):
    """
    Si el proceso muere a mitad, lo único que evita perder la conversación es que
    el alias ya esté escrito: el webhook enruta al destino y se puede reintentar.
    """
    r = entorno["redis"]
    await _sembrar_conversacion_bsuid(r)

    vistos = {}

    original = mig._migrar_canal

    async def _espia(redis_client, identity_key, phone, canal, *a, **kw):
        vistos["alias_al_empezar_canal"] = await redis_client.get(f"bsuid_alias:{identity_key}")
        return await original(redis_client, identity_key, phone, canal, *a, **kw)

    monkeypatch.setattr(mig, "_migrar_canal", _espia)

    await migrate_identity_to_phone(CLAVE, TELEFONO, CANAL)

    assert vistos["alias_al_empezar_canal"] == TELEFONO


async def test_reejecutar_es_idempotente(entorno):
    r = entorno["redis"]
    await _sembrar_conversacion_bsuid(r)

    primera = await migrate_identity_to_phone(CLAVE, TELEFONO, CANAL)
    segunda = await migrate_identity_to_phone(CLAVE, TELEFONO, CANAL)

    assert primera.outcome == "migrated"
    assert segunda.ok is True
    assert segunda.outcome == "already_migrated"
    assert await r.get(f"conv_meta:{TELEFONO}:{CANAL}") is not None


async def test_resolver_alias_devuelve_el_telefono_migrado(entorno):
    r = entorno["redis"]
    await _sembrar_conversacion_bsuid(r)

    assert await resolver_alias(r, CLAVE) is None
    await migrate_identity_to_phone(CLAVE, TELEFONO, CANAL)
    assert await resolver_alias(r, CLAVE) == TELEFONO


async def test_una_migracion_en_curso_bloquea_a_la_otra(entorno):
    r = entorno["redis"]
    await _sembrar_conversacion_bsuid(r)
    await r.set(f"migration_lock:{CLAVE}", "sofia", ex=60)

    res = await migrate_identity_to_phone(CLAVE, TELEFONO, CANAL, source="panel")

    assert res.ok is False
    assert res.outcome == "locked"
    assert await r.get(f"conv_meta:{CLAVE}:{CANAL}") is not None


# ═══════════════════════════════════════════════════════════════════════════════
# HUBSPOT
# ═══════════════════════════════════════════════════════════════════════════════


async def test_escribe_whatsapp_id_y_phone_en_el_contacto(entorno):
    await _sembrar_conversacion_bsuid(entorno["redis"])

    await migrate_identity_to_phone(CLAVE, TELEFONO, CANAL)

    updates = dict(entorno["hubspot"].updates)
    assert updates["123456"] == {"whatsapp_id": TELEFONO, "phone": TELEFONO}


async def test_adopta_el_contacto_existente_en_vez_de_duplicar(entorno):
    """
    El cliente ya había escrito desde un número. Política acordada: adoptar el
    contacto que ya estaba, nunca llamar a la API de merge de HubSpot.
    """
    entorno["hubspot"].existente = "999999"
    await _sembrar_conversacion_bsuid(entorno["redis"])

    res = await migrate_identity_to_phone(CLAVE, TELEFONO, CANAL)

    assert res.ok is True
    assert res.outcome == "adopted"
    assert res.contact_id == "999999"
    # Al adoptado solo se le escribe el teléfono; su whatsapp_id ya era correcto.
    assert entorno["hubspot"].updates == [("999999", {"phone": TELEFONO})]


async def test_un_fallo_de_hubspot_no_aborta_la_migracion(entorno):
    """
    ADR-PLAT-001: no lanzar. El alias ya está escrito, así que HubSpot puede
    reintentarse; lo que no se puede es dejar la conversación a medias.
    """
    async def _revienta(*a, **kw):
        raise RuntimeError("HubSpot 429")

    entorno["hubspot"].update_contact = _revienta
    await _sembrar_conversacion_bsuid(entorno["redis"])

    res = await migrate_identity_to_phone(CLAVE, TELEFONO, CANAL)

    assert res.ok is True
    assert await entorno["redis"].get(f"conv_meta:{TELEFONO}:{CANAL}") is not None


# ═══════════════════════════════════════════════════════════════════════════════
# MONGO
# ═══════════════════════════════════════════════════════════════════════════════


async def test_reindexa_los_mensajes_historicos(entorno):
    entorno["mongo"].db.messages.docs = [
        {"_id": 1, "phone": CLAVE, "content": "hola"},
        {"_id": 2, "phone": CLAVE, "content": "mi numero es 3001234567"},
        {"_id": 3, "phone": "+573009998877", "content": "de otro cliente"},
    ]
    await _sembrar_conversacion_bsuid(entorno["redis"])

    await migrate_identity_to_phone(CLAVE, TELEFONO, CANAL)

    docs = entorno["mongo"].db.messages.docs
    assert [d["phone"] for d in docs] == [TELEFONO, TELEFONO, "+573009998877"]


async def test_conversations_sin_colision_solo_cambia_de_clave(entorno):
    entorno["mongo"].db.conversations.docs = [
        {"_id": 1, "phone": CLAVE, "canal": CANAL, "message_count": 4},
    ]
    await _sembrar_conversacion_bsuid(entorno["redis"])

    await migrate_identity_to_phone(CLAVE, TELEFONO, CANAL)

    doc = entorno["mongo"].db.conversations.docs[0]
    assert doc["phone"] == TELEFONO
    assert doc["has_phone"] is True
    assert doc["routing_address"] == BSUID
    assert doc["message_count"] == 4


async def test_conversations_con_colision_fusiona_y_borra_el_origen(entorno):
    """
    `conversations` tiene índice único (phone, canal): un $set ciego lanzaría
    DuplicateKeyError y dejaría el hilo partido. Hay que fusionar contadores.
    """
    entorno["mongo"].db.conversations.docs = [
        {
            "_id": 1, "phone": CLAVE, "canal": CANAL, "message_count": 4,
            "first_message_at": "2026-09-01", "last_message_at": "2026-09-07",
            "last_message_preview": "mi numero es 3001234567",
            "last_message_sender": "client",
        },
        {
            "_id": 2, "phone": TELEFONO, "canal": CANAL, "message_count": 10,
            "first_message_at": "2026-08-01", "last_message_at": "2026-08-20",
            "archived": True,
        },
    ]
    await _sembrar_conversacion_bsuid(entorno["redis"])

    await migrate_identity_to_phone(CLAVE, TELEFONO, CANAL)

    docs = entorno["mongo"].db.conversations.docs
    assert len(docs) == 1, "el documento origen debe desaparecer"
    fusionado = docs[0]
    assert fusionado["_id"] == 2
    assert fusionado["message_count"] == 14
    assert fusionado["first_message_at"] == "2026-08-01"   # el más antiguo gana
    assert fusionado["last_message_at"] == "2026-09-07"    # el más reciente gana
    assert fusionado["last_message_preview"] == "mi numero es 3001234567"
    # Un cierre viejo no puede enterrar un hilo que sigue vivo.
    assert fusionado["archived"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET
# ═══════════════════════════════════════════════════════════════════════════════


async def test_avisa_al_panel_con_las_dos_claves(entorno):
    """
    El panel abierto tiene `currentPhone` apuntando a la clave vieja. Sin
    `old_phone` no sabría a qué contacto aplicar el cambio.
    """
    await _sembrar_conversacion_bsuid(entorno["redis"])

    await migrate_identity_to_phone(CLAVE, TELEFONO, CANAL)

    assert entorno["ws"].enviados, "el panel no fue notificado"
    _, _, payload = entorno["ws"].enviados[-1]
    assert payload["type"] == "contact_updated"
    assert payload["action"] == "phone_migrated"
    assert payload["old_phone"] == CLAVE
    assert payload["phone"] == TELEFONO
