# info_tool.py
from pydantic import BaseModel, Field
from langchain_core.tools import tool

# ===== SCHEMAS DE ENTRADA =====

class InfoInstitucionalSchema(BaseModel):
    """Esquema para información institucional de GlobalHome."""
    tema: str = Field(
        description="Tema específico sobre la empresa: contacto, filosofía, misión, historia, cobertura de propiedades, métodos de pago online, etc."
    )

class SoporteContactoSchema(BaseModel):
    """Esquema para consultas de soporte técnico y administrativo."""
    tema: str = Field(
        description="Tema de soporte: administraciones y multas, caja de pagos, contabilidad y facturas, contratos y terminación, servicios públicos, soporte jurídico/legal."
    )

class AsesoriaLegalBlogSchema(BaseModel):
    """Esquema para asesoría legal y artículos educativos sobre arrendamiento."""
    tema: str = Field(
        description="Tema legal/educativo: claves y riesgos del arriendo, legalidad de contratos, estudios y prevención de fraude, gastos de administración, incrementos según ley."
    )

# ===== DEFINICIÓN DE TOOLS =====

@tool("info_institucional", args_schema=InfoInstitucionalSchema)
def info_institucional_func(tema: str) -> str:
    """
    Obtiene información institucional de GlobalHome: contacto (teléfono, email, horarios),
    filosofía empresarial, misión, historia, cobertura geográfica de propiedades,
    métodos de pago online disponibles.

    Útil para preguntas como:
    - "¿Cómo puedo contactar a la empresa?"
    - "¿Cuál es la misión de GlobalHome?"
    - "¿En qué zonas tienen propiedades?"
    - "¿Cómo puedo pagar mi arriendo online?"
    """
    return f"[TOOL] info_institucional ejecutada para tema: '{tema}'"

@tool("soporte_contacto", args_schema=SoporteContactoSchema)
def soporte_contacto_func(tema: str) -> str:
    """
    Proporciona soporte técnico y administrativo para clientes de GlobalHome:
    consultas sobre administraciones y multas, caja de pagos, contabilidad y facturas,
    contratos y procesos de terminación, servicios públicos, soporte jurídico y legal.

    Útil para preguntas como:
    - "Tengo una multa de la administración, ¿qué hago?"
    - "¿Cómo pago en la caja de GlobalHome?"
    - "Necesito mi factura de este mes"
    - "¿Cómo termino mi contrato de arriendo?"
    - "¿Quién paga los servicios públicos?"
    """
    return f"[TOOL] soporte_contacto ejecutada para tema: '{tema}'"

@tool("asesoria_legal_blog", args_schema=AsesoriaLegalBlogSchema)
def asesoria_legal_blog_func(tema: str) -> str:
    """
    Proporciona asesoría legal y artículos educativos sobre arrendamiento en Colombia:
    claves y riesgos del arriendo, legalidad de contratos, estudios y prevención de fraude,
    gastos de administración, incrementos de arriendo según la ley.

    Útil para preguntas como:
    - "¿Qué riesgos debo considerar al arrendar?"
    - "¿Mi contrato de arriendo es legal?"
    - "¿Cómo evitar fraudes al arrendar?"
    - "¿Cuánto pueden subir el arriendo este año?"
    - "¿Qué cubre el gasto de administración?"
    """
    return f"[TOOL] asesoria_legal_blog ejecutada para tema: '{tema}'"

# ===== LISTA DE TODAS LAS TOOLS =====

ALL_TOOLS = [info_institucional_func, soporte_contacto_func, asesoria_legal_blog_func]