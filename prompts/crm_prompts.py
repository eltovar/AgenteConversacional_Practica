# prompts/crm_prompts.py

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
- urgencia: (inmediata, próximos días, próximo mes, etc.)
- comentarios_adicionales: (cualquier otra información relevante)

Responde SOLO con un JSON válido con las entidades encontradas. Si no encuentras una entidad, omítela del JSON.

Ejemplo de respuesta:
{
    "tipo_propiedad": "apartamento",
    "tipo_operacion": "arriendo",
    "ubicacion": "Chapinero",
    "presupuesto": "2-3 millones",
    "caracteristicas": "2 habitaciones, parqueadero",
    "urgencia": "próximo mes"
}

Mensaje del usuario: {user_message}"""

# ═══════════════════════════════════════════════════════════════════════════════
# PROMPTS PARA CALIFICACIÓN DE LEADS (FASE AWAITING_PROPERTY_DATA)
# ═══════════════════════════════════════════════════════════════════════════════

PROPERTY_QUALIFICATION_PROMPTS = {
    "ubicacion": (
        "¡Perfecto! Para ayudarte mejor, ¿en qué zona o barrio te gustaría "
        "encontrar tu {tipo_propiedad}? 🏠\n\n"
        "Por ejemplo: Chapinero, Usaquén, Poblado, etc."
    ),
    "presupuesto": (
        "Excelente elección. ¿Cuál es tu presupuesto aproximado para "
        "{tipo_operacion}? 💰\n\n"
        "Puedes indicarlo como rango, por ejemplo: '2 a 3 millones' o '200 a 300 millones'.\n"
        "Si prefieres no indicarlo, escribe 'sin definir'."
    ),
    "correo": (
        "¿Me podrías compartir tu correo electrónico para enviarte "
        "la información? 📧"
    ),
    "tiempo": (
        "¿Para cuándo necesitas el inmueble? ⏰\n\n"
        "Por ejemplo: 'inmediato', 'en 2 meses', 'este año', etc.\n"
        "Si aún no lo tienes claro, escribe 'sin prisa'."
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
    "¡Perfecto! Ya tengo toda la información sobre lo que buscas:\n\n"
    "📍 Zona: {ubicacion}\n"
    "💰 Presupuesto: {presupuesto}\n"
    "📧 Correo: {correo}\n"
    "⏰ Tiempo: {tiempo}\n"
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
