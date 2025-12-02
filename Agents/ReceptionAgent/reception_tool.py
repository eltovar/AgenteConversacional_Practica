# reception_tool.py (NUEVO)
from pydantic import BaseModel, Field
from typing import Literal
from langchain_core.tools import tool

# ===== SCHEMAS DE ENTRADA =====

class ClassifyIntentSchema(BaseModel):
    """Esquema de entrada para clasificar la intencion del usuario."""
    intent: Literal["info", "leadsales", "ambiguous"] = Field(
        ...,
        description="La intencion del usuario: 'info' (busca información), 'leadsales' (quiere contacto comercial), 'ambiguous' (no está claro)"
    )
    reason: str = Field(
        ...,
        description="Breve justificación de la clasificación (1-2 frases)"
    )

class ExtractPIISchema(BaseModel):
    """Esquema de entrada para extraer información personal del cliente."""
    name: str = Field(
        ...,
        description="El nombre completo del cliente extraído del mensaje"
    )

# ===== DEFINICIÓN DE TOOLS =====

@tool("classify_intent", args_schema=ClassifyIntentSchema)
def classify_intent_func(intent: str, reason: str) -> str:
    """ Clasifica la intención del usuario en una conversación."""
    return f" Intención clasificada como '{intent}'. Razón: {reason}"

@tool("extract_lead_pii", args_schema=ExtractPIISchema)
def extract_lead_pii_func(name: str) -> str:
    """
    Extrae el nombre del cliente de su mensaje.

    Úsala cuando el usuario haya proporcionado su nombre y necesites
    registrarlo para transferirlo al equipo de ventas. """
    return f" Nombre extraído: {name}"

# ===== LISTA DE TODAS LAS TOOLS =====

RECEPTION_TOOLS = [classify_intent_func, extract_lead_pii_func]
