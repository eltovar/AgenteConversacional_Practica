"""Shared panel authentication helper.

Canonical home of `_validate_api_key`, moved verbatim from
`middleware.outbound_panel` — no behavior changes. The legacy module
re-imports the name so existing patch targets
(`middleware.outbound_panel._validate_api_key`) keep working.
"""

import os
from typing import Optional

from logging_config import logger
from utils.safe_logging import safe_id


def _validate_api_key(api_key: Optional[str]) -> bool:
    """Valida la API key del admin."""
    if not api_key:
        logger.warning("[Panel] API Key no proporcionada en header")
        return False

    # Normalizar entrada (quitar espacios y comillas si existen)
    provided = api_key.strip().strip('"').strip("'")

    # Soporta dos nombres de variable de entorno por compatibilidad
    expected = os.getenv("ADMIN_API_KEY") or os.getenv("PANEL_API_KEY")

    # Si no hay configurada ninguna, usar el valor por defecto seguro para dev
    if not expected:
        expected = "protect_admin_2024_xK9mP3qR"

    # Normalizar valor esperado (quitar espacios y comillas si existen)
    expected = expected.strip().strip('"').strip("'")

    if provided == expected:
        logger.debug(f"[Panel] API Key validada correctamente")
        return True

    # Log para debugging (solo primeros 8 chars por seguridad)
    logger.warning(f"[Panel] API Key inválida. Recibido={safe_id(provided, 'api_key')} Esperado={safe_id(expected, 'api_key')}")
    return False
