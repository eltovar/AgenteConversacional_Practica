# utils/business_hours.py
"""
Módulo de gestión de horarios laborales.
Determina si estamos en horario de atención y genera mensajes apropiados.
Proyecto Sofía - Inmobiliaria Proteger
"""

from datetime import datetime, time, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo
from logging_config import logger


# Zona horaria de Colombia
TIMEZONE = ZoneInfo("America/Bogota")

# Horarios de atención por día de la semana
# Formato: {día_semana: (hora_apertura, hora_cierre)} o None si está cerrado
# día_semana: 0=Lunes, 1=Martes, ..., 6=Domingo
BUSINESS_HOURS = {
    0: (time(8, 30), time(17, 0)),   # Lunes: 8:30 AM - 5:00 PM
    1: (time(8, 30), time(17, 0)),   # Martes: 8:30 AM - 5:00 PM
    2: (time(8, 30), time(17, 0)),   # Miércoles: 8:30 AM - 5:00 PM
    3: (time(8, 30), time(17, 0)),   # Jueves: 8:30 AM - 5:00 PM
    4: (time(8, 30), time(17, 0)),   # Viernes: 8:30 AM - 5:00 PM
    5: (time(8, 30), time(12, 0)),   # Sábado: 8:30 AM - 12:00 PM
    6: None,                          # Domingo: Cerrado
}

# Nombres de días en español
DAY_NAMES = {
    0: "lunes",
    1: "martes",
    2: "miércoles",
    3: "jueves",
    4: "viernes",
    5: "sábado",
    6: "domingo",
}


def get_current_time() -> datetime:
    """Obtiene la hora actual en zona horaria de Colombia."""
    return datetime.now(TIMEZONE)


def is_business_hours(check_time: Optional[datetime] = None) -> bool:
    """
    Verifica si estamos dentro del horario laboral.

    Args:
        check_time: Hora a verificar (opcional, usa hora actual si no se proporciona)

    Returns:
        True si estamos en horario laboral, False en caso contrario
    """
    now = check_time or get_current_time()
    day = now.weekday()
    current_time = now.time()

    hours = BUSINESS_HOURS.get(day)
    if hours is None:
        return False

    start, end = hours
    return start <= current_time <= end


def get_hours_for_day(day: int) -> Optional[Tuple[time, time]]:
    """
    Obtiene el horario de un día específico.

    Args:
        day: Día de la semana (0=Lunes, 6=Domingo)

    Returns:
        Tupla (apertura, cierre) o None si está cerrado
    """
    return BUSINESS_HOURS.get(day)


def get_next_opening(from_time: Optional[datetime] = None) -> str:
    """
    Calcula cuándo será la próxima apertura y retorna mensaje amigable.

    Args:
        from_time: Hora desde la cual calcular (opcional)

    Returns:
        Mensaje indicando cuándo abre (ej: "hoy a las 8:30 AM", "mañana a las 8:30 AM")
    """
    now = from_time or get_current_time()
    day = now.weekday()
    current_time = now.time()

    # Verificar si aún no abrimos hoy
    today_hours = BUSINESS_HOURS.get(day)
    if today_hours and current_time < today_hours[0]:
        return f"hoy a las {_format_time(today_hours[0])}"

    # Buscar el próximo día que abra
    for i in range(1, 8):
        next_day = (day + i) % 7
        next_hours = BUSINESS_HOURS.get(next_day)
        if next_hours:
            if i == 1:
                return f"mañana a las {_format_time(next_hours[0])}"
            else:
                day_name = DAY_NAMES[next_day]
                return f"el {day_name} a las {_format_time(next_hours[0])}"

    return "pronto"  # Fallback


def get_out_of_hours_message(include_emoji: bool = True) -> str:
    """
    Genera mensaje para clientes que contactan fuera de horario.

    Args:
        include_emoji: Si incluir emoji en el mensaje

    Returns:
        Mensaje amigable indicando que un asesor contactará
    """
    next_open = get_next_opening()
    emoji = " 📝" if include_emoji else ""

    return (
        f"En este momento estamos fuera de nuestro horario de atención. "
        f"Un asesor se pondrá en contacto contigo {next_open}. "
        f"¡Tu solicitud ya quedó registrada!{emoji}"
    )


def get_business_hours_info() -> str:
    """
    Retorna información del horario de atención para mostrar al cliente.

    Returns:
        Texto con horarios de atención
    """
    return (
        "Nuestro horario de atención es:\n"
        "• Lunes a Viernes: 8:30 AM - 5:00 PM\n"
        "• Sábados: 8:30 AM - 12:00 PM\n"
        "• Domingos: Cerrado"
    )


def _format_time(t: time) -> str:
    """
    Formatea una hora en formato AM/PM legible.

    Args:
        t: Objeto time

    Returns:
        String formateado (ej: "8:30 AM", "5:00 PM")
    """
    hour = t.hour
    minute = t.minute
    period = "AM" if hour < 12 else "PM"

    if hour > 12:
        hour -= 12
    elif hour == 0:
        hour = 12

    if minute == 0:
        return f"{hour}:00 {period}"
    else:
        return f"{hour}:{minute:02d} {period}"


def should_add_out_of_hours_message(handoff_priority: str) -> bool:
    """
    Determina si se debe agregar mensaje de fuera de horario.

    Solo se agrega si:
    - El cliente quiere hablar con asesor (handoff high o immediate)
    - Estamos fuera de horario

    Args:
        handoff_priority: Prioridad del handoff ("none", "low", "medium", "high", "immediate")

    Returns:
        True si se debe agregar mensaje de fuera de horario
    """
    if handoff_priority not in ["high", "immediate"]:
        return False

    if is_business_hours():
        return False

    logger.info(
        f"[BusinessHours] Fuera de horario con handoff {handoff_priority} - "
        "Se agregará mensaje de fuera de horario"
    )
    return True


# Función de conveniencia para verificar horario
def check_business_hours() -> dict:
    """
    Retorna estado actual del horario de atención.

    Returns:
        Dict con información del estado actual
    """
    now = get_current_time()
    in_hours = is_business_hours(now)

    return {
        "is_open": in_hours,
        "current_time": now.strftime("%H:%M"),
        "current_day": DAY_NAMES[now.weekday()],
        "next_opening": None if in_hours else get_next_opening(now),
        "timezone": "America/Bogota",
    }