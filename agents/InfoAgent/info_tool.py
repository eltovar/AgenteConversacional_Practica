# info_tool.py
from pydantic import BaseModel, Field
from langchain_core.tools import tool

# ===== SCHEMAS DE ENTRADA =====

class InfoInstitucionalSchema(BaseModel):
    """Esquema para información institucional de Inmobiliaria Proteger."""
    tema: str = Field(
        description="Tema específico sobre la empresa: contacto, filosofía, misión, historia, horarios, cobertura de propiedades, métodos de pago online, etc."
    )

class SoporteContactoSchema(BaseModel):
    """Esquema para consultas de soporte técnico y administrativo."""
    tema: str = Field(
        description="Tema de soporte: administraciones y multas, caja de pagos, contabilidad y facturas, contratos y terminación, servicios públicos, soporte jurídico/legal, reparaciones, estudios de crédito El Libertador para arriendo."
    )

# ===== DEFINICIÓN DE TOOLS =====

@tool("info_institucional", args_schema=InfoInstitucionalSchema)
def info_institucional_func(tema: str) -> str:
    """
    Obtiene información institucional de Inmobiliaria Proteger: contacto (teléfono, email, horarios),
    filosofía empresarial, misión, historia, cobertura geográfica de propiedades,
    métodos de pago online disponibles.
    """
    return f"[TOOL] info_institucional ejecutada para tema: '{tema}'"

@tool("soporte_contacto", args_schema=SoporteContactoSchema)
def soporte_contacto_func(tema: str) -> str:
    """
    Proporciona soporte técnico y administrativo para clientes de Inmobiliaria Proteger:
    consultas sobre administraciones y multas, caja de pagos, contabilidad y facturas,
    contratos y procesos de terminación, servicios públicos, soporte jurídico y legal,
    reparaciones, y estudios de crédito El Libertador para arriendo (requisitos, proceso digital, link).
    """
    return f"[TOOL] soporte_contacto ejecutada para tema: '{tema}'"

# ===== LISTA DE TODAS LAS TOOLS =====

ALL_TOOLS = [info_institucional_func, soporte_contacto_func]