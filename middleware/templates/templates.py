"""
Templates predefinidos de WhatsApp (se cargan a Redis si no existen).

content_sid          : Content-SID de Twilio (HXxxx...) asignado tras aprobación de Meta.
                       Con este SID el mensaje llega aunque la ventana de 24h esté cerrada.
                       Dejar en None hasta tener la aprobación.

content_variables_map: Orden de variables que se pasan como {{1}}, {{2}}... al template
                       de Twilio. Debe coincidir con el orden definido en el Content
                       Template Builder de Twilio.
"""

DEFAULT_TEMPLATES = {
    "reactivacion_general": {
        "id": "reactivacion_general",
        "name": "Reactivación General",
        "category": "reactivacion",
        "body": (
            "Hola, {nombre}. Te habla la asesora {asesor} de Inmobiliaria Proteger. "
            "¿Cómo has estado?"
        ),
        "variables": ["nombre", "asesor"],
        # Twilio: reactivacion_saludo — pendiente aprobación Meta
        "content_sid": "HXd9f7d3d851e542015b2b9f5aa1fab784",
        "content_variables_map": ["nombre", "asesor"],  # {{1}}=nombre, {{2}}=asesor
        "is_default": True,
    },
    "cita_confirmacion": {
        "id": "cita_confirmacion",
        "name": "Confirmación de Cita",
        "category": "cita",
        "body": (
            "Hola, {nombre}. Te confirmamos tu cita para el {fecha} a las {hora}. "
            "Te esperamos en {direccion}. ¿Nos confirmas tu asistencia?"
        ),
        "variables": ["nombre", "fecha", "hora", "direccion"],
        "content_sid": None,
        "content_variables_map": ["nombre", "fecha", "hora", "direccion"],
        "is_default": True,
    },
    "cita_recordatorio": {
        "id": "cita_recordatorio",
        "name": "Recordatorio de Cita",
        "category": "cita",
        "body": (
            "Hola, {nombre}. Te recordamos que mañana {fecha} tienes cita a las {hora}. "
            "Te esperamos."
        ),
        "variables": ["nombre", "fecha", "hora"],
        "content_sid": None,
        "content_variables_map": ["nombre", "fecha", "hora"],
        "is_default": True,
    },
    "seguimiento_visita": {
        "id": "seguimiento_visita",
        "name": "Seguimiento Post-Visita",
        "category": "seguimiento",
        "body": (
            "Hola, {nombre}. Te habla {asesor} de Inmobiliaria Proteger. "
            "Esperamos que la visita al inmueble haya sido de tu agrado. "
            "¿Te gustaría agendar otra visita o tienes alguna pregunta?"
        ),
        "variables": ["nombre", "asesor"],
        "content_sid": None,
        "content_variables_map": ["nombre", "asesor"],
        "is_default": True,
    },
    "seguimiento_24h": {
        "id": "seguimiento_24h",
        "name": "Seguimiento 24 horas",
        "category": "seguimiento",
        "body": (
            "Hola, {nombre}. Te habla {asesor} de Inmobiliaria Proteger. "
            "¿Pudiste revisar la información que te enviamos? "
            "Estamos aquí para resolver cualquier duda."
        ),
        "variables": ["nombre", "asesor"],
        "content_sid": None,
        "content_variables_map": ["nombre", "asesor"],
        "is_default": True,
    },
    "cita_cancelacion": {
        "id": "cita_cancelacion",
        "name": "Cancelación de Cita",
        "category": "cita",
        "body": (
            "Hola, {nombre}. Lamentamos informarte que la cita del {fecha} a las {hora} "
            "ha sido cancelada. ¿Te gustaría reagendarla para otro momento?"
        ),
        "variables": ["nombre", "fecha", "hora"],
        "content_sid": None,
        "content_variables_map": ["nombre", "fecha", "hora"],
        "is_default": True,
    },
    "cita_reagendar": {
        "id": "cita_reagendar",
        "name": "Reagendar Cita",
        "category": "cita",
        "body": (
            "Hola, {nombre}. ¿Te gustaría reagendar tu cita? "
            "Tenemos disponibilidad el {fecha} a las {hora}. ¿Te funciona?"
        ),
        "variables": ["nombre", "fecha", "hora"],
        "content_sid": None,
        "content_variables_map": ["nombre", "fecha", "hora"],
        "is_default": True,
    },
    "promocion_general": {
        "id": "promocion_general",
        "name": "Promoción General",
        "category": "promocion",
        "body": (
            "Hola, {nombre}. Te habla {asesor} de Inmobiliaria Proteger. "
            "Tenemos una promoción especial para ti. ¿Te gustaría conocer los detalles?"
        ),
        "variables": ["nombre", "asesor"],
        "content_sid": None,
        "content_variables_map": ["nombre", "asesor"],
        "is_default": True,
    },
    "agradecimiento": {
        "id": "agradecimiento",
        "name": "Agradecimiento",
        "category": "seguimiento",
        "body": (
            "Hola, {nombre}. Gracias por confiar en Inmobiliaria Proteger. "
            "Fue un placer atenderte. Si necesitas algo más, aquí estamos para ayudarte."
        ),
        "variables": ["nombre"],
        "content_sid": None,
        "content_variables_map": ["nombre"],
        "is_default": True,
    },
}
