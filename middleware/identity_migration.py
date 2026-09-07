"""
Migración de una conversación BSUID a su teléfono real.

Meta activó usernames en WhatsApp: Twilio entrega esas conversaciones con un
routing address BSUID (`whatsapp:CC.XXXX`) y el sistema las indexa bajo una
clave interna `bsuid_<sha256[:24]>`. Cuando el cliente comparte su número —o la
asesora lo escribe en el panel— esa clave debe pasar a ser el teléfono en Redis,
Mongo y HubSpot.

Lo que NO cambia nunca: la dirección de entrega. El BSUID sobrevive en
`conv_meta.routing_address` y sigue siendo el destino de Twilio. Migramos el
índice, no el canal.

Garantías de diseño (el ORDEN de las operaciones es la garantía):

1. **Alias primero.** `bsuid_alias:{clave} → +57…` se escribe antes de tocar
   nada. Si el worker muere en cualquier paso posterior, el webhook ya enruta al
   destino correcto y una reejecución termina el trabajo.
2. **Copiar antes de borrar.** Ninguna clave origen se elimina hasta que su
   destino está escrito.
3. **Idempotente.** Reejecutar sobre una migración completa no rompe nada.
4. **Nunca lanza** (ADR-PLAT-001). Devuelve un `MigrationResult` y el flujo del
   mensaje continúa aunque la migración falle.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from logging_config import logger
from utils.safe_logging import safe_error, safe_id, safe_phone

from .phone_normalizer import PhoneNormalizer
from .whatsapp_identity import is_bsuid_identity_key

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTES
# ═══════════════════════════════════════════════════════════════════════════════

ALIAS_PREFIX = "bsuid_alias:"
LOCK_PREFIX = "migration_lock:"
LOCK_TTL_SECONDS = 60

STATE_PREFIX = "conv_state:"
META_PREFIX = "conv_meta:"
PANEL_MARKER_PREFIX = "conv_was_panel:"
ACTIVE_CONTACTS_ZSET = "active_conversations_sorted"
BOT_CONTROLLED_SET = "bot_controlled_conversations"
ADVISOR_INBOX_PREFIX = "advisor_inbox:"
LAST_CLIENT_MSG_PREFIX = "last_client_msg:"
PHONE_CACHE_PREFIX = "phone_cache:"
DEAL_ID_CACHE_PREFIX = "deal_id_cache:"
BULK_PENDING_PREFIX = "bulk_no_responde_pending:"
SOFIA_HISTORY_PREFIX = "message_store:"

# Prioridad de estados para decidir cuál sobrevive cuando origen y destino
# tienen conversación viva. Espejo del bloque P3-D v2 de conversation_state.py.
_STATUS_PRIO = {
    "IN_CONVERSATION": 4,
    "HUMAN_ACTIVE": 3,
    "PENDING_HANDOFF": 2,
    "BOT_ACTIVE": 1,
}

# Campos del meta origen que se copian al destino solo si allí faltan.
_META_MERGE_FIELDS = (
    "contact_id",
    "display_name",
    "assigned_owner_id",
    "assigned_owner_ids",
    "primary_owner_id",
    "handoff_reason",
    "deal_id",
    "deal_stage",
    "transfer_history",
    "canal_origen",
    "canal_display",
    "seguimiento_origin",
    "last_advisor_message",
    "username",
    "bot_turns",
    "had_signal",
    "first_turn_at",
    "phone_asked",
)


# ═══════════════════════════════════════════════════════════════════════════════
# RESULTADO
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class MigrationResult:
    """Qué pasó. Nunca una excepción: el llamador sigue su flujo con esto."""

    ok: bool
    identity_key: str
    phone: Optional[str] = None
    contact_id: Optional[str] = None
    #  "migrated"          → la clave pasó al teléfono
    #  "adopted"           → el teléfono ya tenía contacto en HubSpot; se adoptó
    #  "already_migrated"  → el alias ya apuntaba a este mismo teléfono
    #  "invalid_phone" | "not_bsuid" | "locked" | "error"
    outcome: str = "error"
    routing_address: Optional[str] = None
    canales: List[str] = field(default_factory=list)
    detail: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


def _as_text(value: Any) -> Optional[str]:
    """Redis puede devolver bytes o str según el pool. Normaliza a str."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _loads(raw: Any) -> Dict[str, Any]:
    text = _as_text(raw)
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _copy_with_ttl(redis_client, src_key: str, dst_key: str) -> bool:
    """
    Copia un valor simple conservando su TTL exacto.

    El TTL importa de verdad en `last_client_msg`: es la ventana de 24 h de
    WhatsApp. Copiar sin TTL la vuelve eterna; copiar con TTL nuevo la reinicia.
    Las dos cosas hacen que el panel mienta sobre si puede escribir libre o
    tiene que usar plantilla.
    """
    raw = await redis_client.get(src_key)
    if raw is None:
        return False

    ttl = await redis_client.ttl(src_key)
    if ttl and ttl > 0:
        await redis_client.set(dst_key, raw, ex=ttl)
    else:
        await redis_client.set(dst_key, raw)
    return True


async def _migrar_miembro_zset(
    redis_client, zset_key: str, viejo: str, nuevo: str
) -> bool:
    """Mueve un miembro conservando su score (el orden del panel es el score)."""
    score = await redis_client.zscore(zset_key, viejo)
    if score is None:
        return False
    await redis_client.zadd(zset_key, {nuevo: score})
    await redis_client.zrem(zset_key, viejo)
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# HUBSPOT
# ═══════════════════════════════════════════════════════════════════════════════


async def _resolver_contacto_hubspot(
    identity_key: str, phone: str, contact_id_actual: Optional[str]
) -> tuple[Optional[str], bool]:
    """
    Deja el teléfono escrito en HubSpot y devuelve (contact_id, adoptado).

    La búsqueda de contactos del proyecto es por `idProperty: whatsapp_id`, no
    por `phone`. Migrar significa, por tanto, escribir `whatsapp_id`.

    Si ya existe un contacto con ese `whatsapp_id`, el cliente escribió antes
    desde un número: se ADOPTA ese contacto (política acordada) en vez de
    fusionar. No se llama a la API de merge de HubSpot — es irreversible.
    """
    from integrations.hubspot import hubspot_client

    try:
        existente = await hubspot_client.search_contact_by_phone(phone)
    except Exception as e:
        logger.warning(f"[Migration] Búsqueda HubSpot falló: {safe_error(e)}")
        existente = None

    if existente:
        if contact_id_actual and str(existente) != str(contact_id_actual):
            logger.warning(
                "[Migration] contacto_duplicado: el teléfono ya pertenece a %s; "
                "se adopta y se abandona %s (requiere limpieza manual en HubSpot)",
                safe_id(existente, "contact"),
                safe_id(contact_id_actual, "contact"),
            )
        try:
            await hubspot_client.update_contact(existente, {"phone": phone})
        except Exception as e:
            logger.warning(f"[Migration] No se pudo escribir phone en el adoptado: {safe_error(e)}")
        return str(existente), True

    if not contact_id_actual:
        return None, False

    try:
        await hubspot_client.update_contact(
            contact_id_actual, {"whatsapp_id": phone, "phone": phone}
        )
    except Exception as e:
        # El contacto se queda con whatsapp_id=bsuid_… El alias ya está escrito,
        # así que una reejecución vuelve a intentarlo.
        logger.warning(
            "[Migration] No se pudo migrar whatsapp_id en HubSpot para %s: %s",
            safe_id(contact_id_actual, "contact"),
            safe_error(e),
        )
        return str(contact_id_actual), False

    return str(contact_id_actual), False


# ═══════════════════════════════════════════════════════════════════════════════
# MONGO
# ═══════════════════════════════════════════════════════════════════════════════


async def _migrar_mongo(identity_key: str, phone: str, routing_address: Optional[str]) -> None:
    """
    Reescribe la clave en `messages` y en `conversations`.

    `conversations` tiene índice ÚNICO (phone, canal): si el destino ya existe
    hay que FUSIONAR contadores y borrar el origen, no reescribir — un $set
    ciego lanzaría DuplicateKeyError y dejaría la conversación partida.
    """
    from database.mongodb_client import get_mongo_manager

    mongo = get_mongo_manager()
    if not await mongo.connect():
        logger.warning("[Migration] Mongo no disponible: la clave queda migrada solo en Redis")
        return

    # ── messages: reescritura directa, no hay índice único por teléfono ──
    try:
        res = await mongo.db.messages.update_many(
            {"phone": identity_key}, {"$set": {"phone": phone}}
        )
        logger.info(f"[Migration] messages reindexados: {res.modified_count}")
    except Exception as e:
        logger.warning(f"[Migration] Error reindexando messages: {safe_error(e)}")

    # ── conversations: un documento por canal, con fusión si hay colisión ──
    try:
        origenes = await mongo.db.conversations.find({"phone": identity_key}).to_list(length=50)
    except Exception as e:
        logger.warning(f"[Migration] No se pudieron leer conversations: {safe_error(e)}")
        return

    for origen in origenes:
        canal = origen.get("canal", "whatsapp")
        try:
            destino = await mongo.db.conversations.find_one({"phone": phone, "canal": canal})

            if destino is None:
                actualizacion = {"phone": phone, "has_phone": True}
                if routing_address:
                    actualizacion["routing_address"] = routing_address
                await mongo.db.conversations.update_one(
                    {"_id": origen["_id"]}, {"$set": actualizacion}
                )
                continue

            # Fusión: el destino se queda con el rango temporal completo y la
            # suma de mensajes; el origen desaparece.
            set_fields: Dict[str, Any] = {"has_phone": True}
            if routing_address:
                set_fields["routing_address"] = routing_address

            o_first, d_first = origen.get("first_message_at"), destino.get("first_message_at")
            if o_first and (not d_first or o_first < d_first):
                set_fields["first_message_at"] = o_first

            o_last, d_last = origen.get("last_message_at"), destino.get("last_message_at")
            if o_last and (not d_last or o_last > d_last):
                set_fields["last_message_at"] = o_last
                set_fields["last_message_preview"] = origen.get("last_message_preview", "")
                set_fields["last_message_sender"] = origen.get("last_message_sender", "")

            for campo in ("contact_id", "owner_id", "display_name", "canal_origen"):
                if origen.get(campo) and not destino.get(campo):
                    set_fields[campo] = origen[campo]

            # Un cierre en el destino no puede enterrar un hilo vivo del origen.
            if destino.get("archived") and not origen.get("archived"):
                set_fields["archived"] = False

            await mongo.db.conversations.update_one(
                {"_id": destino["_id"]},
                {
                    "$set": set_fields,
                    "$inc": {"message_count": int(origen.get("message_count", 0) or 0)},
                },
            )
            await mongo.db.conversations.delete_one({"_id": origen["_id"]})
            logger.info(f"[Migration] conversations fusionadas en canal {canal}")
        except Exception as e:
            logger.warning(
                f"[Migration] Error migrando conversations canal {canal}: {safe_error(e)}"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# REDIS
# ═══════════════════════════════════════════════════════════════════════════════


async def _migrar_canal(
    redis_client,
    identity_key: str,
    phone: str,
    canal: str,
    routing_address: Optional[str],
    contact_id: Optional[str],
    source: str,
) -> None:
    """Migra todas las claves de UN canal, copiando antes de borrar."""
    src_meta = f"{META_PREFIX}{identity_key}:{canal}"
    dst_meta = f"{META_PREFIX}{phone}:{canal}"
    src_state = f"{STATE_PREFIX}{identity_key}:{canal}"
    dst_state = f"{STATE_PREFIX}{phone}:{canal}"
    src_marker = f"{PANEL_MARKER_PREFIX}{identity_key}:{canal}"
    dst_marker = f"{PANEL_MARKER_PREFIX}{phone}:{canal}"
    src_member = f"{identity_key}:{canal}"
    dst_member = f"{phone}:{canal}"

    # ── 1. Meta: merge campo a campo, el destino manda si ya tiene valor ──
    meta_origen = _loads(await redis_client.get(src_meta))
    meta_destino = _loads(await redis_client.get(dst_meta))

    fusionado = dict(meta_destino) if meta_destino else dict(meta_origen)
    if meta_destino and meta_origen:
        for campo in _META_MERGE_FIELDS:
            if meta_origen.get(campo) and not fusionado.get(campo):
                fusionado[campo] = meta_origen[campo]
        o_created = meta_origen.get("created_at", "")
        if o_created and (not fusionado.get("created_at") or o_created < fusionado["created_at"]):
            fusionado["created_at"] = o_created

    # La identidad sigue siendo BSUID: es lo que hace que el panel resuelva el
    # routing. Lo que cambia es que ahora SÍ tiene teléfono.
    fusionado["phone_normalized"] = phone
    fusionado["identity_type"] = "bsuid"
    fusionado["has_phone"] = True
    fusionado["phone_source"] = source
    if routing_address:
        fusionado["routing_address"] = routing_address
    if contact_id:
        fusionado["contact_id"] = contact_id
    fusionado.pop("_temp_meta", None)

    if fusionado:
        await redis_client.set(dst_meta, json.dumps(fusionado))

    # ── 2. Estado: gana el de mayor prioridad, conservando su TTL ──
    estado_origen = _as_text(await redis_client.get(src_state))
    estado_destino = _as_text(await redis_client.get(dst_state))
    if estado_origen and _STATUS_PRIO.get(estado_origen, 0) > _STATUS_PRIO.get(estado_destino, 0):
        await _copy_with_ttl(redis_client, src_state, dst_state)

    # ── 3. Marcador de re-entrada al panel ──
    if await redis_client.exists(src_marker):
        await redis_client.set(dst_marker, "1")

    # ── 4. Índices ordenados: el score ES el orden del panel ──
    await _migrar_miembro_zset(redis_client, ACTIVE_CONTACTS_ZSET, src_member, dst_member)

    if await redis_client.sismember(BOT_CONTROLLED_SET, src_member):
        await redis_client.sadd(BOT_CONTROLLED_SET, dst_member)
        await redis_client.srem(BOT_CONTROLLED_SET, src_member)

    # ── 5. Inbox de no-leídos del asesor dueño ──
    owner = fusionado.get("assigned_owner_id") or meta_origen.get("assigned_owner_id")
    if owner:
        await _migrar_miembro_zset(
            redis_client, f"{ADVISOR_INBOX_PREFIX}{owner}", src_member, dst_member
        )

    # ── 6. Memoria de Sofía: sin esto pierde el hilo de la conversación ──
    src_hist = f"{SOFIA_HISTORY_PREFIX}{identity_key}:{canal}"
    dst_hist = f"{SOFIA_HISTORY_PREFIX}{phone}:{canal}"
    try:
        if await redis_client.exists(src_hist) and not await redis_client.exists(dst_hist):
            await redis_client.rename(src_hist, dst_hist)
    except Exception as e:
        logger.debug(f"[Migration] Historial de Sofía no migrado: {safe_error(e)}")

    # ── 7. Borrado del origen, solo ahora ──
    await redis_client.delete(src_meta, src_state, src_marker)


async def _migrar_claves_globales(
    redis_client, identity_key: str, phone: str, contact_id: Optional[str]
) -> None:
    """Claves de conversación que no llevan canal en el nombre."""
    # Ventana de 24 h: el TTL es el dato, no el valor.
    await _copy_with_ttl(
        redis_client,
        f"{LAST_CLIENT_MSG_PREFIX}{identity_key}",
        f"{LAST_CLIENT_MSG_PREFIX}{phone}",
    )
    await redis_client.delete(f"{LAST_CLIENT_MSG_PREFIX}{identity_key}")

    for prefijo in (DEAL_ID_CACHE_PREFIX, BULK_PENDING_PREFIX, PHONE_CACHE_PREFIX):
        src = f"{prefijo}{identity_key}"
        if await _copy_with_ttl(redis_client, src, f"{prefijo}{phone}"):
            await redis_client.delete(src)

    # El inverso contact_id → clave debe apuntar al teléfono, o el panel
    # resolvería la conversación vieja al editar el contacto.
    if contact_id:
        await redis_client.set(f"{PHONE_CACHE_PREFIX}{contact_id}", phone, ex=86400)
        await redis_client.set(f"{PHONE_CACHE_PREFIX}{phone}", contact_id, ex=86400)


# ═══════════════════════════════════════════════════════════════════════════════
# API PÚBLICA
# ═══════════════════════════════════════════════════════════════════════════════


async def resolver_alias(redis_client, identity_key: str) -> Optional[str]:
    """
    Teléfono al que ya migró esta clave BSUID, si migró.

    Es lo que hace que la migración persista: sin esto, el siguiente mensaje del
    cliente —que ya no trae su número— volvería a crear la conversación vieja y
    el historial haría ping-pong entre las dos claves.
    """
    try:
        return _as_text(await redis_client.get(f"{ALIAS_PREFIX}{identity_key}"))
    except Exception as e:
        logger.warning(f"[Migration] No se pudo leer el alias: {safe_error(e)}")
        return None


async def migrate_identity_to_phone(
    identity_key: str,
    phone_raw: str,
    canal: str = "whatsapp",
    *,
    source: str = "sofia",
) -> MigrationResult:
    """
    Reemplaza la clave `bsuid_…` por el teléfono en Redis, Mongo y HubSpot.

    Args:
        identity_key: clave interna actual (`bsuid_<hash>`).
        phone_raw:    teléfono tal como llegó; se valida aquí, no antes.
        canal:        canal del mensaje que disparó la migración. Se migran
                      TODOS los canales de la conversación, no solo este.
        source:       "sofia" (lo dio el cliente) o "panel" (lo puso la asesora).

    No lanza nunca. Devuelve siempre un MigrationResult.
    """
    from .conversation_state import ConversationStateManager  # noqa: F401  (tipo)
    from .websocket_manager import ws_manager

    if not is_bsuid_identity_key(identity_key):
        return MigrationResult(
            ok=False, identity_key=identity_key, outcome="not_bsuid",
            detail="La clave de origen no es una identidad BSUID",
        )

    validacion = PhoneNormalizer().normalize(phone_raw or "")
    if not validacion.is_valid:
        logger.warning(
            "[Migration] Teléfono rechazado para %s: %s",
            safe_id(identity_key, "identity"),
            safe_error(validacion.error_message),
        )
        return MigrationResult(
            ok=False, identity_key=identity_key, outcome="invalid_phone",
            detail=validacion.error_message,
        )
    phone = validacion.normalized

    try:
        from .outbound_panel import _get_redis_client, _get_state_manager

        redis_client = await _get_redis_client()
        state_manager = _get_state_manager()
    except Exception as e:
        logger.error(f"[Migration] Sin Redis, migración abortada: {safe_error(e)}")
        return MigrationResult(
            ok=False, identity_key=identity_key, outcome="error", detail=str(e)
        )

    lock_key = f"{LOCK_PREFIX}{identity_key}"
    tengo_lock = False
    try:
        tengo_lock = bool(
            await redis_client.set(lock_key, source, nx=True, ex=LOCK_TTL_SECONDS)
        )
        if not tengo_lock:
            logger.info(
                "[Migration] Otra migración en curso para %s — se omite",
                safe_id(identity_key, "identity"),
            )
            return MigrationResult(
                ok=False, identity_key=identity_key, phone=phone, outcome="locked"
            )

        alias_previo = await resolver_alias(redis_client, identity_key)
        if alias_previo == phone:
            logger.info(
                "[Migration] %s ya estaba migrada — nada que hacer",
                safe_id(identity_key, "identity"),
            )
            return MigrationResult(
                ok=True, identity_key=identity_key, phone=phone,
                outcome="already_migrated",
            )

        # ── Inventario: canales, routing y contacto actuales ──
        canales: List[str] = []
        routing_address: Optional[str] = None
        contact_id: Optional[str] = None
        try:
            for raw_key in await redis_client.keys(f"{META_PREFIX}{identity_key}:*"):
                key_txt = _as_text(raw_key) or ""
                sufijo = key_txt.split(f"{META_PREFIX}{identity_key}:", 1)[-1]
                if sufijo:
                    canales.append(sufijo)
                meta = _loads(await redis_client.get(key_txt))
                routing_address = routing_address or meta.get("routing_address")
                contact_id = contact_id or meta.get("contact_id")
        except Exception as e:
            logger.warning(f"[Migration] Inventario de canales incompleto: {safe_error(e)}")

        if not canales:
            canales = [(canal or "whatsapp").lower()]
        if not contact_id:
            contact_id = _as_text(await redis_client.get(f"{PHONE_CACHE_PREFIX}{identity_key}"))

        # ── PASO 1: el alias, ANTES de tocar nada más ──
        await redis_client.set(f"{ALIAS_PREFIX}{identity_key}", phone)
        logger.info(
            "[Migration] alias escrito %s → %s (source=%s, canales=%s)",
            safe_id(identity_key, "identity"), safe_phone(phone), source, canales,
        )

        # ── PASO 2: HubSpot ──
        contact_id, adoptado = await _resolver_contacto_hubspot(
            identity_key, phone, contact_id
        )

        # ── PASO 3: Redis, canal por canal ──
        for canal_item in canales:
            try:
                await _migrar_canal(
                    redis_client, identity_key, phone, canal_item,
                    routing_address, contact_id, source,
                )
            except Exception as e:
                logger.warning(
                    f"[Migration] Canal {canal_item} incompleto: {safe_error(e)}"
                )

        await _migrar_claves_globales(redis_client, identity_key, phone, contact_id)

        # ── PASO 4: Mongo ──
        await _migrar_mongo(identity_key, phone, routing_address)

        # ── PASO 5: avisar a los paneles abiertos ──
        payload = {
            "type": "contact_updated",
            "action": "phone_migrated",
            "phone": phone,
            "old_phone": identity_key,
            "canal": canales[0] if canales else "whatsapp",
            "contact_id": contact_id,
        }
        try:
            meta_final = await state_manager.get_meta(phone, canales[0] if canales else "whatsapp")
            owner = getattr(meta_final, "assigned_owner_id", None) if meta_final else None
            if owner:
                await ws_manager.publish_to_advisor(redis_client, owner, payload)
            else:
                await ws_manager.publish_broadcast(redis_client, payload)
        except Exception as e:
            logger.warning(f"[Migration] WS phone_migrated no enviado: {safe_error(e)}")

        logger.info(
            "[Migration] ok %s → %s (contact=%s, adoptado=%s, source=%s)",
            safe_id(identity_key, "identity"), safe_phone(phone),
            safe_id(contact_id, "contact"), adoptado, source,
        )
        return MigrationResult(
            ok=True,
            identity_key=identity_key,
            phone=phone,
            contact_id=contact_id,
            outcome="adopted" if adoptado else "migrated",
            routing_address=routing_address,
            canales=canales,
        )

    except Exception as e:
        logger.error(
            "[Migration] failed %s: %s",
            safe_id(identity_key, "identity"), safe_error(e), exc_info=True,
        )
        return MigrationResult(
            ok=False, identity_key=identity_key, phone=phone,
            outcome="error", detail=str(e),
        )
    finally:
        if tengo_lock:
            try:
                await redis_client.delete(lock_key)
            except Exception:
                pass
