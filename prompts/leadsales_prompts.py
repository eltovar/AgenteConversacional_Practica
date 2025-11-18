# prompts/leadsales_prompts.py
from prompts.sofia_personality import SOFIA_PERSONALITY

# Template para respuesta de confirmación de handoff (TRANSFERRED_LEADSALES)
LEADSALES_CONFIRMATION_TEMPLATE = (
    SOFIA_PERSONALITY + "\n\n"
    "Gracias, {lead_name}. Tu información ha sido enviada a nuestro equipo de ventas. "
    "Un asesor se pondrá en contacto contigo muy pronto. "
    "¿Tienes alguna otra pregunta informativa sobre nuestros servicios o propiedades?"
)