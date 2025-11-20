# Gestión de Secrets y Credenciales

## Índice
1. [Principios de Seguridad](#principios-de-seguridad)
2. [Rotación de Secrets](#rotación-de-secrets)
3. [Compartir Credenciales](#compartir-credenciales)
4. [Configuración por Ambiente](#configuración-por-ambiente)
5. [Deployment en Railway](#deployment-en-railway)
6. [Monitoreo y Alertas](#monitoreo-y-alertas)
7. [Respuesta a Incidentes](#respuesta-a-incidentes)

---

## Principios de Seguridad

### Regla de Oro
**NUNCA commitear secrets en Git. NUNCA compartir secrets por canales inseguros.**

### Protecciones Implementadas

#### 1. `.gitignore`
```bash
# Secrets y credenciales
.env
.env.*
!.env.example
*.key
*.pem
credentials.*
secrets.*
config/local.*
```

#### 2. Validación al Inicio
Los archivos [`app.py`](../app.py) y [`orchestrator.py`](../orchestrator.py) validan secrets al iniciar:

```python
REQUIRED_SECRETS = ["OPENAI_API_KEY"]
missing = [key for key in REQUIRED_SECRETS if not os.getenv(key)]
if missing:
    raise EnvironmentError(
        f"❌ Missing required secrets: {', '.join(missing)}"
    )
```

#### 3. Templates sin Valores Reales
- `.env.example` - Template general
- `.env.development.example` - Template para desarrollo
- `.env.production.example` - Template para producción

Estos archivos **NO contienen valores reales** y son seguros para Git.

---

## Rotación de Secrets

### Política de Rotación

| Secret | Frecuencia | Método |
|--------|------------|--------|
| OpenAI API Key | **90 días** | Dashboard de OpenAI |
| Pinecone API Key | 90 días | Dashboard de Pinecone |
| Webhooks Secret | Al detectar compromiso | Configuración del CRM |

### Proceso de Rotación: OpenAI API Key

#### Paso 1: Generar Nueva Key
1. Ve a [platform.openai.com/api-keys](https://platform.openai.com/api-keys)
2. Click en "Create new secret key"
3. Nombra la key: `inmobiliaria-prod-2025-02` (incluye mes/año)
4. **Copia inmediatamente** (solo se muestra una vez)
5. Guarda temporalmente en tu password manager

#### Paso 2: Actualizar en Railway
1. Ve a tu proyecto en Railway
2. Settings → Variables
3. Click en `OPENAI_API_KEY`
4. Pega la nueva key
5. Click "Update" → Railway reiniciará automáticamente

#### Paso 3: Verificar en Producción
```bash
# Revisar logs en Railway
railway logs

# Buscar línea de confirmación:
# "INFO: Configuración cargada: {...}"
```

#### Paso 4: Revocar Key Antigua
1. Espera **24 horas** (para detectar problemas)
2. Ve a OpenAI dashboard → API Keys
3. Revoca la key antigua
4. Verifica que no haya errores en logs de Railway

#### Paso 5: Actualizar Desarrollo Local
```bash
# Edita tu .env local
OPENAI_API_KEY=sk-proj-new-key-here

# Reinicia el servidor
# La aplicación validará la nueva key automáticamente
```

### Calendario de Rotación

Crea recordatorios trimestrales:
```
15 Febrero 2025 - Rotar OpenAI Key
15 Mayo 2025 - Rotar OpenAI Key
15 Agosto 2025 - Rotar OpenAI Key
15 Noviembre 2025 - Rotar OpenAI Key
```

---

## Compartir Credenciales

### ✅ Métodos Seguros

#### 1. Password Manager (Recomendado)
- **1Password**: Crear vault compartido para el equipo
- **Bitwarden**: Organización con colecciones
- **LastPass**: Carpetas compartidas

```
Estructura recomendada:
├── Inmobiliaria/
│   ├── OpenAI API - Producción
│   ├── OpenAI API - Development
│   ├── Pinecone API - Producción
│   └── Railway Dashboard
```

#### 2. Variables de Entorno del Sistema
En servidores compartidos (no Railway):
```bash
# En ~/.bashrc o ~/.zshrc
export OPENAI_API_KEY="sk-proj-..."
export OPENAI_MODEL="gpt-4o-mini"
```

#### 3. Secrets Management Services
Para equipos grandes:
- AWS Secrets Manager
- Azure Key Vault
- HashiCorp Vault

### ❌ NUNCA Usar

| Método | Riesgo |
|--------|--------|
| Email | No encriptado, queda en historial |
| Slack/Teams | Puede quedar en logs, búsquedas |
| WhatsApp | No es para credenciales empresariales |
| Comentarios en código | Se commitea a Git |
| Google Docs compartido | Permisos pueden cambiar |
| Captura de pantalla | Se puede compartir accidentalmente |

---

## Configuración por Ambiente

### Development (Local)

**Archivo:** `.env` (creado desde `.env.development.example`)

```bash
# Copiar template
cp .env.development.example .env

# Editar con tu editor
code .env
# O usar nano/vim en terminal
```

**Contenido:**
```bash
APP_ENV=development
APP_DEBUG=true
APP_LOG_LEVEL=DEBUG

OPENAI_API_KEY=sk-proj-your-dev-key
OPENAI_MODEL=gpt-4o-mini
OPENAI_TEMPERATURE=0.3      # Más creatividad para experimentar
OPENAI_MAX_TOKENS=2048      # Límite bajo para ahorrar
```

**Seguridad:**
- Usa una API key **diferente** de producción
- Configura límites de gasto en OpenAI dashboard (ej: $10/mes para dev)
- El archivo `.env` está en `.gitignore` y **nunca se commitea**

### Production (Railway)

**NO usar archivo `.env`** - Configurar en Railway dashboard:

1. **Acceder a Variables:**
   ```
   Railway → Tu Proyecto → Settings → Variables
   ```

2. **Añadir Variables (sin comillas):**
   ```
   APP_ENV=production
   APP_DEBUG=false
   APP_LOG_LEVEL=INFO
   APP_HOST=0.0.0.0
   APP_PORT=8000

   OPENAI_API_KEY=sk-proj-your-production-key
   OPENAI_MODEL=gpt-4o-mini
   OPENAI_TEMPERATURE=0.1
   OPENAI_MAX_TOKENS=4096
   OPENAI_TIMEOUT=60
   ```

3. **Railway reinicia automáticamente** al guardar cambios

**Seguridad:**
- API key diferente de development
- Límites de gasto más altos (según uso esperado)
- Alertas configuradas en OpenAI dashboard

### Staging (Opcional)

Si tienes ambiente de staging:

```bash
# Variables en Railway (proyecto separado)
APP_ENV=staging
APP_DEBUG=false
APP_LOG_LEVEL=DEBUG    # Más verbose para debugging

# Usa API key de producción pero con límites
OPENAI_API_KEY=sk-proj-staging-key
```

---

## Deployment en Railway

### Setup Inicial

#### 1. Crear Proyecto en Railway

```bash
# Instalar CLI de Railway
npm install -g @railway/cli

# Login
railway login

# Vincular proyecto (en raíz del repo)
railway link
```

#### 2. Configurar Variables de Entorno

Opción A: **Dashboard Web** (Recomendado)
1. Ve a [railway.app](https://railway.app)
2. Selecciona tu proyecto
3. Settings → Variables → "New Variable"
4. Añade cada variable del archivo `.env.production.example`

Opción B: **CLI**
```bash
# Añadir variables una por una
railway variables set OPENAI_API_KEY="sk-proj-..."
railway variables set APP_ENV="production"
railway variables set APP_DEBUG="false"

# Ver variables configuradas
railway variables
```

#### 3. Configurar Build

Railway detecta automáticamente Python. Verifica que exista:

**`railway.json`** (opcional, para control explícito):
```json
{
  "build": {
    "builder": "NIXPACKS"
  },
  "deploy": {
    "startCommand": "uvicorn app:app --host 0.0.0.0 --port $PORT",
    "healthcheckPath": "/health",
    "restartPolicyType": "ON_FAILURE"
  }
}
```

#### 4. Deploy

```bash
# Hacer commit y push (Railway detecta automáticamente)
git push origin main

# O deploy manual desde CLI
railway up
```

### Verificación Post-Deploy

```bash
# Ver logs en tiempo real
railway logs

# Buscar líneas de confirmación:
# "INFO: Configuración cargada: {...}"
# "INFO: Application startup complete."

# Probar endpoint de salud
curl https://tu-proyecto.railway.app/health
```

### Rollback en Caso de Error

```bash
# Listar deployments
railway deployments

# Rollback al deployment anterior
railway rollback <deployment-id>
```

---

## Monitoreo y Alertas

### OpenAI Usage Dashboard

1. **Configurar Límites de Gasto:**
   - Ve a [platform.openai.com/settings/organization/billing/limits](https://platform.openai.com/settings/organization/billing/limits)
   - Hard limit: Máximo mensual (ej: $100/mes)
   - Soft limit: Alerta cuando llegas a X% (ej: 80%)

2. **Alertas por Email:**
   - Settings → Billing → Email notifications
   - Activa "Usage thresholds" y "Billing issues"

3. **Revisar Usage:**
   ```
   Dashboard → Usage
   - Requests por día
   - Tokens consumidos
   - Costo por modelo
   ```

### Railway Logs

```bash
# Ver logs en tiempo real
railway logs --follow

# Filtrar por nivel
railway logs | grep ERROR
railway logs | grep WARNING

# Buscar problemas de API key
railway logs | grep "api_key"
```

### Alertas Críticas

Configura alertas para:
- **Error 401 Unauthorized:** API key inválida o revocada
- **Error 429 Rate Limit:** Excediste límite de requests
- **Error 500:** Error interno del servidor

```python
# En tu código, loggea errores críticos
import logging

logger = logging.getLogger(__name__)

try:
    response = llm.invoke(messages)
except Exception as e:
    logger.error(f"OpenAI API Error: {type(e).__name__} - {str(e)}")
    # Enviar alerta (ej: a Slack, email, etc.)
```

---

## Respuesta a Incidentes

### Escenario 1: API Key Expuesta en Git

**Síntomas:**
- GitHub Advanced Security alerta
- Alguien encuentra la key en historial de commits
- OpenAI envía email de "API key exposed"

**Acciones Inmediatas (< 5 minutos):**

1. **Revocar Key Inmediatamente:**
   ```
   OpenAI Dashboard → API Keys → Revoke
   ```

2. **Generar Nueva Key:**
   ```
   Create new secret key → Copiar
   ```

3. **Actualizar en Railway:**
   ```
   Railway → Settings → Variables → OPENAI_API_KEY → Update
   ```

4. **Actualizar Localmente:**
   ```bash
   # Edita .env
   OPENAI_API_KEY=sk-proj-new-key
   ```

**Acciones de Seguimiento (< 1 hora):**

5. **Limpiar Git History:**
   ```bash
   # Instalar BFG Repo-Cleaner
   brew install bfg  # macOS
   # O descargar de https://rtyley.github.io/bfg-repo-cleaner/

   # Limpiar secrets del historial
   bfg --replace-text secrets.txt .git

   # Force push (avisar al equipo primero)
   git push origin --force --all
   ```

6. **Revisar Usage:**
   - Ve a OpenAI Dashboard → Usage
   - Busca requests sospechosos (IPs desconocidas, volumen anormal)

7. **Notificar al Equipo:**
   ```
   Asunto: [CRÍTICO] API Key comprometida - Rotación completada

   - API key antigua revocada: sk-proj-xxx (últimos 4 caracteres)
   - Nueva key desplegada en producción
   - Sin impacto detectado en servicios
   - Acción requerida: Actualizar .env local con nueva key
   ```

### Escenario 2: Spike de Costos

**Síntomas:**
- Factura de OpenAI más alta de lo normal
- Alerta de "usage threshold exceeded"

**Investigación:**

1. **Revisar Usage en OpenAI:**
   ```
   Dashboard → Usage → Date range: última semana
   - ¿Qué modelo consumió más?
   - ¿Qué día/hora ocurrió el spike?
   ```

2. **Revisar Logs en Railway:**
   ```bash
   railway logs --since 7d | grep "OpenAI API"

   # Buscar patrones:
   # - Loops infinitos
   # - Requests duplicados
   # - Errores que causan retries
   ```

3. **Revisar Código:**
   ```python
   # Buscar posibles causas:
   # - max_tokens muy alto sin control
   # - Falta de rate limiting
   # - Errores que causan retries sin backoff
   ```

**Mitigación:**

```python
# Implementar rate limiting
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model="gpt-4o-mini",  # Modelo más económico
    max_tokens=2048,      # Reducir tokens
    timeout=30,           # Timeout más corto
    max_retries=2         # Limitar retries
)
```

### Escenario 3: Railway Deployment Falla

**Síntomas:**
- Deploy falla con error 500
- Logs muestran: "Missing required secrets: OPENAI_API_KEY"

**Solución:**

1. **Verificar Variables en Railway:**
   ```
   Railway → Settings → Variables
   - ¿Existe OPENAI_API_KEY?
   - ¿Está bien escrita (sin espacios extra)?
   ```

2. **Rollback Temporal:**
   ```bash
   railway deployments
   railway rollback <último-deployment-funcional>
   ```

3. **Corregir y Re-Deploy:**
   ```bash
   # Asegurar que la variable está configurada
   railway variables set OPENAI_API_KEY="sk-proj-..."

   # Re-deploy
   railway up
   ```

---

## Checklist de Seguridad

### Daily
- [ ] Revisar logs de Railway para errores de autenticación

### Weekly
- [ ] Revisar usage de OpenAI API
- [ ] Verificar que costos están dentro del presupuesto

### Monthly
- [ ] Auditar accesos al proyecto de Railway
- [ ] Revisar límites de gasto en OpenAI

### Quarterly (cada 90 días)
- [ ] **Rotar API keys de OpenAI**
- [ ] Rotar API keys de Pinecone (si aplica)
- [ ] Revisar permisos del repositorio Git

### On Boarding (nuevo desarrollador)
- [ ] Compartir credenciales via password manager
- [ ] Dar acceso a Railway (solo desarrollo, no producción)
- [ ] Compartir este documento (SECRETS_MANAGEMENT.md)
- [ ] Verificar que tiene `.env` local configurado
- [ ] Confirmar que `.env` no se commitea (git status)

### Off Boarding (desarrollador sale)
- [ ] Remover acceso a Railway
- [ ] Remover acceso a OpenAI organization
- [ ] **Rotar todas las API keys** (crítico)
- [ ] Remover de password manager compartido

---

## Referencias

- [OpenAI Best Practices - Safety](https://platform.openai.com/docs/guides/safety-best-practices)
- [Railway Docs - Environment Variables](https://docs.railway.app/develop/variables)
- [OWASP - Key Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Key_Management_Cheat_Sheet.html)
- [CONFIGURATION.md](./CONFIGURATION.md) - Sistema de configuración de la aplicación