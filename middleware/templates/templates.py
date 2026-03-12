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
            "¡Hola, {nombre}! Te habla la asesora {asesor} del Area comercial "
            "Inmobiliaria Proteger ¿Cómo te encuentras el día de hoy?"
        ),
        "variables": ["nombre", "asesor"],
        # Twilio: saludo_reactivador — aprobado Meta (business + user initiated)
        "content_sid": "HXb8ecec6e492b6820ce67d54a6e709a97",
        "content_variables_map": ["nombre", "asesor"],  # {{1}}=nombre, {{2}}=asesor
        "is_default": True,
    },
    "cita_confirmacion": {
        "id": "cita_confirmacion",
        "name": "Confirmación de Cita",
        "category": "cita",
        "body": (
            "Inmobiliaria Proteger confirma la programación de su cita, "
            "con los siguientes detalles:\n\n"
            "📅 Fecha: {fecha}\n"
            "🕒 Hora: {hora}\n"
            "📍 Lugar: {lugar}\n"
            "👤 Asesor asignado: {asesor}\n"
            "📲 Contacto: {contacto}\n\n"
            "Le agradecemos su puntualidad. En caso de requerir una reprogramación "
            "o mayor información, no dude en comunicarse con nosotros.\n"
            "Quedamos atentos a su asistencia."
        ),
        "variables": ["fecha", "hora", "lugar", "asesor", "contacto"],
        "content_sid": None,
        "content_variables_map": ["fecha", "hora", "lugar", "asesor", "contacto"],
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
    "experiencia_post_cita": {
        "id": "experiencia_post_cita",
        "name": "Experiencia después de la cita",
        "category": "agradecimiento",
        "body": (
            "Para nosotros es muy importante conocer tu experiencia para seguir "
            "mejorando la calidad de nuestro servicio. 📈\n"
            "¿Nos podrías regalar tu opinión? https://forms.gle/W3bQbDVFkR4ybVbW6\n"
            "Tus respuestas tomarán solo un minuto, serán anónimas y se usarán "
            "únicamente para optimizar nuestros procesos de atención. 💪\n\n"
            "¡Gracias por confiar en Inmobiliaria Proteger! 💛"
        ),
        "variables": [],
        "content_sid": None,
        "content_variables_map": [],
        "is_default": True,
    },
}
