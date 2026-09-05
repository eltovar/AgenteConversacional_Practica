# Estado actual Staging

Fecha: 2026-09-04

## Alcance

Esta auditoria cubre FASE 0 a FASE 5 del plan recibido en `IMPLEMENTACION_STAGING_SOFIA_CON_CODEX.md`.

Reglas aplicadas:

- No se modifico infraestructura.
- No se modificaron bases de datos.
- No se modificaron webhooks.
- No se imprimen valores secretos.
- Se distingue entre documentado, existe en codigo, desplegado, funciona y validado.

## Evidencia local

| Campo | Resultado |
|---|---|
| Repositorio local | `C:\Proteger_Inmobiliaria\AgenteConversacional_Practica` |
| Git remote | `https://github.com/eltovar/AgenteConversacional_Practica.git` |
| Rama local | `dev_juanrodriguez` |
| Commit local | `f958cba87f9398f1b6c7b31abb6a2482dfc16fb3` |
| Railway CLI | `railway 5.49.0` |
| Railway project documentado | `caring-balance` |
| Railway project vinculado al repo local | No verificado: `railway status` responde `No linked project found` |
| Railway production | No verificado desde CLI |
| Railway staging | No verificado desde CLI |
| Build local configurado | `railway.json` usa Dockerfile |
| Runtime web | `Dockerfile` ejecuta `gunicorn app:app` con 1 worker |
| Procfile | existe, tambien define `gunicorn app:app` con 2 workers |

## Estado por categoria

| Categoria | Estado | Evidencia |
|---|---|---|
| Documentado | Si | Documento externo define production/staging separados en Railway |
| Existe en codigo | Parcial | Codigo soporta Railway vs local con `RAILWAY_ENVIRONMENT`, pero no `APP_ENV=staging` |
| Desplegado | No verificado | CLI local no esta vinculada a Railway |
| Funciona | No verificado | No se ejecuto healthcheck remoto |
| Validado | No | Falta evidencia de servicios aislados, sentinel y smoke tests |

## Servicios production

No verificados en Railway. El documento fuente indica que production deberia contener:

- AgenteConversacional
- MongoDB
- Redis
- PostgreSQL / pgvector
- volumenes

Esto queda como DOCUMENTADO, no como VALIDADO.

## Servicios staging

No verificados en Railway. El documento fuente indica que staging deberia contener:

- AgenteConversacional
- MongoDB de pruebas
- Redis de pruebas
- PostgreSQL / pgvector de pruebas
- volumenes de pruebas

Esto queda como DOCUMENTADO, no como VALIDADO.

## Variables locales detectadas

Solo nombres, sin valores:

- `OPENAI_API_KEY`
- `REDIS_URL`
- `REDIS_PUBLIC_URL`
- `SESSION_TTL`
- `DATABASE_URL`
- `PGHOST`
- `PGPORT`
- `PGPASSWORD`
- `PGDATABASE`
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_PHONE_NUMBER`
- `TWILIO_MESSAGING_SERVICE_SID`
- `TWILIO_FORCE_CONVERSATIONS`
- `HUBSPOT_API_KEY`
- `HUBSPOT_PIPELINE_ID`
- `HUBSPOT_DEAL_STAGE`
- `ADMIN_API_KEY`
- `HUBSPOT_PIPELINE_REDES_SOCIALES_ID`
- `HUBSPOT_REDES_STAGE_NUEVO`
- `FOLLOWUP_ENABLED`
- `FOLLOWUP_DELAY_HOURS`
- `APPOINTMENT_REMINDERS_ENABLED`
- `BUNNY_STORAGE_ZONE_NAME`
- `BUNNY_STORAGE_API_KEY`
- `BUNNY_PULL_ZONE_URL`
- `BUNNY_STORAGE_ENDPOINT`
- `BUNNY_REGION`
- `MONGO_URL`
- `MONGO_PUBLIC_URL`
- `PANEL_BASE_URL`

## Hallazgos principales

1. El directorio local no esta vinculado a Railway, por lo que aun no hay evidencia directa de environments, servicios, volumenes, dominios, replicas o commits desplegados.
2. El codigo actual usa credenciales reales por variables de entorno y no tiene una compuerta central de seguridad para staging.
3. La app puede escribir en sistemas externos: Twilio, HubSpot, MongoDB, Redis, PostgreSQL/pgvector y Bunny.net.
4. `GET /health` existe y valida Redis, pero no reporta MongoDB, PostgreSQL/pgvector ni environment.
5. RAG reindexa en startup y ejecuta DELETE sobre la coleccion configurada; en staging debe apuntar obligatoriamente a una base separada antes de arrancar.

## Riesgo inmediato

No se debe crear ni encender un staging real hasta vincular Railway y confirmar que las variables de staging apuntan a servicios propios. En este momento el riesgo principal es que una app staging use credenciales de production y ejecute jobs, mensajes o escrituras reales.
