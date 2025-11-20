"""Tests de validadores de secrets."""
import pytest
from app.config.secrets_validator import (
    validate_openai_key,
    validate_log_level,
    validate_environment
)


class TestValidateOpenAIKey:
    """Tests para validate_openai_key."""
    
    def test_valid_sk_format(self):
        """API key con formato sk- es válida."""
        assert validate_openai_key("sk-1234567890abcdefghij") is True
    
    def test_valid_sk_proj_format(self):
        """API key con formato sk-proj- es válida."""
        assert validate_openai_key("sk-proj-longerkey1234567890") is True
    
    def test_invalid_prefix(self):
        """Key sin prefijo sk- es inválida."""
        assert validate_openai_key("invalid-key") is False
    
    def test_too_short(self):
        """Key demasiado corta es inválida."""
        assert validate_openai_key("sk-short") is False
    
    def test_empty_string(self):
        """String vacío es inválido."""
        assert validate_openai_key("") is False
    
    def test_none_value(self):
        """None es inválido."""
        assert validate_openai_key(None) is False
    
    def test_non_string(self):
        """Valor no-string es inválido."""
        assert validate_openai_key(12345) is False


class TestValidateLogLevel:
    """Tests para validate_log_level."""
    
    def test_valid_uppercase(self):
        """Niveles válidos en mayúsculas."""
        assert validate_log_level("DEBUG") is True
        assert validate_log_level("INFO") is True
        assert validate_log_level("WARNING") is True
        assert validate_log_level("ERROR") is True
        assert validate_log_level("CRITICAL") is True
    
    def test_valid_lowercase(self):
        """Niveles válidos en minúsculas (case insensitive)."""
        assert validate_log_level("debug") is True
        assert validate_log_level("info") is True
    
    def test_invalid_level(self):
        """Nivel inválido es rechazado."""
        assert validate_log_level("INVALID") is False
        assert validate_log_level("TRACE") is False
    
    def test_empty_string(self):
        """String vacío es inválido."""
        assert validate_log_level("") is False
    
    def test_none_value(self):
        """None es inválido."""
        assert validate_log_level(None) is False


class TestValidateEnvironment:
    """Tests para validate_environment."""
    
    def test_valid_envs(self):
        """Ambientes válidos son aceptados."""
        assert validate_environment("development") is True
        assert validate_environment("staging") is True
        assert validate_environment("production") is True
    
    def test_case_insensitive(self):
        """Validación es case-insensitive."""
        assert validate_environment("Development") is True
        assert validate_environment("PRODUCTION") is True
    
    def test_invalid_env(self):
        """Ambientes inválidos son rechazados."""
        assert validate_environment("prod") is False
        assert validate_environment("dev") is False
        assert validate_environment("test") is False
    
    def test_empty_string(self):
        """String vacío es inválido."""
        assert validate_environment("") is False
    
    def test_none_value(self):
        """None es inválido."""
        assert validate_environment(None) is False
