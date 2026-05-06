"""
scripts/cleanup_no_response.py
==============================

Mueve a "No responde" (lifecyclestage="other") los leads inactivos que cumplen:

  Pool A — Mongo:
    * Tienen exactamente 1 mensaje en `db.messages`
    * Ese único mensaje tiene sender == "advisor" (interacción humana, NO bot)
    * Su timestamp es anterior a now - DAYS días
    * En HubSpot siguen en lifecyclestage ∈ {1326631578 "Nuevo Lead",
                                            1326623075 "En conversación"}

  Pool B — HubSpot huérfano:
    * En HubSpot están en uno de los 2 stages anteriores
    * createdate < now - DAYS días
    * NO tienen ningún mensaje en Mongo (TTL 90d expirado o nunca escribieron)

Acción por candidato (idempotente):
  1) HubSpot PATCH lifecyclestage="" + PATCH lifecyclestage="other"
     (two-step, único modo de mover hacia atrás).
  2) Cierre de conversación via `_close_conversation_internal`:
     BOT_ACTIVE + ZREM active_conversations_sorted + SREM bot_controlled
     + in_panel=False + retirar del inbox del asesor.

Modos:
  Dry-run (default)  → reporte por consola + CSV, sin tocar nada.
  --apply            → tras confirmación interactiva, ejecuta la migración.
  --apply --yes      → omite confirmación (CI / Railway shell).

Uso:
    python -m scripts.cleanup_no_response
    python -m scripts.cleanup_no_response --apply
    python -m scripts.cleanup_no_response --days 45 --apply --yes

Índice recomendado en MongoDB (acelera la aggregation a O(log n) sobre el rango):
    db.messages.createIndex(
        {sender: 1, timestamp: 1, phone: 1},
        {name: "cleanup_no_response_idx", background: true}
    )
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

# Cargar variables de entorno antes de importar módulos del proyecto
load_dotenv()

# ────────────────────────────────────────────────────────────────────────────
# Constantes
# ────────────────────────────────────────────────────────────────────────────

STAGE_NUEVO_LEAD = "1326631578"
STAGE_EN_CONVERSACION = "1326623075"
STAGE_NO_RESPONDE = "other"
ELIGIBLE_STAGES = {STAGE_NUEVO_LEAD, STAGE_EN_CONVERSACION}

HUBSPOT_BASE = "https://api.hubapi.com"
HUBSPOT_BATCH_READ = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/batch/read"
HUBSPOT_SEARCH = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
HUBSPOT_CONTACT = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"

# Reintentos al recibir 429 — alineado con `_hubspot_patch` del panel.
RETRY_BACKOFF_BASE_S = 12

# Pausa entre PATCHes en Fase C para no saturar HubSpot (10 req/s soft cap).
PATCH_PAUSE_S = 0.15


# ────────────────────────────────────────────────────────────────────────────
# Modelo
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class Candidate:
    contact_id: Optional[str]
    phone: Optional[str]
    name: str
    created_at: Optional[datetime]
    last_message_at: Optional[datetime]
    last_message_content: str
    current_stage: Optional[str]
    pool: str  # "A" o "B"
    # Métricas adicionales (opción C)
    last_client_at: Optional[datetime] = None
    msg_count: int = 0
    last_msg_sender: str = ""
    # Resultado tras aplicar
    apply_status: str = "pending"
    apply_error: str = ""

    def display_name(self) -> str:
        return self.name or "(sin nombre)"


# ────────────────────────────────────────────────────────────────────────────
# Utils HubSpot con retry 429
# ────────────────────────────────────────────────────────────────────────────


async def _hs_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: Dict[str, str],
    *,
    json: Optional[dict] = None,
    max_retries: int = 3,
) -> httpx.Response:
    """HTTP HubSpot con retry exponencial en 429. Replica la lógica de
    `outbound_panel._hubspot_patch` para mantener un comportamiento consistente."""
    for attempt in range(1, max_retries + 1):
        resp = await client.request(method, url, headers=headers, json=json)
        if resp.status_code == 429:
            wait = RETRY_BACKOFF_BASE_S * attempt
            print(
                f"  ⚠ HubSpot 429 ({method} {url.rsplit('/', 1)[-1]}) "
                f"intento {attempt}/{max_retries} — esperando {wait}s",
                file=sys.stderr,
            )
            await asyncio.sleep(wait)
            continue
        return resp
    return await client.request(method, url, headers=headers, json=json)


# ────────────────────────────────────────────────────────────────────────────
# Fase A.1 — Aggregation Mongo (Pool A)
# ────────────────────────────────────────────────────────────────────────────


async def discover_pool_a(
    db,
    cutoff_recent: datetime,
    cutoff_old: Optional[datetime] = None,
    require_advisor_last: bool = False,
) -> List[Candidate]:
    """
    Pool A — dos modos según `require_advisor_last`:

    Modo CLIENT_SILENT (default):
      * has_advisor = True
      * first_msg_ts < cutoff_recent (conversación tiene al menos min_days)
      * last_client_ts is None OR last_client_ts < cutoff_recent (cliente silente)

    Modo ADVISOR_LAST (--last-sender-advisor):
      * last_msg_sender = "advisor"
      * cutoff_old <= last_msg_ts <= cutoff_recent  (último msg en ventana)

    `cutoff_recent` = límite superior de antigüedad (más reciente excluido).
    `cutoff_old`    = límite inferior de antigüedad (más viejo excluido). Si None, sin tope.
    """
    pipeline: List[Dict[str, Any]] = [
        {"$sort": {"timestamp": -1}},
        {
            "$group": {
                "_id": "$phone",
                "msg_count": {"$sum": 1},
                "first_msg_ts": {"$last": "$timestamp"},
                "last_msg_ts": {"$first": "$timestamp"},
                "last_msg_content": {"$first": "$content"},
                "last_msg_sender": {"$first": "$sender"},
                "last_client_ts": {
                    "$max": {
                        "$cond": [{"$eq": ["$sender", "client"]}, "$timestamp", None]
                    }
                },
                "has_advisor": {
                    "$max": {"$cond": [{"$eq": ["$sender", "advisor"]}, 1, 0]}
                },
                "hubspot_contact_id": {"$first": "$hubspot_contact_id"},
            }
        },
    ]

    match: Dict[str, Any] = {}
    if require_advisor_last:
        match["last_msg_sender"] = "advisor"
        ts_filter: Dict[str, Any] = {"$lte": cutoff_recent}
        if cutoff_old is not None:
            ts_filter["$gte"] = cutoff_old
        match["last_msg_ts"] = ts_filter
    else:
        match["has_advisor"] = 1
        match["first_msg_ts"] = {"$lt": cutoff_recent}
        match["$or"] = [
            {"last_client_ts": None},
            {"last_client_ts": {"$lt": cutoff_recent}},
        ]
        if cutoff_old is not None:
            match["last_msg_ts"] = {"$gte": cutoff_old}

    pipeline.append({"$match": match})

    candidates: List[Candidate] = []
    async for doc in db.messages.aggregate(pipeline, allowDiskUse=True):
        candidates.append(
            Candidate(
                contact_id=doc.get("hubspot_contact_id"),
                phone=doc["_id"],
                name="",  # se hidrata en Fase A.3
                created_at=None,
                last_message_at=doc.get("last_msg_ts"),
                last_message_content=(doc.get("last_msg_content") or "")[:240],
                current_stage=None,
                pool="A",
                last_client_at=doc.get("last_client_ts"),
                msg_count=doc.get("msg_count", 0),
                last_msg_sender=doc.get("last_msg_sender", ""),
            )
        )

    return candidates


# ────────────────────────────────────────────────────────────────────────────
# Fase A.2 — Search HubSpot (Pool B)
# ────────────────────────────────────────────────────────────────────────────


async def discover_pool_b(
    client: httpx.AsyncClient,
    headers: Dict[str, str],
    cutoff: datetime,
    phones_in_mongo: Set[str],
) -> List[Candidate]:
    """
    Busca contactos en HubSpot en stages elegibles con createdate < cutoff
    y los filtra a los que NO tienen NINGÚN mensaje en Mongo.
    """
    # HubSpot search timestamps: milisegundos epoch UTC.
    cutoff_ms = int(cutoff.astimezone(timezone.utc).timestamp() * 1000)

    body_template = lambda after: {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "lifecyclestage", "operator": "EQ", "value": stage},
                    {"propertyName": "createdate", "operator": "LT", "value": cutoff_ms},
                ]
            }
            for stage in ELIGIBLE_STAGES
        ],
        "properties": ["phone", "firstname", "lastname", "createdate", "lifecyclestage"],
        "limit": 100,
        **({"after": after} if after else {}),
    }

    candidates: List[Candidate] = []
    after: Optional[str] = None
    page = 0
    while True:
        page += 1
        resp = await _hs_request(client, "POST", HUBSPOT_SEARCH, headers, json=body_template(after))
        if resp.status_code != 200:
            print(f"  ✖ HubSpot search falló: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
            break

        data = resp.json()
        for r in data.get("results", []):
            cid = r.get("id")
            props = r.get("properties", {})
            phone = props.get("phone")

            # Pool B: descartar si existe en Mongo (allí caería en Pool A o no aplica)
            if phone and phone in phones_in_mongo:
                continue

            try:
                created_at = (
                    datetime.fromisoformat(props["createdate"].replace("Z", "+00:00"))
                    if props.get("createdate")
                    else None
                )
            except Exception:
                created_at = None

            full_name = f"{props.get('firstname', '') or ''} {props.get('lastname', '') or ''}".strip()
            candidates.append(
                Candidate(
                    contact_id=cid,
                    phone=phone,
                    name=full_name,
                    created_at=created_at,
                    last_message_at=None,
                    last_message_content="(sin mensajes en MongoDB — TTL expirado o nunca conversó)",
                    current_stage=props.get("lifecyclestage"),
                    pool="B",
                )
            )

        paging = data.get("paging", {}).get("next", {})
        after = paging.get("after")
        if not after:
            break
        # HubSpot search soft cap 10000 resultados — nunca aplicará a 391 contactos
        if page > 20:
            print("  ⚠ Más de 20 páginas de search — abortando paginación por seguridad", file=sys.stderr)
            break

    return candidates


# ────────────────────────────────────────────────────────────────────────────
# Fase A.3 — Hidratación batch HubSpot para Pool A
# ────────────────────────────────────────────────────────────────────────────


async def hydrate_pool_a(
    client: httpx.AsyncClient,
    headers: Dict[str, str],
    pool_a: List[Candidate],
) -> Tuple[List[Candidate], List[Candidate]]:
    """
    Para los Candidates de Pool A:
      * llena name, current_stage, created_at desde HubSpot batch/read
      * devuelve (válidos, descartados) — válidos son los que siguen en
        ELIGIBLE_STAGES; descartados tienen apply_status explicando el motivo.
    """
    with_id = [c for c in pool_a if c.contact_id]
    without_id = [c for c in pool_a if not c.contact_id]
    for c in without_id:
        c.apply_status = "skipped_no_contact_id"

    valid: List[Candidate] = []
    discarded: List[Candidate] = list(without_id)

    for chunk_start in range(0, len(with_id), 100):
        chunk = with_id[chunk_start : chunk_start + 100]
        body = {
            "properties": ["firstname", "lastname", "phone", "lifecyclestage", "createdate"],
            "inputs": [{"id": c.contact_id} for c in chunk],
        }
        resp = await _hs_request(client, "POST", HUBSPOT_BATCH_READ, headers, json=body)
        if resp.status_code not in (200, 207):
            print(f"  ✖ HubSpot batch/read falló: {resp.status_code}", file=sys.stderr)
            for c in chunk:
                c.apply_status = "skipped_hubspot_unreachable"
                discarded.append(c)
            continue

        results_by_id = {r["id"]: r for r in resp.json().get("results", [])}
        for c in chunk:
            r = results_by_id.get(c.contact_id)
            if not r:
                c.apply_status = "skipped_not_in_hubspot"
                discarded.append(c)
                continue
            props = r.get("properties", {})
            stage = props.get("lifecyclestage") or "(sin stage)"
            c.name = f"{props.get('firstname', '') or ''} {props.get('lastname', '') or ''}".strip()
            c.current_stage = stage
            try:
                c.created_at = (
                    datetime.fromisoformat(props["createdate"].replace("Z", "+00:00"))
                    if props.get("createdate")
                    else None
                )
            except Exception:
                pass
            if stage not in ELIGIBLE_STAGES:
                c.apply_status = f"skipped_stage_{stage}"
                discarded.append(c)
                continue
            valid.append(c)

    return valid, discarded


# ────────────────────────────────────────────────────────────────────────────
# Fase A.4 — Phones en Mongo (para excluir Pool B)
# ────────────────────────────────────────────────────────────────────────────


async def get_phones_in_mongo(db) -> Set[str]:
    return set(await db.messages.distinct("phone"))


# ────────────────────────────────────────────────────────────────────────────
# Reporte
# ────────────────────────────────────────────────────────────────────────────


def fmt_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


STAGE_NAMES = {
    "1326631578": "Nuevo Lead",
    "1326623075": "En conversación",
    "marketingqualifiedlead": "Visita agendada",
    "salesqualifiedlead": "Visita realizada",
    "opportunity": "En estudio",
    "customer": "Cerrado ganado",
    "evangelist": "Cerrado perdido",
    "other": "No responde",
    "subscriber": "Reubicados",
    "lead": "Aprobado",
    "1353539189": "Venta",
}


def _stage_label(stage: Optional[str]) -> str:
    if not stage:
        return "—"
    return STAGE_NAMES.get(stage, stage)


def print_report(candidates: List[Candidate], discarded: Optional[List[Candidate]] = None) -> None:
    if not candidates and not discarded:
        print("\n  No se encontraron candidatos.\n")
        return

    print(f"\n{'='*130}")
    print(f"REPORTE DE CANDIDATOS A MOVER — total {len(candidates)}")
    print(f"  Pool A (Mongo: 1 msg advisor >30d, stage elegible): {sum(1 for c in candidates if c.pool == 'A')}")
    print(f"  Pool B (HubSpot huérfano sin Mongo):                {sum(1 for c in candidates if c.pool == 'B')}")
    print(f"{'='*130}")

    if candidates:
        header = (
            f"{'#':>3}  {'Pool':4}  {'ContactID':>12}  {'Phone':17}  "
            f"{'Creado':16}  {'ÚltMsg':16}  {'ÚltCliente':16}  {'#Msg':>4}  "
            f"{'Stage':18}  Nombre / Último mensaje"
        )
        print(header)
        print("-" * len(header))
        for i, c in enumerate(candidates, 1):
            msg = c.last_message_content.replace("\n", " ")[:50]
            sender_tag = f"[{c.last_msg_sender[:3]}]" if c.last_msg_sender else ""
            last_client = fmt_dt(c.last_client_at) if c.last_client_at else "(nunca)"
            print(
                f"{i:>3}  {c.pool:4}  {(c.contact_id or '—'):>12}  "
                f"{(c.phone or '—'):17}  {fmt_dt(c.created_at):16}  "
                f"{fmt_dt(c.last_message_at):16}  {last_client:16}  "
                f"{c.msg_count:>4}  {_stage_label(c.current_stage):18}  "
                f"{c.display_name()[:24]} {sender_tag} {msg}"
            )
    else:
        print("  (ningún candidato elegible)")

    if discarded:
        print(f"\n{'─'*130}")
        print(f"POOL A DESCARTADOS (encontrados en Mongo pero ya fuera de stage elegible) — {len(discarded)}")
        print(f"{'─'*130}")
        header2 = (
            f"{'#':>3}  {'ContactID':>12}  {'Phone':17}  {'Creado':16}  "
            f"{'ÚltMsg':16}  {'Stage actual en HubSpot':26}  Nombre"
        )
        print(header2)
        print("-" * len(header2))
        for i, c in enumerate(discarded, 1):
            reason = c.apply_status.replace("skipped_stage_", "").replace("skipped_", "")
            stage_display = _stage_label(c.current_stage) if c.current_stage else reason
            print(
                f"{i:>3}  {(c.contact_id or '—'):>12}  {(c.phone or '—'):17}  "
                f"{fmt_dt(c.created_at):16}  {fmt_dt(c.last_message_at):16}  "
                f"{stage_display:26}  {c.display_name()[:28]}"
            )
        print()
        # Resumen agrupado por etapa
        from collections import Counter
        stage_counts: Counter = Counter()
        for c in discarded:
            label = _stage_label(c.current_stage) if c.current_stage else c.apply_status
            stage_counts[label] += 1
        print("  Distribución por etapa actual:")
        for stage_label, count in stage_counts.most_common():
            print(f"    {count:>3}x  {stage_label}")


def write_csv(candidates: List[Candidate], discarded: Optional[List[Candidate]] = None) -> Path:
    out = Path(f"cleanup_report_{datetime.now().strftime('%Y-%m-%d_%H%M')}.csv")
    all_rows = list(candidates) + (discarded or [])
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "pool", "contact_id", "phone", "name",
                "current_stage", "current_stage_label",
                "created_at", "last_message_at", "last_client_at",
                "msg_count", "last_msg_sender", "last_message_content",
                "apply_status", "apply_error",
            ]
        )
        for c in all_rows:
            w.writerow(
                [
                    c.pool, c.contact_id or "", c.phone or "", c.name,
                    c.current_stage or "", _stage_label(c.current_stage),
                    fmt_dt(c.created_at), fmt_dt(c.last_message_at),
                    fmt_dt(c.last_client_at) if c.last_client_at else "(nunca)",
                    c.msg_count, c.last_msg_sender, c.last_message_content,
                    c.apply_status, c.apply_error,
                ]
            )
    return out


# ────────────────────────────────────────────────────────────────────────────
# Selección interactiva
# ────────────────────────────────────────────────────────────────────────────


def parse_selection(input_str: str, total: int) -> List[int]:
    """
    Parsea expresiones como '1,3,5-7' / 'all' / 'todos' / 'none' / ''
    a una lista ordenada de índices 1-based válidos.
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
                print(f"  ⚠ rango inválido ignorado: '{part}'", file=sys.stderr)
        else:
            try:
                selected.add(int(part))
            except ValueError:
                print(f"  ⚠ número inválido ignorado: '{part}'", file=sys.stderr)
    return sorted(i for i in selected if 1 <= i <= total)


def interactive_select(candidates: List[Candidate]) -> List[Candidate]:
    """
    Muestra prompt interactivo para que el usuario elija qué candidatos aplicar.
    Permite reintentar si la selección está vacía o se quiere ajustar.
    """
    if not candidates:
        return []

    while True:
        print(f"\n  ¿Cuáles de los {len(candidates)} contactos quieres mover a 'No responde'?")
        print("    Ejemplos:  '1,3,5-7'   'all' / 'todos'   'none' / '' para abortar")
        try:
            raw = input("  Selección > ").strip()
        except EOFError:
            return []

        idxs = parse_selection(raw, len(candidates))
        if not idxs:
            confirm_abort = input("  No seleccionaste nada. ¿Abortar? [s/N]: ").strip().lower()
            if confirm_abort in ("s", "si", "sí", "y", "yes"):
                return []
            continue

        selected = [c for i, c in enumerate(candidates, 1) if i in idxs]
        print(f"\n  Vas a mover {len(selected)} contacto(s):")
        for j, c in enumerate(selected, 1):
            print(
                f"    {j:>3}. cid={c.contact_id or '—':>12}  {c.phone or '—':17}  "
                f"{_stage_label(c.current_stage):18}  {c.display_name()[:40]}"
            )
        confirm = input(
            f"\n  Escribe 'CONFIRMAR' para aplicar a estos {len(selected)} (o 'r' para reseleccionar): "
        ).strip()
        if confirm == "CONFIRMAR":
            return selected
        if confirm.lower() in ("r", "reselect", "reseleccionar"):
            continue
        print("  Abortado.")
        return []


# ────────────────────────────────────────────────────────────────────────────
# Fase C — Aplicar
# ────────────────────────────────────────────────────────────────────────────


async def apply_changes(
    candidates: List[Candidate],
    client: httpx.AsyncClient,
    headers: Dict[str, str],
) -> Tuple[int, int]:
    """Importa lazy `_close_conversation_internal` para reutilizar la lógica
    idempotente del panel (Redis ZSET, bot_controlled, in_panel, inbox)."""
    from middleware.outbound_panel import _close_conversation_internal  # type: ignore

    ok = err = 0
    total = len(candidates)
    for i, c in enumerate(candidates, 1):
        if not c.contact_id:
            c.apply_status = "skipped_no_contact_id"
            err += 1
            continue

        prefix = f"  [{i:>3}/{total}] cid={c.contact_id} phone={c.phone or '—':17}"
        try:
            url = f"{HUBSPOT_CONTACT}/{c.contact_id}"
            # Two-step lifecyclestage update — único modo de mover hacia atrás
            r1 = await _hs_request(client, "PATCH", url, headers, json={"properties": {"lifecyclestage": ""}})
            if r1.status_code != 200:
                raise RuntimeError(f"clear stage HTTP {r1.status_code} {r1.text[:120]}")
            r2 = await _hs_request(
                client, "PATCH", url, headers,
                json={"properties": {"lifecyclestage": STAGE_NO_RESPONDE}},
            )
            if r2.status_code != 200:
                raise RuntimeError(f"set stage HTTP {r2.status_code} {r2.text[:120]}")

            close_result: Dict[str, Any] = {}
            if c.phone:
                close_result = await _close_conversation_internal(c.phone, "whatsapp")

            c.apply_status = "ok"
            ok += 1
            print(f"{prefix}  ✓ stage→other  redis={close_result.get('new_status', '—')}")
        except Exception as e:
            c.apply_status = "error"
            c.apply_error = str(e)[:200]
            err += 1
            print(f"{prefix}  ✖ {e}", file=sys.stderr)

        await asyncio.sleep(PATCH_PAUSE_S)

    return ok, err


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30, help="Alias de --min-days (mantenido por compat). Default 30.")
    parser.add_argument("--min-days", type=int, default=None, help="Días mínimos desde el último mensaje (más reciente excluido). Default = --days.")
    parser.add_argument("--max-days", type=int, default=None, help="Días máximos desde el último mensaje (más viejo excluido). Default = sin tope.")
    parser.add_argument("--last-sender-advisor", action="store_true", help="Exigir que el último mensaje sea de la asesora (modo follow-up).")
    parser.add_argument("--apply", action="store_true", help="Ejecutar la migración (default: dry-run)")
    parser.add_argument("--yes", action="store_true", help="Saltar la confirmación interactiva")
    parser.add_argument("--limit", type=int, default=0, help="Limitar a N candidatos (debug)")
    parser.add_argument("--skip-pool-b", action="store_true", help="Omitir búsqueda en HubSpot (solo Mongo)")
    parser.add_argument("--mongo-uri", dest="mongo_uri_arg", default=None, help="URI de MongoDB (sobreescribe .env — útil localmente con la URL pública de Railway)")
    args = parser.parse_args()

    mongo_uri = args.mongo_uri_arg or (
        os.getenv("MONGO_PUBLIC_URL") or
        os.getenv("MONGODB_PUBLIC_URL") or
        os.getenv("MONGO_URL") or
        os.getenv("MONGODB_URL")
    )
    hs_token = os.getenv("HUBSPOT_API_KEY")
    if not mongo_uri:
        print("ERROR: MONGO_URL no configurada en .env. Usa --mongo-uri <url>", file=sys.stderr)
        return 2
    if not hs_token:
        print("ERROR: HUBSPOT_API_KEY no configurada en .env", file=sys.stderr)
        return 2

    min_days = args.min_days if args.min_days is not None else args.days
    max_days = args.max_days  # None = sin tope
    now = datetime.now(timezone.utc)
    cutoff_recent = now - timedelta(days=min_days)
    cutoff_old = now - timedelta(days=max_days) if max_days is not None else None

    mode_label = "ADVISOR_LAST" if args.last_sender_advisor else "CLIENT_SILENT"
    window_label = (
        f">{min_days}d y <{max_days}d"
        if max_days is not None
        else f">{min_days}d"
    )
    print(f"\n▶ Cleanup 'No Responde' — modo {mode_label} — ventana {window_label}")
    print(f"  cutoff_recent = {fmt_dt(cutoff_recent)} UTC", end="")
    if cutoff_old is not None:
        print(f"   cutoff_old = {fmt_dt(cutoff_old)} UTC")
    else:
        print()
    print(f"  Modo: {'APPLY' if args.apply else 'DRY-RUN'}")

    headers = {"Authorization": f"Bearer {hs_token}", "Content-Type": "application/json"}

    mongo = AsyncIOMotorClient(mongo_uri)
    db = mongo.get_database("inmobiliaria_chat")

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        # Fase A.1 — Pool A en Mongo
        print("\n[A.1] Aggregation Mongo (Pool A)…")
        pool_a = await discover_pool_a(
            db,
            cutoff_recent=cutoff_recent,
            cutoff_old=cutoff_old,
            require_advisor_last=args.last_sender_advisor,
        )
        print(f"      candidatos brutos: {len(pool_a)}")

        # Fase A.3 — Hidratar y validar stage HubSpot
        print("[A.3] Hidratando Pool A contra HubSpot batch/read…")
        pool_a_valid, pool_a_discarded = await hydrate_pool_a(client, headers, pool_a)
        print(f"      válidos en stage elegible: {len(pool_a_valid)}")
        print(f"      descartados (stage ya cambiado): {len(pool_a_discarded)}")

        # Fase A.2 — Pool B (no aplica en modo ADVISOR_LAST por definición)
        pool_b: List[Candidate] = []
        if args.last_sender_advisor:
            print("[A.2] Pool B omitido en modo ADVISOR_LAST (no hay msgs en Mongo).")
        elif not args.skip_pool_b:
            print("[A.2] Search HubSpot (Pool B huérfano)…")
            phones_in_mongo = await get_phones_in_mongo(db)
            pool_b = await discover_pool_b(client, headers, cutoff_recent, phones_in_mongo)
            print(f"      candidatos huérfanos: {len(pool_b)}")

        candidates = pool_a_valid + pool_b
        if args.limit > 0:
            candidates = candidates[: args.limit]

        # Reporte
        print_report(candidates, pool_a_discarded)
        csv_path = write_csv(candidates, pool_a_discarded)
        print(f"\n  📄 CSV escrito: {csv_path.resolve()}")

        if not candidates:
            return 0

        if not args.apply:
            print("\n  Dry-run — no se hicieron cambios. Re-ejecuta con --apply para migrar.\n")
            return 0

        # Fase B — selección interactiva (cherry-pick) o confirmación bulk
        if args.yes:
            to_apply = candidates
            print(f"\n  ⚠ --yes: aplicando a los {len(candidates)} candidatos sin selección manual.")
        else:
            to_apply = interactive_select(candidates)
            if not to_apply:
                print("  Sin selección — no se aplicaron cambios.")
                return 0

        # Marcar los NO seleccionados con apply_status para auditoría
        applied_ids = {id(c) for c in to_apply}
        for c in candidates:
            if id(c) not in applied_ids:
                c.apply_status = "skipped_user_unselected"

        # Fase C — ejecutar
        print(f"\n[C] Aplicando cambios sobre {len(to_apply)} contactos…")
        ok, err = await apply_changes(to_apply, client, headers)

        # CSV final con resultados
        csv_path = write_csv(candidates, pool_a_discarded)
        print(f"\n  Resumen: ✓ {ok} ok    ✖ {err} errores")
        print(f"  📄 CSV final: {csv_path.resolve()}\n")
        return 0 if err == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrumpido.", file=sys.stderr)
        sys.exit(130)
