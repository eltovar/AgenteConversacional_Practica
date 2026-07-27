"""
scripts/recover_unanswered.py
=============================

Recupera chats sin respuesta del asesor y los re-inserta en el panel.

Busca contactos donde:
  - El último mensaje registrado es del CLIENTE
  - No se ha contestado en X horas (configurable)
  - Están en embudo "En conversación" y/o "No responde"
  - El mensaje del cliente fue posterior a la última respuesta del bot o del asesor

Menú interactivo para seleccionar embudo, ventana de tiempo y tipo de filtro.
Dry-run por defecto; --execute aplica los cambios.

Uso:
    python scripts/recover_unanswered.py                   # interactivo, dry-run
    python scripts/recover_unanswered.py --execute         # interactivo, aplica
    python scripts/recover_unanswered.py --stage both --hours 72 --filter post-bot --execute
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

# Agregar raíz del proyecto al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

# ────────────────────────────────────────────────────────────────────────────
# Constantes
# ────────────────────────────────────────────────────────────────────────────

BOG_TZ = timezone(timedelta(hours=-5))

STAGE_EN_CONVERSACION = "1326623075"
STAGE_NO_RESPONDE = "other"

STAGE_NAMES = {
    "1326623075": "En conversación",
    "1326631578": "Nuevo Lead",
    "marketingqualifiedlead": "Visita agendada",
    "salesqualifiedlead": "Visita realizada",
    "opportunity": "En estudio",
    "customer": "Cerrado ganado",
    "evangelist": "Cerrado perdido",
    "other": "No responde",
    "subscriber": "Reubicados",
    "lead": "Aprobado",
    "1353539189": "Venta",
    "1326632628": "Otros Municipios",
    "1326632209": "Otras Areas",
}

ACTIVE_ADVISORS = {
    "89096378": "Jubeny",
    "89096380": "Luisa",
    "89096379": "Admin",
}

FILTER_POST_BOT = "post-bot"
FILTER_POST_ADVISOR = "post-advisor"


# ────────────────────────────────────────────────────────────────────────────
# Menú interactivo
# ────────────────────────────────────────────────────────────────────────────


def interactive_menu() -> Dict[str, Any]:
    """Menú de 3 pasos que retorna la configuración seleccionada."""
    config: Dict[str, Any] = {}

    # Paso 1: Embudo
    print("\n" + "=" * 60)
    print("  RECUPERAR CHATS SIN RESPUESTA")
    print("=" * 60)
    print("\n  Paso 1 — Seleccionar embudo:")
    print("    [1] En conversación")
    print("    [2] No responde")
    print("    [3] Ambos")
    while True:
        try:
            choice = input("\n  Embudo > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Abortado.")
            sys.exit(0)
        if choice == "1":
            config["stages"] = {STAGE_EN_CONVERSACION}
            config["stage_label"] = "En conversación"
            break
        elif choice == "2":
            config["stages"] = {STAGE_NO_RESPONDE}
            config["stage_label"] = "No responde"
            break
        elif choice == "3":
            config["stages"] = {STAGE_EN_CONVERSACION, STAGE_NO_RESPONDE}
            config["stage_label"] = "En conversación + No responde"
            break
        print("    Opción inválida. Escribe 1, 2 o 3.")

    # Paso 2: Ventana de tiempo
    print(f"\n  Paso 2 — Tiempo sin respuesta (embudo: {config['stage_label']}):")
    print("    [1] 24 horas")
    print("    [2] 48 horas")
    print("    [3] 72 horas")
    print("    [4] Personalizado")
    while True:
        try:
            choice = input("\n  Ventana > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Abortado.")
            sys.exit(0)
        if choice == "1":
            config["hours"] = 24
            break
        elif choice == "2":
            config["hours"] = 48
            break
        elif choice == "3":
            config["hours"] = 72
            break
        elif choice == "4":
            try:
                custom = input("    Horas > ").strip()
                h = int(custom)
                if h < 1:
                    print("    Debe ser al menos 1 hora.")
                    continue
                config["hours"] = h
                break
            except (ValueError, EOFError, KeyboardInterrupt):
                print("    Número inválido.")
                continue
        print("    Opción inválida. Escribe 1, 2, 3 o 4.")

    # Paso 3: Filtro post-respuesta
    print(f"\n  Paso 3 — Filtrar mensajes posteriores a:")
    print("    [1] Post-Bot (último msg cliente > última respuesta de SofIA/bot)")
    print("    [2] Post-Asesor (último msg cliente > última respuesta del asesor)")
    while True:
        try:
            choice = input("\n  Filtro > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Abortado.")
            sys.exit(0)
        if choice == "1":
            config["filter"] = FILTER_POST_BOT
            config["filter_label"] = "Post-Bot (SofIA)"
            break
        elif choice == "2":
            config["filter"] = FILTER_POST_ADVISOR
            config["filter_label"] = "Post-Asesor"
            break
        print("    Opción inválida. Escribe 1 o 2.")

    # Resumen
    print(f"\n  {'-' * 50}")
    print(f"  Configuracion:")
    print(f"    Embudo:  {config['stage_label']}")
    print(f"    Ventana: {config['hours']}h sin respuesta")
    print(f"    Filtro:  {config['filter_label']}")
    print(f"  {'-' * 50}")

    return config


def parse_stage_arg(value: str) -> Set[str]:
    """Convierte argumento --stage a set de stage IDs."""
    mapping = {
        "en_conversacion": {STAGE_EN_CONVERSACION},
        "no_responde": {STAGE_NO_RESPONDE},
        "both": {STAGE_EN_CONVERSACION, STAGE_NO_RESPONDE},
        "ambos": {STAGE_EN_CONVERSACION, STAGE_NO_RESPONDE},
    }
    result = mapping.get(value.lower())
    if not result:
        print(f"ERROR: --stage inválido: {value}. Usa: en_conversacion, no_responde, both")
        sys.exit(1)
    return result


# ────────────────────────────────────────────────────────────────────────────
# HubSpot helper con cache + retry 429
# ────────────────────────────────────────────────────────────────────────────

_hs_stage_cache: Dict[str, str] = {}


async def _get_hubspot_stage(client, contact_id: str) -> str:
    if contact_id in _hs_stage_cache:
        return _hs_stage_cache[contact_id]
    try:
        resp = await client.get(
            f"/crm/v3/objects/contacts/{contact_id}",
            params={"properties": "lifecyclestage,hubspot_owner_id,firstname"},
        )
        if resp.status_code == 200:
            props = resp.json().get("properties", {})
            stage = props.get("lifecyclestage", "")
            _hs_stage_cache[contact_id] = stage
            return stage
        elif resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 2))
            await asyncio.sleep(retry_after)
            return await _get_hubspot_stage(client, contact_id)
        else:
            _hs_stage_cache[contact_id] = ""
            return ""
    except Exception as e:
        print(f"    HubSpot error para {contact_id}: {e}")
        return ""


async def _get_hubspot_owner(client, contact_id: str) -> Optional[str]:
    """Obtiene hubspot_owner_id. Reutiliza cache de stage si ya existe."""
    try:
        resp = await client.get(
            f"/crm/v3/objects/contacts/{contact_id}",
            params={"properties": "hubspot_owner_id"},
        )
        if resp.status_code == 200:
            return resp.json().get("properties", {}).get("hubspot_owner_id")
        elif resp.status_code == 429:
            await asyncio.sleep(2)
            return await _get_hubspot_owner(client, contact_id)
    except Exception:
        pass
    return None


# ────────────────────────────────────────────────────────────────────────────
# Discovery
# ────────────────────────────────────────────────────────────────────────────


async def discover_candidates(
    mongo_db,
    redis_conn,
    hs_client,
    stages: Set[str],
    cutoff: datetime,
    post_filter: str,
) -> "tuple[List[Dict], List[Dict]]":
    """
    Busca contactos con mensaje pendiente del cliente.

    Retorna (candidates, skipped).
    """
    candidates: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    # Paso 1: Conversations con último mensaje del cliente
    query: Dict[str, Any] = {
        "last_message_sender": "client",
        "last_message_at": {"$lt": cutoff},
        "archived": {"$ne": True},
    }

    docs = await mongo_db.conversations.find(
        query,
        {
            "phone": 1, "canal": 1, "contact_id": 1, "owner_id": 1,
            "last_message_at": 1, "last_message_preview": 1,
        },
    ).sort("last_message_at", -1).to_list(length=5000)

    print(f"\n  Conversaciones con último msg de cliente (antes de {cutoff.strftime('%Y-%m-%d %H:%M')} UTC): {len(docs)}")

    for conv in docs:
        phone = conv.get("phone")
        if not phone:
            continue

        contact_id = conv.get("contact_id")
        if not contact_id:
            skipped.append({"phone": phone, "reason": "sin_contact_id"})
            continue

        canal = conv.get("canal", "whatsapp")

        # Paso 2: Verificar lifecyclestage en HubSpot
        stage = await _get_hubspot_stage(hs_client, contact_id)
        if stage not in stages:
            skipped.append({
                "phone": phone,
                "reason": f"stage={STAGE_NAMES.get(stage, stage)} (no coincide con filtro)",
            })
            continue

        # Paso 3: Post-filtro — verificar en messages que el último msg
        # del cliente sea posterior al último bot/advisor
        sender_to_check = "bot" if post_filter == FILTER_POST_BOT else "advisor"
        is_valid = await _verify_post_filter(
            mongo_db, phone, sender_to_check, conv.get("last_message_at"),
        )
        if not is_valid:
            skipped.append({
                "phone": phone,
                "reason": f"último msg cliente NO es posterior al último {sender_to_check}",
            })
            continue

        # Paso 4: Resolver owner (MongoDB → Redis → HubSpot)
        owner_id = conv.get("owner_id")

        if not owner_id:
            meta_key = f"conv_meta:{phone}:{canal}"
            raw_meta = await redis_conn.get(meta_key)
            if raw_meta:
                try:
                    meta = json.loads(raw_meta)
                    owner_id = meta.get("assigned_owner_id")
                except Exception:
                    pass

        if not owner_id:
            owner_id = await _get_hubspot_owner(hs_client, contact_id)

        if not owner_id or owner_id not in ACTIVE_ADVISORS:
            skipped.append({
                "phone": phone,
                "reason": f"owner={owner_id} (no es asesor activo)",
            })
            continue

        # Paso 5: Info adicional para el reporte
        last_msg_at = conv.get("last_message_at")
        hours_ago = ""
        if last_msg_at:
            if last_msg_at.tzinfo is None:
                last_msg_at = last_msg_at.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - last_msg_at
            hours_ago = f"{delta.total_seconds() / 3600:.0f}h"

        candidates.append({
            "phone": phone,
            "canal": canal,
            "owner_id": owner_id,
            "owner_name": ACTIVE_ADVISORS.get(owner_id, owner_id),
            "contact_id": contact_id,
            "stage": stage,
            "stage_name": STAGE_NAMES.get(stage, stage),
            "last_msg_at": last_msg_at,
            "hours_ago": hours_ago,
            "preview": (conv.get("last_message_preview") or "")[:60],
        })

    return candidates, skipped


async def _verify_post_filter(
    mongo_db,
    phone: str,
    sender_type: str,
    client_last_at: Optional[datetime],
) -> bool:
    """
    Verifica que el último mensaje del cliente sea POSTERIOR al último
    mensaje del sender_type (bot o advisor).

    Si no hay mensajes del sender_type → True (cliente escribió y nadie respondió).
    Si client_last_at es None → False (no hay referencia).
    """
    if not client_last_at:
        return False

    last_response = await mongo_db.messages.find_one(
        {"phone": phone, "sender": sender_type},
        {"timestamp": 1},
        sort=[("timestamp", -1)],
    )

    if not last_response:
        return True

    response_ts = last_response.get("timestamp")
    if not response_ts:
        return True

    if response_ts.tzinfo is None:
        response_ts = response_ts.replace(tzinfo=timezone.utc)
    if client_last_at.tzinfo is None:
        client_last_at = client_last_at.replace(tzinfo=timezone.utc)

    return client_last_at > response_ts


# ────────────────────────────────────────────────────────────────────────────
# Reporte
# ────────────────────────────────────────────────────────────────────────────


def print_report(candidates: List[Dict], skipped: List[Dict], config: Dict) -> None:
    stage_label = config.get("stage_label", "")
    hours = config.get("hours", 0)
    filter_label = config.get("filter_label", "")

    print("\n" + "=" * 90)
    print(f"  REPORTE — {len(candidates)} chats sin respuesta")
    print(f"  Embudo: {stage_label} | Ventana: >{hours}h | Filtro: {filter_label}")
    print("=" * 90)

    if not candidates:
        print("\n  No se encontraron contactos que cumplan los criterios.\n")
        if skipped:
            _print_skip_summary(skipped)
        return

    # Agrupar por asesor
    by_owner: Dict[str, List[Dict]] = {}
    for c in candidates:
        by_owner.setdefault(c["owner_name"], []).append(c)

    for owner_name, items in sorted(by_owner.items()):
        print(f"\n  {owner_name} ({len(items)} chats)")
        print(f"  {'-' * 85}")
        print(f"  {'#':>3}  {'Telefono':17}  {'Embudo':18}  {'Hace':>5}  {'Preview'}")
        print(f"  {'-' * 85}")
        for i, c in enumerate(items, 1):
            print(
                f"  {i:>3}  {c['phone']:17}  "
                f"{c['stage_name']:18}  "
                f"{c['hours_ago']:>5}  "
                f"{c['preview']}"
            )

    # Resumen por embudo
    by_stage: Dict[str, int] = {}
    for c in candidates:
        by_stage[c["stage_name"]] = by_stage.get(c["stage_name"], 0) + 1

    print(f"\n  {'-' * 40}")
    print(f"  Por embudo:")
    for stage_name, count in sorted(by_stage.items(), key=lambda x: -x[1]):
        print(f"    {count:>3}x  {stage_name}")
    print(f"  Total: {len(candidates)}")

    _print_skip_summary(skipped)


def _print_skip_summary(skipped: List[Dict]) -> None:
    if not skipped:
        return
    reasons: Dict[str, int] = {}
    for s in skipped:
        r = s.get("reason", "desconocido")
        # Agrupar razones similares por prefijo
        if r.startswith("stage="):
            r = "stage no coincide con filtro"
        reasons[r] = reasons.get(r, 0) + 1

    print(f"\n  Descartados: {len(skipped)}")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"    {count:>4}x  {reason}")


# ────────────────────────────────────────────────────────────────────────────
# Seleccion interactiva de contactos
# ────────────────────────────────────────────────────────────────────────────


def parse_selection(input_str: str, total: int) -> List[int]:
    """
    Parsea expresiones como '1,3,5-7' / 'all' / 'todos' / 'none' / ''
    a una lista ordenada de indices 1-based validos.
    """
    s = input_str.strip().lower()
    if s in ("all", "todos", "*"):
        return list(range(1, total + 1))
    if s in ("none", "ninguno", "no", ""):
        return []

    selected: Set[int] = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a_str, b_str = part.split("-", 1)
                a, b = int(a_str.strip()), int(b_str.strip())
                lo, hi = min(a, b), max(a, b)
                selected.update(range(lo, hi + 1))
            except ValueError:
                print(f"    Rango invalido ignorado: '{part}'")
        else:
            try:
                selected.add(int(part))
            except ValueError:
                print(f"    Numero invalido ignorado: '{part}'")
    return sorted(i for i in selected if 1 <= i <= total)


def interactive_select(candidates: List[Dict]) -> List[Dict]:
    """
    Muestra la lista numerada y pide al usuario seleccionar cuales recuperar.
    Retorna la sublista seleccionada.
    """
    if not candidates:
        return []

    # Numerar todos los candidatos (flat, sin agrupar por asesor)
    print(f"\n  Lista completa de {len(candidates)} contactos encontrados:")
    print(f"  {'-' * 90}")
    print(f"  {'#':>3}  {'Telefono':17}  {'Asesor':10}  {'Embudo':18}  {'Hace':>5}  {'Preview'}")
    print(f"  {'-' * 90}")
    for i, c in enumerate(candidates, 1):
        print(
            f"  {i:>3}  {c['phone']:17}  "
            f"{c['owner_name']:10}  "
            f"{c['stage_name']:18}  "
            f"{c['hours_ago']:>5}  "
            f"{c['preview']}"
        )

    while True:
        print(f"\n  Selecciona cuales recuperar:")
        print(f"    Ejemplos: '1,3,5-7'  'all' / 'todos'  'none' / '' para cancelar")
        try:
            raw = input("  Seleccion > ").strip()
        except (EOFError, KeyboardInterrupt):
            return []

        idxs = parse_selection(raw, len(candidates))
        if not idxs:
            try:
                confirm = input("  No seleccionaste nada. Cancelar? [s/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return []
            if confirm in ("s", "si", "y", "yes"):
                return []
            continue

        selected = [c for i, c in enumerate(candidates, 1) if i in idxs]
        print(f"\n  Vas a recuperar {len(selected)} contacto(s):")
        for j, c in enumerate(selected, 1):
            print(
                f"    {j:>3}. {c['phone']:17}  "
                f"{c['owner_name']:10}  {c['stage_name']}"
            )

        try:
            confirm = input(
                f"\n  Escribe 'SI' para confirmar (o 'r' para reseleccionar): "
            ).strip().upper()
        except (EOFError, KeyboardInterrupt):
            return []

        if confirm == "SI":
            return selected
        if confirm.lower() in ("r", "reselect", "reseleccionar"):
            continue
        print("  Cancelado.")
        return []


# ────────────────────────────────────────────────────────────────────────────
# Ejecucion
# ────────────────────────────────────────────────────────────────────────────


async def apply_recovery(
    candidates: List[Dict],
    state_mgr,
    redis_conn,
) -> int:
    """Aplica la recuperación: add_to_advisor_inbox para cada contacto."""
    applied = 0
    total = len(candidates)

    for i, c in enumerate(candidates, 1):
        try:
            await state_mgr.add_to_advisor_inbox(
                advisor_id=c["owner_id"],
                phone=c["phone"],
                canal=c["canal"],
            )
            applied += 1
            print(f"  [{i:>3}/{total}] {c['phone']} → inbox de {c['owner_name']}")
        except Exception as e:
            print(f"  [{i:>3}/{total}] ERROR {c['phone']}: {e}")

    # WS broadcast para refresh inmediato del panel
    if applied > 0:
        try:
            await redis_conn.publish("ws:broadcast", json.dumps({
                "type": "contacts_updated",
                "reason": "recover_unanswered",
                "count": applied,
            }))
            print(f"\n  WS broadcast enviado — panel se refrescará")
        except Exception as e:
            print(f"\n  WS broadcast falló (non-fatal): {e}")

    return applied


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recuperar chats sin respuesta del asesor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Aplicar cambios (default: dry-run)",
    )
    parser.add_argument(
        "--stage", type=str, default=None,
        help="Embudo: en_conversacion, no_responde, both (default: interactivo)",
    )
    parser.add_argument(
        "--hours", type=int, default=None,
        help="Horas sin respuesta (default: interactivo)",
    )
    parser.add_argument(
        "--filter", type=str, default=None, dest="post_filter",
        help="Filtro: post-bot, post-advisor (default: interactivo)",
    )
    args = parser.parse_args()

    # Determinar configuración: CLI args o menú interactivo
    has_all_args = args.stage and args.hours and args.post_filter
    if has_all_args:
        config = {
            "stages": parse_stage_arg(args.stage),
            "stage_label": args.stage,
            "hours": args.hours,
            "filter": args.post_filter,
            "filter_label": "Post-Bot" if args.post_filter == FILTER_POST_BOT else "Post-Asesor",
        }
    else:
        config = interactive_menu()

    dry_run = not args.execute

    if dry_run:
        print("\n  Modo: DRY RUN (usar --execute para aplicar)")
    else:
        print("\n  Modo: EXECUTE — se aplicarán cambios")

    # ── Conexiones ──

    import httpx
    import redis.asyncio as aioredis
    from database.mongodb_client import get_mongo_manager
    from middleware.conversation_state import ConversationStateManager

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
        return 2

    hubspot_token = (
        os.environ.get("HUBSPOT_API_KEY")
        or os.environ.get("HUBSPOT_ACCESS_TOKEN")
        or ""
    )
    if not hubspot_token:
        print("ERROR: HUBSPOT_API_KEY / HUBSPOT_ACCESS_TOKEN no configurada")
        return 2

    mongo = get_mongo_manager()
    if not await mongo.connect():
        print("ERROR: No se pudo conectar a MongoDB")
        return 2

    redis_conn = aioredis.from_url(redis_url, decode_responses=True, max_connections=5)
    state_mgr = ConversationStateManager()

    hs_client = httpx.AsyncClient(
        base_url="https://api.hubapi.com",
        headers={"Authorization": f"Bearer {hubspot_token}"},
        timeout=15,
    )

    try:
        await redis_conn.ping()
        print("  Redis: OK")
    except Exception as e:
        print(f"ERROR: Redis no disponible: {e}")
        return 2

    # ── Discovery ──

    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=config["hours"])

    print(f"\n  Buscando chats sin respuesta desde hace >{config['hours']}h...")

    try:
        candidates, skipped = await discover_candidates(
            mongo_db=mongo.db,
            redis_conn=redis_conn,
            hs_client=hs_client,
            stages=config["stages"],
            cutoff=cutoff,
            post_filter=config["filter"],
        )

        # ── Reporte ──

        print_report(candidates, skipped, config)

        # ── Ejecucion ──

        if dry_run:
            print(f"\n  ** DRY RUN — no se aplicaron cambios **")
            print(f"  Ejecutar con --execute para aplicar.\n")
        elif candidates:
            to_apply = interactive_select(candidates)
            if not to_apply:
                print("  Sin seleccion — no se aplicaron cambios.")
            else:
                print(f"\n  Aplicando recuperacion de {len(to_apply)} chats...")
                applied = await apply_recovery(to_apply, state_mgr, redis_conn)
                print(f"\n  Resultado: {applied}/{len(to_apply)} recuperados")
        else:
            print("\n  Nada que aplicar.\n")

    finally:
        await hs_client.aclose()
        await redis_conn.aclose()
        if hasattr(state_mgr, "redis") and state_mgr.redis:
            try:
                await state_mgr.redis.aclose()
            except Exception:
                pass

    print("  Finalizado.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n  Interrumpido.", file=sys.stderr)
        sys.exit(130)
