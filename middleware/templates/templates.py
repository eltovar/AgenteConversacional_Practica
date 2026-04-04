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
    "saludo_reactivador_inmueble": {
        "id": "saludo_reactivador_inmueble",
        "name": "Saludo Reactivador Inmueble",
        "category": "reactivacion",
        "body": (
            "Cordial saludo,\n"
            "Le escribe Yubeny Ocampo de Inmobiliaria Proteger.\n"
            "Espero que se encuentre muy bien.\n"
            "Lo contacto porque me informan que se encuentra en búsqueda de inmueble para arriendo."
        ),
        "variables": [],
        # Twilio: saludo_reactivador_inmueble — aprobado Meta
        "content_sid": "HX3a6e93c829bb7704476d398dc9639f21",
        "content_variables_map": [],
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
    "reactivacion_inmueble_link": {
        "id": "reactivacion_inmueble_link",
        "name": "Reactivación Inmueble",
        "category": "reactivacion",
        "body": (
            "Cordial saludo,\n"
            "Le escribe {asesor} de Inmobiliaria Proteger.\n"
            "Espero que se encuentre muy bien.\n"
            "Lo contacto porque me informan que se encuentra interesado en el inmueble "
            "{inmueble} Quedo atenta a tu respuesta."
        ),
        "variables": ["asesor", "inmueble"],
        # {{1}}=asesor, {{2}}=inmueble
        "content_sid": "HX00e43ed82eb07074ca930e5974e18b09",
        "content_variables_map": ["asesor", "inmueble"],
        "is_default": True,
    },
    "seguimiento_personalizado": {
        "id": "seguimiento_personalizado",
        "name": "Seguimiento Personalizado",
        "category": "seguimiento",
        "body": (
            "Hola {nombre} ¿Como se encuentra el día de hoy? "
            "{tema}. Estaré pendiente a su respuesta."
        ),
        "variables": ["nombre", "tema"],
        # {{1}}=nombre, {{2}}=tema
        "content_sid": "HX015a4c21c7aeb082448aeaa97396dbbf",
        "content_variables_map": ["nombre", "tema"],
        "is_default": True,
    },
}
