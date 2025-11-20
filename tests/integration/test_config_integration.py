"""Tests de integración del sistema de configuración."""
import pytest
import sys
import os
from pathlib import Path
from importlib import reload


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


@pytest.fixture
def temp_env_file(tmp_path, monkeypatch):
    """Crea un archivo .env temporal y configura el entorno."""
    env_file = tmp_path / ".env"

    # Cambiar al directorio temporal
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    monkeypatch.chdir(tmp_path)

    yield env_file

    # Restaurar directorio original
    os.chdir(original_cwd)


def test_app_imports_with_valid_config(monkeypatch, temp_env_file, clean_modules):
    """Test: App completa importa correctamente con config válida."""
    # Crear .env temporal con configuración válida
    temp_env_file.write_text(
        "OPENAI_API_KEY=sk-test1234567890123456789\n"
        "APP_ENV=development\n"
        "APP_DEBUG=true\n"
        "APP_LOG_LEVEL=DEBUG\n"
    )

    # Configurar variables de entorno
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234567890123456789")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("APP_DEBUG", "true")
    monkeypatch.setenv("APP_LOG_LEVEL", "DEBUG")

    # Intentar importar módulo de configuración
    try:
        from app.config import settings

        # Verificar que la configuración se cargó correctamente
        assert settings.openai.api_key == "sk-test1234567890123456789"
        assert settings.app.env == "development"
        assert settings.app.debug is True
        assert settings.app.log_level == "DEBUG"

    except ImportError as e:
        pytest.fail(f"Fallo al importar módulos: {e}")
    except Exception as e:
        pytest.fail(f"Error inesperado: {e}")


def test_app_config_validates_correctly(monkeypatch, clean_modules):
    """Test: Validación de configuración funciona correctamente."""
    # Configurar variables válidas
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-validkey1234567890")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_TEMPERATURE", "0.1")
    monkeypatch.setenv("APP_ENV", "production")

    from app.config import settings

    # Verificar valores
    assert settings.openai.model == "gpt-4o-mini"
    assert settings.openai.temperature == 0.1
    assert settings.app.env == "production"


def test_app_fails_without_openai_key(monkeypatch, clean_modules):
    """Test: Validación de Pydantic requiere api_key."""
    # Este test verifica que la validación custom funciona correctamente
    from pydantic import ValidationError
    from app.config.settings import OpenAIConfig

    # BaseSettings siempre lee env vars, así que probamos la validación
    # creando una instancia con un valor inválido que no pase el validator
    with pytest.raises(ValidationError) as exc_info:
        # Pasar api_key vacía debe fallar el validator validate_api_key_format
        OpenAIConfig(api_key="")

    # Verificar que el error está relacionado con api_key
    error_message = str(exc_info.value).lower()
    assert "api_key" in error_message or "validation" in error_message


def test_app_fails_with_invalid_api_key_format(monkeypatch, clean_modules):
    """Test: App falla con formato de API key inválido."""
    # Key que no empieza con 'sk-'
    monkeypatch.setenv("OPENAI_API_KEY", "invalid-key-format")

    with pytest.raises(Exception) as exc_info:
        from app.config import settings
        _ = settings.openai.api_key

    error_message = str(exc_info.value).lower()
    assert "sk-" in error_message or "validation" in error_message


def test_app_fails_with_invalid_temperature(monkeypatch, clean_modules):
    """Test: App falla con temperatura fuera de rango."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-validkey1234567890")
    monkeypatch.setenv("OPENAI_TEMPERATURE", "3.0")  # Fuera del rango 0.0-2.0

    with pytest.raises(Exception) as exc_info:
        from app.config import settings
        _ = settings.openai.temperature

    error_message = str(exc_info.value).lower()
    assert "less than or equal to 2" in error_message or "validation" in error_message


def test_logging_doesnt_expose_secrets(monkeypatch, clean_modules):
    """Test: get_safe_config() no expone secrets completos."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-supersecret123456789012345")
    monkeypatch.setenv("APP_ENV", "development")

    from app.config import settings

    # Obtener configuración segura
    safe_config = settings.get_safe_config()

    # Convertir a string para simular logging
    safe_config_str = str(safe_config)

    # Verificar que el secret completo NO aparece
    assert "sk-supersecret123456789012345" not in safe_config_str

    # Verificar que el prefijo enmascarado SÍ aparece
    assert safe_config["openai"]["api_key_prefix"] == "sk-supe..."

    # Verificar que la flag de configuración está presente
    assert safe_config["openai"]["api_key_configured"] is True

    # Verificar que el modelo está presente (no es secret)
    assert safe_config["openai"]["model"] == "gpt-4o-mini"


def test_get_safe_config_masks_secrets(monkeypatch, clean_modules):
    """Test: get_safe_config() enmascara secrets correctamente."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-my-super-secret-key-123456789")
    # Configurar Pinecone completo (todas las variables requeridas)
    monkeypatch.setenv("PINECONE_API_KEY", "pc-secret-pinecone-key-987654321")
    monkeypatch.setenv("PINECONE_ENVIRONMENT", "us-east1-gcp")
    monkeypatch.setenv("PINECONE_INDEX_NAME", "test-index")

    from app.config import settings

    safe_config = settings.get_safe_config()

    # Convertir a string para buscar
    safe_config_str = str(safe_config)

    # Verificar que secrets completos NO están
    assert "sk-proj-my-super-secret-key-123456789" not in safe_config_str
    assert "pc-secret-pinecone-key-987654321" not in safe_config_str

    # Verificar que hay indicadores de configuración
    assert safe_config["openai"]["api_key_configured"] is True
    assert safe_config["openai"]["model"] == "gpt-4o-mini"

    # Verificar que Pinecone está configurado pero el secret no aparece
    assert safe_config["pinecone"]["configured"] is True
    assert safe_config["pinecone"]["index_name"] == "test-index"


def test_different_environments_load_correctly(monkeypatch, clean_modules):
    """Test: Diferentes ambientes cargan configuración correctamente."""
    test_cases = [
        ("development", True, "DEBUG"),
        ("staging", False, "INFO"),
        ("production", False, "INFO"),
    ]

    for env, expected_debug, expected_log_level in test_cases:
        # Limpiar módulos entre iteraciones
        if 'app.config' in sys.modules:
            del sys.modules['app.config']
        if 'app.config.settings' in sys.modules:
            del sys.modules['app.config.settings']

        # Configurar ambiente
        monkeypatch.setenv("OPENAI_API_KEY", "sk-testkey1234567890")
        monkeypatch.setenv("APP_ENV", env)
        monkeypatch.setenv("APP_DEBUG", str(expected_debug).lower())
        monkeypatch.setenv("APP_LOG_LEVEL", expected_log_level)

        from app.config import settings

        # Verificar configuración
        assert settings.app.env == env, f"Fallo en ambiente {env}"
        assert settings.app.debug == expected_debug, f"Fallo en debug para {env}"
        assert settings.app.log_level == expected_log_level, f"Fallo en log_level para {env}"


def test_optional_pinecone_config(monkeypatch, clean_modules):
    """Test: Configuración de Pinecone es opcional."""
    # Solo configurar OpenAI (Pinecone es opcional)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-validkey1234567890")
    monkeypatch.delenv("PINECONE_API_KEY", raising=False)
    monkeypatch.delenv("PINECONE_ENVIRONMENT", raising=False)
    monkeypatch.delenv("PINECONE_INDEX_NAME", raising=False)

    # No debería fallar
    from app.config import settings

    # Pinecone debe tener valores por defecto None cuando no está configurado
    assert settings.pinecone.api_key is None
    assert settings.pinecone.environment is None
    assert settings.pinecone.index_name is None

    # Verificar que is_configured retorna False
    assert settings.pinecone.is_configured is False


def test_settings_singleton_behavior(monkeypatch, clean_modules):
    """Test: Settings se comporta como singleton."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-singleton-test-key-12345")
    monkeypatch.setenv("APP_ENV", "development")

    from app.config import settings as settings1
    from app.config import settings as settings2

    # Deberían ser la misma instancia
    assert settings1 is settings2
    assert id(settings1) == id(settings2)

    # Cambios en uno se reflejan en el otro
    assert settings1.openai.api_key == settings2.openai.api_key