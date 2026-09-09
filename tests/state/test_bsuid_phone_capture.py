"""
Sofía pide el teléfono a los contactos que llegan sin número (BSUID).

La regla es la misma que rige el nombre: se pide una vez, no se insiste y NUNCA
bloquea el handoff. Lo que se fija aquí:

- El teléfono se pide SOLO cuando el contexto lo indica. Para el tráfico
  telefónico —hoy la práctica totalidad— el prompt no cambia ni una línea.
- El LLM propone, `PhoneNormalizer` decide. Un presupuesto o un código de
  inmueble jamás pueden convertirse en la clave de una conversación.
- Se pide una vez por CONVERSACIÓN, no por mensaje.
"""
import json

import fakeredis.aioredis
import pytest

from middleware.conversation_state import ConversationStateManager
from middleware.sofia_brain import MessageAnalysis, SofiaBrain
from prompts.middleware.brain import (
    SINGLE_STREAM_ANALYSIS_SCHEMA,
    SOFIA_SINGLE_STREAM_SYSTEM_PROMPT,
)

CLAVE = "bsuid_aaaabbbbccccddddeeeeffff"


# ═══════════════════════════════════════════════════════════════════════════════
# EL PROMPT
# ═══════════════════════════════════════════════════════════════════════════════


def test_el_prompt_pide_el_campo_telefono_en_el_json():
    assert '"telefono_detectado"' in SOFIA_SINGLE_STREAM_SYSTEM_PROMPT
    assert "- telefono_detectado:" in SOFIA_SINGLE_STREAM_SYSTEM_PROMPT


def test_el_esquema_declara_el_campo_como_opcional():
    props = SINGLE_STREAM_ANALYSIS_SCHEMA["properties"]["analisis"]["properties"]
    assert props["telefono_detectado"]["type"] == ["string", "null"]
    # Nunca obligatorio: un turno sin teléfono es el caso normal.
    assert "telefono_detectado" not in SINGLE_STREAM_ANALYSIS_SCHEMA["properties"]["analisis"]["required"]


def test_el_prompt_no_pide_el_telefono_por_defecto():
    """
    El 100% del tráfico actual llega con teléfono. Si Sofía empezara a pedirlo a
    todo el mundo, añadiría fricción a miles de conversaciones para resolver un
    caso que hoy es minoritario.
    """
    bloque = SOFIA_SINGLE_STREAM_SYSTEM_PROMPT.split("TELÉFONO DEL CLIENTE")[1][:400]
    assert "Por defecto NO pidas el teléfono" in bloque
    assert "SOLO si el contexto lo indica" in SOFIA_SINGLE_STREAM_SYSTEM_PROMPT


def test_el_prompt_deja_claro_que_el_telefono_no_bloquea():
    bloque = SOFIA_SINGLE_STREAM_SYSTEM_PROMPT.split("TELÉFONO DEL CLIENTE")[1][:900]
    assert "nunca bloqueante" in bloque
    assert "NO retengas el handoff" in bloque


def test_el_prompt_avisa_de_los_falsos_positivos():
    """Presupuestos y códigos de inmueble son el error clásico del extractor."""
    guia = SOFIA_SINGLE_STREAM_SYSTEM_PROMPT.split("- telefono_detectado:")[1][:1200]
    assert "presupuesto" in guia.lower()
    assert "código" in guia.lower() or "codigo" in guia.lower()
    assert "NUNCA inventes" in guia


# ═══════════════════════════════════════════════════════════════════════════════
# EL MODELO DE ANÁLISIS
# ═══════════════════════════════════════════════════════════════════════════════


def test_el_analisis_recoge_el_telefono_del_json_del_llm():
    a = MessageAnalysis.from_dict({
        "emocion": "neutral",
        "handoff_priority": "high",
        "nombre_detectado": "Carlos",
        "telefono_detectado": "3001234567",
    })
    assert a.telefono_detectado == "3001234567"
    assert a.nombre_detectado == "Carlos"


def test_un_analisis_sin_telefono_no_rompe():
    a = MessageAnalysis.from_dict({"emocion": "neutral", "handoff_priority": "none"})
    assert a.telefono_detectado is None


def test_el_telefono_viaja_en_to_dict():
    a = MessageAnalysis(telefono_detectado="3001234567")
    assert a.to_dict()["telefono_detectado"] == "3001234567"


# ═══════════════════════════════════════════════════════════════════════════════
# VALIDACIÓN: EL LLM PROPONE, PhoneNormalizer DECIDE
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def validar():
    from middleware.webhook_handler import _validated_phone_from_analysis
    return _validated_phone_from_analysis


@pytest.mark.parametrize("entrada,esperado", [
    ("3001234567", "+573001234567"),
    ("+57 300 123 4567", "+573001234567"),
    ("573001234567", "+573001234567"),
    ("Mi numero es 3109876543", "+573109876543"),
    ("300 123 4567", "+573001234567"),
])
def test_acepta_telefonos_reales(validar, entrada, esperado):
    assert validar(entrada) == esperado


@pytest.mark.parametrize("basura", [
    None,
    "",
    "   ",
    "300 millones",          # presupuesto
    "500.000.000",           # presupuesto en pesos
    "3001",                  # código de inmueble
    "12",                    # área en m2
    "2026",                  # año
    "no me interesa",
    "bsuid_aaaabbbbccccddddeeeeffff",   # la propia clave interna
])
def test_descarta_lo_que_no_es_un_telefono(validar, basura):
    """
    Cada uno de estos, si pasara, se convertiría en la clave de una conversación
    real — un daño que no se deshace solo.
    """
    assert validar(basura) is None


# ═══════════════════════════════════════════════════════════════════════════════
# EL CONTEXTO QUE VE EL LLM
# ═══════════════════════════════════════════════════════════════════════════════


def _contexto(**kwargs):
    # _format_lead_context no usa `self`: se invoca sin construir SofiaBrain,
    # que abriría un pool de Redis.
    return SofiaBrain._format_lead_context(None, kwargs)


def test_sin_needs_phone_no_se_menciona_el_telefono():
    """Es la garantía de no-regresión para los contactos telefónicos."""
    texto = _contexto(firstname="Carlos", chatbot_location="Envigado")
    assert "TELÉFONO" not in texto.upper()


def test_con_needs_phone_se_le_pide_una_vez():
    texto = _contexto(needs_phone=True)
    assert "CONTACTO SIN TELÉFONO" in texto
    assert "UNA sola vez" in texto
    assert "NO retengas el handoff" in texto


def test_si_ya_se_pidio_se_le_dice_que_no_insista():
    texto = _contexto(needs_phone=True, phone_already_asked=True)
    assert "TELÉFONO YA SOLICITADO" in texto
    assert "NO se lo vuelvas a pedir" in texto
    assert "CONTACTO SIN TELÉFONO" not in texto


def test_el_telefono_convive_con_el_contexto_de_portal():
    """
    Un contacto sin teléfono puede llegar además desde Finca Raíz. Las dos
    instrucciones tienen que caber en el mismo turno: si el bloque del teléfono
    estuviera en la cadena elif de los portales, una anularía a la otra.
    """
    texto = _contexto(needs_phone=True, portal_link=True, portal_name="Finca Raíz")
    assert "LINK DE PORTAL INMOBILIARIO" in texto
    assert "CONTACTO SIN TELÉFONO" in texto


# ═══════════════════════════════════════════════════════════════════════════════
# SE PIDE UNA VEZ POR CONVERSACIÓN
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def state_manager():
    sm = ConversationStateManager(redis_url="redis://localhost:6379")
    sm.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return sm


async def test_marcar_pedido_deja_el_flag_en_la_meta(state_manager):
    await state_manager.redis.set(
        f"conv_meta:{CLAVE}:whatsapp",
        json.dumps({"phone_normalized": CLAVE, "identity_type": "bsuid"}),
    )

    assert await state_manager.mark_phone_asked(CLAVE, "whatsapp") is True

    meta = json.loads(await state_manager.redis.get(f"conv_meta:{CLAVE}:whatsapp"))
    assert meta["phone_asked"] is True
    assert meta["phone_asked_at"]


async def test_marcar_dos_veces_no_pisa_la_marca_original(state_manager):
    await state_manager.redis.set(
        f"conv_meta:{CLAVE}:whatsapp", json.dumps({"phone_normalized": CLAVE})
    )
    await state_manager.mark_phone_asked(CLAVE, "whatsapp")
    primera = json.loads(await state_manager.redis.get(f"conv_meta:{CLAVE}:whatsapp"))["phone_asked_at"]

    await state_manager.mark_phone_asked(CLAVE, "whatsapp")
    segunda = json.loads(await state_manager.redis.get(f"conv_meta:{CLAVE}:whatsapp"))["phone_asked_at"]

    assert primera == segunda


async def test_sin_meta_no_se_marca(state_manager):
    """
    Fallar aquí significa que Sofía lo volverá a pedir. Es el fallo benigno de
    los dos: mejor preguntar dos veces que dar por preguntado lo que no se pidió.
    """
    assert await state_manager.mark_phone_asked("bsuid_inexistente", "whatsapp") is False


async def test_el_flag_se_lee_de_vuelta_por_get_meta(state_manager):
    await state_manager.redis.set(
        f"conv_meta:{CLAVE}:whatsapp",
        json.dumps({"phone_normalized": CLAVE, "phone_asked": True}),
    )
    meta = await state_manager.get_meta(CLAVE, "whatsapp")
    assert meta is not None and meta.phone_asked is True


async def test_una_conversacion_nueva_no_tiene_el_flag(state_manager):
    await state_manager.redis.set(
        f"conv_meta:{CLAVE}:whatsapp", json.dumps({"phone_normalized": CLAVE})
    )
    meta = await state_manager.get_meta(CLAVE, "whatsapp")
    assert meta is not None and meta.phone_asked is False
