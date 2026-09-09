import pytest

from middleware.phone_normalizer import PhoneNormalizer


@pytest.mark.parametrize(
    ("raw_phone", "expected"),
    [
        ("whatsapp:+573001234567", "+573001234567"),
        ("+573001234567", "+573001234567"),
        ("573001234567", "+573001234567"),
        ("3001234567", "+573001234567"),
        ("03001234567", "+573001234567"),
    ],
)
def test_colombianos_siguen_intactos(raw_phone, expected):
    result = PhoneNormalizer().normalize(raw_phone)

    assert result.is_valid is True
    assert result.normalized == expected
    assert result.country_code == "57"
    assert result.national_number == "3001234567"


@pytest.mark.parametrize(
    ("raw_phone", "expected", "country_code"),
    [
        ("whatsapp:+14155552671", "+14155552671", "1"),
        ("+14155552671", "+14155552671", "1"),
        ("whatsapp:+34600111222", "+34600111222", "34"),
        ("+5215512345678", "+5215512345678", "52"),
        ("+447700900123", "+447700900123", "44"),
    ],
)
def test_extranjeros_con_codigo_explicito_son_validos(raw_phone, expected, country_code):
    result = PhoneNormalizer().normalize(raw_phone)

    assert result.is_valid is True
    assert result.normalized == expected
    assert result.country_code == country_code


@pytest.mark.parametrize(
    "raw_phone",
    [
        "+123",
        "+1234567890123456",
        "whatsapp:+123",
        "whatsapp:+1234567890123456",
    ],
)
def test_extranjeros_fuera_de_rango_e164_se_rechazan(raw_phone):
    result = PhoneNormalizer().normalize(raw_phone)

    assert result.is_valid is False
    assert "internacional" in result.error_message

