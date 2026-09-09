# SofIA — Agente Conversacional de Inmobiliaria Proteger

Sistema conversacional de WhatsApp con IA para captación y atención de leads
inmobiliarios, más un panel web en tiempo real donde las asesoras toman el
control de la conversación cuando el bot ya no alcanza.

En producción sobre Railway. La documentación de abajo describe el sistema tal
como está en el código, no como se planeó.

---

## Índice

- [Qué hace el sistema](#qué-hace-el-sistema)
- [Stack](#stack)
- [Flujo central](#flujo-central)
- [Estructura del proyecto](#estructura-del-proyecto)
- [Módulos](#módulos)
- [Automatizaciones programadas](#automatizaciones-programadas)
- [API HTTP](#api-http)
- [Almacenamiento y claves](#almacenamiento-y-claves)
- [Tests](#tests)
- [Despliegue](#despliegue)
- [Reglas críticas antes de tocar código](#reglas-críticas-antes-de-tocar-código)
- [Documentación complementaria](#documentación-complementaria)

---

## Qué hace el sistema

Un cliente escribe por WhatsApp desde alguno de los 16 canales registrados
(portales inmobiliarios, redes sociales, la página web, WhatsApp directo). El
sistema:

1. **Recibe** el mensaje por webhook de Twilio y normaliza el teléfono.
2. **Identifica el canal** de origen por los enlaces del mensaje y le asigna
   una asesora dueña según el equipo de ese canal.
3. **Responde con IA** (SofIA) mientras la conversación esté en `BOT_ACTIVE`,
   consultando la base de conocimiento por RAG.
4. **Escala a una persona** cuando detecta intención de compra o arriendo: el
   estado pasa a `HUMAN_ACTIVE` y la conversación aparece en el panel.
5. **Registra todo en HubSpot** — contacto, etapa del embudo y timeline.
6. **Agenda, recuerda y da seguimiento a las citas** de visita de forma
   automática.

---

## Stack

| Capa | Tecnología |
|---|---|
| Backend | FastAPI (Python 3.12) sobre Gunicorn + UvicornWorker |
| LLM | OpenAI GPT-4o-mini + `text-embedding-3-small`, orquestado con LangChain |
| Estado conversacional | Redis (`redis.asyncio`) |
| Historial | MongoDB (Motor, async) |
| Vectores / RAG | PgVector sobre PostgreSQL |
| CRM | HubSpot API v3 (modelo *contact-centric*, sin Deals) |
| Mensajería | Twilio WhatsApp (webhook + Conversations API tras feature flag) |
| Media | Bunny.net Storage CDN |
| Tareas programadas | APScheduler (AsyncIO) |
| Frontend | HTML + JavaScript vanilla + Tailwind CSS |
| Deploy | Railway (build por Dockerfile) |

---

## Flujo central

```
Cliente WhatsApp
   │
   ▼
Twilio ──POST /whatsapp/webhook──► webhook_handler.py
                                      │
              ┌───────────────────────┴───────────────────────┐
              │                                               │
        [BOT_ACTIVE]                                  [HUMAN_ACTIVE]
              │                                               │
              ▼                                               ▼
        sofia_brain.py                              outbound_panel.py
      (memoria Redis L1)                                      │
              │                                               ▼
              ▼                                     websocket_manager.py
      Agentes: Reception / Info / CRM                         │
              │                                               ▼
              └──────────────┬────────────────────►  Panel de Asesoras
                             │
                             ▼
          HubSpot (contacto + etapa + timeline)
          MongoDB (historial)
          Redis (estado + inbox)
```

El motor vivo es **`middleware/sofia_brain.py`** (`SofiaBrain`), importado por
`webhook_handler.py:29`. El orquestador clásico `agents/orchestrator.py` sigue
existiendo y se usa desde `app.py:336` para el procesamiento por sesión; no es
el mismo camino que el del webhook de WhatsApp.

---

## Estructura del proyecto

```
AgenteConversacional_Practica/
│
├── app.py                          # FastAPI app, schedulers, memory watchdog (2.238 líneas)
├── main.py / startup.py            # Arranque e indexación inicial del RAG
├── state_manager.py                # FSM de sesión
├── llm_client.py                   # Wrapper de LangChain
│
├── agents/
│   ├── orchestrator.py             # Enrutado por intención
│   ├── ReceptionAgent/             # Clasificación de intención + captura de PII
│   ├── InfoAgent/                  # Consultas informativas con RAG
│   └── CRMAgent/crm_agent.py       # Sincronización con HubSpot (887 líneas)
│
├── middleware/
│   ├── webhook_handler.py          # Entrada de Twilio (2.714 líneas)
│   ├── sofia_brain.py              # Motor IA con memoria Redis L1
│   ├── outbound_panel.py           # Backend del panel (10.202 líneas)
│   ├── conversation_state.py       # Modelos de estado en Redis (2.055 líneas)
│   ├── appointment_manager.py      # Agenda de citas
│   ├── confirmacion_cita.py        # Composición del mensaje de confirmación
│   ├── depuracion_no_responde.py   # Motor de decisión del embudo "No responde"
│   ├── job_depuracion_masivos.py   # Job nocturno de depuración
│   ├── stage_filter.py             # Filtro por etapa con pull completo de HubSpot
│   ├── contact_manager.py          # Gestión de contactos
│   ├── phone_normalizer.py         # Normalización de teléfonos
│   ├── websocket_manager.py        # WebSockets del panel
│   ├── query_profiler.py           # Observabilidad de latencia (Server-Timing)
│   ├── templates/                  # Plantillas de WhatsApp y Content SIDs
│   └── PanelAsesores/              # Frontend del panel (index.js: 8.366 líneas)
│
├── integrations/hubspot/
│   ├── hubspot_client.py           # Cliente HTTP
│   ├── lead_assigner.py            # Asignación de leads por canal
│   ├── contact_finder.py           # Búsqueda de contactos
│   ├── pipeline_router.py          # Enrutado de etapas del embudo
│   ├── timeline_logger.py          # Eventos de timeline
│   ├── outbound_handler.py         # Webhooks salientes
│   └── deal_tracker.py, lead_counter.py, hubspot_utils.py
│
├── utils/
│   ├── advisors_registry.py        # Fuente única de identidad de asesoras
│   ├── channels_registry.py        # Fuente única de canales
│   ├── reply_trace.py              # Diagnóstico de citaciones entrantes
│   ├── reply_quote_formatter.py    # Formato de citaciones salientes
│   ├── pii_validator.py            # Extracción y validación de PII
│   ├── message_aggregator.py       # Agregación de mensajes seguidos
│   ├── media_processor.py          # Audio, imagen y documentos
│   ├── link_detector.py            # Detección de canal por enlace
│   ├── property_code_detector.py   # Códigos de inmueble
│   └── business_hours.py, date_parser.py, twilio_client.py
│
├── prompts/
│   ├── persona/                    # Identidad de SofIA e info de la empresa
│   ├── conversation/               # Prompts de reception, info y CRM
│   └── middleware/brain.py         # Prompt del motor único (el que ve el cliente)
│
├── rag/                            # Indexación, vector store y servicio RAG
├── database/mongodb_client.py      # Manager de MongoDB
├── knowledge_base/*.txt            # 8 documentos indexados en PgVector
├── tests/                          # 103 archivos, 1.375 funciones de test
└── docs/rediseno/                  # Documentación del rediseño en curso
```

---

## Módulos

### Motor conversacional

`middleware/sofia_brain.py` mantiene una memoria L1 en Redis y compone la
respuesta con un único prompt de sistema (`prompts/middleware/brain.py`). Los
agentes especializados viven en `agents/`:

| Agente | Responsabilidad |
|---|---|
| `ReceptionAgent` | Clasifica la intención del mensaje y captura PII |
| `InfoAgent` | Responde consultas informativas con RAG sobre `knowledge_base/` |
| `CRMAgent` | Identifica o crea el contacto en HubSpot y mueve la etapa del embudo |

La base de conocimiento se indexa al arrancar, en trozos de 500 tokens con 100
de solapamiento. Cambiar el *chunking* obliga a reindexar por completo.

### Agenda de citas

`middleware/appointment_manager.py` guarda las citas en Redis. Cada cita
(`Appointment`) lleva teléfono normalizado, canal, fecha ISO con offset,
asesora asignada y banderas de idempotencia para no repetir notificaciones.

Estados posibles (`AppointmentStatus`):

| Estado | Significado |
|---|---|
| `pending` | Agendada, esperando |
| `confirmed` | El cliente confirmó asistencia |
| `completed` | Visita realizada |
| `cancelled` | Cancelada |
| `no_show` | El cliente no asistió |

Todo el módulo trabaja en `America/Bogota`. Al agendar desde el panel,
`middleware/confirmacion_cita.py` compone el mensaje de confirmación que recibe
el cliente: decide *si hay con qué enviar* y *con qué texto*, pero no envía. El
texto sale de la plantilla `cita_confirmacion` almacenada en Redis, así que
editarla desde el panel cambia el mensaje sin tocar código.

Endpoints relacionados: `POST`/`GET /contacts/{id}/appointments`,
`PATCH /appointments/{id}`, `PATCH /appointments/{id}/cancel`,
`DELETE /appointments/{id}`, `GET /metrics/appointments`.

### Citaciones de mensajes

Cuando un cliente responde citando un mensaje anterior de WhatsApp, el sistema
lo detecta y lo reproduce en el panel. Son dos módulos con direcciones opuestas:

- **`utils/reply_trace.py`** — entrada. Interpreta el `ChannelMetadata` que
  manda Twilio, decide si de verdad hay una citación
  (`es_contexto_de_respuesta`), extrae la referencia y diagnostica el caso
  (`diagnosticar`, `es_perdida`). Incluye `forma_del_payload` y
  `forma_es_nueva`, que avisan cuando Twilio cambia el formato del payload.
  Cableado en `webhook_handler.py` (líneas 423 y 1653).
- **`utils/reply_quote_formatter.py`** — salida. `inject_quote` y
  `reply_audio_intro` anteponen el bloque citado al mensaje que envía la
  asesora. Cableado en `outbound_panel.py:64`.

> **Límite conocido:** Twilio solo conserva el contexto de citación unos 7 días.
> Si el mensaje citado es más antiguo, la referencia llega vacía. No es un fallo
> del panel.

### Roles internos y asignación

`utils/advisors_registry.py` es la **fuente única** de identidad de las
asesoras. Antes esto vivía dentro de `lead_assigner.py`; ahora el asignador
deriva su configuración de aquí. Añadir o quitar una asesora es tocar una sola
entrada.

Cada asesora tiene tres banderas que **no significan lo mismo**:

| Bandera | Qué decide |
|---|---|
| `receives_leads` | Recibe asignación automática de leads nuevos |
| `uses_panel` | Tiene bandeja en el panel |
| `receives_transfers` | Es el destino de las transferencias por embudo (solo una) |

Registro actual:

| ID HubSpot | Nombre | Equipo | Leads | Panel | Transferencias |
|---|---|---|---|---|---|
| 89096378 | Jubeny | `equipo_portales` | ✅ | ✅ | — |
| 89096380 | Luisa | `equipo_directo` | ✅ | ✅ | ✅ |
| 89096379 | Monica | `equipo_respaldo` | — | ✅ | — |
| 82598814 | Equipo de Marketing | `equipo_marketing` | — | — | — |

Mónica ilustra por qué las banderas están separadas: no recibe leads
automáticos, pero atiende los que le transfieren a mano, así que su bandeja
existe y hay que mantenerla.

`utils/channels_registry.py` hace lo equivalente con los 16 canales
(`finca_raiz`, `metrocuadrado`, `mercado_libre`, `ciencuadras`, `pagina_web`,
`instagram`, `facebook`, `linkedin`, `youtube`, `tiktok`, `whatsapp_directo`,
`whatsapp`, `google_ads`, `referido`, `desconocido`, `default`). Cada canal
define su equipo, su asesora dueña, su categoría, un bonus de score y los
patrones de enlace que lo identifican.

El panel expone además un flujo de transferencia con aceptación explícita:
`POST /contacts/{id}/transfer-request` → `transfer-accept` / `transfer-reject`.

### Seguimiento y depuración de embudo

- **`middleware/depuracion_no_responde.py`** — decide qué contacto del embudo
  "No responde" se da por perdido. Ofrece tres modos de conteo de agresividad
  creciente (`MODO_*`); el modo de historial completo mantiene el candado de no
  tocar a quien habló después del último masivo.
- **`middleware/job_depuracion_masivos.py`** — el job nocturno que aplica esa
  decisión (`ID_JOB = "depuracion_no_responde"`).
- **`middleware/stage_filter.py`** — cuando la asesora filtra por etapa, trae
  desde HubSpot *todos* sus contactos en esa etapa, con tope
  `MAX_STAGE_CONTACTS` (500 por defecto) y techo de 10 páginas.
- **Campañas masivas** — `POST /bulk-campaigns`, con vista previa
  (`/bulk-campaigns/preview`) y un procesador que corre cada 15 segundos.

## Portales de cada asesora
  **Asesora seguimiento:** Metro cuadrado y Finca Raiz + contactos transferidos a la asesora de seguimiento
  **Asesora Interna:** Todos los portales exeptuando los de la Asesora ed seguimienoto

## Embudos:
   Actualmente esta de la siguiente manera: Nuevo Lead, En conversacion, Visita Agendada, Visita Realizada, En Estudio, Cerrado Ganado, Cerrado Perdido, No Responde (gestion de seguimiento), Seguimiento, Hasta 1.5, Hasta 2, Hasta 2.5, Hasta 3 (Presupuestos para seguimiento), Aprobado, Local o Bodega (Seguimiento), Ventas, Otros municipios (seguimiento),Propietarios (seguimiento), Ya Encontro, Reubicados (Seguimiento)  
  *Actualmente la distribuicion de los portales se hacen modificando codigo. El plan es hacer sistema basado en datos donde esta seccion sea configurable*

  Estos son los 21 embudos actualmente en el panel. Como se esta haciendo sistema basado en roles, cada rol gestiona x embudos. Por ejemplo, los embudos escritos anteriormente, los que tienen el "Seguimiento" al lado son los que solamente gestiona el rol de seguimiento. Por lo tanto, cuando la asesora interna agrega un lead en el embudo de no responde automaticamente este es transferido al Owner_id / usuario de la asesora de seguimiento

  

### Panel de asesoras

Frontend en `middleware/PanelAsesores/`, servido en `/whatsapp/panel/static`.
`index.js` (8.366 líneas) maneja la bandeja, el chat, las citas y las
notificaciones; `metrics.html` y `metrics.js` el tablero de métricas con
Chart.js. La actualización en vivo llega por WebSocket dirigido por
`assigned_owner_id`, de modo que cada asesora solo recibe lo suyo.

---

## Automatizaciones programadas

16 jobs de APScheduler, registrados en `app.py`. Corren sobre el mismo worker de
Gunicorn, así que deben ser async y cortos.

| Job ID | Disparador | Función |
|---|---|---|
| `scheduler_leader_election` | cada 60 s | `_try_become_scheduler_leader` |
| `scheduler_lock_heartbeat` | cada 120 s | `_renew_scheduler_lock` |
| `memory_watchdog` | cada 2 min | `_memory_watchdog` |
| `scheduled_messages` | cada 1 min | `check_scheduled_messages` |
| `bulk_campaign_processor` | cada 15 s | `_process_bulk_campaign_tick_safe` |
| `rescue_stalled_leads` | cada 5 min | `rescue_stalled_leads` |
| `apt_reminders` | cada 30 min | `check_appointment_reminders` |
| `apt_followups` | cada 15 min | `check_appointment_followups` |
| `apt_followup2` | cada 15 min | segundo seguimiento post-cita |
| `followup_24h` | cada 1 h | `check_and_send_followups` |
| `advisor_24h_notifications` | cada 1 h | notificaciones a asesoras |
| `conv_timeouts` | cada 2 h | `check_conversation_timeouts` |
| `reconcile_owner_ids` | cada 6 h | `reconcile_owner_ids` |
| `aprobados_daily_reminder` | 09:00 Bogotá | `check_aprobados_daily` |
| `rebuild_zset_nightly` | 03:00 Bogotá | `rebuild_zset_from_conversations` |
| `depuracion_no_responde` | 05:00 Bogotá | `check_depuracion_masivos` |

**Elección de líder.** Railway solapa procesos durante el despliegue, así que el
liderazgo del scheduler no puede decidirse una sola vez al arrancar. Un worker
toma un lock en Redis, lo renueva por *heartbeat*, y si detecta que el lock ya
es de otro worker **pausa todos sus jobs** y vuelve a la elección
(`app.py:955-975`).

---

## API HTTP

87 rutas registradas en tres routers (`app.py:205-207`). Resumen por familia:

| Familia | Rutas | Ejemplos |
|---|---|---|
| Webhooks | 4 | `POST /webhook`, `/outbound`, `/status`, `/hubspot/webhook` |
| Contactos | 15 | `GET /contacts`, `/contacts/search`, `/contacts/{id}/detail`, `/hydrate`, `/take-control`, `/close`, `/mark-read` |
| Transferencias | 4 | `/transfer`, `/transfer-request`, `/transfer-accept`, `/transfer-reject` |
| Mensajes | 6 | `POST /send-message`, `/send-message-json`, `/send-template`, `PATCH` y `DELETE /messages/{id}` |
| Citas | 5 | `POST` y `GET /contacts/{id}/appointments`, `PATCH /appointments/{id}`, `/cancel`, `DELETE` |
| Notas | 4 | `POST`, `GET`, `PATCH`, `DELETE /contacts/{id}/notes` |
| Plantillas | 6 | `GET`, `POST`, `PUT`, `DELETE /templates`, `GET /config/template-sids` |
| Programados | 3 | `POST` y `GET /contacts/{id}/scheduled-messages`, `DELETE` |
| Campañas | 4 | `POST /bulk-campaigns`, `/preview`, `GET /bulk-campaigns/{id}`, `/last/{id}` |
| Métricas | 6 | `GET /metrics`, `/metrics/appointments`, `/export`, `/export-excel` |
| Asesoras y workers | 7 | `GET /advisors`, `PATCH /advisors/{id}`, CRUD de `/workers` |
| Notificaciones | 3 | `GET /notifications`, `/{id}/read`, `/read-all` |
| Administración | 11 | `/admin/activate-bot`, `/activate-human`, `/recover-outage`, `/recover-lost-conversations`, `/sync-owners`, `/cleanup-stale-inbox` |
| Salud y diagnóstico | 6 | `GET /health`, `/diagnose`, `/debug/redis`, `/ws/stats`, `/stages`, `/window-status/{phone}` |

Los endpoints administrativos validan `ADMIN_API_KEY` mediante
`outbound_panel._validate_api_key`.

---

## Almacenamiento y claves

### Redis — estado conversacional

```
conv_state:{phone}:{canal}        BOT_ACTIVE | HUMAN_ACTIVE | IN_CONVERSATION
conv_meta:{phone}:{canal}         JSON del contacto
active_conversations_sorted       ZSET de la bandeja del panel
last_client_msg:{phone}           control de la ventana de 24 h
phone_cache:{phone}               contact_id de HubSpot
```

> ⚠️ Renombrar una de estas claves sin script de migración borra el estado de
> **todas** las conversaciones activas.

### MongoDB

Colecciones `conversations`, `messages` y `appointments`. Una conversación se
identifica por el par **(teléfono, canal)**, no solo por el teléfono: hay
clientes con conversaciones simultáneas en canales distintos.

> ⚠️ Mongo devuelve *datetimes* naive en UTC. Compararlos contra un
> `datetime.now(TIMEZONE)` con zona lanza `TypeError` o desfasa 5 horas.

### PgVector

Base de conocimiento vectorizada desde `knowledge_base/*.txt`.

---

## Tests

103 archivos con 1.375 funciones de test, organizados por área:

```
tests/agents/        tests/middleware/     tests/panel/       tests/rag/
tests/api/           tests/orchestrator/   tests/prompts/     tests/state/
tests/e2e/           tests/qa/             tests/utils/       tests/webhook/
```

```bash
pytest                      # suite completa
pytest tests/panel -v       # solo el panel
pytest tests/rag -v         # solo RAG
```

Configuración en `pytest.ini`. Dependencias de test en `requirements-test.txt`.

---

## Despliegue

Railway construye con **Dockerfile** (`railway.json`), no con nixpacks ni con el
`Procfile`. El comando real levanta un worker único:

```
gunicorn app:app --worker-class uvicorn.workers.UvicornWorker --workers 1 \
  --timeout 30 --graceful-timeout 15 --keep-alive 25 \
  --max-requests 500 --max-requests-jitter 50
```

> El `Procfile` del repositorio declara `--workers 2`, pero **no se usa** en
> Railway porque el builder configurado es el Dockerfile.

### Variables de entorno

```
OPENAI_API_KEY                 HUBSPOT_ACCESS_TOKEN
TWILIO_ACCOUNT_SID             TWILIO_AUTH_TOKEN
TWILIO_PHONE_NUMBER            TWILIO_CONVERSATIONS_ENABLED   # feature flag
MONGODB_URI                    REDIS_URL
PGVECTOR_DATABASE_URL          BUNNY_STORAGE_API_KEY
ADMIN_API_KEY

QUERY_PROFILER_ENABLED         # default true
QUERY_PROFILER_SLOW_MS         # default 50
SERVER_TIMING_ENABLED          # default true
MAX_STAGE_CONTACTS             # default 500
HS_STAGE_CACHE_TTL             # default 60
```

Plantilla completa en `.env.example`.

### Observabilidad

`middleware/query_profiler.py` mide el desglose de latencia por request
(MongoDB / HubSpot / resto) y emite el header `Server-Timing`, que Chrome
DevTools grafica en la pestaña Network. Enmascara los paths y nunca registra
filtros de query, porque el panel tiene más de diez rutas con el teléfono en la
URL.

```bash
railway logs --tail
railway logs --lines 200 | grep -E "(ERROR|CRITICAL|memory|MongoDB|Redis)"
railway logs | grep QueryProfiler
```

---

## Reglas críticas antes de tocar código

1. **Singletons.** Redis, httpx y HubSpot se instancian una sola vez
   (`outbound_panel._get_redis_client`, `get_httpx_client`,
   `integrations/__init__.py`). Crear conexiones fuera de ellos produce fugas de
   memoria que disparan el reinicio del worker.
2. **PII.** Nunca registrar teléfonos completos ni nombres de clientes en
   producción. `utils/pii_validator.py` es la fuente de verdad.
3. **Claves de Redis.** No renombrar sin script de migración.
4. **Feature flag de Twilio.** Verificar `TWILIO_CONVERSATIONS_ENABLED` antes de
   tocar el flujo de mensajería.
5. **Archivos de alto riesgo:** `outbound_panel.py`, `webhook_handler.py`,
   `sofia_brain.py`, `crm_agent.py`, `conversation_state.py`.

Detalle completo en [`CLAUDE.md`](../CLAUDE.md) y en
[`ESTADO_ACTUAL_PROYECTO.md`](../ESTADO_ACTUAL_PROYECTO.md).

---

## Documentación complementaria

El rediseño en curso está documentado en [`docs/rediseno/`](rediseno/):

| Documento | Contenido |
|---|---|
| [`00-INDICE.md`](rediseno/00-INDICE.md) | Índice del rediseño |
| [`ESTADO.md`](rediseno/ESTADO.md) | Avance de la planeación |
| [`01-glosario.md`](rediseno/01-glosario.md) | Glosario y etapas del embudo |
| [`02-actores-y-roles.md`](rediseno/02-actores-y-roles.md) | Actores y roles |
| [`04-inventario-actual.md`](rediseno/04-inventario-actual.md) | Inventario del sistema actual |
| [`05-casos-de-uso.md`](rediseno/05-casos-de-uso.md) | Casos de uso |
| [`06-matriz-permisos.md`](rediseno/06-matriz-permisos.md) | Matriz de permisos |
| [`08-maquinas-de-estado.md`](rediseno/08-maquinas-de-estado.md) | Máquinas de estado |
| [`10-modelo-de-datos.md`](rediseno/10-modelo-de-datos.md) | Modelo de datos |
| [`11-fuentes-de-verdad.md`](rediseno/11-fuentes-de-verdad.md) | Fuentes de verdad |
| [`12-arquitectura.md`](rediseno/12-arquitectura.md) | Arquitectura objetivo |
| [`13-contratos-api.md`](rediseno/13-contratos-api.md) | Contratos de API |
| [`14-adrs.md`](rediseno/14-adrs.md) | Decisiones de arquitectura |
| [`18-migracion.md`](rediseno/18-migracion.md) | Plan de migración |
| [`AUDITORIA.md`](rediseno/AUDITORIA.md) | Auditoría |

Los wireframes están en [`rediseno/wireframes/`](rediseno/wireframes/).

También quedan en el repositorio dos documentos de la etapa de prototipo:
[`api/llm_client.md`](api/llm_client.md) y
[`implementation/pr1_info_agent_refactor.md`](implementation/pr1_info_agent_refactor.md).

---

**Última revisión:** 2026-09-01 · **Rama:** `main` · **Commits:** 543
