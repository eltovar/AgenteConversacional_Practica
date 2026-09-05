# Matriz de variables Staging

Fecha: 2026-09-04

No se incluyen valores secretos. Production y Staging no pudieron auditarse en Railway porque el proyecto local no esta vinculado.

| Variable | Codigo la usa | Production | Staging | Debe diferir | Riesgo |
|---|---|---|---|---|---|
| `OPENAI_API_KEY` | Si | No verificado | No verificado | Opcional | Costo y uso real de modelos |
| `REDIS_URL` | Si | No verificado | No verificado | Si | Compartir estado, locks, Pub/Sub, scheduler |
| `REDIS_PUBLIC_URL` | Si | No verificado | No verificado | Si | Local puede apuntar a production |
| `SESSION_TTL` | Si | No verificado | No verificado | No obligatorio | Retencion de estado |
| `DATABASE_URL` | Si | No verificado | No verificado | Si | RAG puede borrar/reindexar coleccion productiva |
| `VECTOR_COLLECTION_NAME` | Si, con default | No verificado | No verificado | Recomendado | Aislamiento adicional de embeddings |
| `PGHOST` | Indirecto/no principal | No verificado | No verificado | Si | Confusion operativa |
| `PGPORT` | Indirecto/no principal | No verificado | No verificado | Si | Confusion operativa |
| `PGPASSWORD` | Indirecto/no principal | No verificado | No verificado | Si | Credencial sensible |
| `PGDATABASE` | Indirecto/no principal | No verificado | No verificado | Si | DB productiva vs staging |
| `MONGO_URL` | Si | No verificado | No verificado | Si | Datos reales expuestos/modificados |
| `MONGODB_URL` | Si, fallback | No verificado | No verificado | Si | Datos reales expuestos/modificados |
| `MONGO_PUBLIC_URL` | Si | No verificado | No verificado | Si | Local puede tocar production |
| `MONGODB_PUBLIC_URL` | Si, fallback | No verificado | No verificado | Si | Local puede tocar production |
| `TWILIO_ACCOUNT_SID` | Si | No verificado | No verificado | Idealmente si | Envios reales |
| `TWILIO_AUTH_TOKEN` | Si | No verificado | No verificado | Idealmente si | Envios reales |
| `TWILIO_PHONE_NUMBER` | Si | No verificado | No verificado | Si | Numero productivo |
| `TWILIO_MESSAGING_SERVICE_SID` | Si | No verificado | No verificado | Si | Conversaciones productivas |
| `TWILIO_USE_CONVERSATIONS` | Si | No verificado | No verificado | Segun prueba | Costo MAU Conversations |
| `TWILIO_FORCE_CONVERSATIONS` | Si | No verificado | No verificado | Segun prueba | Crea Conversations |
| `TWILIO_SIGNATURE_MODE` | Si | No verificado | No verificado | Puede diferir | Seguridad webhook |
| `TWILIO_WEBHOOK_BASE_URL` | Si | No verificado | No verificado | Si | Callbacks al entorno equivocado |
| `TWILIO_WHATSAPP_REQUEST_CONTACT_CONTENT_SID` | Si | No verificado | No verificado | Si | Templates de prueba vs reales |
| `TWILIO_MCS_BASE` | Si | No verificado | No verificado | No obligatorio | Descarga media |
| `HUBSPOT_API_KEY` | Si | No verificado | No verificado | Si | Escrituras CRM reales |
| `HUBSPOT_ACCESS_TOKEN` | Si, fallback en algunos jobs | No verificado | No verificado | Si | Escrituras CRM reales |
| `HUBSPOT_PIPELINE_ID` | Si | No verificado | No verificado | Recomendado | Pipeline real |
| `HUBSPOT_DEAL_STAGE` | Si | No verificado | No verificado | Recomendado | Etapa real |
| `HUBSPOT_PIPELINE_REDES_SOCIALES_ID` | Local `.env`; codigo usa variantes | No verificado | No verificado | Recomendado | Pipeline real |
| `HUBSPOT_REDES_STAGE_NUEVO` | Local `.env`; codigo usa variantes | No verificado | No verificado | Recomendado | Etapa real |
| `HUBSPOT_PIPELINE_REDES_ID` | Si | No verificado | No verificado | Recomendado | Pipeline real |
| `HUBSPOT_STAGE_NUEVO_RS` | Si | No verificado | No verificado | Recomendado | Etapa real |
| `HUBSPOT_DEFAULT_OWNER` | Si | No verificado | No verificado | Si | Owner real |
| `HUBSPOT_CLIENT_SECRET` | Si | No verificado | No verificado | Si | Firma webhook |
| `PANEL_BASE_URL` | Si | No verificado | No verificado | Si | URL chat puede apuntar a prod |
| `ADMIN_API_KEY` | Si | No verificado | No verificado | Si | Acceso panel/admin |
| `PANEL_API_KEY` | Si, fallback | No verificado | No verificado | Si | Acceso panel/admin |
| `FOLLOWUP_ENABLED` | Si | No verificado | No verificado | Si | Debe iniciar OFF en staging |
| `FOLLOWUP_DELAY_HOURS` | Si | No verificado | No verificado | No obligatorio | Cadencia pruebas |
| `APPOINTMENT_REMINDERS_ENABLED` | Si | No verificado | No verificado | Si | Debe iniciar OFF en staging |
| `DEPURACION_AUTO_ENABLED` | Si | No verificado | No verificado | Si | Debe iniciar OFF en staging |
| `DEPURACION_AUTO_MODO` | Si | No verificado | No verificado | Si | Cambios masivos HubSpot |
| `DEPURACION_AUTO_TOPE` | Si | No verificado | No verificado | No obligatorio | Limite operativo |
| `WORKER_MEM_LIMIT_MB` | Si | No verificado | No verificado | Puede diferir | Reciclaje worker |
| `BUNNY_STORAGE_ZONE_NAME` | Si | No verificado | No verificado | Si o prefijo staging | Media real |
| `BUNNY_STORAGE_API_KEY` | Si | No verificado | No verificado | Si | Borrado/subida media |
| `BUNNY_PULL_ZONE_URL` | Si | No verificado | No verificado | Si o prefijo staging | URLs publicas |
| `BUNNY_STORAGE_ENDPOINT` | Si | No verificado | No verificado | No obligatorio | Region/storage |
| `BUNNY_REGION` | En `.env`; uso no confirmado | No verificado | No verificado | No obligatorio | Operativo |
| `MESSAGE_AGGREGATION_TIMEOUT` | Si | No verificado | No verificado | No obligatorio | UX webhook |
| `QUERY_PROFILER_ENABLED` | Si | No verificado | No verificado | No obligatorio | Observabilidad |
| `QUERY_PROFILER_SLOW_MS` | Si | No verificado | No verificado | No obligatorio | Observabilidad |
| `MAX_STAGE_CONTACTS` | Si | No verificado | No verificado | No obligatorio | Carga HubSpot |
| `HS_STAGE_CACHE_TTL` | Si | No verificado | No verificado | No obligatorio | Cache HubSpot |
| `ORPHAN_LEAD_WEBHOOK_URL` | Si | No verificado | No verificado | Si | Alertas externas |
| `PORT` | Si | Railway | Railway | No | Runtime |
| `RAILWAY_ENVIRONMENT` | Si | Railway-provided | Railway-provided | Automatico | Solo distingue Railway vs local |
| `RAILWAY_ENVIRONMENT_NAME` | No encontrado como uso operativo | No verificado | No verificado | Si se adopta | Necesario para production/staging |
| `APP_ENV` | No encontrado | No verificado | No verificado | Si se implementa | Faltante para modo staging |
| `ENVIRONMENT` | Buscado, sin uso operativo claro | No verificado | No verificado | Si se implementa | Faltante para modo staging |

## Variables nuevas recomendadas, pendientes de implementar

No se implementan todavia en esta auditoria, pero se recomiendan para la siguiente fase:

- `APP_ENV=production|staging`
- `TWILIO_OUTBOUND_ENABLED=false` en staging
- `TWILIO_ALLOWED_TO_NUMBERS` para allowlist
- `HUBSPOT_WRITE_ENABLED=false` en staging
- `SCHEDULER_OUTBOUND_ENABLED=false` en staging
- `BUNNY_UPLOAD_PREFIX=staging/` o zona Bunny separada
- `STAGING_SENTINEL_ENABLED=true`

Estas variables deben integrarse con cambios minimos y tests antes de desplegar.
