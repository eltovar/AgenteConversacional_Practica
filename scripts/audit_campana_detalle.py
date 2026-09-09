"""
Auditoría detallada — Números completos y patrones.
Complemento del script audit_campana_mayo6.py

Ejecutar:
  python scripts/audit_campana_detalle.py
"""
import asyncio
import os
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict, Counter

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from motor.motor_asyncio import AsyncIOMotorClient

TIMEZONE = ZoneInfo("America/Bogota")
MONGO_URL = (
    os.getenv("MONGO_PUBLIC_URL")
    or os.getenv("MONGODB_PUBLIC_URL")
    or os.getenv("MONGO_URL")
    or os.getenv("MONGODB_URL")
)
if not MONGO_URL:
    raise RuntimeError("No se encontró MONGO_URL / MONGODB_URL")

DB_NAME = "inmobiliaria_chat"
CAMPAIGN_DATE = datetime(2026, 5, 6, 0, 0, 0, tzinfo=TIMEZONE)
CAMPAIGN_END  = datetime(2026, 5, 7, 0, 0, 0, tzinfo=TIMEZONE)
RESPONSE_END  = datetime(2026, 5, 20, 23, 59, 59, tzinfo=TIMEZONE)

# ── Keywords (same as main script) ───────────────────────────
APPOINTMENT_KW = [
    r"\bcita\b", r"\bvisita\b", r"\bagend[aeoó]\b", r"\bprogramar\b",
    r"\bcuando puedo ir\b", r"\bcuándo puedo ir\b",
    r"\bconfirmad[oa]\b", r"\breservad[oa]\b",
    r"\bfecha\b.*\bhora\b", r"\bver el inmueble\b",
    r"\bir a ver\b", r"\bconocer el\b",
]

NO_MATCH_KW = [
    r"\bno tiene[ns]?\b", r"\bno hay\b", r"\bno encuentr[oa]\b",
    r"\bmuy car[oa]\b", r"\bmuy costoso\b", r"\bfuera de.{0,10}presupuesto\b",
    r"\bno me sirve\b", r"\bno se ajusta\b", r"\bno aplica\b",
    r"\botro sector\b", r"\botra zona\b", r"\bno queda\b",
    r"\bpresupuesto\b", r"\brango\b",
    r"\bno tienen nada\b", r"\bnada que\b", r"\bno hay nada\b",
    r"\bmas econom\b", r"\bmás econom\b", r"\bmas barat\b", r"\bmás barat\b",
]

# ── Patrones de necesidad del cliente (para punto 6.3) ───────
PATTERN_RULES = {
    "Presupuesto bajo (< $1.500.000)": [
        r"1[\.\,]?[0-4]\d{2}[\.\,]?\d{3}",  # 1.000.000 - 1.499.999
        r"1[\.\,]?[0-4]\s*millon",
        r"un mill[oó]n",
        r"millon\s*(doscientos|trescientos|cuatrocientos)",
    ],
    "Presupuesto límite ($1.500.000 - $1.800.000)": [
        r"1[\.\,]?[5-8]\d{2}[\.\,]?\d{3}",  # 1.500.000 - 1.899.999
        r"1[\.\,]?5\b",
        r"hasta\s*1[\.\,]?[5-8]",
        r"1800",
        r"mill[oó]n\s*(quinientos|seiscientos|setecientos|ochocientos)",
    ],
    "Zona específica no cubierta": [
        r"\ben cali\b", r"\bbogot[aá]\b", r"\bsabaneta\b",
        r"\bitag[uü][ií]\b", r"\bbello\b", r"\bla estrella\b",
        r"\bcaldas\b", r"\bcopacabana\b", r"\bbarbosa\b",
        r"\botro sector\b", r"\botra zona\b", r"\botra ciudad\b",
        r"\bbarrio\b.*\b(dorado|paz|trianon|laureles|poblado|bel[eé]n)\b",
    ],
    "Tipo de inmueble no disponible": [
        r"\bapartaestudio\b", r"\bhabitaci[oó]n\b", r"\bcuarto\b",
        r"\bcomprar?\b", r"\bcompra\b", r"\bventa\b",
        r"\blocal\b", r"\bbodega\b", r"\boficina\b", r"\blote\b",
        r"\bfinca\b",
    ],
    "Precio alto vs. lo ofrecido": [
        r"\bmuy car[oa]\b", r"\bcostoso\b", r"\bcaro\b",
        r"\bfuera de.{0,15}presupuesto\b",
        r"\bno me alcanza\b", r"\bno alcanza\b",
        r"\bmas econom\b", r"\bmás econom\b",
        r"\bmas barat\b", r"\bmás barat\b",
        r"\bno se ajusta\b",
    ],
}


def classify_first_response(text):
    """Classify a first response as positive, negative, neutral, or spam."""
    t = text.lower().strip()

    # Spam: business auto-replies
    spam_signals = [
        "bienvenido", "gracias por comunicarte con", "gracias por escribir",
        "equipo comercial de inmobiliaria proteger",
        "¿cómo podemos ayudarte", "como podemos ayudarte",
        "creamos experiencias", "creamos rituales",
        "especialistas en", "contamos con una amplia",
    ]
    if any(s in t for s in spam_signals):
        return "spam"

    # Negative patterns
    neg_exact = ["no", "no gracias", "no muchas gracias", "no, gracias",
                 "no, muchas gracias", "hola no", "hola. no", "buenos días no",
                 "buenos dias no", "no señora", "no señor", "no señora. gracias",
                 "no señor@"]
    if t.rstrip(".!,") in neg_exact or t.rstrip(".!,").rstrip() in neg_exact:
        return "negative"

    neg_phrases = [
        r"\bya encontr[eé]\b", r"\bya consegu[ií]\b", r"\bya no\b",
        r"\bno estoy interesad\b", r"\bno me interesa\b",
        r"\bya tengo\b", r"\bno necesito\b", r"\bno busco\b",
        r"\bno por ahora\b", r"\bno por el momento\b",
        r"\bya lo resolv[ií]\b", r"\bme voy del pa[ií]s\b",
        r"\bcostosas para lo que ofrec\b",
    ]
    for p in neg_phrases:
        if re.search(p, t):
            return "negative"

    # Positive patterns (including variants)
    pos_patterns = [
        r"^s[ií]\b", r"^s[ií]$", r"^s[ií][ib]?\b", r"^siii*\b",
        r"^sip\b", r"\bsigo\b", r"\bbusco\b", r"\bbuscando\b",
        r"\binteresad[oa]\b", r"\baún\b.*\bbusca\b",
        r"\btodav[ií]a\b", r"\bnecesit[oa]\b", r"\bquiero\b",
        r"\bme interesa\b", r"\bestoy buscando\b", r"\bsigo buscando\b",
        r"\bclaro\b", r"\bpor favor\b", r"\bapartamento\b",
        r"\bcasa\b", r"\binmueble\b", r"\barriendo\b",
        r"\bdisponible\b", r"\bopciones\b", r"\bas[ií] es\b",
        r"^hola,?\s*s[ií]", r"^buenos d[ií]as,?\s*s[ií]",
        r"^buen d[ií]a,?\s*s[ií]",
    ]
    for p in pos_patterns:
        if re.search(p, t):
            return "positive"

    # Greeting only
    greetings = [
        r"^hola[.!]?$", r"^buenos?\s*d[ií]as?[.!]?$",
        r"^buenas?\s*tardes?[.!]?$", r"^buenas?$", r"^buen d[ií]a[.!]?$",
        r"^hola\s*(buenos|buenas|buen|cómo)", r"^gracias$",
    ]
    for p in greetings:
        if re.search(p, t):
            return "greeting"

    return "unclear"


async def main():
    print("=" * 70)
    print("AUDITORÍA DETALLADA — Números completos y patrones")
    print("=" * 70)

    client = AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=10000)
    db = client[DB_NAME]
    await client.admin.command("ping")
    print("✅ Conexión exitosa\n")

    # ── Step 1: Get all bulk recipients ──────────────────────
    bulk_query = {
        "$or": [
            {"metadata.is_bulk_send": True},
            {"content": {"$regex": "^\\[BULK template:", "$options": "i"}},
        ],
        "timestamp": {"$gte": CAMPAIGN_DATE, "$lt": CAMPAIGN_END},
        "sender": "advisor",
    }
    bulk_msgs = await db.messages.find(bulk_query).to_list(length=2000)
    bulk_phones = set(m["phone"] for m in bulk_msgs)

    if not bulk_phones:
        alt_query = {
            "timestamp": {"$gte": CAMPAIGN_DATE, "$lt": CAMPAIGN_END},
            "sender": "advisor", "channel": "whatsapp",
        }
        alt_msgs = await db.messages.find(alt_query).to_list(length=5000)
        phones_count = Counter(m["phone"] for m in alt_msgs)
        if len(phones_count) > 300:
            bulk_phones = set(phones_count.keys())

    print(f"Teléfonos de campaña: {len(bulk_phones)}")

    # ── Step 2: Get first responses ──────────────────────────
    resp_query = {
        "phone": {"$in": list(bulk_phones)},
        "sender": "client",
        "timestamp": {"$gte": CAMPAIGN_DATE, "$lte": RESPONSE_END},
    }
    resp_msgs = await db.messages.find(resp_query, sort=[("timestamp", 1)]).to_list(length=10000)

    first_responses = {}
    all_responses = defaultdict(list)
    for m in resp_msgs:
        p = m["phone"]
        all_responses[p].append(m)
        if p not in first_responses:
            first_responses[p] = m

    # ── Step 3: Classify with improved logic ─────────────────
    positives = []
    negatives = []

    for phone, msg in first_responses.items():
        content = msg.get("content", "")
        cat = classify_first_response(content)
        entry = {
            "phone": phone,
            "content": content[:200],
            "contact_id": msg.get("hubspot_contact_id", ""),
            "category": cat,
        }
        if cat == "positive":
            positives.append(entry)
        elif cat == "negative":
            negatives.append(entry)

    # ── Step 4: Find appointment mentions in full convos ─────
    appointment_clients = []
    no_match_clients = []

    for entry in positives:
        phone = entry["phone"]
        conv_query = {
            "phone": phone,
            "timestamp": {"$gte": CAMPAIGN_DATE, "$lte": RESPONSE_END},
        }
        conv_msgs = await db.messages.find(conv_query, sort=[("timestamp", 1)]).to_list(length=200)
        full_text = " ".join(m.get("content", "") for m in conv_msgs).lower()

        has_appt = any(re.search(kw, full_text) for kw in APPOINTMENT_KW)
        has_nomatch = any(re.search(kw, full_text) for kw in NO_MATCH_KW)

        if has_appt:
            appointment_clients.append({
                "phone": phone,
                "contact_id": entry["contact_id"],
                "message_count": len(conv_msgs),
                "first_response": entry["content"],
            })

        if has_nomatch:
            # Classify into pattern categories
            matched_patterns = []
            for pattern_name, patterns in PATTERN_RULES.items():
                for p in patterns:
                    if re.search(p, full_text):
                        matched_patterns.append(pattern_name)
                        break
            if not matched_patterns:
                matched_patterns = ["No match genérico (sin patrón específico)"]

            no_match_clients.append({
                "phone": phone,
                "contact_id": entry["contact_id"],
                "message_count": len(conv_msgs),
                "first_response": entry["content"],
                "matched_patterns": matched_patterns,
                "full_text_preview": full_text[:500],
            })

    # ══════════════════════════════════════════════════════════
    # OUTPUT
    # ══════════════════════════════════════════════════════════

    print(f"\n{'=' * 70}")
    print(f"PUNTO 1: Los 8 contactos con mención de CITA (números completos)")
    print(f"{'=' * 70}")
    for i, c in enumerate(appointment_clients, 1):
        print(f"\n  {i}. Teléfono: {c['phone']}")
        print(f"     HubSpot ID: {c['contact_id']}")
        print(f"     Mensajes en conversación: {c['message_count']}")
        print(f"     Primera respuesta: \"{c['first_response'][:120]}\"")

    print(f"\n{'=' * 70}")
    print(f"PUNTO 2: Contactos con respuestas POSITIVAS (números completos)")
    print(f"{'=' * 70}")
    print(f"\n  Total positivos: {len(positives)}")
    print(f"\n  ── Primeros 25 positivos ──")
    for i, c in enumerate(positives[:25], 1):
        print(f"  {i:>3}. {c['phone']}  →  \"{c['content'][:80]}\"")

    print(f"\n{'=' * 70}")
    print(f"PUNTO 2b: Contactos con respuestas NEGATIVAS (números completos)")
    print(f"{'=' * 70}")
    print(f"\n  Total negativos: {len(negatives)}")
    print(f"\n  ── Primeros 25 negativos ──")
    for i, c in enumerate(negatives[:25], 1):
        print(f"  {i:>3}. {c['phone']}  →  \"{c['content'][:80]}\"")

    print(f"\n{'=' * 70}")
    print(f"PUNTO 3: Conteo por PATRÓN DETECTADO (punto 6.3)")
    print(f"{'=' * 70}")
    pattern_counter = Counter()
    pattern_examples = defaultdict(list)
    for c in no_match_clients:
        for pat in c["matched_patterns"]:
            pattern_counter[pat] += 1
            if len(pattern_examples[pat]) < 3:
                pattern_examples[pat].append({
                    "phone": c["phone"],
                    "preview": c["first_response"][:100],
                })

    print(f"\n  Total clientes perdidos por no-match: {len(no_match_clients)}")
    print(f"\n  Desglose por patrón:")
    for pattern, count in pattern_counter.most_common():
        print(f"\n    ▸ {pattern}: {count} clientes")
        for ex in pattern_examples[pattern]:
            print(f"      - {ex['phone']}: \"{ex['preview'][:80]}\"")

    # ── Export JSON ──────────────────────────────────────────
    export = {
        "appointment_contacts_full": [
            {"phone": c["phone"], "contact_id": c["contact_id"],
             "message_count": c["message_count"], "first_response": c["first_response"]}
            for c in appointment_clients
        ],
        "positive_contacts_sample": [
            {"phone": c["phone"], "content": c["content"], "contact_id": c["contact_id"]}
            for c in positives[:30]
        ],
        "negative_contacts_sample": [
            {"phone": c["phone"], "content": c["content"]}
            for c in negatives[:30]
        ],
        "no_match_pattern_counts": dict(pattern_counter.most_common()),
        "no_match_details": [
            {"phone": c["phone"], "contact_id": c["contact_id"],
             "patterns": c["matched_patterns"], "first_response": c["first_response"]}
            for c in no_match_clients
        ],
    }

    json_path = os.path.join(os.path.dirname(__file__), "audit_campana_detalle_results.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(export, f, ensure_ascii=False, indent=2)
    print(f"\n\n📄 Resultados exportados a: {json_path}")

    client.close()
    print("✅ Completado.")


if __name__ == "__main__":
    asyncio.run(main())
