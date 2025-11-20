"""Tests de cambio entre ambientes y configuraciones opcionales."""
import pytest
import sys


@pytest.fixture
def clean_modules():
    """Limpia módulos importados para forzar re-importación."""
    modules_to_remove = [
        'app.config.settings',
        'app.config',
        'app',
    ]
    for module in modules_to_remove:
        if module in sys.modules:
            del sys.modules[module]
    yield
    # Cleanup después del test
    for module in modules_to_remove:
        if module in sys.modules:
            del sys.modules[module]


def test_production_defaults(monkeypatch, clean_modules):
    """Test: Producción tiene debug deshabilitado por defecto."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234567890123456789")
    monkeypatch.setenv("APP_ENV", "production")
    # No configurar APP_DEBUG ni APP_LOG_LEVEL para usar defaults
    monkeypatch.delenv("APP_DEBUG", raising=False)
    monkeypatch.delenv("APP_LOG_LEVEL", raising=False)

    from app.config.settings import Settings
    settings = Settings()

    assert settings.app.env == "production"
    assert settings.app.debug is False
    assert settings.app.log_level == "INFO"


def test_development_allows_debug(monkeypatch, clean_modules):
    """Test: Development permite debug."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234567890123456789")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("APP_DEBUG", "true")

    from app.config.settings import Settings
    settings = Settings()

    assert settings.app.env == "development"
    assert settings.app.debug is True


def test_staging_environment(monkeypatch, clean_modules):
    """Test: Staging es un ambiente válido."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234567890123456789")
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("APP_DEBUG", "false")

    from app.config.settings import Settings
    settings = Settings()

    assert settings.app.env == "staging"
    assert settings.app.debug is False
    assert settings.app.log_level == "INFO"


def test_invalid_environment_fails(monkeypatch, clean_modules):
    """Test: Ambiente inválido causa error al importar el módulo."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234567890123456789")
    monkeypatch.setenv("APP_ENV", "invalid")

    # Al importar app.config.settings con APP_ENV inválido, falla en el singleton
    with pytest.raises((EnvironmentError, OSError)) as exc_info:
        from app.config import settings

    error_message = str(exc_info.value).lower()
    assert "development" in error_message or "staging" in error_message or "production" in error_message


def test_optional_pinecone_config_not_configured(monkeypatch, clean_modules):
    """Test: Pinecone es opcional cuando no está configurado."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234567890123456789")
    monkeypatch.delenv("PINECONE_API_KEY", raising=False)
    monkeypatch.delenv("PINECONE_ENVIRONMENT", raising=False)
    monkeypatch.delenv("PINECONE_INDEX_NAME", raising=False)

    from app.config.settings import Settings
    settings = Settings()

    # Sin Pinecone configurado
    assert settings.pinecone.is_configured is False
    assert settings.pinecone.api_key is None
    assert settings.pinecone.environment is None
    assert settings.pinecone.index_name is None


def test_optional_pinecone_config_fully_configured(monkeypatch, clean_modules):
    """Test: Pinecone se configura correctamente cuando se proveen todas las variables."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234567890123456789")
    monkeypatch.setenv("PINECONE_API_KEY", "test-key")
    monkeypatch.setenv("PINECONE_ENVIRONMENT", "us-east-1")
    monkeypatch.setenv("PINECONE_INDEX_NAME", "test-index")

    from app.config.settings import Settings
    settings = Settings()

    assert settings.pinecone.is_configured is True
    assert settings.pinecone.api_key == "test-key"
    assert settings.pinecone.environment == "us-east-1"
    assert settings.pinecone.index_name == "test-index"


def test_pinecone_partial_configuration_fails(monkeypatch, clean_modules):
    """Test: Pinecone parcialmente configurado falla la validación."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234567890123456789")
    # Solo configurar API key, falta environment e index_name
    monkeypatch.setenv("PINECONE_API_KEY", "test-key")
    monkeypatch.delenv("PINECONE_ENVIRONMENT", raising=False)
    monkeypatch.delenv("PINECONE_INDEX_NAME", raising=False)

    with pytest.raises(EnvironmentError) as exc_info:
        from app.config.settings import Settings
        Settings()

    error_message = str(exc_info.value)
    assert "Pinecone parcialmente configurado" in error_message
    assert "PINECONE_API_KEY" in error_message
    assert "PINECONE_ENVIRONMENT" in error_message
    assert "PINECONE_INDEX_NAME" in error_message


def test_log_level_validation(monkeypatch, clean_modules):
    """Test: Solo niveles de log válidos son aceptados."""
    from pydantic import ValidationError

    valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

    for level in valid_levels:
        # Limpiar módulos entre iteraciones
        if 'app.config.settings' in sys.modules:
            del sys.modules['app.config.settings']

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234567890123456789")
        monkeypatch.setenv("APP_LOG_LEVEL", level)

        from app.config.settings import AppConfig
        config = AppConfig()

        assert config.log_level == level

    # Test nivel inválido - probar directamente con AppConfig
    monkeypatch.setenv("APP_LOG_LEVEL", "INVALID")

    with pytest.raises(ValidationError) as exc_info:
        from app.config.settings import AppConfig
        AppConfig()

    error_message = str(exc_info.value).lower()
    assert "debug" in error_message or "info" in error_message


def test_host_and_port_defaults(monkeypatch, clean_modules):
    """Test: Host y puerto tienen valores por defecto correctos."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234567890123456789")

    from app.config.settings import Settings
    settings = Settings()

    assert settings.app.host == "0.0.0.0"
    assert settings.app.port == 8000


def test_port_validation(monkeypatch, clean_modules):
    """Test: Puerto debe estar en rango válido 1-65535."""
    from pydantic import ValidationError

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234567890123456789")

    # Puerto válido
    monkeypatch.setenv("APP_PORT", "3000")
    from app.config.settings import AppConfig
    config = AppConfig()
    assert config.port == 3000

    # Puerto fuera de rango (alto) - probar directamente con AppConfig
    monkeypatch.setenv("APP_PORT", "70000")

    with pytest.raises(ValidationError) as exc_info:
        AppConfig()

    error_message = str(exc_info.value).lower()
    assert "less than or equal to 65535" in error_message


def test_temperature_range_validation(monkeypatch, clean_modules):
    """Test: Temperature debe estar entre 0.0 y 2.0."""
    from pydantic import ValidationError

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234567890123456789")

    # Temperaturas válidas
    valid_temps = ["0.0", "0.5", "1.0", "1.5", "2.0"]

    for temp in valid_temps:
        if 'app.config.settings' in sys.modules:
            del sys.modules['app.config.settings']

        monkeypatch.setenv("OPENAI_TEMPERATURE", temp)

        from app.config.settings import OpenAIConfig
        config = OpenAIConfig()

        assert config.temperature == float(temp)

    # Temperatura fuera de rango - probar directamente con OpenAIConfig
    monkeypatch.setenv("OPENAI_TEMPERATURE", "3.0")

    with pytest.raises(ValidationError) as exc_info:
        from app.config.settings import OpenAIConfig
        OpenAIConfig()

    error_message = str(exc_info.value).lower()
    assert "less than or equal to 2" in error_message