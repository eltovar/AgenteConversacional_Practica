# utils/date_parser.py
"""
Utilidad para parsear fechas en lenguaje natural (español colombiano).

Soporta formatos como:
- "mañana", "pasado mañana"
- "el lunes", "el próximo viernes"
- "en 3 días", "en una semana"
- "el 15 de marzo", "15/03/2024"
- "a las 3pm", "15:00", "en la tarde"
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

import dateparser

logger = logging.getLogger(__name__)

# Timezone de Colombia
TIMEZONE_BOGOTA = ZoneInfo("America/Bogota")


class AppointmentDateParser:
    """Parser de fechas para citas en español colombiano."""

    DATEPARSER_SETTINGS = {
        'PREFER_DATES_FROM': 'future',
        'PREFER_DAY_OF_MONTH': 'first',
        'DATE_ORDER': 'DMY',
        'TIMEZONE': 'America/Bogota',
        'RETURN_AS_TIMEZONE_AWARE': True,
        # Parsers válidos de dateparser: base-formats, timestamp, relative-time, etc.
        'PARSERS': ['relative-time', 'absolute-time', 'base-formats'],
    }

    # Mapeo de expresiones comunes de hora
    HOUR_MAPPINGS = {
        "en la mañana": "09:00",
        "por la mañana": "09:00",
        "temprano": "08:00",
        "medio día": "12:00",
        "mediodía": "12:00",
        "al mediodía": "12:00",
        "en la tarde": "15:00",
        "por la tarde": "15:00",
        "en la noche": "19:00",
        "por la noche": "19:00",
    }

    @classmethod
    def parse_appointment_datetime(
        cls,
        fecha_str: Optional[str],
        hora_str: Optional[str] = None
    ) -> Optional[datetime]:
        """
        Parsea fecha y hora de cita desde strings.

        Args:
            fecha_str: Fecha en formato ISO o lenguaje natural
            hora_str: Hora en formato HH:MM o lenguaje natural

        Returns:
            datetime combinado o None si no se puede parsear
        """
        if not fecha_str:
            return None

        try:
            # Intentar parsear fecha
            parsed_date = dateparser.parse(
                fecha_str,
                languages=['es'],
                settings=cls.DATEPARSER_SETTINGS
            )

            if not parsed_date:
                # Intentar formato ISO directo
                try:
                    parsed_date = datetime.fromisoformat(fecha_str)
                    if parsed_date.tzinfo is None:
                        parsed_date = parsed_date.replace(tzinfo=TIMEZONE_BOGOTA)
                except ValueError:
                    logger.warning(f"[DateParser] No se pudo parsear fecha: {fecha_str}")
                    return None

            # Asegurar que tenga timezone
            if parsed_date.tzinfo is None:
                parsed_date = parsed_date.replace(tzinfo=TIMEZONE_BOGOTA)

            # Si hay hora, parsearla y combinar
            if hora_str:
                hora_parsed = cls._parse_time(hora_str)
                if hora_parsed:
                    parsed_date = parsed_date.replace(
                        hour=hora_parsed[0],
                        minute=hora_parsed[1],
                        second=0,
                        microsecond=0
                    )
            else:
                # Si no hay hora, usar mediodía por defecto
                parsed_date = parsed_date.replace(
                    hour=12,
                    minute=0,
                    second=0,
                    microsecond=0
                )

            return parsed_date

        except Exception as e:
            logger.error(f"[DateParser] Error parseando fecha '{fecha_str}': {e}")
            return None

    @classmethod
    def _parse_time(cls, hora_str: str) -> Optional[Tuple[int, int]]:
        """
        Parsea una expresión de hora.

        Args:
            hora_str: Hora en formato natural o HH:MM

        Returns:
            Tupla (hora, minuto) o None
        """
        if not hora_str:
            return None

        hora_str = hora_str.lower().strip()

        # Verificar mapeo de expresiones comunes
        for expr, time_val in cls.HOUR_MAPPINGS.items():
            if expr in hora_str:
                parts = time_val.split(":")
                return (int(parts[0]), int(parts[1]))

        # Intentar parsear con dateparser
        try:
            parsed = dateparser.parse(
                hora_str,
                languages=['es'],
                settings={'PREFER_DATES_FROM': 'current_period'}
            )
            if parsed:
                return (parsed.hour, parsed.minute)
        except Exception:
            pass

        # Intentar formato HH:MM directo
        try:
            if ":" in hora_str:
                parts = hora_str.replace("pm", "").replace("am", "").strip().split(":")
                hour = int(parts[0])
                minute = int(parts[1]) if len(parts) > 1 else 0

                # Ajustar PM
                if "pm" in hora_str.lower() and hour < 12:
                    hour += 12
                elif "am" in hora_str.lower() and hour == 12:
                    hour = 0

                return (hour, minute)
        except ValueError:
            pass

        # Intentar solo número (hora)
        try:
            # "a las 3", "3pm", etc.
            cleaned = hora_str.replace("a las", "").replace("pm", "").replace("am", "").strip()
            hour = int(cleaned)

            # Si dice PM o es menor a 7, probablemente es PM
            if "pm" in hora_str.lower():
                if hour < 12:
                    hour += 12
            elif hour < 7:  # Asume PM para horas pequeñas (citas usualmente no son a las 3am)
                hour += 12

            return (hour, 0)
        except ValueError:
            pass

        return None

    @classmethod
    def validate_future_appointment(
        cls,
        appointment_dt: datetime
    ) -> Tuple[bool, str]:
        """
        Valida que la cita sea en el futuro y razonable.

        Args:
            appointment_dt: Datetime de la cita

        Returns:
            (es_valida, mensaje_error)
        """
        now = datetime.now(appointment_dt.tzinfo or TIMEZONE_BOGOTA)

        if appointment_dt < now:
            return False, "La fecha de cita ya pasó"

        # Mínimo 1 hora en el futuro
        min_future = now + timedelta(hours=1)
        if appointment_dt < min_future:
            return False, "La cita debe ser al menos 1 hora en el futuro"

        # Máximo 90 días en el futuro
        max_future = now + timedelta(days=90)
        if appointment_dt > max_future:
            return False, "La fecha de cita es muy lejana (máximo 90 días)"

        # Validar hora razonable (7am - 8pm)
        if appointment_dt.hour < 7 or appointment_dt.hour > 20:
            return False, "La hora de cita debe ser entre 7:00 AM y 8:00 PM"

        return True, ""

    @classmethod
    def format_appointment_for_message(
        cls,
        appointment_dt: datetime,
        include_day_name: bool = True
    ) -> str:
        """
        Formatea la fecha/hora de una cita para mostrar en un mensaje.

        Args:
            appointment_dt: Datetime de la cita
            include_day_name: Si incluir el nombre del día

        Returns:
            String formateado como "lunes 15 de marzo a las 3:00 PM"
        """
        # Nombres de días en español
        dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
        meses = [
            "enero", "febrero", "marzo", "abril", "mayo", "junio",
            "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"
        ]

        dia_semana = dias[appointment_dt.weekday()]
        dia = appointment_dt.day
        mes = meses[appointment_dt.month - 1]

        # Formatear hora
        hour = appointment_dt.hour
        minute = appointment_dt.minute
        am_pm = "AM" if hour < 12 else "PM"
        if hour > 12:
            hour -= 12
        elif hour == 0:
            hour = 12

        hora_str = f"{hour}:{minute:02d} {am_pm}"

        if include_day_name:
            return f"{dia_semana} {dia} de {mes} a las {hora_str}"
        else:
            return f"{dia} de {mes} a las {hora_str}"