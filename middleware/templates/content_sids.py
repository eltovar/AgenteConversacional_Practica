# middleware/templates/content_sids.py
"""
Fuente única de verdad para los Content SIDs (HX...) de las plantillas de Twilio.

Why: los Content SIDs pertenecen a la CUENTA de Twilio. Al migrar de cuenta,
TODOS cambian — aunque Meta duplique las plantillas del lado de la WABA.
Antes vivían hardcodeados en 5 lugares (templates.py, outbound_panel.py, app.py
y dos arrays en PanelAsesores/index.js), así que un cambio de cuenta obligaba a
editar cuatro archivos y desplegar bajo presión.

Ahora se resuelven por variable de entorno con el SID vigente como default:
el sistema sigue funcionando sin tocar nada, y el día de la migración basta con
definir las variables en Railway.

Los defaults corresponden a la cuenta Twilio vigente desde la migración del
4-ago-2026 (WABA 1579913223763554). El Account SID no se versiona: vive en
`TWILIO_ACCOUNT_SID` en Railway.
Procedimiento de cambio: docs/RUNBOOK_MIGRACION_TWILIO.md (FASE 5.3)

⚠️ En la migración NO se recrearon 4 plantillas que existían en la cuenta anterior:
`saludo_reactivador`, `seguimiento_cita`, `seguimiento_personalizado` y
`reactivacion_con_link`. Sus constantes y referencias fueron eliminadas.
Textos originales por si hay que rehacerlas: docs/PLANTILLAS_PARA_RECREAR.md
"""

import os
from typing import Optional


def _sid(env_name: str, default: str = "") -> str:
    """Lee un Content SID de entorno, cayendo al default de la cuenta actual."""
    return (os.getenv(env_name) or default).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Plantillas de reactivación y saludo
# ─────────────────────────────────────────────────────────────────────────────
# Twilio: `saludo_inicial` (10-sep-2026). Sustituye a la vieja `saludo_inmueble`,
# que se da de baja en Twilio: su SID ya no se nombra en ningún sitio del repo a
# propósito, para que nada apunte a una plantilla que va a dejar de existir.
# Ambas tenían cero variables, así que el cambio fue un reemplazo limpio de SID.
SALUDO_INMUEBLE = _sid(
    "TWILIO_TPL_SALUDO_INMUEBLE", "HX7dccecd8a5ee36a74a6f1099b088f6a6"
)
REACTIVACION_LINK = _sid(
    "TWILIO_TPL_REACTIVACION_LINK", "HXb733162e38a6786faf5db72133660a0d"
)
AUN_EN_BUSQUEDA = _sid(
    "TWILIO_TPL_AUN_EN_BUSQUEDA", "HXfd6fcb949b5747ca39d7b19af4f988fe"
)

# ─────────────────────────────────────────────────────────────────────────────
# Plantillas de seguimiento
# ─────────────────────────────────────────────────────────────────────────────
MENSAJE_PERSONALIZADO = _sid(
    "TWILIO_TPL_MENSAJE_PERSONALIZADO", "HXbca931e16b226053193dc3e003e06372"
)

# ─────────────────────────────────────────────────────────────────────────────
# Plantillas de schedulers (app.py). Los nombres de entorno describen el momento
# del ciclo de la cita: los antiguos TWILIO_FOLLOWUP1/2_TEMPLATE_SID nunca
# llegaron a existir en Railway, así que renombrarlos no rompe nada.
# ─────────────────────────────────────────────────────────────────────────────
FOLLOWUP_1 = _sid(
    "TWILIO_TPL_CITA_SEGUIMIENTO", "HXb586a84cf325689db3efd19ec2f2e93f"
)
# Twilio: `experiencia_citav2`. Sustituye a `experiencia_cita`
# (HX1f96c23a505ea1b292401dbd7d0de13d), que sigue aprobada pero ya no se consume.
# Misma variable {{1}}=nombre; solo cambia el texto (más corto).
FOLLOWUP_2 = _sid(
    "TWILIO_TPL_CITA_EXPERIENCIA", "HXf09d950f78a7997939a0424203d18598"
)
RECORDATORIO_CITA: Optional[str] = _sid(
    "TWILIO_REMINDER_TEMPLATE_SID", "HXdd7160cc287929ec3ae4d18160e7c51b"
) or None

# No consumida por el backend — se usa manualmente desde el panel / scripts ad-hoc.
# Se registra aquí para que el SID viva en un solo lugar.
CAMPANA_PROPIETARIOS = _sid(
    "TWILIO_TPL_CAMPANA_PROPIETARIOS", "HXfe0ca6d5004d215819a05d504577e4d7"
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
