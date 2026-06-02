# Estado Actual del Proyecto — SofIA Conversacional
## Inmobiliaria Proteger — Agente Conversacional Multicanal

**Última actualización:** 2026-06-01  
**Rama activa:** `twilio-conversations-migration`  
**Desarrollador:** CyberTovar  
**Deploy:** Railway Pro — `$20/mes`

---

## 1. Resumen Ejecutivo

SofIA es un sistema **multi-agente de atención al cliente** para el sector inmobiliario que combina inteligencia artificial conversacional con un panel administrativo para asesoras humanas. Opera sobre **un único número de WhatsApp** y gestiona el tráfico entre el bot (SofIA) y las asesoras de forma transparente para el cliente.

**Flujo central:**
```
Cliente WhatsApp
    → Twilio Webhook
        → webhook_handler.py
            → [BOT_ACTIVE] → SofiaBrain → Orchestrator → Agentes IA
            → [HUMAN_ACTIVE] → Panel Asesoras (outbound_panel.py)
    → HubSpot CRM (contactos, timeline)
    → MongoDB (historial real-time)
    → Redis (estado de conversación)
```

---

## 2. Stack Técnico

| Capa | Tecnología | Propósito |
|------|-----------|-----------|
| **Backend** | FastAPI (Python 3.12) | API REST + WebSockets + Webhook |
| **Deploy** | Railway Pro (1 worker Gunicorn) | Producción con memory watchdog |
| **LLM** | OpenAI GPT-4o-mini | Conversación + extracción entidades |
| **Embeddings** | OpenAI text-embedding-3-small | Vectorización RAG |
| **Framework IA** | LangChain | Orquestación LLM + historial |
| **Estado** | Redis (redis.asyncio) | Estado conversacional + cache |
| **Mensajes** | MongoDB (Motor async) | Historial real-time del panel |
| **Vectores** | PgVector (PostgreSQL) | Knowledge base RAG |
| **CRM** | HubSpot API v3 | Gestión contactos (sin Deals) |
| **Mensajería** | Twilio WhatsApp | Envío/recepción mensajes |
| **Media CDN** | Bunny.net Storage | Audio, imágenes, documentos |
| **Audio** | OpenAI Whisper | Transcripción de notas de voz |
| **Imágenes** | GPT-4o-mini | Análisis visual |
| **Scheduler** | APScheduler (AsyncIO) | Recordatorios + auto-transiciones |

---

## 3. Arquitectura de Carpetas

```
AgenteConversacional_Practica/
├── app.py                        # Punto de entrada FastAPI + schedulers APScheduler
├── main.py                       # Entry point alternativo
├── llm_client.py                 # Singleton GPT-4o-mini + embeddings
├── state_manager.py              # StateManager (para CLI/testing)
├── logging_config.py             # Configuración centralizada de logs
│
├── agents/                       # Sistema multi-agente
│   ├── orchestrator.py           # Router central de agentes (async)
│   ├── ReceptionAgent/           # Clasificación de intención
│   │   ├── reception_agent.py    # Lógica de enrutamiento (info/crm/ambiguous)
│   │   └── reception_tool.py     # Tool LangChain: classify_intent
│   ├── InfoAgent/                # Respuestas RAG
│   │   ├── info_agent.py         # Consultas a knowledge base
│   │   └── info_tool.py          # Tool LangChain para búsqueda
│   └── CRMAgent/
│       └── crm_agent.py          # Calificación lead → HubSpot (8k+ LOC)
│
├── middleware/                   # Capa intermedia Twilio ↔ Humanos
│   ├── webhook_handler.py        # Punto de entrada Twilio webhooks
│   ├── outbound_panel.py         # Panel asesoras: 8,238 líneas, 16+ endpoints
│   ├── sofia_brain.py            # Motor IA con memoria Redis (L1 qualification)
│   ├── conversation_state.py     # Modelos y manager Redis (ConversationMeta)
│   ├── websocket_manager.py      # WebSocket manager (panel real-time)
│   ├── appointment_manager.py    # Gestor de citas Redis + schedulers
│   ├── contact_manager.py        # Gestión de contactos
│   ├── phone_normalizer.py       # Normalización E.164 colombia
│   ├── stage_filter.py           # Filtros de etapa HubSpot
│   └── PanelAsesores/
│       ├── index.html            # UI del panel (Tailwind CSS)
│       └── index.js              # Lógica frontend panel
│
├── prompts/                      # Sistema de prompts centralizado
│   ├── persona/
│   │   ├── identity.py           # Personalidad Sofía (SOFIA_PERSONALITY)
│   │   └── company_info.py       # Info empresa, directorio contactos
│   ├── conversation/
│   │   ├── crm.py                # Prompts CRMAgent (calificación lead)
│   │   ├── info.py               # Prompts InfoAgent (RAG)
│   │   └── reception.py          # Prompts ReceptionAgent (clasificación)
│   └── middleware/
│       └── brain.py              # Prompts SofiaBrain (SOFIA_MIDDLEWARE_SYSTEM_PROMPT)
│
├── knowledge_base/               # Base de conocimiento RAG (8 archivos .txt)
│   ├── informacion_institucional.txt   # Historia, horarios, contacto general
│   ├── info_cobertura_propiedades.txt  # Cobertura geográfica
│   ├── info_estudios_libertador.txt    # Info Colegio Libertador
│   ├── info_pagos_online.txt           # Métodos de pago
│   ├── soporte_caja_pagos.txt          # Soporte caja
│   ├── soporte_contabilidad_facturas.txt
│   ├── soporte_contratos_terminacion.txt
│   └── soporte_departamentos.txt
│
├── rag/                          # Sistema RAG vectorial
│   ├── rag_service.py            # Orquestador RAG (carga, indexación, búsqueda)
│   ├── vector_store.py           # PgVectorStore (PostgreSQL + pgvector)
│   └── data_loader.py            # Chunking de documentos (500 tokens, 100 overlap)
│
├── integrations/
│   ├── __init__.py               # Expone hubspot_client singleton
│   └── hubspot/
│       ├── hubspot_client.py     # HTTP client HubSpot API v3 (tenacity retry)
│       ├── lead_assigner.py      # Round-robin asignación por canal
│       ├── contact_finder.py     # Búsqueda contactos por teléfono
│       ├── outbound_handler.py   # Envío mensajes outbound
│       ├── pipeline_router.py    # Routing canal → pipeline HubSpot
│       ├── timeline_logger.py    # Registro timeline eventos HubSpot
│       ├── deal_tracker.py       # (Legacy) Tracking de deals
│       ├── lead_counter.py       # Conteo de leads
│       └── hubspot_utils.py      # Utilidades: normalize_phone, calculate_lead_score
│
├── database/
│   └── mongodb_client.py         # Motor async client (mensajes + citas)
│
├── utils/
│   ├── twilio_client.py          # Twilio REST client + 429 backoff exponencial
│   ├── media_processor.py        # Bunny.net upload + Whisper + GPT-4o-mini
│   ├── message_aggregator.py     # Agrupación de mensajes cortos (debounce)
│   ├── link_detector.py          # Detección portales inmobiliarios (LinkDetector)
│   ├── property_code_detector.py # Detección códigos de inmuebles
│   ├── business_hours.py         # Horarios laborales + mensajes fuera de horario
│   ├── date_parser.py            # Parsing fechas conversacionales
│   ├── pii_validator.py          # Validación PII + extracción nombres (regex)
│   ├── reply_quote_formatter.py  # Formato citas/respuestas en WhatsApp
│   └── link_detector.py          # Detección de links portales inmobiliarios
│
├── tests/                        # Suite de pruebas
│   ├── panel/                    # 25+ tests del panel (51/51 PASS)
│   └── ...                       # agents, api, e2e, middleware, orchestrator, etc.
│
├── scripts/                      # Scripts de utilidad y diagnóstico
└── docs/                         # Documentación técnica
```

---

## 4. Flujo de Conversación Detallado

### 4.1 Pipeline de Mensaje Entrante (BOT_ACTIVE)

```
1. Twilio POST /whatsapp/webhook
   └── webhook_handler.py
       ├── PhoneNormalizer.normalize() → E.164
       ├── Redis lookup → conv_state:{phone}:{canal}
       ├── [BOT_ACTIVE] → SofiaBrain.process_message()
       │   ├── LangChain + RedisChatMessageHistory (MAX_HISTORY_MESSAGES=15)
       │   ├── Single-stream analysis → MessageAnalysis (emocion, intencion_visita, etc.)
       │   ├── [handoff_priority=high] → transición a HUMAN_ACTIVE
       │   └── Respuesta via twilio_client (REST, evita timeout 15s)
       │
       └── [HUMAN_ACTIVE] → espejeo a HubSpot Timeline, NO responder
           └── WebSocket broadcast → Panel Asesoras
```

### 4.2 Pipeline Orquestador (Agentes IA)

El sistema multi-agente opera independientemente del middleware (flujo CLI/testing):

```
orchestrator.process_message(session_id, user_message)
    │
    ├── [is_new_session | stale > 24h] → RECEPTION_START, is_first_message=True
    │
    ├── [CRM_CONVERSATION] → crm_agent.process_conversation()
    │   ├── _extract_entities() → LLM JSON parsing
    │   ├── [is_first_turn] → _generate_conversation_response() + LLM
    │   ├── [has_name] → process_lead_handoff() → HubSpot search-or-create
    │   │   ├── search_contact_by_phone() → deduplicación
    │   │   ├── create_contact() | update_contact() → HubSpot API v3
    │   │   ├── _activate_human_in_panel() → Redis HUMAN_ACTIVE + ZSET
    │   │   └── lead_assigner.get_next_owner(channel_origin)
    │   └── partial_hubspot_sync() → background task
    │
    ├── [TRANSFERRED_INFO] → info_agent.process_info_query()
    │   └── RAGService → pgvector search → LLM response
    │
    └── [RECEPTION_START] → reception_agent.process_message()
        ├── LinkDetector.analizar_mensaje() → fast-track to CRM si link inmueble
        ├── LLM + classify_intent tool (tool_choice forzado)
        │   ├── intent=crm → CRM_CONVERSATION
        │   ├── intent=info → TRANSFERRED_INFO
        │   └── intent=ambiguous → AWAITING_CLARIFICATION
        └── Retry logic: hasta 3 intentos
```

### 4.3 Estados de Conversación (Redis)

```
Redis Keys:
  conv_state:{phone}:{canal}   → BOT_ACTIVE | HUMAN_ACTIVE | IN_CONVERSATION
  conv_meta:{phone}:{canal}    → JSON (contact_id, owner_id, display_name, timestamps)
  active_conversations_sorted  → ZSET (timestamp score, para ordenar inbox)
  last_client_msg:{phone}      → timestamp último mensaje cliente (ventana 24h)
  phone_cache:{phone}          → contact_id HubSpot
```

**Ciclo de vida:**
```
BOT_ACTIVE → (handoff manual asesora | handoff CRMAgent) → HUMAN_ACTIVE
HUMAN_ACTIVE → (cierre asesora | inactividad TTL 72h) → BOT_ACTIVE
IN_CONVERSATION → estado temporal durante sesión activa asesora
```

---

## 5. Sistema de Agentes IA

### 5.1 ReceptionAgent
- **Propósito:** Clasificar intención del primer mensaje
- **LLM Tool:** `classify_intent` (tool_choice forzado, retry x3)
- **Intenciones:** `info` | `crm` | `ambiguous`
- **Fast-track:** Si detecta link de portal inmobiliario → directo a CRM
- **Prompt:** `prompts/conversation/reception.py` + `RECEPTION_SYSTEM_PROMPT`

### 5.2 InfoAgent (RAG)
- **Propósito:** Responder preguntas institucionales/soporte
- **Vector Store:** PgVector (PostgreSQL) con embeddings text-embedding-3-small
- **Knowledge Base:** 8 archivos .txt → chunks 500 tokens, overlap 100
- **Cobertura KB:**
  - Información institucional, horarios, contacto (desde 1990, Lonja de Propiedad Raíz)
  - Pagos online, caja, contabilidad
  - Contratos, terminación, departamentos
  - Cobertura propiedades (Área Metropolitana Antioquia)
- **Carga:** En startup del servidor (evita timeouts)

### 5.3 CRMAgent (El más complejo)
- **Propósito:** Calificar lead y sincronizar con HubSpot CRM
- **Modo:** Conversacional (mínimo 2 turnos antes de intentar sync)
- **Datos que recopila:**
  - Nombre completo (OBLIGATORIO — es el trigger para handoff)
  - Tipo de propiedad, operación, zona, presupuesto, características, email
- **Extracción de entidades:** LLM → JSON parsing (con fallback a limpieza de string)
- **Extracción de nombre:** Regex (`pii_validator.robust_extract_name`) + LLM
- **Link arrival:** Si llegó por link de portal → extrae entidades del URL
- **Sync a HubSpot:** search-before-create (deduplicación por teléfono)
- **Sync parcial:** background task (asyncio.create_task) si hay entidades pero no nombre
- **Post-sync:** Activa HUMAN_ACTIVE en Redis + ZSET para que aparezca en panel
- **Lead scoring:** `calculate_lead_score()` → 0-100 (bonus por canal, código inmueble, link)
- **Budget parser:** Convierte texto colombiano → entero (ej: "500 millones" → 500000000)

### 5.4 SofiaBrain (Middleware AI)
- **Propósito:** IA del middleware que opera cuando BOT_ACTIVE (independiente del orquestador)
- **Memoria:** LangChain `RedisChatMessageHistory` (MAX_HISTORY_MESSAGES=15)
- **Single-stream analysis:** Un solo LLM call produce `MessageAnalysis`:
  - `emocion`, `sentiment_score`, `intencion_visita`, `handoff_priority`
  - `cita_confirmada`, `fecha_cita_mencionada`, `hora_cita_mencionada`
  - `nombre_detectado`, datos CRM extraídos
- **Feature flag:** `TWILIO_CONVERSATIONS_ENABLED` (migración a Conversations API)
- **Prompt:** `prompts/middleware/brain.py` → `SOFIA_MIDDLEWARE_SYSTEM_PROMPT`

---

## 6. Panel de Asesoras (outbound_panel.py)

El archivo más grande del sistema: **8,238 líneas**, 16+ endpoints REST + WebSocket.

### 6.1 Endpoints Principales

| Endpoint | Función |
|----------|---------|
| `GET /whatsapp/panel/` | Sirve UI HTML del panel |
| `GET /whatsapp/panel/contacts` | Lista contactos activos (paginado, ZSET) |
| `POST /whatsapp/panel/send-message` | Envía mensaje asesora → cliente |
| `POST /whatsapp/panel/send-template` | Envía template Twilio aprobado |
| `POST /whatsapp/panel/take-control` | Asesora toma control (BOT → HUMAN_ACTIVE) |
| `POST /whatsapp/panel/close-conversation` | Cierra chat (HUMAN → BOT_ACTIVE) |
| `GET /whatsapp/panel/conversation/{phone}` | Historial completo (MongoDB) |
| `POST /whatsapp/panel/upload-media` | Sube media → Bunny.net → Twilio |
| `WS /whatsapp/panel/ws/{owner_id}` | WebSocket real-time por asesora |
| `GET /whatsapp/panel/metrics` | Dashboard métricas |
| `GET /whatsapp/panel/appointments` | Gestión de citas |
| `POST /whatsapp/panel/bulk-campaign` | Campañas masivas por embudo |

### 6.2 Singletons críticos (anti-leak de memoria)

```python
_state_manager_singleton: ConversationStateManager   # línea 334
_redis_pool: Redis                                    # línea 97 → _get_redis_client()
_httpx_client: httpx.AsyncClient                      # línea 100 → get_httpx_client()
hubspot_client: HubSpotClient                         # integrations/__init__.py
```

### 6.3 Funcionalidades del Panel

- **Inbox ordenado:** ZSET `active_conversations_sorted` (score = timestamp)
- **Portal chips:** Indicadores visuales por canal origen (Meta, Finca Raíz, etc.)
- **Badge no leídos:** Contador de mensajes sin leer por conversación
- **Ventana 24h:** Bloqueo automático si no hay mensaje del cliente en 24h
- **Notificaciones flotantes:** Alertas sin respuesta (ventana 5h)
- **Respuestas citadas:** `reply_quote_formatter.inject_quote()`
- **Media:** Audio (OGG→MP4 + transcripción), imágenes (análisis GPT-4o), documentos
- **Templates Twilio:** Plantillas preaprobadas para ventanas cerradas
  - Template seguimiento_cita: `HX9696fc73c9dbb5f76382bd77b5f410c8`
- **Chip "📢 Masivo":** Identifica contactos de campañas bulk
- **Auto-cierre "No Responde":** Seleccionar embudo "other" → cierre automático

---

## 7. Integraciones Externas

### 7.1 HubSpot CRM API v3

**Arquitectura:** Contact-centric (sin Deals)

| Propiedad | Pipeline HubSpot |
|-----------|-----------------|
| `lifecyclestage = 1326631578` | Nuevo Lead |
| `lifecyclestage = 1326623075` | En Conversación |
| `lifecyclestage = marketingqualifiedlead` | Visita Agendada |
| `lifecyclestage = salesqualifiedlead` | Visita Realizada |

**Transiciones automáticas:**
- `_update_contact_to_en_conversacion`: Nuevo Lead → En conversación (al iniciar chat)
- `_update_contact_to_visita_realizada`: APScheduler 1h30min post-cita
- `PROTECTED_STAGES_POST_VISITA = {salesqualifiedlead, opportunity, customer, evangelist}`

**Propiedades custom del chatbot:**
```
chatbot_property_type, chatbot_rooms, chatbot_location, chatbot_budget,
chatbot_urgency, chatbot_operation_type, chatbot_preference, chatbot_score,
chatbot_email, chatbot_conversation, chatbot_timestamp, canal_origen,
whatsapp_id, chatbot_portal_url, url_chat
```

**Retry logic:** Tenacity (`stop_after_attempt=3, wait_exponential`)  
**HubSpotPropertyValidator:** Filtra propiedades no existentes antes de enviar  
**Cache:** `contact_stage:{contact_id}` TTL 3600s

### 7.2 Twilio

- **API:** Programmable Messaging (form-encoded) + Conversations API (feature-flagged)
- **Feature flag:** `TWILIO_CONVERSATIONS_ENABLED=true/false`
- **Problema histórico:** IS9deed (legacy) vs ISc6359 (SofIA_Proteger default) — split-brain resuelto
- **Timeout fix:** Respuesta inmediata 200 OK + procesamiento async (evita retry a los 15s)
- **429 Backoff:** `_post_with_429_retry()` en `utils/twilio_client.py` — backoff 2/4/8s, max 3 reintentos
- **WAMid:** `wamid_idx` en MongoDB, `_capture_outbound_wamid_deferred` desactivado (Twilio no expone via REST GET)

### 7.3 MongoDB (Motor async)

**Colecciones:**
- `messages`: Historial real-time con TTL
- `contacts`: Cache local de contactos HubSpot
- `appointments`: Citas programadas (APScheduler)
- `bulk_campaigns`: Campañas masivas

**Serialización:** `_iso_bogota()` — BSON UTC → ISO 8601 con offset Bogotá (-05:00)

### 7.4 Bunny.net CDN

- **Uso:** Almacenamiento permanente de media (audio, imágenes, documentos)
- **Flujo entrante:** Twilio media URL → descarga → Bunny.net upload → URL permanente
- **Flujo saliente:** Panel asesora sube archivo → Bunny.net → URL → Twilio envío
- **Audio:** ffmpeg/libopus para conversión OGG → MP4 (compatible WhatsApp)

### 7.5 Redis

**Patrones de keys principales:**
```
conv_state:{phone}:{canal}          → estado conversación (TTL 72h)
conv_meta:{phone}:{canal}           → metadata JSON (TTL 72h)
active_conversations_sorted         → ZSET inbox panel
last_client_msg:{phone}             → timestamp última actividad cliente
phone_cache:{phone}                 → contact_id HubSpot (cache)
contact_stage:{contact_id}          → lifecyclestage HubSpot (TTL 3600s)
contact_name:{contact_id}           → nombre contacto (TTL 4h)
deal_id_cache:{phone}               → deal_id HubSpot (legacy)
round_robin:{team}                  → índice round-robin asignación
bulk_campaign:{campaign_id}         → metadatos campaña masiva
```

### 7.6 PostgreSQL + pgvector

- **Colección:** `sofia_knowledge_base`
- **Chunk size:** 500 tokens, overlap 100
- **Modelo embeddings:** `text-embedding-3-small`
- **Inicialización:** Al startup del servidor (no lazy)
- **Limpieza:** DELETE total antes de re-indexar (evita duplicados)

---

## 8. Distribución de Portales por Asesora

| Canal de Origen | Asesora | Owner ID HubSpot | Equipo |
|----------------|---------|-----------------|--------|
| WhatsApp Directo, WhatsApp | Jubeny | 89096378 | equipo_portales |
| Mercado Libre, LinkedIn | Jubeny | 89096378 | equipo_portales |
| Facebook, Instagram, TikTok | Jubeny | 89096378 | equipo_portales |
| Ciencuadras, Charly | Jubeny | 89096378 | equipo_portales |
| Página Web, YouTube | Luisa | 89096380 | equipo_directo |
| Finca Raíz, MetroCuadrado | Luisa | 89096380 | equipo_directo |
| Google Ads, Referido, Desconocido | Round-robin | 89096378/80 | default |
| Equipo Marketing (solo métricas) | N/A | 82598814 | equipo_marketing |
| Respaldo (manual only) | Monica | 89096379 | equipo_respaldo |

**Nota importante:** WhatsApp migrado de Luisa → Jubeny el 2026-04-20. Solo afecta leads NUEVOS.

---

## 9. Schedulers (APScheduler)

| Job | Frecuencia | Función |
|-----|-----------|---------|
| Memory watchdog | Cada 2 min | SIGTERM si RSS > 4GB → Gunicorn reinicia worker |
| Recordatorio cita 24h | Cron | Envía template 24h antes de la cita |
| Recordatorio cita 2h | Cron | Envía template 2h antes |
| Auto-transición visita realizada | 1h30min post-cita | `lifecyclestage → salesqualifiedlead` |
| Cleanup conversaciones inactivas | Cron | TTL automático Redis |

---

## 10. Módulos de Utilidades

| Módulo | Función |
|--------|---------|
| `PhoneNormalizer` | Normalización E.164 Colombia (57XXXXXXXXXX) |
| `LinkDetector` | Detecta portales: Finca Raíz, MetroCuadrado, ML, Instagram, etc. |
| `PropertyCodeDetector` | Detecta códigos de inmuebles en mensajes |
| `BusinessHours` | Horarios Lun-Vie 8:30-17:00, Sab 8:30-12:00 + mensajes automáticos |
| `MessageAggregator` | Debounce: agrupa mensajes cortos antes de responder |
| `MediaProcessor` | Descarga Twilio → transcripción Whisper → Bunny CDN |
| `PiiValidator` | Extracción regex de nombres (`robust_extract_name`) |
| `ReplyQuoteFormatter` | Formato WhatsApp para mensajes citados |
| `DateParser` | Parsing fechas en lenguaje natural español |
| `TwilioClient` | Wrapper con backoff exponencial 429 |

---

## 11. Optimización de Memoria (Historia Crítica)

### Problema original: 31GB RAM en 48h en Railway Pro

**Causa raíz:** Múltiples fugas independientes. Resueltas en rondas progresivas:

**Ronda 1 (commit 8cb592f):**
- Singleton `_global_state_manager` en `app.py`
- History trim: `_MAX_HISTORY_ENTRIES=60` en `orchestrator.py`
- Deploy Dockerfile (jemalloc + `--max-requests 500`)

**Ronda 2 (commit 85ce67b):**
- Singleton `_state_manager_singleton` en `outbound_panel.py` (16 endpoints)
- Singleton `_hs_singleton` en `integrations/hubspot/__init__.py`

**Ronda 3 (commit 7552699):**
- Todos los `async with httpx.AsyncClient()` reemplazados por singletons
- `contact_finder.py`, `outbound_handler.py`, `media_processor.py`, `webhook_handler.py`, `twilio_client.py`, `lead_assigner.py`, `appointment_manager.py`

**Ronda 4 (ROOT CAUSE):**
- Memory watchdog (SIGTERM si > 4GB) → patrón sawtooth controlado
- `LD_PRELOAD=libjemalloc2` → reduce fragmentación glibc malloc 50-70%
- `batch limit=1000 → 300` en queries panel
- `--workers 1 --timeout 30 --graceful-timeout 15`

**Estado actual:** Patrón sawtooth 0.3GB → 4GB → 0.3GB (ciclo ~1-2h, controlado)

---

## 12. Gestión de Tests

```
tests/
├── panel/           # 51/51 PASS (panel endpoints)
├── agents/          # Tests por agente
├── api/             # Endpoints REST
├── e2e/             # Flujos end-to-end
├── middleware/       # webhook, state
├── orchestrator/    # Routing logic
├── rag/             # RAG service
├── state/           # Redis state
├── webhook/         # Twilio webhook
└── ...
```

**Cobertura clave:** Visita realizada, stage filter, mark-read, quote injection, URL validation, 4 requirements principales.

---

## 13. Rama Activa: twilio-conversations-migration

**Objetivo:** Migrar de Twilio Programmable Messaging (form-encoded) a Conversations API (JSON).

**Feature flag:** `TWILIO_CONVERSATIONS_ENABLED=true/false`  
**Problema resuelto (abr 30):** Split-brain entre `IS9deed` (legacy) y `ISc6359` (SofIA_Proteger).  
**Estado actual:** Migración en progreso. El webhook_handler.py ya soporta ambos formatos con flag.

**Cambios necesarios para completar migración:**
- Validar WAMid tracking en Conversations API
- `_capture_outbound_wamid_deferred` → implementar cuando Twilio exponga WAMid outbound
- Tests de integración Conversations API

---

## 14. Variables de Entorno Requeridas

```bash
# LLM
OPENAI_API_KEY=sk-...

# HubSpot
HUBSPOT_API_KEY=pat-...

# Twilio
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=+57...
TWILIO_CONVERSATIONS_ENABLED=false  # true para activar Conversations API

# Redis
REDIS_URL=redis://...
REDIS_PUBLIC_URL=redis://...   # (desarrollo local)

# MongoDB
MONGODB_URL=mongodb+srv://...

# PostgreSQL (pgvector)
DATABASE_URL=postgresql://...

# Bunny.net
BUNNY_API_KEY=...
BUNNY_STORAGE_ZONE=...
BUNNY_CDN_URL=...

# Panel
ADMIN_API_KEY=...

# Railway
RAILWAY_ENVIRONMENT=production  # auto-set por Railway
```

---

## 15. Persona de Sofía (Identidad del Bot)

- **Nombre:** Sofía
- **Rol:** Asesora virtual de Inmobiliaria Proteger
- **Tono:** Profesional, cercana, concisa, empática
- **Tuteo:** Siempre tuteo profesional
- **Identidad como IA:** NUNCA se identifica como IA o bot
- **Empresa:** Habla en primera persona plural (nosotros, nuestros)
- **Restricciones:** No inventa precios; no dice "Genial"; evita signos de exclamación
- **Idiomas:** Español, inglés u otro si el cliente lo solicita
- **Cobertura:** Área Metropolitana de Antioquia (Medellín, Envigado, Itagüí, Sabaneta, Bello, etc.)

---

## 16. Campañas Masivas (Bulk Campaigns — mayo 2026)

- **Almacenamiento:** MongoDB `bulk_campaigns`
- **Scheduler:** APScheduler async, envío isolado per-asesora
- **Templates autorizados:** 2 templates Twilio aprobados
- **Auto-promoción "No Responde":** Si cliente responde a campaña → HUMAN_ACTIVE
- **Panel:** Contactos ocultos hasta que responden; chip visual "📢 Masivo"
- **Flujo:** Asesora crea campaña → bulk send → MongoDB tracking → auto-open inbox si respuesta

---

## 17. Consideraciones Arquitectónicas

### Puntos Fuertes
1. **Singletons universales** — cero fugas de conexión en producción
2. **Async-first** — todo el stack usa asyncio nativo (FastAPI + Motor + redis.asyncio)
3. **Feature flags** — migración Conversations API sin downtime
4. **Memory watchdog** — auto-healing ante fragmentación de memoria
5. **Deduplicación leads** — search-before-create en HubSpot
6. **jemalloc** — gestión agresiva de arenas de memoria
7. **Message aggregator** — debounce evita respuestas fragmentadas

### Deuda Técnica
1. `outbound_panel.py` — 8,238 líneas, candidato a refactor modular
2. WAMid outbound tracking — `_capture_outbound_wamid_deferred` sin implementar
3. `state_manager.py` vs `conversation_state.py` — dos gestores de estado (agents vs middleware)
4. Tests E2E incompletos para Conversations API
5. `deal_tracker.py` — módulo legacy (Deal fue eliminado del pipeline)

---

## 18. Comandos de Desarrollo

```bash
# Levantar servidor local
uvicorn app:app --reload --port 8001

# Tests panel
pytest tests/panel/ -v

# Re-indexar knowledge base
python scripts/reindex_knowledge_base.py

# Diagnostico contactos
python scripts/diagnostico_contactos_panel.py

# Fix canal mismatch
python scripts/fix_canal_mismatch.py

# Recuperar mensajes Twilio
python scripts/recover_twilio_messages.py
```

---

## 19. Historial de Cambios Significativos

| Fecha | Commit | Cambio |
|-------|--------|--------|
| Abr 2026 | 8cb592f | Singleton CSM app.py + history trim + Dockerfile |
| Abr 2026 | 85ce67b | Singleton CSM outbound_panel.py (16 endpoints) |
| Abr 2026 | 7552699 | Singleton HubSpotClient httpx |
| Abr 2026 | — | Ronda 2-4 memory optimization (watchdog, jemalloc) |
| Abr 20 | — | WhatsApp migrado Luisa → Jubeny |
| Abr 30 | — | Fix split-brain Conversations API + fix WAMid |
| May 2026 | — | Bulk Campaigns + No Responde auto-close |
| May 2026 | — | Twilio 429 backoff exponencial |
| May 2026 | a0aedd9 | Fix canal mismatch WhatsApp → panel asesoras |
| May 28 2026 | — | Setup SRE: CLAUDE.md + claude_desktop_config.json (Railway MCP) |

---

## 20. Setup SRE — Monitoreo Activo (2026-05-28)

### Objetivo
Convertir a Claude en un SRE activo capaz de ejecutar `railway logs`, analizar anomalías del stack y generar reportes consolidados por sesión de desarrollo.

### Lo que se configuró

**`CLAUDE.md` (nuevo archivo en raíz del proyecto)**
- Cerebro estático del repositorio — se carga automáticamente en cada sesión de Claude CLI
- Documenta: singletons, redis keys, feature flags, archivos de alto riesgo, SRE commands
- Ruta: `AgenteConversacional_Practica/CLAUDE.md`

**`claude_desktop_config.json` (nuevo archivo)**
- Configura el servidor MCP de Railway para Claude Desktop
- Ruta: `C:\Users\Salo\AppData\Roaming\Claude\claude_desktop_config.json`
- Contenido:
```json
{
  "mcpServers": {
    "railway": {
      "command": "npx",
      "args": ["-y", "@railway/mcp-server"]
    }
  }
}
```
- **Requiere reiniciar Claude Desktop** para que el MCP se cargue

### Pasos pendientes para completar el setup SRE

1. **Instalar Railway CLI** (usuario debe ejecutar en PowerShell/Terminal de Windows):
   ```powershell
   npm i -g @railway/cli
   railway login        # abre browser para OAuth
   cd C:\Users\Salo\Desktop\AgenteConversacional_Practica
   railway link         # vincula el proyecto Railway
   ```
   Nota: `.railway/` ya existe en `C:\Users\Salo\.railway` — Railway fue configurado previamente.

2. **Reiniciar Claude Desktop** después de guardar `claude_desktop_config.json`

3. **Verificar MCP activo** en Claude Desktop: debería aparecer el servidor "railway" en herramientas disponibles

### Capacidades SRE habilitadas post-setup
- `railway logs --lines 200` → análisis automático de anomalías
- Detección: `MongoServerSelectionTimeoutError`, `ConnectionError: Redis`, `memory watchdog + SIGTERM`, `422 Unprocessable`, `HubSpot 429`, `OpenAI APIError`
- Correlación error ↔ archivo fuente ↔ asesora afectada

---

## Sesión 2026-05-30 — Persistencia permanente del historial del panel

### Problema reportado
Conversaciones de más de una semana desaparecían del inbox del panel, aunque las notas seguían existiendo en HubSpot. Las asesoras perdían trazabilidad de leads históricos. Credibilidad afectada.

### Diagnóstico (causa raíz)
El inbox del panel se alimentaba **exclusivamente** del ZSET Redis `active_conversations_sorted`. Cinco mecanismos lo vaciaban sin re-hidratación:

1. **Ghost cleanup automático** en `get_active_contacts()` — borraba (`zrem`) cualquier miembro cuyo `conv_meta` hubiera expirado.
2. **TTL asimétrico de `conv_meta`** — 48–72h dinámico para contactos sin handoff explícito vs 365d con handoff. Los leads que pasaban por `update_activity`/`ensure_meta_with_channel` sin handoff caían en 2–3 días.
3. **Marcador `conv_was_panel`** expira a 365d (válido, pero no es la causa de 1 semana).
4. **TTL MongoDB messages 90d** — bomba de tiempo a 3 meses (siguiente fuga).
5. **Scripts manuales destructivos** (`cleanup_redis.py`, `cleanup_no_history.py`) con credenciales hardcoded.

### Cambios implementados (Plan de 6 fases)

**Fase 4.1 — TTL MongoDB `messages` 90d → 730d** (`database/mongodb_client.py:174-189`)
Subido a 2 años. Comentario incluye instrucciones para aplicar el cambio sobre el índice existente en producción (`dropIndex` + `createIndex`, collMod no soporta nombre custom).

**Fase 4.2 — Desactivar ghost cleanup + TTL `conv_meta` unificado a 365d** (`middleware/conversation_state.py`)
- `ghosts_to_remove` renombrado a `ghosts_detected` — ahora solo log warning, NO `zrem`. Job nocturno valida contra MongoDB antes de purgar.
- `bot_ghosts` en `BOT_CONTROLLED_SET` con mismo tratamiento.
- 6 callsites de `_calculate_dynamic_ttl()` para `meta_key` reemplazados por `PANEL_TTL_SECONDS` (365d): `update_activity`, `update_client_message_timestamp`, `update_advisor_message_timestamp`, `transfer_contact`, `ensure_meta_with_channel`, P3-D v2 canal merge.
- `state_key` mantiene TTL corto (7d) — refleja lifecycle real.

**Fase 4.3 — Colección MongoDB `conversations` (fuente permanente del inbox)** (`database/mongodb_client.py`)
- Nueva colección sin TTL — 1 documento por `(phone, canal)`.
- 6 métodos nuevos: `upsert_conversation_on_message`, `update_conversation_meta`, `get_conversation`, `find_conversations_by_owner`, `find_recent_conversations`, `search_conversations`.
- 5 índices nuevos: `(phone, canal) unique`, `(owner_id, last_message_at desc)`, `last_message_at`, `contact_id sparse`, `(archived, last_message_at desc)`.
- Instrumentación en `save_message()` con `asyncio.create_task()` fire-and-forget — 0 impacto en latencia del path crítico.
- Sincronización metadatos en `activate_human`, `request_handoff`, `transfer_contact` (Redis ↔ MongoDB).

**Fase 4.4 — Fallback Mongo en `get_active_contacts`** (`middleware/conversation_state.py:457+`, `middleware/outbound_panel.py:4795+`)
- Nuevo método `get_archived_conversations_from_mongo(owner_id, limit, exclude_phones)` retorna conversaciones con formato compatible.
- Cableado en `GET /contacts` cuando `advisor` está presente: tras leer ZSET, hidrata con conversaciones de MongoDB que no estén en el ZSET.
- Marca `_from_mongo_fallback=True` para debugging.

**Fase 4.5 — Límites de historial individual subidos** (`middleware/outbound_panel.py`)
- `/conversations/{phone}`: default 50→100, max 100→500.
- `/history/{contact_id}`: default 50→100, max 100→500.

**Fase 4.6 — Job nocturno `rebuild_zset_from_conversations`** (`app.py:589+`, scheduler `app.py:1419+`)
- APScheduler `CronTrigger(hour=3, minute=0, timezone="America/Bogota")` con `id="rebuild_zset_nightly"`.
- Dentro del bloque `_is_scheduler_leader` (un solo worker ejecuta).
- Lee `find_recent_conversations(since=90d)` con cap 5000, diff contra ZSET actual, `zadd` solo miembros faltantes (no pisa scores existentes).

### Archivos modificados
| Archivo | Líneas | Cambios |
|---------|--------|---------|
| `database/mongodb_client.py` | +210 | TTL 730d, colección `conversations`, 6 métodos nuevos, 5 índices, instrumentación `save_message` |
| `middleware/conversation_state.py` | +120 | Quitar ghost cleanup, unificar TTL meta 365d, nuevo método fallback Mongo, sync metadatos handoff |
| `middleware/outbound_panel.py` | +25 | Cableado fallback Mongo, límites historial subidos |
| `app.py` | +75 | Función `rebuild_zset_from_conversations` + registro APScheduler 3 AM |

### Validación
- `python -m py_compile` ✅ los 4 archivos compilan
- Smoke test estático (AST + grep) ✅ todas las defs y constantes presentes
- Cero tests existentes dependen de `_calculate_dynamic_ttl` o `ghosts_to_remove` → backward compatible
- Singletons preservados (`get_mongo_manager`, `_get_state_manager`)
- Memory watchdog impact: 1 op MongoDB extra fire-and-forget por mensaje (despreciable)
- Redis keys NO renombradas
- Feature flag `TWILIO_CONVERSATIONS_ENABLED` no tocado

### Deploy checklist post-merge

1. Aplicar el TTL nuevo al índice existente de `messages` en producción (MongoDB no soporta `collMod expireAfterSeconds` con nombre custom de índice):
   ```js
   db.messages.dropIndex("timestamp_ttl_idx")
   db.messages.createIndex({timestamp:1}, {expireAfterSeconds: 730*86400, name:"timestamp_ttl_idx"})
   ```
2. La colección `conversations` se crea automáticamente al primer `save_message` post-deploy. Índices se crean en `_ensure_indexes()` al primer connect.
3. **Opcional (recomendado):** ejecutar backfill manual de `conversations` desde `messages` históricos:
   ```js
   db.messages.aggregate([
     {$sort: {timestamp: 1}},
     {$group: {_id: {phone:"$phone", canal:"$channel"},
               first: {$first: "$timestamp"}, last: {$last: "$timestamp"},
               last_preview: {$last: "$content"}, last_sender: {$last: "$sender"},
               count: {$sum: 1}, contact_id: {$last: "$hubspot_contact_id"}}},
     {$project: {phone:"$_id.phone", canal:"$_id.canal",
                 first_message_at:"$first", last_message_at:"$last",
                 last_message_preview: {$substr: ["$last_preview", 0, 200]},
                 last_message_sender:"$last_sender", message_count:"$count",
                 contact_id:"$contact_id", archived:false,
                 created_at:"$first", updated_at: new Date()}},
     {$out: "conversations"}
   ])
   ```
4. Verificar `maxmemory-policy` Redis Railway — preferir `noeviction` para proteger ZSET sin TTL.
5. Programar `mongodump --db inmobiliaria_chat` diario.
6. Eliminar credenciales hardcoded de `cleanup_redis.py`, `cleanup_no_history.py`, `inactive_conversation.py` → mover a `scripts/` con `--dry-run` por defecto.

### Métricas a monitorear post-deploy
- Crecimiento de colección `conversations` (debería ser ~1 doc por contacto único).
- `[Panel][MongoFallback]` en logs — cantidad de conversaciones recuperadas vía fallback.
- `[Rebuild ZSET]` diario 3 AM — cuántas conversaciones re-agrega.
- Memoria del worker: confirmar que el patrón sawtooth no cambia (0.3GB→4GB→0.3GB).

### Recuperación de historial perdido (script de reconciliación)

Caso confirmado: contactos como Celia Gonzales (`+573015396265`) tienen historial truncado en MongoDB pero completo en HubSpot. Causa probable: `memory watchdog SIGTERM` mata el worker durante `insert_one` en MongoDB mientras que la nota a HubSpot sí se envía (corre en task separado con tenacity retry).

**Solución:** `scripts/reconcile_history_from_hubspot.py` — re-importa notas HubSpot faltantes a MongoDB `messages` + upsert paralelo en `conversations`.

Características:
- Idempotente (dedupe por `sha1(timestamp_min + sender + content[:200])`)
- Tres modos: diagnóstico (default, no escribe), `--dry-run` (simula), `--confirm` (escribe)
- Filtros: `--contact-id`, `--phone`, `--owner-id`, `--since`, `--gap-threshold`, `--limit-contacts`
- Marca cada mensaje restaurado con `metadata._restored_from_hubspot=True` para auditoría
- Respeta rate limits HubSpot (pausa 0.2s entre contactos, usa `_rate_limited_request` del singleton)

**Workflow recomendado post-deploy:**
```bash
# 1. Diagnóstico global — qué contactos tienen huecos significativos
python scripts/reconcile_history_from_hubspot.py --gap-threshold 5

# 2. Validar con dry-run en un contacto específico
python scripts/reconcile_history_from_hubspot.py --phone +573015396265 --dry-run

# 3. Restauración real, caso quirúrgico (recomendado primero):
python scripts/reconcile_history_from_hubspot.py --phone +573015396265 --confirm

# 4. Si funciona bien, barrido por asesora:
python scripts/reconcile_history_from_hubspot.py --owner-id 89096378 --confirm
```

### Backlog (no incluido en esta sesión)
- Endpoint `GET /contacts/search-history?advisor=X&q=...&date_from=...` que consulta `search_conversations` directamente.
- Toggle "ver archivadas" en `PanelAsesores/index.js`.
- Migración Redis ZSET → MongoDB como fuente primaria (paginación cursor-based desde Mongo).
- Eliminar `cleanup_redis.py` / `cleanup_no_history.py` o moverlos a `scripts/` con confirmación.

---

---

## Sesión 2026-06-01 — Auditoría BI de rendimiento Mayo 2026

### Objetivo
Informe ejecutivo de rendimiento del agente conversacional SofIA para mayo 2026, cruzando datos de MongoDB, HubSpot CRM y Twilio WhatsApp API.

### Hallazgos clave
- **12,807 mensajes** procesados en mayo (6,134 client / 4,734 advisor / 1,788 bot / 151 system).
- **785 clientes únicos** escribieron (790 según Twilio — 5 contactos fantasma por timeouts de MongoDB).
- **574 usuarios nuevos** (primera interacción histórica). HubSpot creó 617 contactos (43 por creación CRM-first desde portales).
- **Tasa de automatización: 2.4%** — solo 31 conversaciones se resolvieron sin intervención humana.
- **97.6% requirió asesora humana** — Sofía actúa casi exclusivamente como agente de recepción/clasificación, no resolutivo.
- Pico de tráfico: **6 de mayo** con 229 clientes y 782 mensajes (investigar fuente de campaña).
- Ventana activa: 8 AM–5 PM concentra el 91.2% del tráfico.
- Colección `conversations` en MongoDB tiene **0 documentos** — requiere backfill post-deploy del 30/05.

### Archivos generados
| Archivo | Contenido |
|---------|-----------|
| `INFORME_RENDIMIENTO_MAYO_2026.md` | Reporte ejecutivo completo con 5 secciones |
| `scripts/audit_mayo_2026.py` | Script de extracción MongoDB + Twilio |
| `scripts/audit_mayo_2026_results.json` | Datos crudos de la auditoría |

### Recomendaciones pendientes de implementar
1. **Prompt de Sofía:** Ampliar resolución autónoma (FAQs de disponibilidad, precios, requisitos) para subir tasa de automatización del 2.4%.
2. **Dead letter queue:** Encolar mensajes en Redis cuando `save_message()` falle (evitar los 5 fantasma/mes).
3. **Backfill `conversations`:** Script de agregación desde `messages` para poblar la colección vacía.
4. **Sincronizar resumen IA a HubSpot:** Timeline notes automáticas al cierre de conversación.
5. **Flujo fuera de horario:** IA más agresiva capturando datos completos + callback programado.

---

*Este documento es la fuente de verdad del estado actual del proyecto. Debe actualizarse en cada sesión de desarrollo relevante.*
