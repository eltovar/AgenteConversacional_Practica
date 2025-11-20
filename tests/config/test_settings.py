"""Tests unitarios de Settings con pytest."""
import os
import pytest
from pydantic import ValidationError
from app.config.settings import Settings, OpenAIConfig, AppConfig


@pytest.fixture
def clean_env(monkeypatch):
    """Limpia variables de entorno antes de cada test."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_TEMPERATURE", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("APP_DEBUG", raising=False)
    monkeypatch.delenv("APP_LOG_LEVEL", raising=False)


def test_valid_openai_key(monkeypatch):
    """Test: API key válida es aceptada."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234567890123456789")
    config = OpenAIConfig()
    assert config.api_key == "sk-test1234567890123456789"


def test_invalid_openai_key_format(monkeypatch):
    """Test: API key sin prefijo sk- es rechazada."""
    monkeypatch.setenv("OPENAI_API_KEY", "invalid_key")
    with pytest.raises(ValidationError) as exc_info:
        OpenAIConfig()
    assert 'debe empezar con "sk-"' in str(exc_info.value)


def test_invalid_openai_key_too_short(monkeypatch):
    """Test: API key demasiado corta es rechazada."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-short")
    with pytest.raises(ValidationError) as exc_info:
        OpenAIConfig()
    assert "muy corta" in str(exc_info.value)


def test_missing_openai_key(clean_env):
    """Test: Falta de API key genera error claro."""
    with pytest.raises(ValidationError) as exc_info:
        OpenAIConfig()
    assert "Field required" in str(exc_info.value)


def test_settings_safe_config(monkeypatch):
    """Test: get_safe_config no expone secrets completos."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secretkey1234567890")
    settings = Settings()
    safe = settings.get_safe_config()
    
    # Verificar que la key completa NO está expuesta
    assert "sk-secretkey1234567890" not in str(safe), "API key completa expuesta en safe_config"
    
    # Verificar estructura esperada
    assert safe["openai"]["api_key_configured"] is True
    assert safe["openai"]["model"] == "gpt-4o-mini"
    assert safe["openai"]["api_key_prefix"] == "sk-secr..."
    

def test_environment_validation(monkeypatch):
    """Test: Solo acepta ambientes válidos (development, staging, production)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234567890123456789")
    monkeypatch.setenv("APP_ENV", "invalid_env")
    
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    # Pydantic 2.x usa "Input should be" para Literal
    assert "Input should be" in str(exc_info.value)


def test_default_values(monkeypatch):
    """Test: Valores por defecto son correctos."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234567890123456789")
    settings = Settings()
    
    assert settings.app.env == "development"
    assert settings.app.debug is False
    assert settings.app.log_level == "INFO"
    assert settings.openai.model == "gpt-4o-mini"
    assert settings.openai.temperature == 0.1


def test_custom_temperature_validation(monkeypatch):
    """Test: Temperature fuera de rango [0.0, 2.0] es rechazada."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234567890123456789")
    monkeypatch.setenv("OPENAI_TEMPERATURE", "3.0")
    
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "less than or equal to 2" in str(exc_info.value).lower()


def test_pinecone_optional(monkeypatch):
    """Test: Pinecone es opcional y no bloquea inicialización."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234567890123456789")
    settings = Settings()
    
    assert settings.pinecone.is_configured is False
    assert settings.pinecone.api_key is None


def test_pinecone_partial_config_detected(monkeypatch):
    """Test: Configuración parcial de Pinecone genera error."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234567890123456789")
    monkeypatch.setenv("PINECONE_API_KEY", "test-key")
    # Falta PINECONE_ENVIRONMENT y PINECONE_INDEX_NAME
    
    settings = Settings()
    with pytest.raises(EnvironmentError) as exc_info:
        settings.validate_required_secrets()
    assert "parcialmente configurado" in str(exc_info.value)
