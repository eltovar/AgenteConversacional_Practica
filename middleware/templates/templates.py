"""
Templates predefinidos de WhatsApp (se cargan a Redis si no existen).

content_sid          : Content-SID de Twilio (HXxxx...) asignado tras aprobación de Meta.
                       Con este SID el mensaje llega aunque la ventana de 24h esté cerrada.
                       Dejar en None hasta tener la aprobación.

content_variables_map: Orden de variables que se pasan como {{1}}, {{2}}... al template
                       de Twilio. Debe coincidir con el orden definido en el Content
                       Template Builder de Twilio.

Los content_sid se resuelven desde `content_sids.py` (variables de entorno con el
SID vigente como default). No hardcodear SIDs aquí: cambian al migrar de cuenta Twilio.
"""

from . import content_sids as _sids

# Plantillas que dispara el scheduler por su cuenta (app.py:1562 y app.py:1684).
# No se ofrecen en el picker del chat a NADIE: reenviarlas a mano le llega al
# cliente dos veces. Siguen en el catálogo, se siguen enviando solas y el modal de
# administración las sigue mostrando — ocultar no es borrar.
#
# `cita_confirmacion` también se envía sola (al agendar la cita), pero se decidió
# dejarla visible para poder reenviarla a mano; por eso no está en esta lista.
SOLO_AUTOMATICAS = ("seguimiento_cita", "experiencia_cita")

DEFAULT_TEMPLATES = {
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
    "saludo_reactivador_inmueble": {
        "id": "saludo_reactivador_inmueble",
        "name": "Saludo Inicial",
        "category": "reactivacion",
        # Espejo exacto del texto aprobado en Meta. La clave `saludo_reactivador_inmueble`
        # se conserva: es el id con el que el panel envía y el que ya vive en Redis.
        "body": (
            "Buenos días, como estas? te escribe Yubeny Ocampo. "
            "el inmueble esta disponible, deseás que te agende cita para verlo?"
        ),
        "variables": [],
        # Twilio: saludo_inicial — aprobado Meta
        "content_sid": _sids.SALUDO_INMUEBLE,
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
        "content_sid": _sids.REACTIVACION_LINK,
        "content_variables_map": ["asesor", "inmueble"],
        "is_default": True,
    },
    "seguimiento_cita": {
        "id": "seguimiento_cita",
        "name": "Seguimiento Post-Cita (1/2)",
        "category": "seguimiento",
        "body": (
            "Hola {nombre} 😁 ¿Qué te pareció el inmueble? "
            "Cuéntanos para continuar con tu proceso!!"
        ),
        "variables": ["nombre"],
        # {{1}}=nombre — Twilio: seguimiento_de_cita
        "content_sid": _sids.FOLLOWUP_1,
        "content_variables_map": ["nombre"],
        "is_default": True,
    },
    "experiencia_cita": {
        "id": "experiencia_cita",
        "name": "Encuesta de Experiencia (2/2)",
        "category": "seguimiento",
        "body": (
            "Hola {nombre},  Para nosotros es importante conocer tu experiencia: "
            "https://forms.gle/W3bQbDVFkR4ybVbW6 \n"
            "Esta encuesta toma un minuto y es anónima. "
            "¡Gracias por confiar en nosotros! 💛"
        ),
        "variables": ["nombre"],
        # {{1}}=nombre — Twilio: experiencia_citav2
        "content_sid": _sids.FOLLOWUP_2,
        "content_variables_map": ["nombre"],
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
        "content_sid": _sids.MENSAJE_PERSONALIZADO,
        "content_variables_map": ["nombre", "tema"],
        "is_default": True,
    },
}
