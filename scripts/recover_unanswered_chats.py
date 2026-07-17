"""
Script de recuperación de chats sin contestar.

Busca contactos que tienen mensaje pendiente del cliente y los re-inserta
en advisor_inbox para que aparezcan con badge en el panel.

DOS categorías:

1. POST-MASIVO (No Responde):
   - Cliente respondió después del último masivo (15 julio 2026)
   - Contacto en embudo "No responde" (lifecyclestage=other)
   - Último mensaje en MongoDB es del cliente

2. EN CONVERSACIÓN:
   - Último mensaje es del cliente (post-filtro SofIA)
   - Contacto en embudo "En conversación" (lifecyclestage=1326623075)
   - Estado Redis: HUMAN_ACTIVE o IN_CONVERSATION

Uso:
    python scripts/recover_unanswered_chats.py [--dry-run] [--execute]

    Por defecto: dry-run (solo lista, no modifica).
    --execute: aplica los cambios (agrega a advisor_inbox + ZSET + WS notify).
"""
import asyncio
import os
import sys
import json
import argparse
from datetime import datetime, timedelta, timezone

# Agregar raíz del proyecto al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

BOG_TZ = timezone(timedelta(hours=-5))
BULK_CUTOFF = datetime(2026, 7, 15, 0, 0, 0, tzinfo=BOG_TZ)

STAGE_NO_RESPONDE = "other"
STAGE_EN_CONVERSACION = "1326623075"

ACTIVE_ADVISORS = {"89096378", "89096380", "89096379"}


async def main(dry_run: bool = True):
    from database.mongodb_client import get_mongo_manager
    from middleware.conversation_state import ConversationStateManager, ConversationStatus
    import redis.asyncio as aioredis

    is_railway = os.environ.get("RAILWAY_ENVIRONMENT") is not None
    if is_railway:
        redis_url = os.environ.get("REDIS_URL", "")
    else:
        redis_url = (
            os.environ.get("REDIS_PUBLIC_URL")
            or os.environ.get("REDIS_URL")
            or ""
        )
    if not redis_url:
        print("ERROR: REDIS_URL / REDIS_PUBLIC_URL no configurada")
        return

    hubspot_token = os.environ.get("HUBSPOT_API_KEY") or os.environ.get("HUBSPOT_ACCESS_TOKEN") or ""
    if not hubspot_token:
        print("ERROR: HUBSPOT_API_KEY no configurada")
        return

    mongo = get_mongo_manager()
    if not await mongo.connect():
        print("ERROR: No se pudo conectar a MongoDB")
        return

    r = aioredis.from_url(redis_url, decode_responses=True, max_connections=5)
    state_mgr = ConversationStateManager()  # usa lógica interna de URL pública/privada

    import httpx
    hs_client = httpx.AsyncClient(
        base_url="https://api.hubapi.com",
        headers={"Authorization": f"Bearer {hubspot_token}"},
        timeout=15,
    )

    try:
        await r.ping()
        print("Redis: OK")
    except Exception as e:
        print(f"ERROR: Redis no disponible: {e}")
        return

    recovered_cat1 = []
    recovered_cat2 = []
    skipped = []

    # ═══════════════════════════════════════════════════════════════════
    # CATEGORÍA 1: Post-masivo "No Responde"
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("CATEGORÍA 1: Post-masivo 'No Responde'")
    print("=" * 70)

    bulk_messages = await mongo.db.messages.find(
        {
            "metadata.is_bulk_send": True,
            "timestamp": {"$gte": BULK_CUTOFF},
        },
        {"phone": 1, "timestamp": 1, "metadata": 1},
    ).to_list(length=5000)

    bulk_phones = {}
    for msg in bulk_messages:
        phone = msg.get("phone")
        if not phone:
            continue
        ts = msg.get("timestamp")
        if phone not in bulk_phones or (ts and ts > bulk_phones[phone]):
            bulk_phones[phone] = ts

    print(f"  Phones con masivo desde {BULK_CUTOFF.date()}: {len(bulk_phones)}")

    for phone, bulk_ts in bulk_phones.items():
        # Verificar en conversations que último mensaje es del cliente
        conv = await mongo.db.conversations.find_one(
            {"phone": phone},
            {"last_message_sender": 1, "last_message_at": 1, "contact_id": 1,
             "canal": 1, "owner_id": 1, "last_message_preview": 1},
        )
        if not conv:
            skipped.append({"phone": phone, "reason": "sin_conversacion_mongo"})
            continue

        if conv.get("last_message_sender") != "client":
            skipped.append({"phone": phone, "reason": f"ultimo_msg_no_es_client ({conv.get('last_message_sender')})"})
            continue

        last_msg_at = conv.get("last_message_at")
        if last_msg_at and bulk_ts and last_msg_at <= bulk_ts:
            skipped.append({"phone": phone, "reason": "ultimo_msg_antes_del_masivo"})
            continue

        contact_id = conv.get("contact_id")
        if not contact_id:
            skipped.append({"phone": phone, "reason": "sin_contact_id"})
            continue

        # Verificar lifecyclestage en HubSpot
        stage = await _get_hubspot_stage(hs_client, contact_id)
        if stage != STAGE_NO_RESPONDE:
            skipped.append({"phone": phone, "reason": f"stage={stage} (no es 'other')"})
            continue

        # Determinar owner
        owner_id = conv.get("owner_id")
        if not owner_id:
            meta_key = f"conv_meta:{phone}:{conv.get('canal', 'whatsapp')}"
            raw_meta = await r.get(meta_key)
            if raw_meta:
                try:
                    meta = json.loads(raw_meta)
                    owner_id = meta.get("assigned_owner_id")
                except Exception:
                    pass

        if not owner_id:
            try:
                resp = await hs_client.get(
                    f"/crm/v3/objects/contacts/{contact_id}",
                    params={"properties": "hubspot_owner_id"},
                )
                if resp.status_code == 200:
                    owner_id = resp.json().get("properties", {}).get("hubspot_owner_id")
            except Exception:
                pass

        if not owner_id or owner_id not in ACTIVE_ADVISORS:
            skipped.append({"phone": phone, "reason": f"owner={owner_id} (no activo)"})
            continue

        canal = conv.get("canal", "whatsapp")
        recovered_cat1.append({
            "phone": phone,
            "canal": canal,
            "owner_id": owner_id,
            "contact_id": contact_id,
            "last_msg_at": str(last_msg_at),
            "last_msg_preview": (conv.get("last_message_preview") or "")[:80],
        })

    print(f"  Candidatos Cat1: {len(recovered_cat1)}")

    # ═══════════════════════════════════════════════════════════════════
    # CATEGORÍA 2: "En conversación" con mensaje del cliente pendiente
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("CATEGORÍA 2: 'En conversación' con mensaje del cliente pendiente")
    print("=" * 70)

    en_conv_docs = await mongo.db.conversations.find(
        {
            "last_message_sender": "client",
            "archived": {"$ne": True},
        },
        {"phone": 1, "canal": 1, "contact_id": 1, "owner_id": 1,
         "last_message_at": 1, "last_message_preview": 1},
    ).sort("last_message_at", -1).to_list(length=3000)

    print(f"  Conversaciones con último msg de cliente: {len(en_conv_docs)}")

    for conv in en_conv_docs:
        phone = conv.get("phone")
        if not phone:
            continue

        # Ya procesado en Cat1 — skip
        if phone in bulk_phones:
            continue

        contact_id = conv.get("contact_id")
        if not contact_id:
            skipped.append({"phone": phone, "reason": "cat2_sin_contact_id"})
            continue

        canal = conv.get("canal", "whatsapp")

        # Verificar estado Redis — solo HUMAN_ACTIVE o IN_CONVERSATION
        state_key = f"conv_state:{phone}:{canal}"
        raw_state = await r.get(state_key)
        if raw_state not in (
            ConversationStatus.HUMAN_ACTIVE.value,
            ConversationStatus.IN_CONVERSATION.value,
            ConversationStatus.PENDING_HANDOFF.value,
        ):
            skipped.append({"phone": phone, "reason": f"cat2_state={raw_state}"})
            continue

        # Verificar lifecyclestage en HubSpot
        stage = await _get_hubspot_stage(hs_client, contact_id)
        if stage != STAGE_EN_CONVERSACION:
            skipped.append({"phone": phone, "reason": f"cat2_stage={stage}"})
            continue

        # Determinar owner
        owner_id = conv.get("owner_id")
        if not owner_id:
            meta_key = f"conv_meta:{phone}:{canal}"
            raw_meta = await r.get(meta_key)
            if raw_meta:
                try:
                    meta = json.loads(raw_meta)
                    owner_id = meta.get("assigned_owner_id")
                except Exception:
                    pass

        if not owner_id or owner_id not in ACTIVE_ADVISORS:
            skipped.append({"phone": phone, "reason": f"cat2_owner={owner_id}"})
            continue

        recovered_cat2.append({
            "phone": phone,
            "canal": canal,
            "owner_id": owner_id,
            "contact_id": contact_id,
            "last_msg_at": str(conv.get("last_message_at")),
            "last_msg_preview": (conv.get("last_message_preview") or "")[:80],
            "state": raw_state,
        })

    print(f"  Candidatos Cat2: {len(recovered_cat2)}")

    # ═══════════════════════════════════════════════════════════════════
    # RESUMEN
    # ═══════════════════════════════════════════════════════════════════
    all_recovered = recovered_cat1 + recovered_cat2
    print("\n" + "=" * 70)
    print(f"RESUMEN: {len(all_recovered)} chats a recuperar "
          f"(Cat1={len(recovered_cat1)}, Cat2={len(recovered_cat2)})")
    print(f"Skipped: {len(skipped)}")
    print("=" * 70)

    by_owner = {}
    for r_item in all_recovered:
        oid = r_item["owner_id"]
        by_owner.setdefault(oid, []).append(r_item)

    for oid, items in by_owner.items():
        print(f"\n  Advisor {oid}: {len(items)} chats")
        for it in items[:10]:
            cat = "CAT1" if it in recovered_cat1 else "CAT2"
            print(f"    [{cat}] {it['phone']} | {it['last_msg_at']} | {it['last_msg_preview'][:50]}")
        if len(items) > 10:
            print(f"    ... y {len(items) - 10} más")

    if dry_run:
        print("\n  ** DRY RUN — no se aplicaron cambios **")
        print("  Ejecutar con --execute para aplicar.\n")
    else:
        print("\n  Aplicando cambios...")
        applied = 0
        for item in all_recovered:
            try:
                await state_mgr.add_to_advisor_inbox(
                    advisor_id=item["owner_id"],
                    phone=item["phone"],
                    canal=item["canal"],
                )

                # Asegurar que está en ZSET
                member = f"{item['phone']}:{item['canal']}"
                now_ts = datetime.now(BOG_TZ).timestamp()
                await r.zadd(
                    "active_conversations_sorted",
                    {member: now_ts},
                    nx=True,
                )

                applied += 1
            except Exception as e:
                print(f"    ERROR aplicando {item['phone']}: {e}")

        print(f"  Aplicados: {applied}/{len(all_recovered)}")

        # Notificar vía WS broadcast para refresh inmediato
        try:
            await r.publish("ws:broadcast", json.dumps({
                "type": "contacts_updated",
                "reason": "recover_unanswered",
                "count": applied,
            }))
            print("  WS broadcast enviado para refresh del panel")
        except Exception as e:
            print(f"  WS broadcast falló (non-fatal): {e}")

    # Cleanup
    await hs_client.aclose()
    await r.aclose()
    if hasattr(state_mgr, 'redis') and state_mgr.redis:
        await state_mgr.redis.aclose()

    print("\nFinalizado.")


_hs_stage_cache = {}

async def _get_hubspot_stage(client, contact_id: str) -> str:
    if contact_id in _hs_stage_cache:
        return _hs_stage_cache[contact_id]
    try:
        resp = await client.get(
            f"/crm/v3/objects/contacts/{contact_id}",
            params={"properties": "lifecyclestage"},
        )
        if resp.status_code == 200:
            stage = resp.json().get("properties", {}).get("lifecyclestage", "")
            _hs_stage_cache[contact_id] = stage
            return stage
        elif resp.status_code == 429:
            await asyncio.sleep(2)
            return await _get_hubspot_stage(client, contact_id)
        else:
            _hs_stage_cache[contact_id] = ""
            return ""
    except Exception as e:
        print(f"    HubSpot error para {contact_id}: {e}")
        return ""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Recuperar chats sin contestar")
    parser.add_argument("--execute", action="store_true", help="Aplicar cambios (default: dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="Solo listar, no modificar (default)")
    args = parser.parse_args()

    dry_run = not args.execute
    if dry_run:
        print("Modo: DRY RUN (usar --execute para aplicar)")
    else:
        print("Modo: EXECUTE — se aplicarán cambios")

    asyncio.run(main(dry_run=dry_run))
