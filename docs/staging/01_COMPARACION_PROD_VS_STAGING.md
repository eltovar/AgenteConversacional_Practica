# Comparacion Production vs Staging

Fecha: 2026-09-04

## Estado de evidencia

Railway no pudo auditarse desde este directorio porque la CLI reporta `No linked project found`. Por tanto, la columna Production/Staging combina:

- DOCUMENTADO: aparece en el plan externo.
- CODIGO: existe en el repositorio.
- NO VERIFICADO: no hay evidencia directa de Railway.

## Tabla obligatoria

| Componente | Production | Staging | Aislado | Estado | Accion |
|---|---|---|---|---|---|
| API | Documentada como `AgenteConversacional`; codigo FastAPI en `app.py` | Documentada como copia staging | No verificado | NO VERIFICADO | Vincular Railway, confirmar servicio, rama, dominio y commit |
| MongoDB | Codigo usa `MONGO_URL`/`MONGODB_URL` en Railway | Debe usar Mongo propio | No verificado | RIESGO ALTO | Crear/validar servicio Mongo staging antes de encender API |
| Redis | Codigo usa `REDIS_URL` en Railway | Debe usar Redis propio | No verificado | RIESGO ALTO | Crear/validar Redis staging; comprobar que no comparte claves |
| PostgreSQL | Codigo usa `DATABASE_URL` para pgvector | Debe usar Postgres propio | No verificado | RIESGO ALTO | Crear/validar Postgres staging con extension vector |
| pgvector | `rag/vector_store.py` usa `langchain_postgres.PGVector` | Debe tener coleccion staging | No verificado | RIESGO ALTO | Separar `DATABASE_URL` y opcionalmente `VECTOR_COLLECTION_NAME` |
| WebSocket | Existe en panel con Redis Pub/Sub `ws:broadcast` | Debe publicar solo en Redis staging | No verificado | PARCIAL EN CODIGO | Validar aislamiento por Redis y URL panel staging |
| Scheduler | Existe `AsyncIOScheduler` con varios jobs | Debe arrancar seguro/off para outbound | No verificado | RIESGO ALTO | Agregar/validar flags globales antes de staging |
| Twilio | Codigo envia WhatsApp si hay credenciales | Debe estar outbound OFF por defecto | No verificado | RIESGO ALTO | Crear flag `TWILIO_OUTBOUND_ENABLED` o equivalente tras revisar naming |
| HubSpot | Codigo crea/actualiza contactos, deals y notas | Debe estar WRITE OFF por defecto | No verificado | RIESGO ALTO | Crear flag central de escritura HubSpot o portal sandbox |
| OpenAI | Codigo usa OpenAI para chat, embeddings, vision, whisper | Puede compartirse con control de gasto | No aplica a datos, si a costos | PARCIAL EN CODIGO | Separar tests mock/integracion/e2e y medir consumo |
| Bunny | Codigo sube media a Bunny.net | Debe usar carpeta/zona staging | No verificado | RIESGO MEDIO | Separar zona o prefijo `staging/` antes de pruebas media |

## Diferencias detectadas en codigo

| Tema | Codigo actual | Riesgo para staging |
|---|---|---|
| Environment | Se detecta Railway con `RAILWAY_ENVIRONMENT`; no se encontro `APP_ENV` o `ENVIRONMENT` operativo | Staging y production pueden comportarse igual |
| Redis | Muchos componentes usan `REDIS_URL` o `REDIS_PUBLIC_URL` | Si variables se copian desde production, se comparte estado |
| MongoDB | DB fija `inmobiliaria_chat` en `database/mongodb_client.py` | Aunque el host sea distinto, el nombre de DB no distingue staging |
| RAG | Startup limpia embeddings de la coleccion configurada antes de reindexar | Si `DATABASE_URL` apunta a production, borra embeddings production de esa coleccion |
| Schedulers | Algunos jobs estan siempre registrados; otros dependen de flags | Staging podria enviar mensajes, reconciliar owners o ejecutar campañas |
| HubSpot | Escrituras directas en cliente, panel, transferencias y jobs | Staging podria modificar contactos reales |
| Twilio | Cliente disponible si hay credenciales suficientes | Staging podria enviar WhatsApp reales |

## Costos potenciales

| Recurso | Costo potencial |
|---|---|
| API Railway | CPU/RAM mientras este encendida |
| MongoDB | almacenamiento/volumen aunque API este apagada |
| Redis | almacenamiento/servicio mientras exista |
| PostgreSQL/pgvector | almacenamiento, CPU y extension vector |
| OpenAI | chat, embeddings, Whisper y vision durante pruebas |
| Twilio | mensajes, Conversations API, media y callbacks |
| Bunny.net | storage, bandwidth y requests |

## Conclusion

Staging no puede declararse creado ni validado todavia. El siguiente paso seguro es vincular o seleccionar el proyecto Railway correcto y auditar environments/servicios reales antes de crear recursos.
