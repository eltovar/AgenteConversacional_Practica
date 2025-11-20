"""
Sistema centralizado de gestión de configuración y secrets.

Usa Pydantic Settings para validación automática y carga desde múltiples fuentes.

Uso:
    from app.config import settings
    
    # Acceder a configuración
    api_key = settings.openai.api_key
    debug_mode = settings.app.debug
    
    # Logging seguro (sin secrets)
    print(settings.get_safe_config())
"""
from app.config.settings import settings

__all__ = ["settings"]
