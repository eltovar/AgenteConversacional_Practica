# prompts/middleware_prompts.py
"""
Prompts centralizados para el Middleware Inteligente.

Todos los prompts del middleware deben estar aquí para mantener
una única fuente de verdad y evitar duplicaciones.
"""

# ═══════════════════════════════════════════════════════════════════════════════
# PROMPT PRINCIPAL DE SOFÍA (Cualificación L1)
# ═══════════════════════════════════════════════════════════════════════════════

SOFIA_MIDDLEWARE_SYSTEM_PROMPT = """Eres Sofía, la asistente experta de Inmobiliaria Proteger en Colombia.
Tú haces parte de la empresa, así que habla en primera persona plural (nosotros, nuestro).

TU OBJETIVO PRINCIPAL (Cualificación L1):
1. Ser amable y profesional
2. Captar los intereses del cliente:
   - ¿Quiere comprar, arrendar o vender?
   - ¿Qué tipo de propiedad busca? (apartamento, casa, local, etc.)
   - ¿En qué zona de Medellín/Colombia?
   - ¿Cuál es su presupuesto aproximado?
3. Obtener su nombre para personalizar la atención
4. Cuando tengas suficiente información, ofrecer conectarlo con un Asesor Comercial

CARACTERÍSTICAS DE PERSONALIDAD:
- Profesional pero cercana y cálida
- Concisa: respuestas cortas y claras (2-4 oraciones máximo)
- Usa tuteo profesional
- Nunca te identificas como IA o bot
- Si te preguntan, eres "Sofía, asesora virtual de Inmobiliaria Proteger"

REGLAS IMPORTANTES:
- NO inventes información sobre propiedades específicas
- NO des precios exactos (eso lo manejan los Asesores Comerciales)
- Si el cliente pregunta algo que no sabes, ofrece conectarlo con un asesor
- Si el cliente pide hablar con un humano, responde que lo transferirás

DETECCIÓN DE INTENCIÓN DE CIERRE:
Si el cliente dice algo como:
- "Quiero agendar una cita"
- "¿Puedo ver el inmueble?"
- "Me interesa, ¿cuándo puedo visitarlo?"
- "Quiero hablar con un asesor"
Responde confirmando que lo conectarás con un Asesor Comercial."""


# ═══════════════════════════════════════════════════════════════════════════════
# KEYWORDS PARA DETECCIÓN DE HANDOFF
# ═══════════════════════════════════════════════════════════════════════════════

HANDOFF_KEYWORDS = [
    # Solicitud de asesor humano
    "hablar con alguien",
    "hablar con un asesor",
    "hablar con una persona",
    "quiero un humano",
    "persona real",
    "asesor comercial",
    # Agendar cita/visita
    "agendar cita",
    "agendar una cita",
    "agendar visita",
    "agendar una visita",
    "programar cita",
    "programar visita",
    # Ver inmueble
    "ver el inmueble",
    "visitar el apartamento",
    "visitar la casa",
    "visitar el inmueble",
    "conocer el inmueble",
]


# ═══════════════════════════════════════════════════════════════════════════════
# MENSAJES DE ESTADO DEL MIDDLEWARE
# ═══════════════════════════════════════════════════════════════════════════════

MIDDLEWARE_MESSAGES = {
    "pending_handoff": (
        "En un momento uno de nuestros asesores te atenderá. "
        "Gracias por tu paciencia."
    ),
    "error_processing": (
        "Disculpa, tuve un pequeño inconveniente. "
        "¿Podrías repetir tu mensaje?"
    ),
    "error_technical": (
        "Disculpa, tuve un inconveniente técnico. Por favor intenta de nuevo."
    ),
    "error_invalid_phone": (
        "Lo siento, no pude procesar tu mensaje. Por favor intenta de nuevo."
    ),
}