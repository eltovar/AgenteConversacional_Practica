"""Configuración centralizada con validación automática usando Pydantic."""
import os
from typing import Literal, Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

# Cargar .env al importar (solo en desarrollo, Railway usa vars de entorno)
load_dotenv()


class OpenAIConfig(BaseSettings):
    """Configuración de OpenAI API."""
    
    api_key: str = Field(..., description="OpenAI API Key (sk-...)")
    model: str = Field(default="gpt-4o-mini", description="Modelo a usar")
    temperature: float = Field(default=0.1, ge=0.0, le=2.0, description="Temperature (0-2)")
    max_tokens: Optional[int] = Field(default=None, ge=1, le=128000, description="Max tokens por respuesta")
    timeout: int = Field(default=60, ge=1, description="Timeout en segundos")
    
    model_config = SettingsConfigDict(env_prefix='OPENAI_')
    
    @field_validator('api_key')
    @classmethod
    def validate_api_key_format(cls, v: str) -> str:
        """Valida formato de OpenAI API key."""
        if not v.startswith('sk-'):
            raise ValueError('OpenAI API key debe empezar con "sk-"')
        if len(v) < 20:
            raise ValueError('OpenAI API key inválida (muy corta)')
        return v


class AppConfig(BaseSettings):
    """Configuración general de la aplicación."""
    
    env: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Ambiente de ejecución"
    )
    debug: bool = Field(default=False, description="Modo debug")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Nivel de logging"
    )
    host: str = Field(default="0.0.0.0", description="Host del servidor")
    port: int = Field(default=8000, ge=1, le=65535, description="Puerto del servidor")
    
    model_config = SettingsConfigDict(env_prefix='APP_')


class PineconeConfig(BaseSettings):
    """Configuración de Pinecone (opcional para RAG vectorial)."""
    
    api_key: Optional[str] = Field(default=None, description="Pinecone API Key")
    environment: Optional[str] = Field(default=None, description="Pinecone environment (ej: us-east1-gcp)")
    index_name: Optional[str] = Field(default=None, description="Nombre del índice vectorial")
    
    model_config = SettingsConfigDict(env_prefix='PINECONE_')
    
    @property
    def is_configured(self) -> bool:
        """Indica si Pinecone está completamente configurado."""
        return all([self.api_key, self.environment, self.index_name])


class Settings(BaseSettings):
    """
    Configuración global del sistema multi-agente.
    
    Carga automáticamente desde:
    1. Variables de entorno del sistema (prioridad en producción/Railway)
    2. Archivo .env (solo en desarrollo local)
    
    Uso:
        from app.config import settings
        
        api_key = settings.openai.api_key
        is_debug = settings.app.debug
    """
    
    # Campos anidados: Pydantic los instancia automáticamente desde env vars
    app: AppConfig = Field(default_factory=AppConfig)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    pinecone: PineconeConfig = Field(default_factory=PineconeConfig)
    
    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'  # Ignorar variables de entorno desconocidas
    )
    
    def validate_required_secrets(self) -> None:
        """
        Valida que todos los secrets críticos estén presentes y sean válidos.
        
        Raises:
            EnvironmentError: Si falta algún secret crítico o formato inválido
        """
        errors = []
        
        # OpenAI es siempre requerido
        if not self.openai.api_key or len(self.openai.api_key) < 20:
            errors.append("OpenAI API key faltante o inválida")
            
        # Pinecone es opcional, pero si se configura debe estar completo
        if any([self.pinecone.api_key, self.pinecone.environment, self.pinecone.index_name]):
            if not self.pinecone.is_configured:
                errors.append(
                    "Pinecone parcialmente configurado. "
                    "Requiere: PINECONE_API_KEY, PINECONE_ENVIRONMENT, PINECONE_INDEX_NAME"
                )
        
        if errors:
            raise EnvironmentError(
                "❌ Configuración inválida:\n" + "\n".join(f"  - {e}" for e in errors) +
                "\n💡 Verifica tu archivo .env o variables de entorno"
            )
    
    def get_safe_config(self) -> dict:
        """
        Retorna configuración sin secrets para logging seguro.

        Returns:
            dict: Configuración ofuscada (sin API keys completas)
        """
        # Type checkers pueden mostrar warnings aquí debido a Field(default_factory=...)
        # pero el código funciona correctamente en runtime
        return {
            "app": {
                "env": self.app.env,  # type: ignore[attr-defined]
                "debug": self.app.debug,  # type: ignore[attr-defined]
                "log_level": self.app.log_level,  # type: ignore[attr-defined]
                "host": self.app.host,  # type: ignore[attr-defined]
                "port": self.app.port,  # type: ignore[attr-defined]
            },
            "openai": {
                "model": self.openai.model,  # type: ignore[attr-defined]
                "temperature": self.openai.temperature,  # type: ignore[attr-defined]
                "api_key_configured": bool(self.openai.api_key),  # type: ignore[attr-defined]
                "api_key_prefix": self.openai.api_key[:7] + "..." if self.openai.api_key else None,  # type: ignore[attr-defined]
            },
            "pinecone": {
                "configured": self.pinecone.is_configured,  # type: ignore[attr-defined]
                "index_name": self.pinecone.index_name if self.pinecone.is_configured else None,  # type: ignore[attr-defined]
            }
        }


# ===== SINGLETON: Instancia global compartida =====
try:
    settings = Settings()
    settings.validate_required_secrets()
except Exception as e:
    raise EnvironmentError(
        f"❌ Error cargando configuración del sistema:\n{e}\n\n"
        f"💡 Solución:\n"
        f"  1. Copia .env.example a .env\n"
        f"  2. Añade tu OPENAI_API_KEY\n"
        f"  3. Verifica el formato (debe empezar con 'sk-')"
    ) from e
