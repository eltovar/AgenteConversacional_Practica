# Mapa de dependencias para Staging

Fecha: 2026-09-04

## Dependencias principales

| Archivo | Funcion / componente | Integracion | Variable usada | Efecto de escritura | Riesgo |
|---|---|---|---|---|---|
| `app.py` | `get_redis_url` | Redis | `REDIS_PUBLIC_URL`, `REDIS_URL` | No escribe por si misma | Si staging copia URL prod comparte estado |
| `app.py` | `_get_app_redis` | Redis | `REDIS_PUBLIC_URL`, `REDIS_URL` | Locks, flags, Pub/Sub, jobs | Alto |
| `app.py` | `health_check` | Redis | Redis URL | PING solamente | Bajo, pero health incompleto |
| `app.py` | `startup_event` | RAG/Postgres/OpenAI/Redis | `DATABASE_URL`, `OPENAI_API_KEY`, Redis vars | Reindexa KB y borra embeddings de coleccion | Alto |
| `app.py` | scheduler jobs | Redis/Mongo/HubSpot/Twilio | `FOLLOWUP_ENABLED`, `APPOINTMENT_REMINDERS_ENABLED`, `DEPURACION_AUTO_ENABLED`, Redis vars | Envia mensajes, actualiza estados, limpia/reconcilia | Alto |
| `app.py` | `reconcile_owner_ids` | HubSpot/Mongo/Redis | `HUBSPOT_ACCESS_TOKEN`, `HUBSPOT_API_KEY` | Corrige owner en Mongo/Redis con fuente HubSpot | Alto |
| `database/mongodb_client.py` | `MongoDBManager.connect` | MongoDB | `MONGO_URL`, `MONGODB_URL`, `MONGO_PUBLIC_URL`, `MONGODB_PUBLIC_URL`, `RAILWAY_ENVIRONMENT` | Crea indices y escribe en `inmobiliaria_chat` durante operaciones | Alto |
| `database/mongodb_client.py` | `_ensure_indexes` | MongoDB | Mongo URL | Crea indices, incluido TTL | Medio |
| `database/mongodb_client.py` | mensajes/citas/campanas | MongoDB | Mongo URL | Inserta/actualiza mensajes, citas, notas, campanas | Alto |
| `middleware/conversation_state.py` | `ConversationStateManager` | Redis | `REDIS_URL`, `REDIS_PUBLIC_URL`, `SESSION_TTL`, `RAILWAY_ENVIRONMENT` | Estados, metas, inbox, ownership | Alto |
| `middleware/conversation_state.py` | `transfer_ownership` | Redis/Mongo/HubSpot | Redis vars, `HUBSPOT_API_KEY`, `PANEL_BASE_URL`, `ADMIN_API_KEY` | Cambia owner en Redis/Mongo y HubSpot | Muy alto |
| `middleware/webhook_handler.py` | `/whatsapp/webhook` | Twilio/Redis/Mongo/HubSpot/OpenAI/WebSocket | Twilio vars, Redis vars, HubSpot vars, OpenAI vars | Procesa inbound y puede responder/sincronizar | Muy alto |
| `middleware/webhook_handler.py` | `/whatsapp/status` | Twilio/Mongo/WebSocket | Twilio vars | Actualiza delivery status | Medio |
| `middleware/webhook_handler.py` | `/hubspot/webhook` | HubSpot | `HUBSPOT_API_KEY` | Puede sincronizar datos relacionados | Alto |
| `middleware/outbound_panel.py` | `/send-message`, `/send-template` | Twilio/Mongo/Redis/HubSpot | Twilio vars, HubSpot vars, Redis vars | Envia WhatsApp y registra mensajes | Muy alto |
| `middleware/outbound_panel.py` | transfer endpoints | Redis/Mongo/HubSpot/WebSocket | `HUBSPOT_API_KEY`, Redis vars | Cambia owner y notifica panel | Muy alto |
| `middleware/outbound_panel.py` | `/admin/sync-owners*` | HubSpot/Mongo/Redis | `HUBSPOT_API_KEY` | Corrige ownership | Muy alto |
| `middleware/outbound_panel.py` | bulk campaigns | Twilio/Mongo/Redis/HubSpot | Twilio vars, Redis vars, HubSpot vars | Envio masivo controlado por scheduler | Muy alto |
| `middleware/websocket_manager.py` | `PUBSUB_CHANNEL` | Redis Pub/Sub/WebSocket | Redis vars por caller | Publica en `ws:broadcast` | Alto si Redis se comparte |
| `rag/vector_store.py` | `PgVectorStore` | PostgreSQL/pgvector/OpenAI embeddings | `DATABASE_URL`, `VECTOR_COLLECTION_NAME`, `OPENAI_API_KEY` | Inserta embeddings; puede borrar coleccion por servicio RAG | Alto |
| `rag/rag_service.py` | `reload_knowledge_base` | PostgreSQL/pgvector/OpenAI | `DATABASE_URL`, `OPENAI_API_KEY` | Limpia e indexa KB | Alto |
| `utils/twilio_client.py` | `send_whatsapp_message` | Twilio | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, `TWILIO_MESSAGING_SERVICE_SID`, `TWILIO_USE_CONVERSATIONS`, `TWILIO_FORCE_CONVERSATIONS` | Envia mensajes y puede crear Conversations | Muy alto |
| `utils/media_processor.py` | media processor | Twilio/OpenAI/Bunny | `TWILIO_*`, `OPENAI_API_KEY`, `BUNNY_*` | Descarga/sube media, transcribe, analiza imagenes | Alto |
| `integrations/hubspot/hubspot_client.py` | `create_contact`, `update_contact`, `create_deal`, `update_deal`, `create_note` | HubSpot | `HUBSPOT_API_KEY`, `HUBSPOT_PIPELINE_ID`, `HUBSPOT_DEAL_STAGE` | Escrituras reales en HubSpot | Muy alto |
| `integrations/hubspot/outbound_handler.py` | HubSpot outbound | HubSpot/Twilio/Redis | `HUBSPOT_CLIENT_SECRET`, Twilio vars, Redis vars | Envio WhatsApp desde webhook HubSpot | Muy alto |
| `integrations/hubspot/lead_assigner.py` | LeadAssigner | Redis/HubSpot/webhook externo | `REDIS_URL`, `REDIS_PUBLIC_URL`, `ORPHAN_LEAD_WEBHOOK_URL` | Persistencia de asignacion y alertas | Alto |
| `scripts/depurar_no_responde.py` | script manual | HubSpot/Mongo/Redis | `HUBSPOT_API_KEY`, `MONGO_*`, `REDIS_*` | Puede mover etapas/limpiar estados segun modo | Alto |

## Claves Redis sensibles detectadas

- `scheduler_leader`
- `rag_kb_lock`
- `ws:broadcast`
- `active_conversations_index`
- `bot_controlled_conversations`
- `phone_cache:{phone}`
- `phone_cache:{contact_id}`
- `bulk_processor_lock`
- `lead_assigner:*`
- `sched_msg_lock:{id}`
- `followup_sent:{phone}`
- `followup_lock:{phone}`

## Jobs APScheduler detectados

| Job | Trigger | Efecto | Riesgo staging |
|---|---|---|---|
| `scheduler_lock_heartbeat` | segundos | Renueva lock Redis | Medio |
| `apt_reminders` | 30 min, si `APPOINTMENT_REMINDERS_ENABLED` | Envia recordatorios Twilio | Alto |
| `followup_24h` | 1 h, si `FOLLOWUP_ENABLED` | Envia followups Twilio/HubSpot | Alto |
| `apt_followups` | 15 min, si `APPOINTMENT_REMINDERS_ENABLED` | Envia post-cita | Alto |
| `apt_followup2` | 15 min, si `APPOINTMENT_REMINDERS_ENABLED` | Envia post-cita 2 | Alto |
| `conv_timeouts` | 2 h | Cambia estados conversacion | Medio |
| `memory_watchdog` | 2 min | Recicla worker por memoria | Bajo |
| `scheduled_messages` | 1 min | Envia plantillas programadas | Muy alto |
| `bulk_campaign_processor` | 15 s | Procesa campanas masivas | Muy alto |
| `advisor_24h_notifications` | 1 h | Notificaciones asesor | Alto |
| `aprobados_daily_reminder` | cron 9:00 | Recordatorio por HubSpot/Twilio | Alto |
| `rebuild_zset_nightly` | cron 3:00 | Reconstruye ZSET desde Mongo | Medio |
| `depuracion_no_responde` | cron 5:00 | Dry-run por defecto; puede cambiar HubSpot si enabled | Alto |
| `reconcile_owner_ids` | 6 h | Corrige owner Redis/Mongo con HubSpot | Muy alto |
| `rescue_stalled_leads` | 5 min | Rescate de leads | Alto |
| `scheduler_leader_election` | segundos | Reintenta liderazgo | Medio |

## Resultado

El repositorio esta preparado para muchas integraciones reales, pero no esta preparado aun para staging seguro solo por crear otra URL. La separacion debe empezar por variables y servicios aislados, seguida por flags de seguridad para cualquier escritura outbound.
