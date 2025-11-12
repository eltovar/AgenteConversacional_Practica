# logging_config.py (NUEVO)
import logging
import sys

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
    logging.basicConfig(
        level=level,
        format=log_format,
        datefmt=date_format,
        handlers=[
            logging.StreamHandler(sys.stdout)  # Salida a consola
        ]
    )

    # Reducir verbosidad de librerías externas
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Logger específico para el proyecto
    logger = logging.getLogger("agent_system")
    logger.setLevel(level)

    return logger

# Instancia global del logger
logger = setup_logging()
