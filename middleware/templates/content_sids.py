# middleware/templates/content_sids.py
"""
Fuente única de verdad para los identificadores de plantillas WhatsApp.

En Meta Cloud API el identificador operativo es el nombre de la plantilla
aprobada en WhatsApp Manager. Durante la migración conservamos el nombre
histórico `content_sid` en los modelos internos para no reescribir todo el panel
de una sola vez.

Prioridad de lectura:
1. META_TPL_*: nombre aprobado en Meta.
2. TWILIO_TPL_*: fallback temporal si se fuerza WHATSAPP_PROVIDER=twilio.
3. Nombre lógico Meta como default seguro para desarrollo.
"""

import os
from typing import Optional


def _template(meta_env: str, twilio_env: str, default: str = "") -> str:
    """Lee el identificador de plantilla vigente para el proveedor activo."""
    return (os.getenv(meta_env) or os.getenv(twilio_env) or default).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Plantillas de reactivación y saludo
# ─────────────────────────────────────────────────────────────────────────────
SALUDO_INMUEBLE = _template(
    "META_TPL_SALUDO_INMUEBLE", "TWILIO_TPL_SALUDO_INMUEBLE", "saludo_reactivador_inmueble"
)
REACTIVACION_LINK = _template(
    "META_TPL_REACTIVACION_LINK", "TWILIO_TPL_REACTIVACION_LINK", "reactivacion_inmueble_link"
)
AUN_EN_BUSQUEDA = _template(
    "META_TPL_AUN_EN_BUSQUEDA", "TWILIO_TPL_AUN_EN_BUSQUEDA", "aun_en_busqueda"
)

# ─────────────────────────────────────────────────────────────────────────────
# Plantillas de seguimiento
# ─────────────────────────────────────────────────────────────────────────────
MENSAJE_PERSONALIZADO = _template(
    "META_TPL_MENSAJE_PERSONALIZADO", "TWILIO_TPL_MENSAJE_PERSONALIZADO", "mensaje_personalizado"
)

# ─────────────────────────────────────────────────────────────────────────────
# Plantillas de schedulers (app.py). Los nombres de entorno describen el momento
# del ciclo de la cita: los antiguos TWILIO_FOLLOWUP1/2_TEMPLATE_SID nunca
# llegaron a existir en Railway, así que renombrarlos no rompe nada.
# ─────────────────────────────────────────────────────────────────────────────
FOLLOWUP_1 = _template(
    "META_TPL_CITA_SEGUIMIENTO", "TWILIO_TPL_CITA_SEGUIMIENTO", "seguimiento_cita"
)
FOLLOWUP_2 = _template(
    "META_TPL_CITA_EXPERIENCIA", "TWILIO_TPL_CITA_EXPERIENCIA", "experiencia_cita"
)
RECORDATORIO_CITA: Optional[str] = _template(
    "META_TPL_RECORDATORIO_CITA", "TWILIO_REMINDER_TEMPLATE_SID", "recordatorio_cita"
) or None

# No consumida por el backend — se usa manualmente desde el panel / scripts ad-hoc.
# Se registra aquí para que el SID viva en un solo lugar.
CAMPANA_PROPIETARIOS = _template(
    "META_TPL_CAMPANA_PROPIETARIOS", "TWILIO_TPL_CAMPANA_PROPIETARIOS", "campana_propietarios"
)


# ─────────────────────────────────────────────────────────────────────────────
# Catálogos derivados — consumidos por el backend y servidos al panel
# ─────────────────────────────────────────────────────────────────────────────

# Plantillas habilitadas para envío masivo. La var key="1" (nombre) se auto-fill
# desde HubSpot firstname per-contacto; las demás llegan del frontend como literal.
BULK_TEMPLATES = [
    {
        "sid": AUN_EN_BUSQUEDA,
        "name": "aun_en_busqueda",
        "label": "¿Aún estás interesado?",
        "preview": "Hola {1} 😊 ¿Aún continúas en la búsqueda de un inmueble para arriendo?",
        "vars": [
            {"key": "1", "label": "Nombre del contacto", "auto_fill": "firstname"},
        ],
    },
    {
        # Antes apuntaba a `seguimiento_personalizado`, que no se recreó en la
        # migración. `mensaje_personalizado` tiene exactamente este texto, así que
        # el preview ahora sí coincide con lo que recibe el cliente.
        "sid": MENSAJE_PERSONALIZADO,
        "name": "mensaje_personalizado",
        "label": "Mensaje Personalizado",
        "preview": "Hola {1} ¿Como se encuentra el día de hoy? {2}. Estaré pendiente a su respuesta.",
        "vars": [
            {"key": "1", "label": "Nombre del contacto", "auto_fill": "firstname"},
            {"key": "2", "label": "Mensaje personalizado"},
        ],
    },
]

# Plantillas que el panel puede programar para una fecha específica.
SCHEDULABLE_TEMPLATES = [
    {
        "sid": AUN_EN_BUSQUEDA,
        "name": "Aun_interesado",
        "label": "¿Aún estás interesado?",
        "preview": "Hola {1} 😊 ¿Aún continúas en la búsqueda de un inmueble para arriendo?",
        "vars": [{"key": "1", "label": "Nombre del contacto"}],
    },
    {
        "sid": MENSAJE_PERSONALIZADO,
        "name": "Mensaje_Personalizado",
        "label": "Mensaje Personalizado",
        "preview": "Hola {1} ¿Como se encuentra el día de hoy? {2}. Estaré pendiente a su respuesta.",
        "vars": [
            {"key": "1", "label": "Nombre del contacto"},
            {"key": "2", "label": "Mensaje personalizado"},
        ],
    },
]

# Mapeo SID → metadata, formato esperado por outbound_panel.BULK_ALLOWED_TEMPLATES.
BULK_ALLOWED_TEMPLATES = {
    t["sid"]: {"name": t["name"], "label": t["label"], "vars": t["vars"]}
    for t in BULK_TEMPLATES
    if t["sid"]
}


def all_sids() -> dict:
    """Devuelve {nombre_lógico: sid} — usado para diagnóstico post-migración."""
    return {
        "saludo_inmueble": SALUDO_INMUEBLE,
        "reactivacion_link": REACTIVACION_LINK,
        "aun_en_busqueda": AUN_EN_BUSQUEDA,
        "mensaje_personalizado": MENSAJE_PERSONALIZADO,
        "followup_1": FOLLOWUP_1,
        "followup_2": FOLLOWUP_2,
        "recordatorio_cita": RECORDATORIO_CITA or "",
        "campana_propietarios": CAMPANA_PROPIETARIOS,
    }
