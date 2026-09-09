"""
scripts/auditoria/audit_visibility.py
===========================

Auditoría de integridad de la visibilidad del panel sobre mensajes fantasmas— SOLO LECTURA.

Convierte en un número la sospecha de que hay contactos con mensaje pendiente
del cliente que las asesoras nunca ven en la barra del panel.

    Verdad de campo  = MongoDB `conversations` con last_message_sender="client"
    Visibilidad real = lo que devuelve GET /whatsapp/panel/contacts

Todo contacto pendiente que no aparezca en la respuesta del panel se clasifica
por causa (ver TAXONOMÍA abajo), leyendo Redis y MongoDB.

Este script NO corrige nada. No existe un modo --execute. Corregir es otro
trabajo; aquí sólo se mide.

Uso:
    railway run python scripts/auditoria/audit_visibility.py
    railway run python scripts/auditoria/audit_visibility.py --hours 168 --json salida.json

Requiere en el entorno:
    REDIS_URL / REDIS_PUBLIC_URL
    MONGODB_URI (o las variantes que resuelve database.mongodb_client)
    ADMIN_API_KEY (o PANEL_API_KEY)
    PANEL_BASE_URL  — p.ej. https://<app>.up.railway.app  (o --base-url)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv

load_dotenv()


# ──────────────────────────────────────────────────────────────────────────────
# TAXONOMÍA DE CAUSAS
# ──────────────────────────────────────────────────────────────────────────────
# Cada causa apunta a un punto concreto del código que puede dejar un contacto
# fuera de la vista de su asesora.

CAUSE_META_MISSING = 1
CAUSE_NOT_IN_PANEL = 2
CAUSE_BOT_SET_NO_META = 3
CAUSE_BEYOND_SCAN_CAP = 4
CAUSE_OWNER_MISMATCH = 5
CAUSE_CANAL_MISMATCH = 6
CAUSE_PANEL_CUT = 7

CAUSE_LABELS: Dict[int, str] = {
    CAUSE_META_MISSING: "conv_meta ausente",
    CAUSE_NOT_IN_PANEL: "in_panel=False",
    CAUSE_BOT_SET_NO_META: "en BOT_CONTROLLED_SET sin meta",
    CAUSE_BEYOND_SCAN_CAP: "fuera del tope de escaneo del ZSET",
    CAUSE_OWNER_MISMATCH: "owner_id ausente o distinto",
    CAUSE_CANAL_MISMATCH: "desajuste de canal",
    CAUSE_PANEL_CUT: "fuera del corte del panel (no leídos + 30 más recientes)",
}

CAUSE_ORIGINS: Dict[int, str] = {
    CAUSE_META_MISSING: (
        "conversation_state.py:1512 — update_client_message_timestamp solo hace "
        "zadd dentro de 'if data:'; sin meta el mensaje del cliente no entra al ZSET"
    ),
    CAUSE_NOT_IN_PANEL: (
        "conversation_state.py:1529 — contacto cerrado que vuelve a escribir; "
        "solo add_to_advisor_inbox lo devuelve al ZSET"
    ),
    CAUSE_BOT_SET_NO_META: (
        "conversation_state.py:456 — el bucle de BOT_CONTROLLED_SET hace continue "
        "sin meta, a diferencia del ZSET que sí rehidrata"
    ),
    CAUSE_BEYOND_SCAN_CAP: (
        "outbound_panel.py:5252 — se escanean 300 miembros del ZSET y DESPUES se "
        "filtra por asesora"
    ),
    CAUSE_OWNER_MISMATCH: (
        "outbound_panel.py:5253 — ni conv_meta.assigned_owner_id ni "
        "conversations.owner_id coinciden con la asesora"
    ),
    CAUSE_CANAL_MISMATCH: (
        "el miembro del ZSET es phone:canal; el canal del mensaje difiere del "
        "almacenado y la meta no se encuentra"
    ),
    CAUSE_PANEL_CUT: (
        "outbound_panel.py:5304 — el panel pide filter_time=all sin limit ni page "
        "(index.js:1381), así que recibe los no leídos + los 30 más recientes. "
        "El resto solo se alcanza ampliando el corte, cosa que la UI no hace."
    ),
}

# El orden de la tabla del plan es didáctico, no operativo. La causa 3 subsume a
# la 1 (estar en BOT_CONTROLLED_SET sin meta implica no tener meta) y es más
# específica, así que gana. Igual la 2, que descarta a la 1 por construcción.
# Se reportan TODAS las causas aplicables; esta lista solo decide cuál encabeza.
CAUSE_PRECEDENCE: Tuple[int, ...] = (
    CAUSE_BOT_SET_NO_META,
    CAUSE_NOT_IN_PANEL,
    CAUSE_META_MISSING,
    CAUSE_CANAL_MISMATCH,
    CAUSE_OWNER_MISMATCH,
    CAUSE_BEYOND_SCAN_CAP,
)

# Tope que aplica outbound_panel.py:5252 antes de filtrar por asesora.
ZSET_SCAN_CAP = 300

# Asesora ficticia para los pendientes cuyo dueño no se puede resolver: no
# pertenecen a la bandeja de nadie, que es exactamente el problema.
NO_OWNER = "SIN_OWNER"

STATUS_VISIBLE = "visible"
STATUS_CUT_ONLY = "fuera_del_corte"
STATUS_INVISIBLE = "invisible"
STATUS_UNMEASURED = "no_medido"
STATUS_CLOSED_OK = "cerrada_a_proposito"


# ──────────────────────────────────────────────────────────────────────────────
# MODELO (puro, sin IO — es lo que cubren los tests)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Pending:
    """Un contacto con mensaje del cliente sin responder, según MongoDB."""

    phone: str
    canal: str
    owner_id_mongo: Optional[str] = None
    contact_id: Optional[str] = None
    last_message_at: Optional[datetime] = None
    archived: bool = False
    preview: str = ""
    # Solo se rellenan para las archivadas — ver _resolver_embudos()
    stage_id: str = ""
    stage_nombre: str = ""
    cierre_incoherente: bool = False


@dataclass(frozen=True)
class RedisFacts:
    """Lo que Redis sabe del contacto en el momento de la auditoría."""

    meta: Optional[Dict[str, Any]] = None
    state: Optional[str] = None
    zset_score: Optional[float] = None
    zset_rank: Optional[int] = None          # 0 = más reciente (orden zrevrange)
    in_bot_set: bool = False
    zset_canales: Tuple[str, ...] = ()       # canales presentes en el ZSET para ese phone
    inbox_of: Tuple[str, ...] = ()           # asesoras que lo tienen como no-leído


@dataclass
class Verdict:
    status: str
    causes: Tuple[int, ...] = ()
    primary: Optional[int] = None
    detail: Dict[str, Any] = field(default_factory=dict)


def _owner_por_canal(canal: str) -> Optional[str]:
    """Dueño del canal segun el registro. None si el canal no esta mapeado."""
    if not canal:
        return None
    try:
        from utils.channels_registry import get_owner_for_channel

        return get_owner_for_channel(canal)
    except Exception:
        return None


def resolve_owner(pending: Pending, facts: RedisFacts) -> Optional[str]:
    """
    Dueño efectivo del contacto, en el mismo orden que usa el panel al construir
    la lista: Redis manda sobre MongoDB, y si ninguno lo tiene se atribuye por
    canal — como hace get_archived_conversations_from_mongo desde el 11-ago-2026.

    Sin este tercer escalon la auditoria mandaria al saco de SIN_OWNER contactos
    que el panel SI muestra, y contaria como invisible algo perfectamente visible.
    """
    meta_owner = (facts.meta or {}).get("assigned_owner_id")
    return meta_owner or pending.owner_id_mongo or _owner_por_canal(pending.canal)


def classify(
    pending: Pending,
    facts: RedisFacts,
    advisor_id: str,
    visible_real: bool,
    visible_techo: bool,
    measured: bool = True,
    scan_cap: int = ZSET_SCAN_CAP,
) -> Verdict:
    """
    Decide si un contacto pendiente es visible y, si no, por qué.

    `visible_real`  — salió en la petición que hace el panel de verdad
                      (filter_time=all, sin limit ni page: index.js:1381).
    `visible_techo` — salió al pedir lo mismo con el corte ampliado y paginando,
                      o sea todo lo que la asesora podría alcanzar si la UI se
                      lo permitiera.

    Función pura: no toca red ni disco. Es el corazón auditable del script.
    """
    if not measured:
        return Verdict(status=STATUS_UNMEASURED)

    if visible_real:
        return Verdict(status=STATUS_VISIBLE)

    # Una conversación cerrada NO es un contacto perdido: es una decisión de la
    # asesora, y decide el EMBUDO. Cerrada en cualquier etapa menos
    # "En conversación" es un desenlace legítimo y no cuenta como invisible.
    #
    # Sin esto el indicador empeora cuando el equipo hace su trabajo: el
    # 12-ago-2026 subió de 2 a 11 en una tarde mientras las asesoras cerraban
    # conversaciones correctamente.
    if pending.archived and not pending.cierre_incoherente:
        return Verdict(
            status=STATUS_CLOSED_OK,
            detail={"embudo": pending.stage_nombre},
        )

    if visible_techo:
        # El dato existe y el pipeline lo construye; lo que lo esconde es el
        # corte de la respuesta. La asesora no tiene forma de llegar a él.
        return Verdict(
            status=STATUS_CUT_ONLY,
            causes=(CAUSE_PANEL_CUT,),
            primary=CAUSE_PANEL_CUT,
            detail={"state": facts.state, "en_inbox_de": list(facts.inbox_of)},
        )

    causes: List[int] = []
    detail: Dict[str, Any] = {}

    meta = facts.meta

    if meta is None:
        causes.append(CAUSE_META_MISSING)
        if facts.in_bot_set:
            causes.append(CAUSE_BOT_SET_NO_META)
    elif meta.get("in_panel", True) is False:
        causes.append(CAUSE_NOT_IN_PANEL)

    if facts.zset_rank is not None and facts.zset_rank >= scan_cap:
        causes.append(CAUSE_BEYOND_SCAN_CAP)
        detail["zset_rank"] = facts.zset_rank

    effective_owner = resolve_owner(pending, facts)
    if effective_owner != advisor_id:
        causes.append(CAUSE_OWNER_MISMATCH)
        detail["owner_meta"] = (meta or {}).get("assigned_owner_id")
        detail["owner_mongo"] = pending.owner_id_mongo
        detail["owner_esperado"] = advisor_id

    otros_canales = tuple(c for c in facts.zset_canales if c != pending.canal)
    if otros_canales and facts.zset_score is None:
        # Está en el ZSET, pero bajo otro canal: el miembro que el panel busca
        # para este documento de MongoDB no existe.
        causes.append(CAUSE_CANAL_MISMATCH)
        detail["canal_mongo"] = pending.canal
        detail["canales_zset"] = list(otros_canales)

    ordered = tuple(c for c in CAUSE_PRECEDENCE if c in causes)
    # Cualquier causa que no esté en la precedencia se conserva al final.
    ordered = ordered + tuple(c for c in causes if c not in ordered)

    detail["state"] = facts.state
    detail["en_zset"] = facts.zset_score is not None
    detail["en_bot_set"] = facts.in_bot_set
    detail["en_inbox_de"] = list(facts.inbox_of)

    return Verdict(
        status=STATUS_INVISIBLE,
        causes=ordered,
        primary=ordered[0] if ordered else None,
        detail=detail,
    )


def order_drift_seconds(pending: Pending, facts: RedisFacts) -> Optional[float]:
    """
    Desfase entre el score del ZSET (que fija el orden de la lista) y el último
    mensaje real.

    Positivo = el contacto está más arriba de lo que le corresponde. Algo que NO
    fue un mensaje le puso el score a "ahora": toma de control, recuperación
    administrativa, cambio de etapa (conversation_state.py:770, 863, 1311, 1604).
    Un contacto que salta a la cabeza días después de su último mensaje real es
    justo lo que se percibe como "apareció días después".

    Ojo: esto NO mide rebuild_zset_from_conversations (app.py:616). Ese job
    re-inserta con el timestamp REAL del mensaje (app.py:663), así que su huella
    es un desfase de cero, indistinguible del caso normal. Lo que resucita se
    puede ver en su log, no aquí.

    Negativo = el ZSET quedó atrás respecto de MongoDB: llegaron mensajes que no
    reordenaron la lista.
    """
    if facts.zset_score is None or pending.last_message_at is None:
        return None
    return facts.zset_score - _epoch(pending.last_message_at)


LAG_BUCKETS: Sequence[Tuple[str, float]] = (
    ("< 1 min", 60),
    ("1 min - 1 h", 3600),
    ("1 h - 24 h", 86400),
    ("> 24 h", math.inf),
)


def bucket_lag(seconds: float) -> str:
    if seconds < 0:
        return "score anterior al mensaje"
    for label, upper in LAG_BUCKETS:
        if seconds < upper:
            return label
    return LAG_BUCKETS[-1][0]


def _epoch(dt: datetime) -> float:
    """Epoch de un datetime, asumiendo UTC si viene sin tz (así lo devuelve Motor)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _norm_phone(raw: Optional[str]) -> str:
    """
    Normaliza con el normalizador del proyecto; si el número no es válido se
    conserva tal cual para no perder el registro de la auditoría.
    """
    if not raw:
        return ""
    try:
        from middleware.phone_normalizer import normalize_phone

        return normalize_phone(raw)
    except Exception:
        return str(raw).strip()


# ──────────────────────────────────────────────────────────────────────────────
# REDIS DE SOLO LECTURA
# ──────────────────────────────────────────────────────────────────────────────


class ReadOnlyRedis:
    """
    Envoltorio que solo expone comandos de lectura.

    No es decoración: cualquier intento de escribir (zadd, set, zrem, expire,
    delete...) revienta con AttributeError en vez de tocar producción. La
    garantía de "solo lectura" del script es estructural, no una promesa del
    comentario de cabecera.
    """

    _ALLOWED = frozenset(
        {"get", "mget", "zrange", "zrevrange", "zscore", "zrevrank",
         "zcard", "smembers", "sismember", "ping", "pipeline", "aclose"}
    )

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name: str):
        if name not in self._ALLOWED:
            raise AttributeError(
                f"ReadOnlyRedis: '{name}' no permitido — este script es de solo lectura"
            )
        attr = getattr(self._client, name)
        if name == "pipeline":
            return lambda *a, **kw: ReadOnlyPipeline(attr(*a, **kw))
        return attr


class ReadOnlyPipeline:
    """Pipeline con la misma restricción, encadenable como el original."""

    _ALLOWED = frozenset({"get", "zscore", "zrevrank", "sismember", "zrange", "execute"})

    def __init__(self, pipe):
        self._pipe = pipe

    def __getattr__(self, name: str):
        if name not in self._ALLOWED:
            raise AttributeError(
                f"ReadOnlyPipeline: '{name}' no permitido — este script es de solo lectura"
            )
        return getattr(self._pipe, name)


# ──────────────────────────────────────────────────────────────────────────────
# ETAPA 1 — ASESORAS, RESUELTAS DESDE DATOS
# ──────────────────────────────────────────────────────────────────────────────


async def resolve_advisors(mongo_db) -> Dict[str, str]:
    """
    {advisor_id: nombre} desde la colección `panel_advisors`.

    Lectura directa en vez de MongoManager.get_advisors(), porque ese método
    llama a init_advisors() que hace upsert — una escritura disfrazada de
    lectura. Si la colección está vacía se cae a OWNERS_CONFIG de
    lead_assigner, que es una constante en memoria.

    Cero nombres propios en este archivo.
    """
    advisors: Dict[str, str] = {}
    try:
        cursor = mongo_db.panel_advisors.find({"active": True})
        async for doc in cursor:
            aid = doc.get("advisor_id")
            if aid:
                advisors[str(aid)] = doc.get("name") or str(aid)
    except Exception as e:
        print(f"  AVISO: no se pudo leer panel_advisors ({e})")

    if advisors:
        return advisors

    print("  panel_advisors vacía — usando lead_assigner.OWNERS_CONFIG")
    try:
        from integrations.hubspot.lead_assigner import LeadAssigner

        for members in LeadAssigner.OWNERS_CONFIG.values():
            for m in members:
                advisors[str(m["id"])] = m.get("name") or str(m["id"])
    except Exception as e:
        print(f"  ERROR: tampoco se pudo leer OWNERS_CONFIG ({e})")

    return advisors


# ──────────────────────────────────────────────────────────────────────────────
# ETAPA 2 — VERDAD DE CAMPO
# ──────────────────────────────────────────────────────────────────────────────


async def fetch_pending(mongo_db, cutoff: datetime, cap: int) -> Tuple[List[Pending], bool]:
    """
    Conversaciones cuyo último mensaje es del cliente dentro de la ventana.

    A diferencia de recover_unanswered.py NO se filtra por `archived` ni se
    exigen `contact_id`: un contacto cerrado que vuelve a escribir, o uno cuyo
    contacto de HubSpot nunca se creó, son justo los casos que se pierden.

    Devuelve (pendientes, se_alcanzo_el_tope). El tope nunca es silencioso.
    """
    query = {
        "last_message_sender": "client",
        "last_message_at": {"$gte": cutoff},
    }
    projection = {
        "phone": 1, "canal": 1, "owner_id": 1, "contact_id": 1,
        "last_message_at": 1, "last_message_preview": 1, "archived": 1, "_id": 0,
    }

    docs = await (
        mongo_db.conversations.find(query, projection)
        .sort("last_message_at", -1)
        .to_list(length=cap + 1)
    )
    truncated = len(docs) > cap
    docs = docs[:cap]

    pendings: List[Pending] = []
    for d in docs:
        phone = _norm_phone(d.get("phone"))
        if not phone:
            continue
        pendings.append(
            Pending(
                phone=phone,
                canal=(d.get("canal") or "whatsapp").lower(),
                owner_id_mongo=str(d["owner_id"]) if d.get("owner_id") else None,
                contact_id=d.get("contact_id"),
                last_message_at=d.get("last_message_at"),
                archived=bool(d.get("archived")),
                preview=(d.get("last_message_preview") or "")[:50],
            )
        )
    return pendings, truncated


async def resolver_embudos(http, pendings: Sequence[Pending]) -> List[Pending]:
    """
    Rellena el embudo de las conversaciones ARCHIVADAS.

    Cerrar es una decision de la asesora, y decide el embudo: cerrada en
    cualquier etapa menos "En conversacion" es un desenlace legitimo. Solo se
    consulta HubSpot para las archivadas —una treintena—, no para todas.

    Si no se puede leer el embudo, `cierre_incoherente` queda en False: ante la
    duda se respeta el cierre en vez de contarlo como fallo.
    """
    from dataclasses import replace

    try:
        from middleware.outbound_panel import (
            HUBSPOT_STAGE_EN_CONVERSACION, PIPELINE_STAGES,
        )
    except Exception as e:
        print(f"  AVISO: no se pudo leer el pipeline ({e}) — los cierres no se clasifican")
        return list(pendings)

    token = os.getenv("HUBSPOT_ACCESS_TOKEN") or os.getenv("HUBSPOT_API_KEY")
    archivadas = [p for p in pendings if p.archived]
    if not archivadas or not token:
        return list(pendings)

    print(f"        {len(archivadas)} archivadas — consultando su embudo…")
    por_phone: Dict[str, Tuple[str, str]] = {}
    for p in archivadas:
        if not p.contact_id:
            continue
        try:
            r = await http.get(
                f"https://api.hubapi.com/crm/v3/objects/contacts/{p.contact_id}",
                params={"properties": "lifecyclestage"},
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 200:
                raw = r.json().get("properties", {}).get("lifecyclestage") or ""
                por_phone[p.phone] = (raw, PIPELINE_STAGES.get(raw, raw or "(vacio)"))
        except Exception:
            pass
        await asyncio.sleep(0.1)

    salida = []
    for p in pendings:
        if not p.archived:
            salida.append(p)
            continue
        stage_id, nombre = por_phone.get(p.phone, ("", "(sin leer)"))
        salida.append(replace(
            p, stage_id=stage_id, stage_nombre=nombre,
            cierre_incoherente=(stage_id == HUBSPOT_STAGE_EN_CONVERSACION),
        ))
    return salida


# ──────────────────────────────────────────────────────────────────────────────
# ETAPA 3 — VISIBILIDAD REAL (se le pregunta al panel)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class AdvisorView:
    advisor_id: str
    measured: bool = False
    visible_real: Set[str] = field(default_factory=set)   # lo que pide index.js
    visible_techo: Set[str] = field(default_factory=set)  # el techo alcanzable
    total_count: int = 0
    error: Optional[str] = None


async def _panel_get(
    http, base_url: str, api_key: str, params: Dict[str, Any], attempts: int = 3
) -> Optional[Dict[str, Any]]:
    """GET a /contacts con respeto por Retry-After. None si no se pudo medir."""
    url = f"{base_url}/whatsapp/panel/contacts"
    for attempt in range(1, attempts + 1):
        try:
            resp = await http.get(url, params=params, headers={"X-API-Key": api_key})
        except Exception as e:
            if attempt == attempts:
                print(f"    fallo de red tras {attempts} intentos: {e}")
                return None
            await asyncio.sleep(2.0 * attempt)
            continue

        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 502, 503, 504):
            wait = float(resp.headers.get("Retry-After", 5 * attempt))
            print(f"    {resp.status_code} — esperando {wait:.0f}s")
            await asyncio.sleep(wait)
            continue
        print(f"    HTTP {resp.status_code} inesperado")
        return None
    return None


async def fetch_advisor_view(
    http,
    base_url: str,
    api_key: str,
    advisor_id: str,
    page_limit: int,
    max_pages: int,
    pause: float,
) -> AdvisorView:
    """
    Une lo que el panel devuelve para una asesora en las dos configuraciones
    que importan.

    1. La petición REAL: `filter_time=all&advisor=X`, sin `limit` ni `page`,
       exactamente lo que construye index.js:1381. El backend aplica sus
       defaults (limit=30, page=1) y devuelve los no leídos + los 30 más
       recientes. Es lo que la asesora ve, ni un contacto más.
    2. El TECHO: lo mismo con el corte ampliado y paginando, para separar
       "el dato no existe" de "el dato existe pero la respuesta lo corta".

    Si cualquier petición falla, la asesora queda SIN MEDIR. Nunca se traduce
    un fallo de red en "contacto invisible".
    """
    view = AdvisorView(advisor_id=advisor_id)

    data = await _panel_get(
        http, base_url, api_key,
        {"advisor": advisor_id, "filter_time": "all"},
    )
    if data is None:
        view.error = "fallo en la consulta real del panel"
        return view
    view.visible_real = {_norm_phone(c.get("phone")) for c in data.get("contacts", [])}
    view.visible_real.discard("")

    page = 1
    while page <= max_pages:
        await asyncio.sleep(pause)
        data = await _panel_get(
            http, base_url, api_key,
            {"advisor": advisor_id, "filter_time": "all",
             "limit": page_limit, "page": page},
        )
        if data is None:
            view.error = f"fallo en la página {page} del techo"
            return view

        contacts = data.get("contacts", [])
        view.total_count = max(view.total_count, int(data.get("total_count") or 0))
        for c in contacts:
            p = _norm_phone(c.get("phone"))
            if p:
                view.visible_techo.add(p)

        if not contacts or (view.total_count and page * page_limit >= view.total_count):
            break
        page += 1

    if page > max_pages:
        print(f"    AVISO: tope de {max_pages} páginas alcanzado para {advisor_id}")

    # Lo real es un subconjunto conceptual del techo; si algo salió en la
    # petición real y no en el techo (carrera con el cache de 20s), se cuenta
    # como alcanzable igual.
    view.visible_techo |= view.visible_real
    view.measured = True
    return view


# ──────────────────────────────────────────────────────────────────────────────
# ETAPA 4 — HECHOS DE REDIS
# ──────────────────────────────────────────────────────────────────────────────


async def fetch_redis_facts(
    r: ReadOnlyRedis,
    pendings: Sequence[Pending],
    advisor_ids: Sequence[str],
    batch: int = 200,
) -> Dict[Tuple[str, str], RedisFacts]:
    """
    Un retrato de Redis para cada pendiente.

    El ZSET completo, el SET de bot y las bandejas se leen una sola vez; el
    rango y los canales alternos se calculan en memoria. Por contacto solo
    quedan dos GET, en lotes.
    """
    from middleware.conversation_state import ConversationStateManager as CSM

    zset_raw = await r.zrevrange(CSM.ACTIVE_CONTACTS_ZSET, 0, -1, withscores=True)
    zset_rank: Dict[str, int] = {}
    zset_score: Dict[str, float] = {}
    canales_por_phone: Dict[str, List[str]] = {}
    for rank, (member, score) in enumerate(zset_raw):
        member = _decode(member)
        zset_rank[member] = rank
        zset_score[member] = float(score)
        if ":" in member:
            ph, cn = member.split(":", 1)
            canales_por_phone.setdefault(ph, []).append(cn.lower())

    bot_set = {_decode(m) for m in await r.smembers(CSM.BOT_CONTROLLED_SET)}

    inbox_por_member: Dict[str, List[str]] = {}
    for aid in advisor_ids:
        try:
            members = await r.zrange(f"{CSM.ADVISOR_INBOX_PREFIX}{aid}", 0, -1)
        except Exception:
            continue
        for m in members:
            inbox_por_member.setdefault(_decode(m), []).append(aid)

    facts: Dict[Tuple[str, str], RedisFacts] = {}
    for start in range(0, len(pendings), batch):
        chunk = pendings[start:start + batch]
        pipe = r.pipeline(transaction=False)
        for p in chunk:
            pipe.get(f"{CSM.META_PREFIX}{p.phone}:{p.canal}")
            pipe.get(f"{CSM.STATE_PREFIX}{p.phone}:{p.canal}")
        results = await pipe.execute()

        for i, p in enumerate(chunk):
            meta_raw = _decode(results[i * 2])
            state_raw = _decode(results[i * 2 + 1])
            member = f"{p.phone}:{p.canal}"

            meta: Optional[Dict[str, Any]] = None
            if meta_raw:
                try:
                    meta = json.loads(meta_raw)
                except Exception:
                    meta = {}

            facts[(p.phone, p.canal)] = RedisFacts(
                meta=meta,
                state=state_raw or None,
                zset_score=zset_score.get(member),
                zset_rank=zset_rank.get(member),
                in_bot_set=member in bot_set,
                zset_canales=tuple(canales_por_phone.get(p.phone, ())),
                inbox_of=tuple(inbox_por_member.get(member, ())),
            )

    return facts


def _decode(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return value


# ──────────────────────────────────────────────────────────────────────────────
# ETAPA 5 — REPORTE
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class Row:
    pending: Pending
    advisor_id: str
    verdict: Verdict
    lag: Optional[float] = None


def build_rows(
    pendings: Sequence[Pending],
    facts: Optional[Dict[Tuple[str, str], RedisFacts]],
    views: Dict[str, AdvisorView],
) -> List[Row]:
    """
    `facts=None` significa que Redis no estaba disponible: se mide la
    visibilidad igual, pero no se inventa una causa. Un RedisFacts() vacío se
    leería como "meta ausente" y llenaría el informe de causa 1 falsa.
    """
    sin_redis = facts is None
    rows: List[Row] = []
    for p in pendings:
        f = (facts or {}).get((p.phone, p.canal), RedisFacts())
        advisor_id = resolve_owner(p, f) or NO_OWNER
        view = views.get(advisor_id)

        if view is None or not view.measured:
            # Sin dueño resoluble no hay bandeja donde buscarlo: es invisible
            # por definición, y la causa es el owner. Si el dueño existe pero su
            # medición falló, queda sin medir.
            if advisor_id == NO_OWNER:
                verdict = classify(p, f, advisor_id, False, False)
            else:
                verdict = classify(p, f, advisor_id, False, False, measured=False)
        else:
            verdict = classify(
                p, f, advisor_id,
                visible_real=p.phone in view.visible_real,
                visible_techo=p.phone in view.visible_techo,
            )

        if sin_redis and verdict.status == STATUS_INVISIBLE:
            verdict = Verdict(status=STATUS_INVISIBLE, detail={"sin_redis": True})

        rows.append(Row(p, advisor_id, verdict, order_drift_seconds(p, f)))
    return rows


def print_report(
    rows: Sequence[Row],
    advisors: Dict[str, str],
    views: Dict[str, AdvisorView],
    hours: int,
    truncated: bool,
) -> None:
    print("\n" + "=" * 92)
    print(f"  AUDITORÍA DE VISIBILIDAD DEL PANEL — ventana de {hours}h — SOLO LECTURA")
    print("=" * 92)

    if truncated:
        print("\n  AVISO: se alcanzó el tope de conversaciones leídas. El número"
              " está subestimado — sube --cap.")

    by_advisor: Dict[str, List[Row]] = {}
    for row in rows:
        by_advisor.setdefault(row.advisor_id, []).append(row)

    print(f"\n  {'ASESORA':26} {'pendientes':>11} {'visibles':>9} "
          f"{'invisibles':>11} {'cortados':>9} {'cerradas':>9} {'sin medir':>10}")
    print(f"  {'-' * 96}")

    for advisor_id in sorted(by_advisor, key=lambda a: advisors.get(a, a)):
        items = by_advisor[advisor_id]
        name = advisors.get(advisor_id, advisor_id)
        tag = f"{name} (…{advisor_id[-3:]})" if advisor_id != NO_OWNER else NO_OWNER
        counts = _count_status(items)
        print(
            f"  {tag[:26]:26} {len(items):>11} {counts[STATUS_VISIBLE]:>9} "
            f"{counts[STATUS_INVISIBLE]:>11} {counts[STATUS_CUT_ONLY]:>9} "
            f"{counts[STATUS_CLOSED_OK]:>9} {counts[STATUS_UNMEASURED]:>10}"
        )

    for advisor_id, view in views.items():
        if not view.measured:
            print(f"\n  SIN MEDIR — {advisors.get(advisor_id, advisor_id)}: {view.error}")

    invisibles = [r for r in rows if r.verdict.status == STATUS_INVISIBLE]

    print(f"\n  {'-' * 86}")
    print(f"  INVISIBLES POR CAUSA PRINCIPAL — {len(invisibles)} en total")
    if not invisibles:
        print("\n    Ninguno. No hay evidencia de fallo de visibilidad en esta corrida.")
    else:
        tally: Dict[Optional[int], int] = {}
        for r in invisibles:
            tally[r.verdict.primary] = tally.get(r.verdict.primary, 0) + 1
        for cause, count in sorted(tally.items(), key=lambda kv: -kv[1]):
            label = CAUSE_LABELS.get(cause, "sin causa identificada")
            print(f"    {count:>4}x  [{cause}] {label}")
            if cause in CAUSE_ORIGINS:
                print(f"           {CAUSE_ORIGINS[cause]}")

        print(f"\n  {'-' * 86}")
        print("  DETALLE DE INVISIBLES")
        print(f"    {'Telefono':17} {'Canal':14} {'Asesora':12} {'Hace':>6}  Causas")
        # Orden por epoch, no por datetime: Motor devuelve naive y el fallback
        # datetime.min es naive — mezclarlos con uno aware revienta la comparación.
        for r in sorted(
            invisibles,
            key=lambda x: _epoch(x.pending.last_message_at) if x.pending.last_message_at else 0.0,
            reverse=True,
        ):
            causes = ",".join(str(c) for c in r.verdict.causes) or "-"
            print(
                f"    {r.pending.phone:17} {r.pending.canal[:14]:14} "
                f"{advisors.get(r.advisor_id, r.advisor_id)[:12]:12} "
                f"{_ago(r.pending.last_message_at):>6}  {causes}"
            )

    cortados = [r for r in rows if r.verdict.status == STATUS_CUT_ONLY]
    if cortados:
        print(f"\n  {'-' * 86}")
        print(f"  CORTADOS POR EL LÍMITE DE LA RESPUESTA — {len(cortados)}")
        print(f"  {CAUSE_ORIGINS[CAUSE_PANEL_CUT]}")
        print(f"\n    {'Telefono':17} {'Canal':14} {'Asesora':12} {'Hace':>6}  En inbox")
        for r in sorted(
            cortados,
            key=lambda x: _epoch(x.pending.last_message_at) if x.pending.last_message_at else 0.0,
            reverse=True,
        ):
            en_inbox = "sí" if r.verdict.detail.get("en_inbox_de") else "NO"
            print(
                f"    {r.pending.phone:17} {r.pending.canal[:14]:14} "
                f"{advisors.get(r.advisor_id, r.advisor_id)[:12]:12} "
                f"{_ago(r.pending.last_message_at):>6}  {en_inbox}"
            )

    print(f"\n  {'-' * 86}")
    print("  DESFASE DE ORDEN (score del ZSET − último mensaje real)")
    lag_tally: Dict[str, int] = {}
    for r in rows:
        if r.lag is None:
            continue
        lag_tally[bucket_lag(r.lag)] = lag_tally.get(bucket_lag(r.lag), 0) + 1
    if not lag_tally:
        print("    Sin datos comparables.")
    else:
        for label, _ in LAG_BUCKETS:
            if label in lag_tally:
                marca = (
                    "  ← subió a la cabeza sin mensaje nuevo"
                    if label == "> 24 h" else ""
                )
                print(f"    {label:14} {lag_tally[label]:>6}{marca}")
        if "score anterior al mensaje" in lag_tally:
            print(f"    {'score < mensaje':14} {lag_tally['score anterior al mensaje']:>6}"
                  f"  ← llegaron mensajes que no reordenaron la lista")
        print("\n    Nota: rebuild_zset_from_conversations (app.py:616) re-inserta con el")
        print("    timestamp real del mensaje, así que su huella es desfase cero. Este")
        print("    histograma NO lo mide; para eso, el log '[Rebuild ZSET]'.")

    print()


def _count_status(rows: Sequence[Row]) -> Dict[str, int]:
    counts = {
        STATUS_VISIBLE: 0, STATUS_INVISIBLE: 0,
        STATUS_CUT_ONLY: 0, STATUS_UNMEASURED: 0, STATUS_CLOSED_OK: 0,
    }
    for r in rows:
        counts[r.verdict.status] = counts.get(r.verdict.status, 0) + 1
    return counts


def _ago(dt: Optional[datetime]) -> str:
    if dt is None:
        return "?"
    delta = datetime.now(timezone.utc) - datetime.fromtimestamp(_epoch(dt), tz=timezone.utc)
    hours = delta.total_seconds() / 3600
    return f"{hours:.0f}h" if hours < 72 else f"{hours / 24:.0f}d"


def build_json(rows: Sequence[Row], advisors: Dict[str, str], hours: int) -> Dict[str, Any]:
    return {
        "generado": datetime.now(timezone.utc).isoformat(),
        "ventana_horas": hours,
        "asesoras": advisors,
        "resumen": _count_status(rows),
        "contactos": [
            {
                "phone": r.pending.phone,
                "canal": r.pending.canal,
                "advisor_id": r.advisor_id,
                "status": r.verdict.status,
                "causa_principal": r.verdict.primary,
                "causas": list(r.verdict.causes),
                "detalle": r.verdict.detail,
                "last_message_at": (
                    r.pending.last_message_at.isoformat()
                    if r.pending.last_message_at else None
                ),
                "archived": r.pending.archived,
                "contact_id": r.pending.contact_id,
                "desfase_orden_s": r.lag,
                "preview": r.pending.preview,
            }
            for r in rows
            if r.verdict.status != STATUS_VISIBLE
        ],
    }


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────


async def _connect_redis(aioredis, explicit: Optional[str]):
    """
    Encuentra un Redis alcanzable desde la máquina local.

    En este proyecto solo existe REDIS_URL, que apunta a redis.railway.internal
    y no resuelve fuera de la red de Railway. El camino habitual es abrir un
    túnel con `railway connect redis` — mismo patrón que scripts/auditoria/audit_inbox.py.

    Devuelve (cliente, origen) o (None, motivo) si no hay ninguno accesible.
    """
    candidatos = []
    if explicit:
        candidatos.append((explicit, "--redis-url"))
    if os.getenv("REDIS_PUBLIC_URL"):
        candidatos.append((os.environ["REDIS_PUBLIC_URL"], "REDIS_PUBLIC_URL"))
    candidatos.append(("redis://localhost:16379", "túnel railway connect"))
    if os.getenv("REDIS_URL"):
        candidatos.append((os.environ["REDIS_URL"], "REDIS_URL"))

    for url, origen in candidatos:
        try:
            client = aioredis.from_url(
                url, decode_responses=True, max_connections=5,
                socket_connect_timeout=3.0, socket_timeout=3.0,
            )
            await client.ping()
            return client, origen
        except Exception:
            try:
                await client.aclose()
            except Exception:
                pass
            continue

    return None, "ninguno alcanzable"


def _resolve_base_url(explicit: Optional[str]) -> Optional[str]:
    candidate = explicit or os.getenv("PANEL_BASE_URL") or ""
    if not candidate:
        domain = os.getenv("RAILWAY_PUBLIC_DOMAIN") or ""
        if domain:
            candidate = f"https://{domain}"
    candidate = candidate.strip().rstrip("/")
    if candidate and not candidate.startswith("http"):
        candidate = f"https://{candidate}"
    return candidate or None


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Auditoría de visibilidad del panel (solo lectura)",
    )
    parser.add_argument("--hours", type=int, default=168,
                        help="Ventana de análisis en horas (default 168 = 1 semana)")
    parser.add_argument("--cap", type=int, default=5000,
                        help="Tope de conversaciones a leer de MongoDB")
    parser.add_argument("--base-url", type=str, default=None,
                        help="URL pública del panel (o PANEL_BASE_URL)")
    parser.add_argument("--redis-url", type=str, default=None,
                        help="Redis alcanzable desde local; por defecto busca el "
                             "túnel de `railway connect redis` en localhost:16379")
    parser.add_argument("--page-limit", type=int, default=100,
                        help="limit por página al consultar /contacts")
    parser.add_argument("--max-pages", type=int, default=15,
                        help="Tope de páginas por asesora")
    parser.add_argument("--pause", type=float, default=2.5,
                        help="Pausa entre peticiones al panel (rate limit 30/min)")
    parser.add_argument("--json", type=str, default=None,
                        help="Ruta donde escribir el detalle en JSON")
    args = parser.parse_args()

    base_url = _resolve_base_url(args.base_url)
    if not base_url:
        print("ERROR: falta la URL del panel. Usa --base-url o PANEL_BASE_URL.")
        return 2

    api_key = os.getenv("ADMIN_API_KEY") or os.getenv("PANEL_API_KEY")
    if not api_key:
        print("ERROR: falta ADMIN_API_KEY / PANEL_API_KEY.")
        return 2

    # Este script corre FUERA del worker: `railway run` inyecta las variables en
    # la máquina local, pero las URLs internas (*.railway.internal) no resuelven
    # desde aquí. Se quita RAILWAY_ENVIRONMENT para que MongoManager tome la rama
    # pública, igual que haría en desarrollo local.
    if os.getenv("MONGO_PUBLIC_URL") or os.getenv("MONGODB_PUBLIC_URL"):
        os.environ.pop("RAILWAY_ENVIRONMENT", None)

    import httpx
    import redis.asyncio as aioredis
    from database.mongodb_client import get_mongo_manager

    mongo = get_mongo_manager()
    if not await mongo.connect():
        print("ERROR: no se pudo conectar a MongoDB.")
        return 2

    raw_redis, redis_origen = await _connect_redis(aioredis, args.redis_url)
    r = ReadOnlyRedis(raw_redis) if raw_redis else None
    http = httpx.AsyncClient(timeout=60.0)

    try:
        if r:
            print(f"  Redis: OK ({redis_origen})")
        else:
            print("\n  AVISO: sin Redis. Se mide visibilidad, pero NO se clasifica")
            print("         la causa de cada invisible. Para clasificar, abre otra")
            print("         terminal con `railway connect redis` y repite.\n")

        print("\n  [1/4] Resolviendo asesoras desde datos…")
        advisors = await resolve_advisors(mongo.db)
        if not advisors:
            print("ERROR: no se pudo resolver ninguna asesora. Se aborta en vez de asumir.")
            return 2
        print(f"        {len(advisors)} asesoras activas")

        print(f"\n  [2/4] Verdad de campo — pendientes de las últimas {args.hours}h…")
        cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
        pendings, truncated = await fetch_pending(mongo.db, cutoff, args.cap)
        print(f"        {len(pendings)} conversaciones con mensaje del cliente sin responder")
        if not pendings:
            print("\n  Nada que auditar en esta ventana.\n")
            return 0

        pendings = await resolver_embudos(http, pendings)

        print(f"\n  [3/4] Visibilidad real — preguntando al panel ({base_url})…")
        views: Dict[str, AdvisorView] = {}
        for advisor_id in advisors:
            print(f"      · {advisors[advisor_id]}")
            views[advisor_id] = await fetch_advisor_view(
                http, base_url, api_key, advisor_id,
                args.page_limit, args.max_pages, args.pause,
            )
            await asyncio.sleep(args.pause)

        if r:
            print("\n  [4/4] Clasificando contra Redis…")
            facts = await fetch_redis_facts(r, pendings, list(advisors))
        else:
            print("\n  [4/4] Sin Redis — no se clasifica la causa.")
            facts = None

        rows = build_rows(pendings, facts, views)
        print_report(rows, advisors, views, args.hours, truncated)

        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(build_json(rows, advisors, args.hours), fh,
                          ensure_ascii=False, indent=2, default=str)
            print(f"  Detalle escrito en {args.json}\n")

    finally:
        await http.aclose()
        if raw_redis is not None:
            try:
                await raw_redis.aclose()
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n  Interrumpido.", file=sys.stderr)
        sys.exit(130)
