# logging_config.py (NUEVO)
import logging
import hashlib
import re
import sys


_PHONE_LIKE_RE = re.compile(r"(?<!\w)(?:whatsapp:)?\+?\d[\d\s().-]{7,}\d(?!\w)")
_TOKEN_QUERY_RE = re.compile(r"([?&](?:token|key|signature|sig|access_token|auth|apikey|api_key)=)[^&\s]+", re.IGNORECASE)


def _mask_phone_match(match):
    raw = match.group(0)
    digits = "".join(re.findall(r"\d+", raw))
    if len(digits) < 10:
        return raw
    suffix = digits[-4:] if len(digits) >= 4 else digits
    digest = hashlib.sha256(digits.encode("utf-8")).hexdigest()[:8]
    return f"phone:***{suffix}#{digest}"


class SensitiveDataFilter(logging.Filter):
    """Filtro defensivo para que un log olvidado no saque PII completa."""

    def filter(self, record):
        message = record.getMessage()
        message = _PHONE_LIKE_RE.sub(_mask_phone_match, message)
        message = _TOKEN_QUERY_RE.sub(r"\1{masked}", message)
        record.msg = message
        record.args = ()
        return True

def setup_logging(level=logging.INFO):
    """
    Configura el sistema de logging para el proyecto.

    Args:
        level: Nivel de logging (default: INFO)
    """
    # Configuración del formato
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    # Configurar el logger raíz
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(SensitiveDataFilter())

    logging.basicConfig(
        level=level,
        format=log_format,
        datefmt=date_format,
        handlers=[handler]
    )

    # Reducir verbosidad de librerías externas
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Logger específico para el proyecto
    logger = logging.getLogger("agent_system")
    logger.setLevel(level)

    return logger

# Instancia global del logger
logger = setup_logging()
