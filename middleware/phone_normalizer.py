# middleware/phone_normalizer.py
"""
Normalizador de Números Telefónicos para Colombia.
"""

import re
from typing import Optional, Tuple
from dataclasses import dataclass
from logging_config import logger


@dataclass
class PhoneValidationResult:
    """Resultado de la validación/normalización de un número telefónico."""

    is_valid: bool
    normalized: str
    original: str
    country_code: str
    national_number: str
    error_message: Optional[str] = None


class PhoneNormalizer:
    """
    Normalizador robusto de números telefónicos para Colombia.

    Diseñado para manejar TODOS los formatos posibles que pueden llegar
    desde Twilio, HubSpot, o entrada manual del usuario.
    """

    # Código de país de Colombia
    COUNTRY_CODE = "57"

    # Prefijos de operadores móviles en Colombia (3XX)
    # Claro: 310-315, 320-321
    # Movistar: 311-314, 320-321 (compartido)
    # Tigo: 300-304
    # Virgin: 319
    # Otros: 316-318, 322-323, 350, etc.
    MOBILE_PREFIXES = {
        "300", "301", "302", "303", "304", "305",  # Tigo/ETB
        "310", "311", "312", "313", "314", "315",  # Claro
        "316", "317", "318", "319",                 # Movistar/Virgin
        "320", "321", "322", "323", "324", "325",  # Claro/Movistar
        "350", "351",                               # Avantel
    }

    # Longitud esperada del número nacional (sin código de país)
    NATIONAL_NUMBER_LENGTH = 10

    # Longitud total con código de país (+57)
    FULL_NUMBER_LENGTH = 12  # Sin el '+'

    def __init__(self, default_country_code: str = "57"):
        """
        Inicializa el normalizador.

        Args:
            default_country_code: Código de país por defecto (Colombia = 57)
        """
        self.default_country_code = default_country_code

    def normalize(self, phone: str) -> PhoneValidationResult:
        """
        Normaliza un número telefónico al formato E.164.

        Args:
            phone: Número en cualquier formato

        Returns:
            PhoneValidationResult con el número normalizado o error
        """
        original = phone

        if not phone:
            return PhoneValidationResult(
                is_valid=False,
                normalized="",
                original=original,
                country_code="",
                national_number="",
                error_message="Número vacío"
            )

        # Paso 1: Limpiar el número
        cleaned = self._clean_phone(phone)

        if not cleaned:
            return PhoneValidationResult(
                is_valid=False,
                normalized="",
                original=original,
                country_code="",
                national_number="",
                error_message="Número no contiene dígitos válidos"
            )

        # Paso 2: Extraer código de país y número nacional
        country_code, national_number = self._extract_components(cleaned)

        # Paso 3: Validar número nacional
        validation_error = self._validate_national_number(national_number)

        if validation_error:
            return PhoneValidationResult(
                is_valid=False,
                normalized="",
                original=original,
                country_code=country_code,
                national_number=national_number,
                error_message=validation_error
            )

        # Paso 4: Construir número normalizado
        normalized = f"+{country_code}{national_number}"

        logger.debug(f"[PhoneNormalizer] {original} → {normalized}")

        return PhoneValidationResult(
            is_valid=True,
            normalized=normalized,
            original=original,
            country_code=country_code,
            national_number=national_number
        )

    def _clean_phone(self, phone: str) -> str:
        """
        Limpia el número eliminando caracteres no numéricos.

        Maneja el caso especial del prefijo 'whatsapp:' de Twilio.
        """
        # Eliminar prefijo de Twilio
        cleaned = phone.replace("whatsapp:", "").strip()

        # Eliminar todos los caracteres no numéricos excepto el '+' inicial
        has_plus = cleaned.startswith("+")

        # Solo mantener dígitos
        cleaned = re.sub(r"[^\d]", "", cleaned)

        # Restaurar '+' si lo tenía (lo usaremos para detectar código de país)
        if has_plus:
            cleaned = "+" + cleaned

        return cleaned

    def _extract_components(self, cleaned: str) -> Tuple[str, str]:
        """
        Extrae código de país y número nacional.

        Casos:
        - +573001234567 → (57, 3001234567)
        - 573001234567 → (57, 3001234567)
        - 3001234567 → (57, 3001234567)  # Asume Colombia
        - 03001234567 → (57, 3001234567)  # Elimina 0 inicial
        """
        # Eliminar el '+' para procesar
        digits = cleaned.lstrip("+")

        # Caso 1: Comienza con código de país Colombia (57)
        if digits.startswith(self.COUNTRY_CODE):
            remaining = digits[len(self.COUNTRY_CODE):]

            # Verificar que lo que sigue sea un número móvil válido
            if len(remaining) == self.NATIONAL_NUMBER_LENGTH:
                return self.COUNTRY_CODE, remaining

            # Si tiene un 0 extra después del código (573001234567 -> correcto)
            # pero 57301234567 sería muy corto
            if len(remaining) == self.NATIONAL_NUMBER_LENGTH - 1:
                # Número incompleto, podría ser un error
                logger.warning(f"[PhoneNormalizer] Número potencialmente incompleto: {cleaned}")
                return self.COUNTRY_CODE, remaining

        # Caso 2: Comienza con 0 (formato local colombiano antiguo)
        if digits.startswith("0"):
            digits = digits[1:]  # Eliminar el 0

        # Caso 3: Es solo el número nacional (10 dígitos comenzando con 3)
        if len(digits) == self.NATIONAL_NUMBER_LENGTH:
            return self.COUNTRY_CODE, digits

        # Caso 4: Es el número completo sin + (12 dígitos)
        if len(digits) == self.FULL_NUMBER_LENGTH and digits.startswith(self.COUNTRY_CODE):
            return self.COUNTRY_CODE, digits[len(self.COUNTRY_CODE):]

        # Caso 5: Número con formato inesperado - intentar extraer los últimos 10 dígitos
        if len(digits) > self.NATIONAL_NUMBER_LENGTH:
            # Asumir que los últimos 10 son el número nacional
            national = digits[-self.NATIONAL_NUMBER_LENGTH:]
            return self.COUNTRY_CODE, national

        # Número muy corto o inválido
        return self.COUNTRY_CODE, digits

    def _validate_national_number(self, national_number: str) -> Optional[str]:
        """
        Valida que el número nacional sea válido para Colombia.

        Returns:
            None si es válido, mensaje de error si no lo es
        """
        # Validar longitud
        if len(national_number) != self.NATIONAL_NUMBER_LENGTH:
            return f"Longitud inválida: {len(national_number)} (esperado: {self.NATIONAL_NUMBER_LENGTH})"

        # Validar que sea un número móvil (comienza con 3)
        if not national_number.startswith("3"):
            return f"No es un número móvil colombiano (debe comenzar con 3)"

        # Validar prefijo de operador (opcional, solo warning)
        prefix = national_number[:3]
        if prefix not in self.MOBILE_PREFIXES:
            logger.warning(f"[PhoneNormalizer] Prefijo no reconocido: {prefix} (puede ser válido)")

        return None


# Función de conveniencia para uso rápido
def normalize_colombian_phone(phone: str) -> str:
    """
    Función de conveniencia para normalizar un número colombiano.

    Args:
        phone: Número en cualquier formato

    Returns:
        Número normalizado en formato E.164 (+573XXXXXXXXX)

    Raises:
        ValueError: Si el número no es válido

    Examples:
        >>> normalize_colombian_phone("whatsapp:+573001234567")
        '+573001234567'
        >>> normalize_colombian_phone("3001234567")
        '+573001234567'
        >>> normalize_colombian_phone("57 300 123 4567")
        '+573001234567'
    """
    normalizer = PhoneNormalizer()
    result = normalizer.normalize(phone)

    if not result.is_valid:
        raise ValueError(f"Número inválido: {result.error_message}")

    return result.normalized


def is_valid_colombian_phone(phone: str) -> bool:
    """
    Verifica si un número es válido para Colombia.

    Args:
        phone: Número a validar

    Returns:
        True si es válido, False si no
    """
    normalizer = PhoneNormalizer()
    return normalizer.normalize(phone).is_valid