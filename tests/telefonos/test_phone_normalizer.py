# tests/middleware/test_phone_normalizer.py
"""
Tests para el normalizador de números telefónicos.
Soporta números colombianos e internacionales E.164.

Ejecutar con: pytest tests/middleware/test_phone_normalizer.py -v
"""

import pytest
import sys
import os

# Agregar el directorio raíz al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Importar directamente del módulo para evitar el __init__.py
from middleware.phone_normalizer import (
    PhoneNormalizer,
    normalize_phone,
    normalize_colombian_phone,
    is_valid_phone,
    is_valid_colombian_phone,
    PhoneValidationResult,
)


class TestPhoneNormalizer:
    """Tests para la clase PhoneNormalizer."""

    def setup_method(self):
        """Setup para cada test."""
        self.normalizer = PhoneNormalizer()

    # ==================== Tests de formato Twilio ====================

    def test_twilio_format_with_plus(self):
        """Test: whatsapp:+573001234567 → +573001234567"""
        result = self.normalizer.normalize("whatsapp:+573001234567")
        assert result.is_valid
        assert result.normalized == "+573001234567"

    def test_twilio_format_without_plus(self):
        """Test: whatsapp:573001234567 → +573001234567"""
        result = self.normalizer.normalize("whatsapp:573001234567")
        assert result.is_valid
        assert result.normalized == "+573001234567"

    # ==================== Tests de formato internacional ====================

    def test_e164_format_already_normalized(self):
        """Test: +573001234567 → +573001234567 (sin cambios)"""
        result = self.normalizer.normalize("+573001234567")
        assert result.is_valid
        assert result.normalized == "+573001234567"

    def test_e164_without_plus(self):
        """Test: 573001234567 → +573001234567"""
        result = self.normalizer.normalize("573001234567")
        assert result.is_valid
        assert result.normalized == "+573001234567"

    # ==================== Tests de formato local colombiano ====================

    def test_local_10_digits(self):
        """Test: 3001234567 → +573001234567"""
        result = self.normalizer.normalize("3001234567")
        assert result.is_valid
        assert result.normalized == "+573001234567"

    def test_local_with_leading_zero(self):
        """Test: 03001234567 → +573001234567"""
        result = self.normalizer.normalize("03001234567")
        assert result.is_valid
        assert result.normalized == "+573001234567"

    # ==================== Tests con espacios y caracteres especiales ====================

    def test_with_spaces(self):
        """Test: +57 300 123 4567 → +573001234567"""
        result = self.normalizer.normalize("+57 300 123 4567")
        assert result.is_valid
        assert result.normalized == "+573001234567"

    def test_with_dashes(self):
        """Test: 300-123-4567 → +573001234567"""
        result = self.normalizer.normalize("300-123-4567")
        assert result.is_valid
        assert result.normalized == "+573001234567"

    def test_with_parentheses(self):
        """Test: (300) 123-4567 → +573001234567"""
        result = self.normalizer.normalize("(300) 123-4567")
        assert result.is_valid
        assert result.normalized == "+573001234567"

    def test_mixed_format(self):
        """Test: +57 (300) 123-4567 → +573001234567"""
        result = self.normalizer.normalize("+57 (300) 123-4567")
        assert result.is_valid
        assert result.normalized == "+573001234567"

    # ==================== Tests de diferentes operadores ====================

    def test_claro_prefix(self):
        """Test: Números Claro (310-315)"""
        result = self.normalizer.normalize("3101234567")
        assert result.is_valid
        assert result.normalized == "+573101234567"

    def test_movistar_prefix(self):
        """Test: Números Movistar (316-319)"""
        result = self.normalizer.normalize("3161234567")
        assert result.is_valid
        assert result.normalized == "+573161234567"

    def test_tigo_prefix(self):
        """Test: Números Tigo (300-304)"""
        result = self.normalizer.normalize("3001234567")
        assert result.is_valid
        assert result.normalized == "+573001234567"

    def test_avantel_prefix(self):
        """Test: Números Avantel (350)"""
        result = self.normalizer.normalize("3501234567")
        assert result.is_valid
        assert result.normalized == "+573501234567"

    # ==================== Tests de validación ====================

    def test_empty_string(self):
        """Test: String vacío → inválido"""
        result = self.normalizer.normalize("")
        assert not result.is_valid
        assert "vacío" in result.error_message.lower()

    def test_too_short(self):
        """Test: Número muy corto → inválido"""
        result = self.normalizer.normalize("300123")
        assert not result.is_valid
        assert "longitud" in result.error_message.lower()

    def test_not_mobile_number(self):
        """Test: Número fijo (no móvil) → inválido"""
        result = self.normalizer.normalize("6012345678")  # Fijo de Bogotá
        assert not result.is_valid
        assert "móvil" in result.error_message.lower()

    def test_only_letters(self):
        """Test: Solo letras → inválido"""
        result = self.normalizer.normalize("abcdefghij")
        assert not result.is_valid

    # ==================== Tests de componentes extraídos ====================

    def test_extracts_country_code(self):
        """Test: Extrae correctamente el código de país"""
        result = self.normalizer.normalize("+573001234567")
        assert result.country_code == "57"

    def test_extracts_national_number(self):
        """Test: Extrae correctamente el número nacional"""
        result = self.normalizer.normalize("+573001234567")
        assert result.national_number == "3001234567"


class TestNormalizeColombianPhone:
    """Tests para la función de conveniencia normalize_colombian_phone."""

    def test_valid_number_returns_normalized(self):
        """Test: Número válido retorna string normalizado"""
        result = normalize_colombian_phone("3001234567")
        assert result == "+573001234567"

    def test_invalid_number_raises_value_error(self):
        """Test: Número inválido lanza ValueError"""
        with pytest.raises(ValueError):
            normalize_colombian_phone("123")


class TestIsValidColombianPhone:
    """Tests para la función is_valid_colombian_phone."""

    def test_valid_number_returns_true(self):
        """Test: Número válido retorna True"""
        assert is_valid_colombian_phone("+573001234567")

    def test_invalid_number_returns_false(self):
        """Test: Número inválido retorna False"""
        assert not is_valid_colombian_phone("123")


class TestEdgeCases:
    """Tests para casos límite y escenarios reales."""

    def setup_method(self):
        """Setup para cada test."""
        self.normalizer = PhoneNormalizer()

    def test_real_twilio_webhook_format(self):
        """Test: Formato real que llega de Twilio webhook"""
        result = self.normalizer.normalize("whatsapp:+573214567890")
        assert result.is_valid
        assert result.normalized == "+573214567890"

    def test_user_input_with_country_code_spaces(self):
        """Test: Usuario escribe con espacios típicos"""
        result = self.normalizer.normalize("57 321 456 7890")
        assert result.is_valid
        assert result.normalized == "+573214567890"

    def test_hubspot_format(self):
        """Test: Formato que puede venir de HubSpot"""
        result = self.normalizer.normalize("+573214567890")
        assert result.is_valid
        assert result.normalized == "+573214567890"

    def test_consistency_multiple_formats_same_number(self):
        """Test: Diferentes formatos del mismo número dan el mismo resultado"""
        formats = [
            "whatsapp:+573001234567",
            "+573001234567",
            "573001234567",
            "3001234567",
            "+57 300 123 4567",
            "300-123-4567",
        ]

        normalized_results = [self.normalizer.normalize(f).normalized for f in formats]

        # Todos deben dar el mismo resultado
        assert all(r == "+573001234567" for r in normalized_results)

    def test_preserves_original_in_result(self):
        """Test: El resultado preserva el número original"""
        original = "whatsapp:+573001234567"
        result = self.normalizer.normalize(original)
        assert result.original == original


class TestInternationalNumbers:
    """Tests para números internacionales (no colombianos)."""

    def setup_method(self):
        self.normalizer = PhoneNormalizer()

    def test_us_number_e164(self):
        """Test: Número EE.UU. en formato E.164"""
        result = self.normalizer.normalize("+12012573369")
        assert result.is_valid
        assert result.normalized == "+12012573369"

    def test_us_number_twilio_format(self):
        """Test: Número EE.UU. con prefijo Twilio"""
        result = self.normalizer.normalize("whatsapp:+12012573369")
        assert result.is_valid
        assert result.normalized == "+12012573369"

    def test_uk_number(self):
        """Test: Número Reino Unido"""
        result = self.normalizer.normalize("+447911123456")
        assert result.is_valid
        assert result.normalized == "+447911123456"

    def test_spain_number(self):
        """Test: Número España"""
        result = self.normalizer.normalize("+34612345678")
        assert result.is_valid
        assert result.normalized == "+34612345678"

    def test_mexico_number(self):
        """Test: Número México"""
        result = self.normalizer.normalize("+525512345678")
        assert result.is_valid
        assert result.normalized == "+525512345678"

    def test_international_with_spaces(self):
        """Test: Número internacional con espacios"""
        result = self.normalizer.normalize("+1 201 257 3369")
        assert result.is_valid
        assert result.normalized == "+12012573369"

    def test_international_with_dashes(self):
        """Test: Número internacional con guiones"""
        result = self.normalizer.normalize("+1-201-257-3369")
        assert result.is_valid
        assert result.normalized == "+12012573369"

    def test_international_too_short(self):
        """Test: Número internacional demasiado corto → inválido"""
        result = self.normalizer.normalize("+12345")
        assert not result.is_valid
        assert "corto" in result.error_message.lower()

    def test_international_too_long(self):
        """Test: Número internacional demasiado largo → inválido"""
        result = self.normalizer.normalize("+12345678901234567")
        assert not result.is_valid
        assert "largo" in result.error_message.lower()

    def test_colombia_still_works_via_international_format(self):
        """Test: Colombia con + sigue siendo colombiano"""
        result = self.normalizer.normalize("+573001234567")
        assert result.is_valid
        assert result.normalized == "+573001234567"
        assert result.country_code == "57"

    def test_international_country_code_detected_us(self):
        """Test: Código de país EE.UU. detectado correctamente"""
        result = self.normalizer.normalize("+12012573369")
        assert result.country_code == "1"

    def test_without_plus_still_assumes_colombia(self):
        """Test: Sin '+' sigue asumiendo Colombia aunque empiece por dígito distinto a 3"""
        # Sin '+' y 10 dígitos → intenta Colombia → falla validación colombiana
        result = self.normalizer.normalize("2012573369")
        assert not result.is_valid  # No es móvil colombiano (no empieza con 3)


class TestNormalizePhone:
    """Tests para la función normalize_phone (nueva)."""

    def test_colombian_number(self):
        result = normalize_phone("3001234567")
        assert result == "+573001234567"

    def test_us_number(self):
        result = normalize_phone("+12012573369")
        assert result == "+12012573369"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            normalize_phone("123")


class TestIsValidPhone:
    """Tests para is_valid_phone (nueva)."""

    def test_colombian_valid(self):
        assert is_valid_phone("+573001234567")

    def test_us_valid(self):
        assert is_valid_phone("+12012573369")

    def test_invalid(self):
        assert not is_valid_phone("123")


# Ejecutar tests si se ejecuta directamente
if __name__ == "__main__":
    pytest.main([__file__, "-v"])