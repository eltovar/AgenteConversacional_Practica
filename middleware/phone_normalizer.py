# middleware/phone_normalizer.py
"""
Normalizador de Números Telefónicos con soporte internacional E.164.

Acepta números colombianos (código 57) e internacionales.
Para números sin código de país, asume Colombia por defecto.
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
    Normalizador robusto de números telefónicos al formato E.164.

    - Números colombianos: acepta con o sin código de país (57).
      Sin código de país, asume Colombia por defecto.
    - Números internacionales: requieren '+' con código de país explícito.
    - Maneja el prefijo 'whatsapp:' de Twilio.
    """

    # Código de país por defecto (Colombia)
    COUNTRY_CODE = "57"

    # Prefijos de operadores móviles en Colombia (3XX)
    MOBILE_PREFIXES = {
        "300", "301", "302", "303", "304", "305",  # Tigo/ETB
        "310", "311", "312", "313", "314", "315",  # Claro
        "316", "317", "318", "319",                 # Movistar/Virgin
        "320", "321", "322", "323", "324", "325",  # Claro/Movistar
        "350", "351",                               # Avantel
    }

    # Longitud del número nacional colombiano (sin código de país)
    NATIONAL_NUMBER_LENGTH = 10

    # Longitud total Colombia con código de país (sin '+')
    FULL_NUMBER_LENGTH = 12  # 57 + 10

    # Límites E.164 (ITU-T): total de dígitos sin '+'
    E164_MIN_DIGITS = 7
    E164_MAX_DIGITS = 15

    def __init__(self, default_country_code: str = "57"):
        self.default_country_code = default_country_code

    def normalize(self, phone: str) -> PhoneValidationResult:
        """
        Normaliza un número telefónico al formato E.164.

        - Números con '+' y código distinto a 57 → internacionales
        - Números con código 57 o sin código → Colombia
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

        # Paso 1: Limpiar
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

        # Paso 2: Detectar si es internacional (tiene '+' y NO empieza con 57)
        digits = cleaned.lstrip("+")
        is_explicit_international = (
            cleaned.startswith("+") and
            not digits.startswith(self.COUNTRY_CODE)
        )

        if is_explicit_international:
            return self._parse_international(cleaned, original)

        # Paso 3: Lógica colombiana (con o sin código 57)
        country_code, national_number = self._extract_components(cleaned)
        validation_error = self._validate_colombian_number(national_number)

        if validation_error:
            return PhoneValidationResult(
                is_valid=False,
                normalized="",
                original=original,
                country_code=country_code,
                national_number=national_number,
                error_message=validation_error
            )

        normalized = f"+{country_code}{national_number}"
        logger.debug(f"[PhoneNormalizer] {original} → {normalized}")

        return PhoneValidationResult(
            is_valid=True,
            normalized=normalized,
            original=original,
            country_code=country_code,
            national_number=national_number
        )

    def _parse_international(self, cleaned: str, original: str) -> PhoneValidationResult:
        """
        Valida y normaliza un número E.164 internacional (no colombiano).
        Requiere '+' con código de país explícito.
        Rango válido: 7-15 dígitos totales (ITU-T E.164).
        """
        digits = cleaned.lstrip("+")

        if len(digits) < self.E164_MIN_DIGITS:
            return PhoneValidationResult(
                is_valid=False,
                normalized="",
                original=original,
                country_code="",
                national_number="",
                error_message=f"Número internacional demasiado corto: {len(digits)} dígitos (mínimo {self.E164_MIN_DIGITS})"
            )

        if len(digits) > self.E164_MAX_DIGITS:
            return PhoneValidationResult(
                is_valid=False,
                normalized="",
                original=original,
                country_code="",
                national_number="",
                error_message=f"Número internacional demasiado largo: {len(digits)} dígitos (máximo {self.E164_MAX_DIGITS})"
            )

        normalized = f"+{digits}"
        country_code = self._detect_country_code_prefix(digits)
        national_number = digits[len(country_code):]

        logger.debug(f"[PhoneNormalizer] Internacional: {original} → {normalized}")

        return PhoneValidationResult(
            is_valid=True,
            normalized=normalized,
            original=original,
            country_code=country_code,
            national_number=national_number
        )

    def _detect_country_code_prefix(self, digits: str) -> str:
        """
        Detecta el código de país basándose en el primer dígito (zona ITU-T).
        Solo para fines informativos — no afecta la normalización.

        Zona 1: USA/Canadá (+1)
        Zona 2: África (+2X)
        Zona 3-4: Europa (+3X, +4X)
        Zona 5: América Latina (+5X, ya manejado Colombia aparte)
        Zona 6: Asia Pacífico (+6X)
        Zona 7: Rusia (+7)
        Zona 8: Asia Oriental (+8X)
        Zona 9: Medio Oriente/Asia Central (+9X)
        """
        if digits.startswith("1"):
            return "1"
        if digits.startswith("7"):
            return "7"
        # Para el resto, devolvemos los primeros 2 dígitos como estimación
        return digits[:2] if len(digits) >= 2 else digits[:1]

    def _clean_phone(self, phone: str) -> str:
        """
        Limpia el número eliminando caracteres no numéricos.
        Maneja el prefijo 'whatsapp:' de Twilio y preserva el '+' inicial.
        """
        cleaned = phone.replace("whatsapp:", "").strip()
        has_plus = cleaned.startswith("+")
        cleaned = re.sub(r"[^\d]", "", cleaned)
        if has_plus:
            cleaned = "+" + cleaned
        return cleaned

    def _extract_components(self, cleaned: str) -> Tuple[str, str]:
        """
        Extrae código de país y número nacional para números colombianos.

        Casos soportados:
        - +573001234567 → (57, 3001234567)
        - 573001234567  → (57, 3001234567)
        - 3001234567    → (57, 3001234567)  asume Colombia
        - 03001234567   → (57, 3001234567)  elimina 0 inicial
        """
        digits = cleaned.lstrip("+")

        # Caso 1: Comienza con código Colombia (57)
        if digits.startswith(self.COUNTRY_CODE):
            remaining = digits[len(self.COUNTRY_CODE):]
            if len(remaining) == self.NATIONAL_NUMBER_LENGTH:
                return self.COUNTRY_CODE, remaining
            if len(remaining) == self.NATIONAL_NUMBER_LENGTH - 1:
                logger.warning(f"[PhoneNormalizer] Número potencialmente incompleto: {cleaned}")
                return self.COUNTRY_CODE, remaining

        # Caso 2: Comienza con 0 (formato local colombiano antiguo)
        if digits.startswith("0"):
            digits = digits[1:]

        # Caso 3: Solo número nacional (10 dígitos)
        if len(digits) == self.NATIONAL_NUMBER_LENGTH:
            return self.COUNTRY_CODE, digits

        # Caso 4: Número completo sin '+' (12 dígitos)
        if len(digits) == self.FULL_NUMBER_LENGTH and digits.startswith(self.COUNTRY_CODE):
            return self.COUNTRY_CODE, digits[len(self.COUNTRY_CODE):]

        # Caso 5: Más de 10 dígitos — tomar los últimos 10
        if len(digits) > self.NATIONAL_NUMBER_LENGTH:
            national = digits[-self.NATIONAL_NUMBER_LENGTH:]
            return self.COUNTRY_CODE, national

        return self.COUNTRY_CODE, digits

    def _validate_colombian_number(self, national_number: str) -> Optional[str]:
        """
        Valida que el número nacional sea un móvil colombiano válido.
        Returns: None si válido, mensaje de error si no.
        """
        if len(national_number) != self.NATIONAL_NUMBER_LENGTH:
            return f"Longitud inválida: {len(national_number)} (esperado: {self.NATIONAL_NUMBER_LENGTH})"

        if not national_number.startswith("3"):
            return "No es un número móvil colombiano (debe comenzar con 3)"

        prefix = national_number[:3]
        if prefix not in self.MOBILE_PREFIXES:
            logger.warning(f"[PhoneNormalizer] Prefijo no reconocido: {prefix} (puede ser válido)")

        return None


# ─── Funciones de conveniencia ────────────────────────────────────────────────

def normalize_phone(phone: str) -> str:
    """
    Normaliza cualquier número telefónico a E.164.
    Acepta colombianos e internacionales.

    Raises:
        ValueError: Si el número no es válido
    """
    result = PhoneNormalizer().normalize(phone)
    if not result.is_valid:
        raise ValueError(f"Número inválido: {result.error_message}")
    return result.normalized


def normalize_colombian_phone(phone: str) -> str:
    """
    Alias de normalize_phone() para compatibilidad hacia atrás.

    Raises:
        ValueError: Si el número no es válido
    """
    return normalize_phone(phone)


def is_valid_phone(phone: str) -> bool:
    """Verifica si un número es válido (colombiano o internacional)."""
    return PhoneNormalizer().normalize(phone).is_valid


def is_valid_colombian_phone(phone: str) -> bool:
    """
    Alias de is_valid_phone() para compatibilidad hacia atrás.
    """
    return is_valid_phone(phone)
