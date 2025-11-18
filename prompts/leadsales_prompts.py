# prompts/leadsales_prompts.py

# Template para respuesta de confirmación de handoff (TRANSFERRED_LEADSALES)
LEADSALES_CONFIRMATION_TEMPLATE = (
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