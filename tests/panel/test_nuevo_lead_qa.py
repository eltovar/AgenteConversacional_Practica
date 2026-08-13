"""
QA Fase 6 — Embudo "Nuevo Lead" (1417459250) restaurado.

Objetivo: todo lead nuevo entra en "Nuevo Lead" y SOLO sale cuando una asesora
responde manualmente. Ni Sofia ni los envios masivos lo promueven.

CICLO DE VIDA QUE SE VALIDA:
  1. Lead entra por WhatsApp      -> Nuevo Lead
  2. Sofia responde               -> Nuevo Lead   (sin cambio)
  3. Envio masivo                 -> Nuevo Lead   (sin cambio, ademas excluido)
  4. Asesora responde manualmente -> En conversacion
  5. Asesora responde de nuevo    -> En conversacion (idempotente)
  6. Contacto en otra etapa       -> intacto (guard)

Ejecutar:
    python -m pytest tests/panel/test_nuevo_lead_qa.py -v
"""
import os
import re
import sys

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

NUEVO_LEAD = "1417459250"
EN_CONVERSACION = "1326623075"
CONTACT_ID = "236935263571"
PHONE = "+573001234567"
CANAL = "whatsapp"

PANEL_JS = os.path.join(ROOT, "middleware", "PanelAsesores", "index.js")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _resp(status=200, text="{}"):
    r = MagicMock()
    r.status_code = status
    r.text = text
    return r


# ═══════════════════════════════════════════════════════════════
# PASO 1 — El lead entra en "Nuevo Lead"
# ═══════════════════════════════════════════════════════════════

def test_paso1_lead_nuevo_entra_en_nuevo_lead():
    """La constante de entrada apunta al embudo restaurado, no a En conversacion."""
    from middleware import contact_manager

    assert contact_manager.STAGE_NUEVO_LEAD == NUEVO_LEAD, (
        f"la entrada apunta a {contact_manager.STAGE_NUEVO_LEAD}, "
        f"deberia ser {NUEVO_LEAD} (Nuevo Lead)"
    )


def test_paso1b_creacion_de_lead_usa_la_constante():
    """El payload de creacion en HubSpot debe usar STAGE_NUEVO_LEAD, no un literal."""
    src = _read(os.path.join(ROOT, "middleware", "contact_manager.py"))
    m = re.search(r'"lifecyclestage":\s*(\S+?),', src)
    assert m, "no se encontro lifecyclestage en la creacion de lead"
    assert m.group(1) == "STAGE_NUEVO_LEAD", (
        f"la creacion usa {m.group(1)} en vez de la constante"
    )


# ═══════════════════════════════════════════════════════════════
# PASO 4 — La asesora responde: unica via de promocion
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_paso4_asesora_responde_promueve():
    """Un lead en 'Nuevo Lead' pasa a 'En conversacion' cuando la asesora escribe."""
    from middleware import outbound_panel as op

    client = MagicMock()
    client.patch = AsyncMock(return_value=_resp(200))

    with patch.object(op, "_get_contact_lifecyclestage", AsyncMock(return_value=NUEVO_LEAD)), \
         patch.object(op, "get_httpx_client", lambda: client), \
         patch.object(op, "_invalidate_contact_stage_cache", AsyncMock()), \
         patch.object(op, "HUBSPOT_API_KEY", "test-key"):
        ok = await op._promote_nuevo_lead_to_en_conversacion(CONTACT_ID, PHONE)

    assert ok is True
    enviado = client.patch.await_args.kwargs["json"]["properties"]["lifecyclestage"]
    assert enviado == EN_CONVERSACION, f"promovio a {enviado}"


@pytest.mark.asyncio
async def test_paso5_promocion_es_idempotente():
    """
    Segundo mensaje de la asesora: el contacto ya esta en 'En conversacion',
    el guard corta y NO se hace otro PATCH a HubSpot.
    """
    from middleware import outbound_panel as op

    client = MagicMock()
    client.patch = AsyncMock(return_value=_resp(200))

    with patch.object(op, "_get_contact_lifecyclestage", AsyncMock(return_value=EN_CONVERSACION)), \
         patch.object(op, "get_httpx_client", lambda: client), \
         patch.object(op, "HUBSPOT_API_KEY", "test-key"):
        ok = await op._promote_nuevo_lead_to_en_conversacion(CONTACT_ID, PHONE)

    assert ok is False
    client.patch.assert_not_awaited(), "hizo PATCH innecesario a HubSpot"


@pytest.mark.asyncio
@pytest.mark.parametrize("etapa", [
    "marketingqualifiedlead",  # Visita agendada
    "salesqualifiedlead",      # Visita realizada
    "customer",                # Cerrado ganado
    "evangelist",              # Cerrado perdido
    "1407668893",              # Seguimiento
    "other",                   # No responde
])
async def test_paso6_guard_no_retrocede_etapas_avanzadas(etapa):
    """
    El guard es lo que evita el dano: un contacto que ya avanzo NO debe volver
    a 'En conversacion' porque la asesora le escriba.
    """
    from middleware import outbound_panel as op

    client = MagicMock()
    client.patch = AsyncMock(return_value=_resp(200))

    with patch.object(op, "_get_contact_lifecyclestage", AsyncMock(return_value=etapa)), \
         patch.object(op, "get_httpx_client", lambda: client), \
         patch.object(op, "HUBSPOT_API_KEY", "test-key"):
        ok = await op._promote_nuevo_lead_to_en_conversacion(CONTACT_ID, PHONE)

    assert ok is False, f"promovio desde '{etapa}' — retrocede el pipeline"
    client.patch.assert_not_awaited()


@pytest.mark.asyncio
async def test_paso4b_fallo_de_hubspot_es_no_fatal():
    """Si HubSpot devuelve error, no se propaga: el mensaje ya se envio."""
    from middleware import outbound_panel as op

    client = MagicMock()
    client.patch = AsyncMock(return_value=_resp(429, "rate limit"))

    with patch.object(op, "_get_contact_lifecyclestage", AsyncMock(return_value=NUEVO_LEAD)), \
         patch.object(op, "get_httpx_client", lambda: client), \
         patch.object(op, "HUBSPOT_API_KEY", "test-key"):
        ok = await op._promote_nuevo_lead_to_en_conversacion(CONTACT_ID, PHONE)

    assert ok is False  # sin excepcion


# ═══════════════════════════════════════════════════════════════
# PASOS 2 y 3 — Sofia y los masivos NO promueven
# ═══════════════════════════════════════════════════════════════

def test_paso2y3_solo_send_message_dispara_la_promocion():
    """
    La promocion cuelga de _update_advisor_timestamp, y su unico llamador es
    POST /send-message. Si apareciera otro, Sofia o los masivos podrian
    promover leads que ningun humano contesto.
    """
    src = _read(os.path.join(ROOT, "middleware", "outbound_panel.py"))

    assert "_promote_nuevo_lead_to_en_conversacion" in src

    # La promocion vive dentro de _update_advisor_timestamp
    i = src.index("async def _update_advisor_timestamp")
    cuerpo = src[i:i + 2500]
    assert "_promote_nuevo_lead_to_en_conversacion" in cuerpo, \
        "la promocion no cuelga del envio de la asesora"

    # Y esa funcion se agenda desde un solo sitio
    llamadas = len(re.findall(r"^\s+_update_advisor_timestamp,\s*$", src, re.MULTILINE))
    print(f"\n  [METRIC] Llamadores de _update_advisor_timestamp: {llamadas}")
    assert llamadas == 1, f"esperado 1 llamador, hay {llamadas}"


def test_paso3_masivos_no_pasan_por_el_hook():
    """El envio masivo usa otra ruta y no debe promover."""
    src = _read(os.path.join(ROOT, "middleware", "outbound_panel.py"))

    i = src.index("async def _send_bulk_template_message")
    cuerpo = src[i:i + 6000]
    assert "_update_advisor_timestamp" not in cuerpo, \
        "el envio masivo dispara la promocion de leads"
    assert "_promote_nuevo_lead_to_en_conversacion" not in cuerpo


def test_paso3b_nuevo_lead_excluido_de_masivos():
    """Un lead recien llegado no debe recibir campanas masivas."""
    from middleware import outbound_panel as op

    assert NUEVO_LEAD in op.BULK_EXCLUDED_STAGES, \
        "Nuevo Lead no esta excluido de los envios masivos"

    js = _read(PANEL_JS)
    m = re.search(r"const BULK_EXCLUDED_STAGES = \[(.*?)\];", js, re.DOTALL)
    assert m, "BULK_EXCLUDED_STAGES no encontrado en el frontend"
    assert NUEVO_LEAD in m.group(1), \
        "el frontend permite elegir Nuevo Lead como embudo de masivos"


# ═══════════════════════════════════════════════════════════════
# Coherencia backend <-> frontend
# ═══════════════════════════════════════════════════════════════

def test_etapa_declarada_en_backend_y_frontend():
    """Si falta en una lista, la etapa no se ve o no se puede seleccionar."""
    from middleware import outbound_panel as op

    assert NUEVO_LEAD in op.PIPELINE_STAGES, "falta en PIPELINE_STAGES"
    assert any(s["id"] == NUEVO_LEAD for s in op.PIPELINE_STAGES_LIST), \
        "falta en PIPELINE_STAGES_LIST"

    js = _read(PANEL_JS)
    m = re.search(r"const PIPELINE_STAGES = \[(.*?)\];", js, re.DOTALL)
    assert m, "PIPELINE_STAGES no encontrado en el frontend"
    assert NUEVO_LEAD in m.group(1), "falta en el PIPELINE_STAGES del frontend"


def test_nuevo_lead_es_la_primera_etapa():
    """
    Es el embudo de entrada: debe ir primero en el dropdown, como en HubSpot.
    """
    from middleware import outbound_panel as op

    assert op.PIPELINE_STAGES_LIST[0]["id"] == NUEVO_LEAD, \
        "Nuevo Lead no es la primera etapa del backend"

    js = _read(PANEL_JS)
    m = re.search(r"const PIPELINE_STAGES = \[\s*\{\s*id:\s*\"([^\"]+)\"", js)
    assert m and m.group(1) == NUEVO_LEAD, \
        "Nuevo Lead no es la primera etapa del frontend"


def test_visible_en_filtro_por_encargado():
    """
    Sofia puede agendar una cita antes de que ninguna asesora escriba. Ese
    contacto sigue en 'Nuevo Lead' y debe seguir apareciendo en el filtro por
    encargado, o la visita se pierde de vista.
    """
    src = _read(os.path.join(ROOT, "middleware", "outbound_panel.py"))
    m = re.search(r"STAGES_VISIBLES_WORKER = \{(.*?)\}", src, re.DOTALL)
    assert m, "STAGES_VISIBLES_WORKER no encontrado"
    assert NUEVO_LEAD in m.group(1), \
        "un lead con cita agendada por Sofia desapareceria del filtro por encargado"


# ═══════════════════════════════════════════════════════════════
# Metricas de calidad
# ═══════════════════════════════════════════════════════════════

def test_metricas_distribucion_de_portales_intacta():
    """
    El cambio toca lifecyclestage, no ownership: la reparticion por canal debe
    quedar exactamente igual (Jubeny 14 / Luisa 2).
    """
    registry = _read(os.path.join(ROOT, "utils", "channels_registry.py"))
    jubeny = len(re.findall(r'"owner_id":\s*"89096378"', registry))
    luisa = len(re.findall(r'"owner_id":\s*"89096380"', registry))

    print(f"\n  [METRIC] Canales Jubeny={jubeny} Luisa={luisa}")
    assert jubeny == 14, f"Jubeny quedo con {jubeny} canales"
    assert luisa == 2, f"Luisa quedo con {luisa} canales"
    assert NUEVO_LEAD not in registry, \
        "la etapa no debe aparecer en el registry de canales"


def test_metricas_cobertura_de_la_etapa():
    """Inventario de sitios donde debe estar declarada la etapa nueva."""
    sitios = {
        "contact_manager (entrada)": os.path.join(ROOT, "middleware", "contact_manager.py"),
        "outbound_panel (backend)": os.path.join(ROOT, "middleware", "outbound_panel.py"),
        "index.js (frontend)": PANEL_JS,
    }
    faltan = [n for n, p in sitios.items() if NUEVO_LEAD not in _read(p)]

    print(f"\n  [METRIC] Archivos con la etapa: {len(sitios) - len(faltan)}/{len(sitios)}")
    assert not faltan, f"la etapa falta en: {faltan}"
