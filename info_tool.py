# tool.py
from pydantic import BaseModel, Field
from typing import Literal
from langchain_core.tools import tool # ✅ NUEVO: Importar el decorador

# 1. Definición del Esquema de Entrada (Pydantic)
class InfoEmpresaSchema(BaseModel):
    """Esquema de entrada para obtener información sobre la empresa."""
    accion: Literal["obtener_info"] = Field(
        description="La acción que el usuario quiere realizar. Debe ser 'obtener_info'."
    )
    tema: str = Field(
        description="El tema específico sobre la empresa que busca el usuario (contacto, filosofía, historia, etc.)."
    )

# 2. Definición de la Tool con decorador tool
@tool("info_empresa_contacto_filosofia", args_schema=InfoEmpresaSchema)
def informacion_empresa_func(accion: str, tema: str) -> str:
    """
    Útil para obtener información general sobre la empresa, como contacto, contacto, filosofía, historia o misión.
    """
    if accion == "obtener_info":
        return (f"✅ Herramienta info_empresa_contacto_filosofia ejecutada. "
                f"La acción es '{accion}' para el tema: '{tema}'. "
                f"El agente DEBE ahora buscar información sobre el tema: '{tema}' "
                f"en los RAGs para generar la respuesta final. (Resultado simulado)")
    else:
        return f"Acción '{accion}' no reconocida."

# 3. Lista de Tools (Las funciones decoradas actúan como Tool)
# Notar: La función decorada 'informacion_empresa_func' AHORA es el objeto Tool.
ALL_TOOLS = [informacion_empresa_func]