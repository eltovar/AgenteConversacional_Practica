"""Validadores de formato para diferentes tipos de secrets."""
import re


def validate_openai_key(key: str) -> bool:
    """
    Valida formato de API key de OpenAI.
    
    Formato esperado: sk-proj-[alphanumeric] o sk-[alphanumeric]
    
    Args:
        key: String con la API key a validar
        
    Returns:
        bool: True si el formato es válido, False en caso contrario
        
    Examples:
        >>> validate_openai_key("sk-proj-abc123...")
        True
        >>> validate_openai_key("invalid-key")
        False
    """
    if not key or not isinstance(key, str):
        return False
    
    # Patrón para OpenAI keys: sk- o sk-proj- seguido de caracteres alfanuméricos
    pattern = r'^sk-(?:proj-)?[a-zA-Z0-9_-]{20,}$'
    return bool(re.match(pattern, key))


def validate_log_level(level: str) -> bool:
    """
    Valida nivel de logging.
    
    Args:
        level: String con el nivel de log (case-insensitive)
        
    Returns:
        bool: True si el nivel es válido, False en caso contrario
        
    Examples:
        >>> validate_log_level("INFO")
        True
        >>> validate_log_level("debug")
        True
        >>> validate_log_level("INVALID")
        False
    """
    if not level or not isinstance(level, str):
        return False
    
    valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    return level.upper() in valid_levels


def validate_environment(env: str) -> bool:
    """
    Valida ambiente de ejecución.
    
    Args:
        env: String con el ambiente (development, staging, production)
        
    Returns:
        bool: True si el ambiente es válido, False en caso contrario
        
    Examples:
        >>> validate_environment("development")
        True
        >>> validate_environment("prod")
        False
    """
    if not env or not isinstance(env, str):
        return False
    
    valid_envs = ["development", "staging", "production"]
    return env.lower() in valid_envs
