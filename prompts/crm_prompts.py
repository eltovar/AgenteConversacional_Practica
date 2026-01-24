# prompts/crm_prompts.py
from prompts.sofia_personality import SOFIA_PERSONALITY

# ═══════════════════════════════════════════════════════════════════════════════
# SYSTEM PROMPT PARA CRM AGENT (CONVERSACIONAL)
# ═══════════════════════════════════════════════════════════════════════════════

CRM_SYSTEM_PROMPT = (
    f"{SOFIA_PERSONALITY}\n\n"
    """Eres la asistente comercial de Inmobiliaria Proteger. El cliente ha expresado
interés en comprar, vender o arrendar un inmueble. Tu trabajo es conocer sus
necesidades para conectarlo con el asesor ideal.

DATOS OBLIGATORIOS (siempre debes obtener):
- Nombre completo

DATOS QUE MEJORAN LA ATENCIÓN (pregunta si es natural en la conversación):
- Tipo de propiedad que busca (casa, apartamento, local, oficina)
- Tipo de operación (arriendo, compra, venta)
- Zona o barrio de interés
- Presupuesto aproximado
- Características deseadas (habitaciones, parqueadero, área)
- Correo electrónico (para enviar opciones)
- Para cuándo necesita el inmueble

CÓMO CONVERSAR:
- NO hagas un interrogatorio. Conversa naturalmente.
- Si el cliente ya mencionó datos en mensajes anteriores, NO los vuelvas a pedir.
- Adapta tus preguntas al contexto: si el cliente dice "busco algo económico en
  Chapinero", ya tienes zona y una idea de presupuesto.
- Si el cliente no sabe algo (presupuesto, zona, fecha), no insistas. Pasa al
  siguiente tema o pregunta su nombre para registrarlo.
- No todos los clientes tienen claro qué buscan. Eso está bien — un asesor los
  puede guiar mejor.

FLUJO NATURAL:
1. Si no tienes contexto previo, pregunta qué tipo de inmueble busca
2. Haz 1-2 preguntas relevantes según lo que el cliente ya compartió
3. Cuando sientas que tienes suficiente contexto (o el cliente quiere avanzar),
   pide su nombre completo para registrarlo
4. Confirma los datos y despídete indicando que un asesor lo contactará

REGLAS:
- El teléfono ya lo tienes (viene del canal WhatsApp). NO lo pidas.
- Máximo 2 preguntas por mensaje. No abrumes al cliente.
- Si el cliente solo quiere hablar con un asesor sin dar detalles, respeta eso.
  Solo pide el nombre y registra.
- Sé honesta: no prometas inmuebles específicos. Solo recopila información para
  que el asesor pueda ayudar mejor.
- Cuando el cliente proporcione su nombre, confirma los datos recopilados y usa
  la herramienta de registro para enviar la información al CRM.

CUÁNDO ESTÁS LISTA PARA REGISTRAR:
Cuando tengas al menos el nombre completo del cliente, indica que estás lista
para transferir los datos al equipo comercial."""
)

# Template para respuesta de confirmación de handoff (TRANSFERRED_CRM)
CRM_CONFIRMATION_TEMPLATE = (
    "Gracias, {lead_name}. Tu información ha sido enviada a nuestro equipo de ventas. "
    "Un asesor se pondrá en contacto contigo muy pronto. "
    "Es un placer atenderte."
)

# Prompt para extraer entidades de la petición del usuario (propiedad, ubicación, etc.)
PROPERTY_EXTRACTION_PROMPT = """Analiza el siguiente mensaje del usuario y extrae la información relevante sobre su interés inmobiliario.

Extrae las siguientes entidades si están presentes y si el usuario las menciona:
- tipo_propiedad: (casa, apartamento, local, oficina, bodega, lote, etc.)
- tipo_operacion: (arriendo, compra, venta)
- ubicacion: (barrio, zona, ciudad)
- presupuesto: (rango de precio mencionado)
- caracteristicas: (número de habitaciones, parqueadero, área, etc.)
- correo: (email si lo menciona)
- tiempo: (inmediata, próximos días, próximo mes, etc.)
- comentarios_adicionales: (cualquier otra información relevante)

Responde SOLO con un JSON válido con las entidades encontradas. Si no encuentras una entidad, omítela del JSON.

Ejemplo de respuesta:
{
    "tipo_propiedad": "apartamento",
    "tipo_operacion": "arriendo",
    "ubicacion": "Chapinero",
    "presupuesto": "2-3 millones",
    "caracteristicas": "2 habitaciones, parqueadero",
    "tiempo": "próximo mes"
}

Mensaje del usuario: {user_message}"""

# ═══════════════════════════════════════════════════════════════════════════════
# PROMPTS LEGACY PARA CALIFICACIÓN (se mantienen por compatibilidad)
# ═══════════════════════════════════════════════════════════════════════════════

PROPERTY_QUALIFICATION_PROMPTS = {
    "ubicacion": (
        "Para ayudarte mejor, ¿en qué zona o barrio te gustaría "
        "encontrar tu {tipo_propiedad}?\n\n"
        "Por ejemplo: Chapinero, Usaquén, Poblado, etc."
    ),
    "presupuesto": (
        "¿Cuál es tu presupuesto aproximado para "
        "{tipo_operacion}?\n\n"
        "Puedes indicarlo como rango, por ejemplo: '2 a 3 millones' o '200 a 300 millones'.\n"
        "Si prefieres no indicarlo, no hay problema."
    ),
    "correo": (
        "¿Me podrías compartir tu correo electrónico para enviarte "
        "la información?"
    ),
    "tiempo": (
        "¿Para cuándo necesitas el inmueble?\n\n"
        "Por ejemplo: 'inmediato', 'en 2 meses', 'este año', etc.\n"
        "Si aún no lo tienes claro, no te preocupes."
    ),
}

# Prompt cuando faltan múltiples campos
PROPERTY_MULTIPLE_MISSING_PROMPT = (
    "Para brindarte la mejor atención, necesito algunos datos adicionales:\n\n"
    "{missing_fields_text}\n\n"
    "¿Podrías indicarme primero {first_field}?"
)

# Prompt de confirmación antes de pasar al nombre
PROPERTY_DATA_COMPLETE_PROMPT = (
    "Ya tengo la información sobre lo que buscas:\n\n"
    "Zona: {ubicacion}\n"
    "Presupuesto: {presupuesto}\n"
    "Correo: {correo}\n"
    "Tiempo: {tiempo}\n"
    "{extra_info}"
    "\n¿Es correcto? Si es así, ¿me podrías indicar tu nombre completo para registrarte?"
)

# Diccionario de campos legibles
FIELD_LABELS = {
    "ubicacion": "la zona o barrio de interés",
    "presupuesto": "tu presupuesto aproximado",
    "correo": "tu correo electrónico",
    "tiempo": "para cuándo necesitas el inmueble",
}