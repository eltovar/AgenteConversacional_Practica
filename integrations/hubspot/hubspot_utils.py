# integrations/hubspot/hubspot_utils.py
"""
Utilidades para normalización y procesamiento de datos de HubSpot.
Funciones puras sin dependencias externas (fáciles de testear).
"""

import re
from typing import Dict, Any, Optional
from logging_config import logger


def normalize_phone_e164(phone: str) -> str:
    """
    Normaliza el teléfono para que sea el ID único en HubSpot.
    Input: "whatsapp:+5491112345678", "+54 9 11...", "54911..."
    Output: "+5491112345678"
    """
    if not phone:
        return ""

    # 1. Eliminar prefijo de Twilio si existe
    clean_phone = phone.replace("whatsapp:", "").strip()

    # 2. Eliminar espacios, guiones y paréntesis
    clean_phone = re.sub(r"[ \-\(\)]", "", clean_phone)

    # 3. Asegurar que empiece con '+'
    if not clean_phone.startswith("+"):
        clean_phone = f"+{clean_phone}"

    return clean_phone


def calculate_lead_score(lead_data: Dict[str, Any]) -> int:
    """
    Calcula un score de calidad del lead (0-100) basado en completitud de datos.

    Lógica de scoring:
    - Nombre completo: +20 puntos
    - Teléfono válido: +20 puntos (obligatorio)
    - Tipo de propiedad: +15 puntos
    - Ubicación: +15 puntos
    - Presupuesto: +15 puntos
    - Características adicionales: +15 puntos
    """
    score = 0

    # Nombre completo (firstname + lastname)
    if lead_data.get("firstname") and lead_data.get("lastname"):
        score += 20
    elif lead_data.get("firstname"):
        score += 10  # Solo nombre

    # Teléfono (obligatorio, siempre debe existir)
    if lead_data.get("phone"):
        score += 20

    # Metadata de propiedad
    metadata = lead_data.get("metadata", {})

    if metadata.get("tipo_propiedad"):
        score += 15

    if metadata.get("ubicacion"):
        score += 15

    if metadata.get("presupuesto"):
        score += 15

    # Características adicionales (habitaciones, área, etc.)
    if metadata.get("caracteristicas"):
        score += 15

    logger.debug(f"[HubSpotUtils] Lead score calculado: {score}/100")
    return min(score, 100)  # Cap en 100


def split_full_name(full_name: str) -> Dict[str, str]:
    """
    Divide un nombre completo en firstname y lastname.
    """
    parts = full_name.strip().split(maxsplit=1)

    if len(parts) == 2:
        return {"firstname": parts[0], "lastname": parts[1]}
    elif len(parts) == 1:
        return {"firstname": parts[0], "lastname": ""}
    else:
        return {"firstname": full_name, "lastname": ""}


def format_conversation_history(history: list) -> str:
    """
    Formatea el historial de conversación para almacenar en HubSpot.
    """
    if not history:
        return ""

    # Limitar a últimos 20 mensajes para evitar exceder límites de API
    limited_history = history[-20:] if len(history) > 20 else history

    return "\n".join(limited_history)


def validate_hubspot_response(response_data: Dict[str, Any]) -> bool:
    """
    Valida que la respuesta de HubSpot tenga la estructura esperada.
    """
    
    # Validar que tenga 'id' (indicador de éxito en creación/actualización)
    if "id" not in response_data:
        logger.error(f"[HubSpotUtils] Respuesta inválida de HubSpot: falta campo 'id'")
        return False

    return True