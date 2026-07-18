"""
Auditoría profunda — Campaña masiva del 6 de mayo de 2026.

Objetivo: Responder con datos concretos:
  1. ¿A cuántos se les envió? (747 conocido, validar)
  2. ¿Cuántos contestaron?
  3. ¿Cuántos positivos (aún buscan) vs negativos (ya no)?
  4. ¿Cuántos agendaron cita vs se perdieron por falta de inmuebles?

Ejecutar:
  python scripts/audit_campana_mayo6.py

Requiere: MONGODB_URL o MONGO_PUBLIC_URL en env (o .env file).
"""
import asyncio
import os
import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from collections import defaultdict, Counter

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from motor.motor_asyncio import AsyncIOMotorClient

TIMEZONE = ZoneInfo("America/Bogota")

# ── Conexión MongoDB ─────────────────────────────────────────
MONGO_URL = (
    os.getenv("MONGO_PUBLIC_URL")
    or os.getenv("MONGODB_PUBLIC_URL")
    or os.getenv("MONGO_URL")
    or os.getenv("MONGODB_URL")
)
if not MONGO_URL:
    raise RuntimeError("No se encontró MONGO_URL / MONGODB_URL en variables de entorno")

DB_NAME = "inmobiliaria_chat"

# ── Fechas de análisis ───────────────────────────────────────
# La campaña se envió el 6 de mayo de 2026.
# Analizamos respuestas hasta 14 días después (20 de mayo).
CAMPAIGN_DATE = datetime(2026, 5, 6, 0, 0, 0, tzinfo=TIMEZONE)
CAMPAIGN_END = datetime(2026, 5, 7, 0, 0, 0, tzinfo=TIMEZONE)   # fin del día de envío
RESPONSE_WINDOW_END = datetime(2026, 5, 20, 23, 59, 59, tzinfo=TIMEZONE)  # 14 días

# ── Palabras clave para clasificación ────────────────────────
# Positivo: cliente aún busca inmueble
POSITIVE_KEYWORDS = [
    r"\bsi\b", r"\bsí\b", r"\bsigo\b", r"\bbusco\b", r"\bbuscando\b",
    r"\binteresad[oa]\b", r"\baun\b", r"\baún\b", r"\btodav[ií]a\b",
    r"\bnecesit[oa]\b", r"\bquiero\b", r"\bme interesa\b",
    r"\bestoy buscando\b", r"\bsigo buscando\b", r"\bsigo interesad\b",
    r"\ben busqueda\b", r"\ben búsqueda\b", r"\bpor favor\b",
    r"\bclaro\b", r"\bclaro que si\b", r"\bme puede\b",
    r"\btiene algo\b", r"\bque opciones\b", r"\bqué opciones\b",
    r"\bapartamento\b", r"\bcasa\b", r"\binmueble\b", r"\barriendo\b",
    r"\barrendamiento\b", r"\blocal\b", r"\bbodega\b",
]

# Negativo: cliente ya no busca
NEGATIVE_KEYWORDS = [
    r"\bya no\b", r"\bno gracias\b", r"\bya encontr[eé]\b",
    r"\bya consegu[ií]\b", r"\bno estoy interesad\b", r"\bno me interesa\b",
    r"\bya tengo\b", r"\bno necesito\b", r"\bno busco\b",
    r"\bno gracias\b", r"\bya no busco\b", r"\bya no necesito\b",
    r"\bno por ahora\b", r"\bno por el momento\b",
]

# Indicadores de falta de inmueble / no match
NO_MATCH_KEYWORDS = [
    r"\bno tiene[ns]?\b", r"\bno hay\b", r"\bno encuentr[oa]\b",
    r"\bmuy car[oa]\b", r"\bmuy costoso\b", r"\bfuera de.{0,10}presupuesto\b",
    r"\bno me sirve\b", r"\bno se ajusta\b", r"\bno aplica\b",
    r"\botro sector\b", r"\botra zona\b", r"\bno queda\b",
    r"\b[123]\s*(?:mill|millon|cuarto|habi)\b",  # menciones de presupuesto
    r"\bpresupuesto\b", r"\brango\b",
    r"\bno tienen nada\b", r"\bnada que\b", r"\bno hay nada\b",
    r"\bmas econom\b", r"\bmás econom\b", r"\bmas barat\b", r"\bmás barat\b",
]

# Indicadores de cita agendada
APPOINTMENT_KEYWORDS = [
    r"\bcita\b", r"\bvisita\b", r"\bagend[aeoó]\b", r"\bprogramar\b",
    r"\bcuando puedo ir\b", r"\bcuándo puedo ir\b",
    r"\bconfirmad[oa]\b", r"\breservad[oa]\b",
    r"\bfecha\b.*\bhora\b", r"\bver el inmueble\b",
    r"\bir a ver\b", r"\bconocer el\b",
]


def classify_response(text: str) -> dict:
    """Clasifica un mensaje del cliente en categorías."""
    text_lower = text.lower().strip()

    result = {
        "is_positive": False,
        "is_negative": False,
        "mentions_no_match": False,
        "mentions_appointment": False,
        "matched_positive": [],
        "matched_negative": [],
        "matched_no_match": [],
        "matched_appointment": [],
    }

    for kw in POSITIVE_KEYWORDS:
        if re.search(kw, text_lower):
            result["is_positive"] = True
            result["matched_positive"].append(kw)

    for kw in NEGATIVE_KEYWORDS:
        if re.search(kw, text_lower):
            result["is_negative"] = True
            result["matched_negative"].append(kw)

    for kw in NO_MATCH_KEYWORDS:
        if re.search(kw, text_lower):
            result["mentions_no_match"] = True
            result["matched_no_match"].append(kw)

    for kw in APPOINTMENT_KEYWORDS:
        if re.search(kw, text_lower):
            result["mentions_appointment"] = True
            result["matched_appointment"].append(kw)

    # Si tiene tanto positivo como negativo, priorizar negativo
    # (ej: "sí busco pero ya no me interesa esa zona")
    if result["is_positive"] and result["is_negative"]:
        result["classification"] = "ambiguous"
    elif result["is_negative"]:
        result["classification"] = "negative"
    elif result["is_positive"]:
        result["classification"] = "positive"
    else:
        result["classification"] = "neutral"

    return result


async def main():
    print("=" * 70)
    print("AUDITORÍA PROFUNDA — Campaña masiva 6 de mayo de 2026")
    print("=" * 70)

    client = AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=10000)
    db = client[DB_NAME]

    # Verificar conexión
    try:
        await client.admin.command("ping")
        print("✅ Conexión a MongoDB exitosa\n")
    except Exception as e:
        print(f"❌ Error conectando a MongoDB: {e}")
        return

    # ══════════════════════════════════════════════════════════
    # PASO 1: Encontrar todos los mensajes masivos del 6 de mayo
    # ══════════════════════════════════════════════════════════
    print("─" * 70)
    print("PASO 1: Mensajes masivos enviados el 6 de mayo")
    print("─" * 70)

    # Los mensajes bulk se guardan con metadata.is_bulk_send = True
    # y content empieza con "[BULK template:"
    bulk_query = {
        "$or": [
            {"metadata.is_bulk_send": True},
            {"content": {"$regex": "^\\[BULK template:", "$options": "i"}},
        ],
        "timestamp": {
            "$gte": CAMPAIGN_DATE,
            "$lt": CAMPAIGN_END,
        },
        "sender": "advisor",
    }

    bulk_messages = await db.messages.find(bulk_query).to_list(length=2000)
    bulk_phones = set()
    for msg in bulk_messages:
        bulk_phones.add(msg["phone"])

    print(f"  Mensajes masivos encontrados: {len(bulk_messages)}")
    print(f"  Teléfonos únicos: {len(bulk_phones)}")

    if not bulk_phones:
        # Intentar buscar de otra forma: todos los outbound del 6 de mayo
        # que NO sean bot (pueden ser mensajes normales + bulk)
        print("\n  ⚠️  No se encontraron mensajes con metadata.is_bulk_send")
        print("  Intentando búsqueda alternativa: todos los outbound advisor del 6 de mayo...\n")

        alt_query = {
            "timestamp": {"$gte": CAMPAIGN_DATE, "$lt": CAMPAIGN_END},
            "sender": "advisor",
            "channel": "whatsapp",
        }
        alt_messages = await db.messages.find(alt_query).to_list(length=5000)

        # Agrupar por teléfono para ver volumen
        phones_count = Counter(m["phone"] for m in alt_messages)
        print(f"  Total mensajes advisor el 6 de mayo: {len(alt_messages)}")
        print(f"  Teléfonos únicos contactados por advisor: {len(phones_count)}")

        # Si hubo más de 500 teléfonos diferentes, probablemente fue la campaña
        if len(phones_count) > 300:
            bulk_phones = set(phones_count.keys())
            bulk_messages = alt_messages
            print(f"  → Asumiendo que estos {len(bulk_phones)} son los receptores de la campaña masiva")
        else:
            print("  → No parece haber habido campaña masiva este día. Abortando.")
            return

    # Mostrar ejemplo de contenido
    print(f"\n  Ejemplo de contenido de mensaje masivo:")
    for msg in bulk_messages[:3]:
        content_preview = msg.get("content", "")[:120]
        print(f"    - [{msg['phone'][-4:]}] {content_preview}")

    # ══════════════════════════════════════════════════════════
    # PASO 2: Encontrar respuestas de estos clientes
    # ══════════════════════════════════════════════════════════
    print(f"\n{'─' * 70}")
    print(f"PASO 2: Respuestas de clientes (6 mayo - 20 mayo)")
    print(f"{'─' * 70}")

    # Buscar todos los mensajes inbound de estos teléfonos después de la campaña
    response_query = {
        "phone": {"$in": list(bulk_phones)},
        "sender": "client",
        "timestamp": {
            "$gte": CAMPAIGN_DATE,
            "$lte": RESPONSE_WINDOW_END,
        },
    }

    response_messages = await db.messages.find(
        response_query,
        sort=[("timestamp", 1)]  # cronológico
    ).to_list(length=10000)

    # Agrupar por teléfono: solo el PRIMER mensaje de cada cliente
    first_responses = {}
    all_responses_by_phone = defaultdict(list)
    for msg in response_messages:
        phone = msg["phone"]
        all_responses_by_phone[phone].append(msg)
        if phone not in first_responses:
            first_responses[phone] = msg

    responded_phones = set(first_responses.keys())
    no_response_phones = bulk_phones - responded_phones

    print(f"  Clientes que respondieron: {len(responded_phones)} ({len(responded_phones)/len(bulk_phones)*100:.1f}%)")
    print(f"  Clientes sin respuesta: {len(no_response_phones)} ({len(no_response_phones)/len(bulk_phones)*100:.1f}%)")

    # ══════════════════════════════════════════════════════════
    # PASO 3: Clasificar respuestas
    # ══════════════════════════════════════════════════════════
    print(f"\n{'─' * 70}")
    print(f"PASO 3: Clasificación de respuestas")
    print(f"{'─' * 70}")

    classifications = {
        "positive": [],     # aún busca inmueble
        "negative": [],     # ya no busca
        "neutral": [],      # respuesta no clasificable
        "ambiguous": [],    # señales mixtas
    }

    for phone, msg in first_responses.items():
        content = msg.get("content", "")
        classification = classify_response(content)
        classification["phone"] = phone
        classification["content"] = content[:200]
        classification["timestamp"] = str(msg.get("timestamp", ""))
        classification["hubspot_contact_id"] = msg.get("hubspot_contact_id", "")
        classifications[classification["classification"]].append(classification)

    print(f"\n  Positivos (aún buscan inmueble): {len(classifications['positive'])} ({len(classifications['positive'])/max(len(responded_phones),1)*100:.1f}%)")
    print(f"  Negativos (ya no buscan): {len(classifications['negative'])} ({len(classifications['negative'])/max(len(responded_phones),1)*100:.1f}%)")
    print(f"  Neutrales (respuesta no clasificable): {len(classifications['neutral'])} ({len(classifications['neutral'])/max(len(responded_phones),1)*100:.1f}%)")
    print(f"  Ambiguos (señales mixtas): {len(classifications['ambiguous'])} ({len(classifications['ambiguous'])/max(len(responded_phones),1)*100:.1f}%)")

    # Mostrar ejemplos de cada categoría
    for category in ["positive", "negative", "neutral", "ambiguous"]:
        items = classifications[category]
        if items:
            print(f"\n  ── Ejemplos {category.upper()} (primeros 10) ──")
            for item in items[:10]:
                phone_last4 = item["phone"][-4:]
                content = item["content"][:100].replace("\n", " ")
                print(f"    [{phone_last4}] \"{content}\"")

    # ══════════════════════════════════════════════════════════
    # PASO 4: Buscar citas agendadas y menciones de "no hay inmueble"
    # ══════════════════════════════════════════════════════════
    print(f"\n{'─' * 70}")
    print(f"PASO 4: Citas agendadas vs clientes perdidos por falta de inmuebles")
    print(f"{'─' * 70}")

    # Buscar en TODAS las conversaciones de los que respondieron positivamente
    # si hubo menciones de citas o de falta de inmuebles
    positive_phones = [c["phone"] for c in classifications["positive"]]

    # Para cada cliente positivo, buscar toda la conversación posterior
    appointment_found = []
    no_match_found = []
    positive_no_outcome = []

    for phone in positive_phones:
        # Obtener toda la conversación de este cliente después del masivo
        conv_query = {
            "phone": phone,
            "timestamp": {
                "$gte": CAMPAIGN_DATE,
                "$lte": RESPONSE_WINDOW_END,
            },
        }
        conv_messages = await db.messages.find(
            conv_query,
            sort=[("timestamp", 1)]
        ).to_list(length=200)

        full_text = " ".join(m.get("content", "") for m in conv_messages).lower()

        has_appointment = False
        has_no_match = False

        for kw in APPOINTMENT_KEYWORDS:
            if re.search(kw, full_text):
                has_appointment = True
                break

        for kw in NO_MATCH_KEYWORDS:
            if re.search(kw, full_text):
                has_no_match = True
                break

        contact_id = ""
        for m in conv_messages:
            if m.get("hubspot_contact_id"):
                contact_id = m["hubspot_contact_id"]
                break

        entry = {
            "phone": phone,
            "contact_id": contact_id,
            "message_count": len(conv_messages),
            "first_response": all_responses_by_phone[phone][0].get("content", "")[:150],
            "has_appointment_mention": has_appointment,
            "has_no_match_mention": has_no_match,
        }

        if has_appointment:
            appointment_found.append(entry)
        elif has_no_match:
            no_match_found.append(entry)
        else:
            positive_no_outcome.append(entry)

    print(f"\n  De {len(positive_phones)} clientes positivos (aún buscaban):")
    print(f"    Con mención de cita/visita: {len(appointment_found)}")
    print(f"    Con mención de falta de inmueble/no match: {len(no_match_found)}")
    print(f"    Sin desenlace claro en mensajes: {len(positive_no_outcome)}")

    if appointment_found:
        print(f"\n  ── Clientes con mención de CITA ({len(appointment_found)}) ──")
        for entry in appointment_found[:15]:
            phone_last4 = entry["phone"][-4:]
            resp = entry["first_response"][:80].replace("\n", " ")
            print(f"    [{phone_last4}] (contact: {entry['contact_id']}) msgs:{entry['message_count']} \"{resp}\"")

    if no_match_found:
        print(f"\n  ── Clientes PERDIDOS por falta de inmueble ({len(no_match_found)}) ──")
        for entry in no_match_found[:15]:
            phone_last4 = entry["phone"][-4:]
            resp = entry["first_response"][:80].replace("\n", " ")
            print(f"    [{phone_last4}] (contact: {entry['contact_id']}) msgs:{entry['message_count']} \"{resp}\"")

    if positive_no_outcome:
        print(f"\n  ── Clientes positivos SIN DESENLACE CLARO ({len(positive_no_outcome)}) ──")
        for entry in positive_no_outcome[:15]:
            phone_last4 = entry["phone"][-4:]
            resp = entry["first_response"][:80].replace("\n", " ")
            print(f"    [{phone_last4}] (contact: {entry['contact_id']}) msgs:{entry['message_count']} \"{resp}\"")

    # ══════════════════════════════════════════════════════════
    # PASO 5: Buscar cambios de pipeline en HubSpot post-campaña
    # ══════════════════════════════════════════════════════════
    print(f"\n{'─' * 70}")
    print(f"PASO 5: Actividad de asesoras con los que respondieron")
    print(f"{'─' * 70}")

    # Ver cuántos mensajes de asesora hubo como respuesta
    advisor_followup_query = {
        "phone": {"$in": list(responded_phones)},
        "sender": "advisor",
        "timestamp": {
            "$gt": CAMPAIGN_DATE,
            "$lte": RESPONSE_WINDOW_END,
        },
        # Excluir los mismos mensajes bulk
        "$nor": [
            {"metadata.is_bulk_send": True},
            {"content": {"$regex": "^\\[BULK template:"}},
        ],
    }
    advisor_followups = await db.messages.find(advisor_followup_query).to_list(length=10000)
    advisor_followup_phones = set(m["phone"] for m in advisor_followups)

    print(f"  Clientes que respondieron Y recibieron seguimiento de asesora: {len(advisor_followup_phones)}")
    print(f"  Clientes que respondieron PERO NO recibieron seguimiento: {len(responded_phones - advisor_followup_phones)}")
    print(f"  Total mensajes de asesora en seguimiento: {len(advisor_followups)}")

    # Distribución temporal de respuestas
    print(f"\n  ── Distribución temporal de respuestas ──")
    response_dates = Counter()
    for phone, msg in first_responses.items():
        ts = msg.get("timestamp")
        if ts:
            if hasattr(ts, 'date'):
                response_dates[str(ts.date())] += 1
            else:
                response_dates[str(ts)[:10]] += 1

    for date_str in sorted(response_dates.keys()):
        print(f"    {date_str}: {response_dates[date_str]} respuestas")

    # ══════════════════════════════════════════════════════════
    # PASO 6: Análisis de contenido detallado — qué buscan los clientes
    # ══════════════════════════════════════════════════════════
    print(f"\n{'─' * 70}")
    print(f"PASO 6: ¿Qué buscan los clientes que respondieron positivamente?")
    print(f"{'─' * 70}")

    # Extraer menciones de tipo de inmueble, presupuesto, zona
    tipo_inmueble = Counter()
    menciones_precio = []

    for entry in classifications["positive"]:
        text = entry["content"].lower()
        if any(w in text for w in ["apartamento", "apto", "aparta"]):
            tipo_inmueble["Apartamento"] += 1
        if any(w in text for w in ["casa"]):
            tipo_inmueble["Casa"] += 1
        if any(w in text for w in ["local", "bodega"]):
            tipo_inmueble["Local/Bodega"] += 1
        if any(w in text for w in ["habitacion", "habitación", "cuarto", "pieza"]):
            tipo_inmueble["Habitación"] += 1
        if any(w in text for w in ["arriendo", "arrendamiento", "alquiler"]):
            tipo_inmueble["Arriendo (genérico)"] += 1

        # Buscar menciones de precio
        precio_match = re.findall(r'(\d[\d.,]*)\s*(?:mill|millon|mil|pesos|\$)', text)
        if precio_match:
            menciones_precio.append({"phone": entry["phone"][-4:], "content": text[:100], "precio_raw": precio_match})

    print(f"\n  Tipo de inmueble mencionado por clientes positivos:")
    for tipo, count in tipo_inmueble.most_common():
        print(f"    {tipo}: {count}")

    if menciones_precio:
        print(f"\n  Clientes que mencionaron presupuesto/precio ({len(menciones_precio)}):")
        for mp in menciones_precio[:10]:
            print(f"    [{mp['phone']}] precio:{mp['precio_raw']} → \"{mp['content'][:80]}\"")

    # ══════════════════════════════════════════════════════════
    # RESUMEN EJECUTIVO
    # ══════════════════════════════════════════════════════════
    print(f"\n{'=' * 70}")
    print(f"RESUMEN EJECUTIVO")
    print(f"{'=' * 70}")
    print(f"""
  ┌─────────────────────────────────────────────────────────┐
  │ Campaña masiva: 6 de mayo de 2026                       │
  ├─────────────────────────────────────────────────────────┤
  │ Mensajes enviados:        {len(bulk_phones):>6}                        │
  │ Clientes que respondieron: {len(responded_phones):>5} ({len(responded_phones)/len(bulk_phones)*100:.1f}%)              │
  │ Sin respuesta:            {len(no_response_phones):>6} ({len(no_response_phones)/len(bulk_phones)*100:.1f}%)              │
  ├─────────────────────────────────────────────────────────┤
  │ CLASIFICACIÓN DE RESPUESTAS:                            │
  │   Positivos (aún buscan): {len(classifications['positive']):>5} ({len(classifications['positive'])/max(len(responded_phones),1)*100:.1f}%)              │
  │   Negativos (ya no):      {len(classifications['negative']):>5} ({len(classifications['negative'])/max(len(responded_phones),1)*100:.1f}%)              │
  │   Neutrales:              {len(classifications['neutral']):>5} ({len(classifications['neutral'])/max(len(responded_phones),1)*100:.1f}%)              │
  │   Ambiguos:               {len(classifications['ambiguous']):>5} ({len(classifications['ambiguous'])/max(len(responded_phones),1)*100:.1f}%)              │
  ├─────────────────────────────────────────────────────────┤
  │ DESENLACE DE POSITIVOS:                                 │
  │   Con mención de cita:    {len(appointment_found):>5}                        │
  │   Perdidos (no match):    {len(no_match_found):>5}                        │
  │   Sin desenlace claro:    {len(positive_no_outcome):>5}                        │
  ├─────────────────────────────────────────────────────────┤
  │ SEGUIMIENTO:                                            │
  │   Con seguimiento asesora: {len(advisor_followup_phones):>4}                        │
  │   Sin seguimiento:        {len(responded_phones - advisor_followup_phones):>5}                        │
  └─────────────────────────────────────────────────────────┘
""")

    # ══════════════════════════════════════════════════════════
    # EXPORTAR JSON para análisis posterior
    # ══════════════════════════════════════════════════════════
    export = {
        "campaign_date": "2026-05-06",
        "analysis_window": "2026-05-06 a 2026-05-20",
        "total_sent": len(bulk_phones),
        "total_responded": len(responded_phones),
        "response_rate": round(len(responded_phones) / max(len(bulk_phones), 1) * 100, 1),
        "no_response": len(no_response_phones),
        "classifications": {
            "positive": len(classifications["positive"]),
            "negative": len(classifications["negative"]),
            "neutral": len(classifications["neutral"]),
            "ambiguous": len(classifications["ambiguous"]),
        },
        "positive_outcomes": {
            "appointment_mention": len(appointment_found),
            "no_match_lost": len(no_match_found),
            "no_clear_outcome": len(positive_no_outcome),
        },
        "followup": {
            "with_advisor_followup": len(advisor_followup_phones),
            "without_followup": len(responded_phones - advisor_followup_phones),
        },
        "response_distribution_by_date": dict(sorted(response_dates.items())),
        "property_type_mentions": dict(tipo_inmueble.most_common()),
        "detailed_positive_responses": [
            {
                "phone_last4": c["phone"][-4:],
                "content": c["content"],
                "contact_id": c.get("hubspot_contact_id", ""),
            }
            for c in classifications["positive"]
        ],
        "detailed_negative_responses": [
            {
                "phone_last4": c["phone"][-4:],
                "content": c["content"],
            }
            for c in classifications["negative"]
        ],
        "detailed_neutral_responses": [
            {
                "phone_last4": c["phone"][-4:],
                "content": c["content"],
            }
            for c in classifications["neutral"]
        ],
        "appointment_details": [
            {
                "phone_last4": e["phone"][-4:],
                "contact_id": e["contact_id"],
                "message_count": e["message_count"],
                "first_response": e["first_response"],
            }
            for e in appointment_found
        ],
        "no_match_details": [
            {
                "phone_last4": e["phone"][-4:],
                "contact_id": e["contact_id"],
                "message_count": e["message_count"],
                "first_response": e["first_response"],
            }
            for e in no_match_found
        ],
        "no_outcome_details": [
            {
                "phone_last4": e["phone"][-4:],
                "contact_id": e["contact_id"],
                "message_count": e["message_count"],
                "first_response": e["first_response"],
            }
            for e in positive_no_outcome
        ],
    }

    json_path = os.path.join(os.path.dirname(__file__), "audit_campana_mayo6_results.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(export, f, ensure_ascii=False, indent=2)
    print(f"\n  📄 Resultados exportados a: {json_path}")

    client.close()
    print("\n✅ Auditoría completada.")


if __name__ == "__main__":
    asyncio.run(main())
