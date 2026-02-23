# Templates predefinidos (se cargan a Redis si no existen)
DEFAULT_TEMPLATES = {
    "reactivacion_general": {
        "id": "reactivacion_general",
        "name": "Reactivación General",
        "category": "reactivacion",
        "body": "¡Hola {nombre}! Soy del equipo de Inmobiliaria Proteger. ¿Sigues interesado/a en nuestros servicios inmobiliarios? Estamos aquí para ayudarte.",
        "variables": ["nombre"],
        "is_default": True
    },
    "cita_confirmacion": {
        "id": "cita_confirmacion",
        "name": "Confirmación de Cita",
        "category": "cita",
        "body": "¡Hola {nombre}! Te confirmamos tu cita para el {fecha} a las {hora}. Te esperamos en {direccion}. ¿Nos confirmas tu asistencia?",
        "variables": ["nombre", "fecha", "hora", "direccion"],
        "is_default": True
    },
    "cita_recordatorio": {
        "id": "cita_recordatorio",
        "name": "Recordatorio de Cita",
        "category": "cita",
        "body": "¡Hola {nombre}! Te recordamos que mañana {fecha} tienes cita a las {hora}. ¡Te esperamos!",
        "variables": ["nombre", "fecha", "hora"],
        "is_default": True
    },
    "seguimiento_visita": {
        "id": "seguimiento_visita",
        "name": "Seguimiento Post-Visita",
        "category": "seguimiento",
        "body": "¡Hola {nombre}! Esperamos que la visita al inmueble haya sido de tu agrado. ¿Te gustaría agendar otra visita o tienes alguna pregunta?",
        "variables": ["nombre"],
        "is_default": True
    },
    "seguimiento_24h": {
        "id": "seguimiento_24h",
        "name": "Seguimiento 24 horas",
        "category": "seguimiento",
        "body": "¡Hola {nombre}! ¿Pudiste revisar la información que te enviamos? Estamos aquí para resolver cualquier duda.",
        "variables": ["nombre"],
        "is_default": True
    },
    "cita_cancelacion": {
        "id": "cita_cancelacion",
        "name": "Cancelación de Cita",
        "category": "cita",
        "body": "¡Hola {nombre}! Lamentamos informarte que la cita del {fecha} a las {hora} ha sido cancelada. ¿Te gustaría reagendarla para otro momento?",
        "variables": ["nombre", "fecha", "hora"],
        "is_default": True
    },
    "cita_reagendar": {
        "id": "cita_reagendar",
        "name": "Reagendar Cita",
        "category": "cita",
        "body": "¡Hola {nombre}! ¿Te gustaría reagendar tu cita? Tenemos disponibilidad el {fecha} a las {hora}. ¿Te funciona?",
        "variables": ["nombre", "fecha", "hora"],
        "is_default": True
    },
    "promocion_general": {
        "id": "promocion_general",
        "name": "Promoción General",
        "category": "promocion",
        "body": "¡Hola {nombre}! Tenemos una promoción especial para ti. ¿Te gustaría conocer los detalles?",
        "variables": ["nombre"],
        "is_default": True
    },
    "agradecimiento": {
        "id": "agradecimiento",
        "name": "Agradecimiento",
        "category": "seguimiento",
        "body": "¡Hola {nombre}! Gracias por confiar en Inmobiliaria Proteger. Fue un placer atenderte. Si necesitas algo más, aquí estamos para ayudarte.",
        "variables": ["nombre"],
        "is_default": True
    },
}
