# Sistema de Configuración

## Índice
1. [Arquitectura](#arquitectura)
2. [Uso en Código](#uso-en-código)
3. [Variables de Entorno](#variables-de-entorno)
4. [Validación](#validación)
5. [Logging Seguro](#logging-seguro)
6. [Ambientes](#ambientes)

---

## Arquitectura

El sistema usa **Pydantic Settings** para gestión centralizada y validada de configuración mediante variables de entorno.

### Componentes Principales

```
app/config/
├── __init__.py              # Exporta settings singleton
├── settings.py              # Configuración con Pydantic BaseSettings
└── secrets_validator.py     # Validadores reutilizables
```

### Clases de Configuración

#### `AppConfig`
Configuración general de la aplicación:
- `env`: Ambiente de ejecución (development/staging/production)
- `debug`: Modo debug (solo development)
- `log_level`: Nivel de logging (DEBUG/INFO/WARNING/ERROR)
- `host` y `port`: Configuración del servidor FastAPI

#### `OpenAIConfig`
Configuración de OpenAI API:
- `api_key`: API key con validación de formato (debe empezar con `sk-`)
- `model`: Modelo a usar (default: `gpt-4o-mini`)
- `temperature`: Creatividad de respuestas (0.0-2.0, default: 0.1)
- `max_tokens`: Límite de tokens por respuesta (default: 4096)
- `timeout`: Timeout de requests en segundos (default: 60)

#### `PineconeConfig`
Configuración de Pinecone (opcional para RAG):
- `api_key`: API key de Pinecone
- `environment`: Región (ej: us-east1-gcp)
- `index_name`: Nombre del índice vectorial

---

## Uso en Código

### Importación del Singleton

```python
from app.config import settings

# Acceso a configuración
api_key = settings.openai.api_key
model = settings.openai.model
debug_mode = settings.app.debug
```

### Ejemplo en Agentes

```python
from langchain_openai import ChatOpenAI
from app.config import settings

# Crear cliente OpenAI con configuración centralizada
llm = ChatOpenAI(
    model=settings.openai.model,
    temperature=settings.openai.temperature,
    max_tokens=settings.openai.max_tokens,
    timeout=settings.openai.timeout,
    api_key=settings.openai.api_key
)
```

### Ejemplo en FastAPI

```python
from fastapi import FastAPI
from app.config import settings

app = FastAPI(debug=settings.app.debug)

@app.on_event("startup")
async def startup_event():
    # Verificar configuración al iniciar
    if settings.app.env == "production" and settings.app.debug:
        raise ValueError("Debug mode no debe estar activo en producción")
```

---

## Variables de Entorno

### Estructura de Naming

Las variables usan **prefijos** para organización:

- `APP_*`: Configuración general de la aplicación
- `OPENAI_*`: Configuración de OpenAI API
- `PINECONE_*`: Configuración de Pinecone

### Archivo `.env` Local

```bash
# Desarrollo local
APP_ENV=development
APP_DEBUG=true
APP_LOG_LEVEL=DEBUG

OPENAI_API_KEY=sk-proj-your-key-here
OPENAI_MODEL=gpt-4o-mini
OPENAI_TEMPERATURE=0.3
```

### Railway (Producción)

En Railway, las variables se configuran en el dashboard:
1. Ve a tu proyecto → Settings → Variables
2. Añade cada variable **sin** comillas:
   ```
   OPENAI_API_KEY=sk-proj-your-actual-key
   APP_ENV=production
   APP_DEBUG=false
   ```

Ver [`.env.production.example`](../.env.production.example) para referencia completa.

---

## Validación

### Validación Automática

Pydantic valida automáticamente al cargar la configuración:

```python
from app.config import Settings

# ❌ Esto fallará con ValueError
settings = Settings()  # Si OPENAI_API_KEY no está definida

# ❌ Esto fallará con ValidationError
OPENAI_API_KEY=invalid_key  # No empieza con 'sk-'
OPENAI_TEMPERATURE=3.0      # Fuera del rango 0.0-2.0
```

### Validadores Personalizados

El sistema incluye validadores en [`app/config/secrets_validator.py`](../app/config/secrets_validator.py):

```python
from app.config.secrets_validator import (
    validate_openai_key,
    validate_environment,
    validate_pinecone_key
)

# Validar antes de usar
if not validate_openai_key(api_key):
    raise ValueError("API key inválida")
```

### Validación al Inicio

Tanto `app.py` como `orchestrator.py` validan secrets al iniciar:

```python
# Validación automática
REQUIRED_SECRETS = ["OPENAI_API_KEY"]
missing = [key for key in REQUIRED_SECRETS if not os.getenv(key)]
if missing:
    raise EnvironmentError(
        f"❌ Missing required secrets: {', '.join(missing)}\n"
        f"💡 Copy .env.example to .env and add your API keys"
    )
```

---

## Logging Seguro

### ⚠️ NUNCA Loggear Secrets Completos

```python
# ❌ INCORRECTO: Expone el secret completo
logger.info(f"API Key: {settings.openai.api_key}")

# ✅ CORRECTO: Usa get_safe_config()
logger.info(f"Configuración cargada: {settings.get_safe_config()}")
```

### Método `get_safe_config()`

Retorna configuración sin exponer secrets:

```python
from app.config import settings

safe_config = settings.get_safe_config()
print(safe_config)
# Output:
# {
#   "app": {"env": "development", "debug": True},
#   "openai": {
#     "model": "gpt-4o-mini",
#     "api_key_configured": True,
#     "api_key_prefix": "sk-proj..."
#   }
# }
```

### Logging en Producción

```python
import logging
from app.config import settings

logger = logging.getLogger(__name__)
logger.setLevel(settings.app.log_level)

# Al iniciar la aplicación
logger.info(f"Iniciando en modo {settings.app.env}")
logger.debug(f"Configuración: {settings.get_safe_config()}")
```

---

## Ambientes

### Development (Desarrollo Local)

```bash
# .env.development.example
APP_ENV=development
APP_DEBUG=true
APP_LOG_LEVEL=DEBUG

OPENAI_MODEL=gpt-4o-mini
OPENAI_TEMPERATURE=0.3      # Más creatividad para explorar
OPENAI_MAX_TOKENS=2048      # Límite bajo para ahorrar costos
```

**Características:**
- Debug mode activo
- Logging detallado (DEBUG)
- Temperature más alta para experimentar
- Max tokens reducido para desarrollo económico

### Production (Railway)

```bash
# Variables en Railway dashboard
APP_ENV=production
APP_DEBUG=false
APP_LOG_LEVEL=INFO

OPENAI_MODEL=gpt-4o-mini
OPENAI_TEMPERATURE=0.1      # Consistencia en respuestas
OPENAI_MAX_TOKENS=4096      # Respuestas completas
```

**Características:**
- Debug mode desactivado
- Logging solo INFO/WARNING/ERROR
- Temperature baja para consistencia
- Max tokens completo para respuestas detalladas

### Cambiar entre Ambientes

```bash
# En desarrollo local
cp .env.development.example .env
# Editar .env con tus API keys

# En producción (Railway)
# Configurar variables en dashboard (no usar .env)
```

---

## Referencias

- [Pydantic Settings Documentation](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
- [SECRETS_MANAGEMENT.md](./SECRETS_MANAGEMENT.md) - Gestión segura de credenciales
- [.env.example](../.env.example) - Template de variables de entorno
- [tests/config/](../tests/config/) - Tests de configuración