# INFORME TÉCNICO: Agente Conversacional Sofía

## Inmobiliaria Proteger - Estado Actual del Proyecto

**Fecha:** 3 de marzo de 2026  
**Versión:** 2.0 (Panel de Asesores v3)

---

## 1. RESUMEN EJECUTIVO

Sistema de **Agente Conversacional Multi-Agente** implementado para Inmobiliaria Proteger que integra:

- **Sofía**: Bot de atención al cliente en WhatsApp
- **Panel de Asesores**: Interfaz web para gestión de conversaciones
- **CRM HubSpot**: Sincronización automática de contactos y deals
- **RAG**: Base de conocimientos con búsqueda semántica

El sistema procesa mensajes de WhatsApp Business API, clasifica intenciones, responde consultas usando RAG, recolecta datos de leads y sincroniza con HubSpot en tiempo real.

---

## 2. ARQUITECTURA GENERAL

### 2.1 Diagrama de Alto Nivel

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│                                    CLIENTES                                          │
├───────────────────────┬───────────────────────┬──────────────────────────────────────┤
│   WhatsApp Users      │   Panel de Asesores   │        HubSpot CRM                   │
│   (Mensajes)          │   (Navegador Web)     │        (Webhooks)                    │
└───────────┬───────────┴───────────┬───────────┴──────────────────┬───────────────────┘
            │                       │                              │
            ▼                       ▼                              ▼
┌───────────────────────────────────────────────────────────────────────────────────────┐
│                              FASTAPI SERVER (Railway)                                 │
│  ┌─────────────────────────────────────────────────────────────────────────────────┐  │
│  │                              MIDDLEWARE LAYER                                    │  │
│  │  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐  ┌───────────────────┐ │  │
│  │  │ Webhook       │  │ Outbound      │  │ WebSocket     │  │ ContactManager    │ │  │
│  │  │ Handler       │  │ Panel API     │  │ Manager       │  │                   │ │  │
│  │  │ /webhook      │  │ /panel/*      │  │ /ws/{id}      │  │ Deduplicación     │ │  │
│  │  └───────┬───────┘  └───────────────┘  └───────────────┘  └───────────────────┘ │  │
│  └──────────┼──────────────────────────────────────────────────────────────────────┘  │
│             │                                                                          │
│  ┌──────────▼──────────────────────────────────────────────────────────────────────┐  │
│  │                           AGENTS LAYER (LangChain)                               │  │
│  │  ┌─────────────────────────────────────────────────────────────────────────────┐ │  │
│  │  │                           ORCHESTRATOR                                       │ │  │
│  │  │  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐                    │ │  │
│  │  │  │ Reception     │  │ Info          │  │ CRM           │                    │ │  │
│  │  │  │ Agent         │──▶│ Agent         │──▶│ Agent         │                    │ │  │
│  │  │  │ (Clasificar)  │  │ (RAG/KB)      │  │ (HubSpot)     │                    │ │  │
│  │  │  └───────────────┘  └───────┬───────┘  └───────┬───────┘                    │ │  │
│  │  └─────────────────────────────┼──────────────────┼────────────────────────────┘ │  │
│  └────────────────────────────────┼──────────────────┼──────────────────────────────┘  │
└───────────────────────────────────┼──────────────────┼──────────────────────────────────┘
                                    │                  │
                ┌───────────────────┴──────────────────┴────────────────────┐
                ▼                   ▼                  ▼                    ▼
┌────────────────────┐  ┌────────────────────┐  ┌────────────────────┐  ┌────────────────┐
│       REDIS        │  │      MONGODB       │  │    POSTGRESQL      │  │   HUBSPOT      │
│   (Estado/Cache)   │  │   (Mensajes/Citas) │  │   (RAG/Vectors)    │  │   (CRM)        │
└────────────────────┘  └────────────────────┘  └────────────────────┘  └────────────────┘
```

### 2.2 Patrón Arquitectónico

El sistema implementa una **Arquitectura Multi-Agente con RAG** utilizando:

| Patrón              | Implementación                                 |
| ------------------- | ---------------------------------------------- |
| **Mediator**        | `orchestrator.py` coordina agentes             |
| **Strategy**        | Agentes intercambiables (Info, CRM, Reception) |
| **Factory**         | Lazy initialization con `get_*()`              |
| **Singleton**       | Clientes globales (LLM, HubSpot, Redis)        |
| **Observer**        | WebSocket para notificaciones tiempo real      |
| **State Machine**   | Estados de conversación en Redis               |
| **Round Robin**     | Distribución de leads por canal                |
| **Circuit Breaker** | Retry con backoff exponencial                  |

---

## 3. STACK TECNOLÓGICO

### 3.1 Backend

| Tecnología             | Versión | Propósito                  |
| ---------------------- | ------- | -------------------------- |
| **Python**             | 3.11+   | Lenguaje principal         |
| **FastAPI**            | ≥0.104  | Web framework async        |
| **LangChain**          | 0.3.27  | Framework para LLM/Tools   |
| **OpenAI GPT-4o-mini** | -       | Modelo de lenguaje         |
| **Pydantic**           | ≥2.0    | Validación y serialización |
| **APScheduler**        | ≥3.10   | Jobs programados           |
| **Tenacity**           | -       | Retry logic                |
| **httpx**              | -       | Cliente HTTP async         |

### 3.2 Bases de Datos

| Tecnología                | Propósito          | Datos Almacenados                  |
| ------------------------- | ------------------ | ---------------------------------- |
| **Redis**                 | Estado distribuido | Sesiones, estados, cache, locks    |
| **MongoDB Atlas**         | Persistencia       | Mensajes, citas, workers, advisors |
| **PostgreSQL + pgvector** | RAG vectorial      | Embeddings, documentos chunkeados  |

### 3.3 Servicios Externos

| Servicio                | Propósito                  |
| ----------------------- | -------------------------- |
| **Twilio WhatsApp API** | Mensajería bidireccional   |
| **HubSpot CRM v3**      | Contactos, Deals, Timeline |
| **OpenAI Embeddings**   | text-embedding-3-small     |
| **Bunny.net CDN**       | Almacenamiento de audios   |
| **Railway**             | Hosting y deployment       |

### 3.4 Frontend

| Tecnología             | Propósito                       |
| ---------------------- | ------------------------------- |
| **Vanilla JavaScript** | Lógica del panel (~3100 líneas) |
| **TailwindCSS**        | Estilos y diseño responsivo     |
| **Jinja2**             | Templating server-side          |
| **WebSocket**          | Notificaciones en tiempo real   |

---

## 4. ESTRUCTURA DEL PROYECTO

```
AgenteConversacional_Practica/
│
├── app.py                      # FastAPI server principal
├── startup.py                  # Inicialización con retry (Railway)
├── main.py                     # CLI para testing local
├── llm_client.py               # Cliente OpenAI singleton
├── state_manager.py            # Gestión de estado del orquestador
├── logging_config.py           # Configuración centralizada de logs
├── requirements.txt            # Dependencias Python
│
├── agents/                     # 🤖 CAPA DE AGENTES
│   ├── orchestrator.py         # Coordinador central (Mediator)
│   ├── ReceptionAgent/         # Clasificación de intenciones
│   │   ├── agent.py            # Agente de recepción
│   │   └── tools.py            # Herramientas (link detector)
│   ├── InfoAgent/              # Consultas RAG
│   │   ├── agent.py            # Agente de información
│   │   └── tools.py            # Herramientas (RAG query)
│   └── CRMAgent/               # Recolección de datos
│       ├── agent.py            # Agente CRM
│       └── tools.py            # Herramientas (HubSpot sync)
│
├── middleware/                 # 🔌 CAPA DE MIDDLEWARE
│   ├── webhook_handler.py      # Webhooks Twilio (~1900 líneas)
│   ├── outbound_panel.py       # API del panel (~4340 líneas)
│   ├── conversation_state.py   # Estados distribuidos
│   ├── contact_manager.py      # Gestión de contactos HubSpot
│   ├── websocket_manager.py    # WebSocket broadcasts
│   ├── phone_normalizer.py     # Normalización E.164
│   ├── sofia_brain.py          # Lógica de respuesta del bot
│   ├── appointment_manager.py  # Gestión de citas
│   └── PanelAsesores/          # 🖥️ FRONTEND
│       ├── index.html          # UI principal (Jinja2 template)
│       ├── index.js            # Lógica JavaScript
│       └── style.css           # Estilos personalizados
│
├── integrations/               # 🔗 INTEGRACIONES
│   └── hubspot/
│       ├── hubspot_client.py   # Cliente HTTP con retry
│       ├── lead_assigner.py    # Asignación Round Robin
│       ├── timeline_logger.py  # Registro en Timeline
│       ├── contact_finder.py   # Búsqueda de contactos
│       ├── deal_tracker.py     # Seguimiento de deals
│       └── pipeline_router.py  # Routing por canal
│
├── database/                   # 💾 CAPA DE DATOS
│   └── mongodb_client.py       # Cliente MongoDB async
│
├── rag/                        # 🧠 RAG SYSTEM
│   ├── rag_service.py          # Orquestación de búsqueda
│   ├── vector_store.py         # pgvector store
│   └── data_loader.py          # Carga y chunking
│
├── knowledge_base/             # 📚 BASE DE CONOCIMIENTOS
│   ├── informacion_institucional.txt
│   ├── info_cobertura_propiedades.txt
│   ├── info_pagos_online.txt
│   ├── soporte_*.txt
│   └── info_estudios_libertador.txt
│
├── prompts/                    # 📝 SYSTEM PROMPTS
│   ├── sofia_personality.py    # Personalidad del bot
│   ├── reception_prompts.py    # Prompts de clasificación
│   ├── info_prompts.py         # Prompts de información
│   ├── crm_prompts.py          # Prompts de CRM
│   └── rag_prompts.py          # Prompts de RAG
│
├── utils/                      # 🛠️ UTILIDADES
│   ├── bunny_uploader.py       # Upload a CDN
│   ├── audio_handler.py        # Procesamiento de audio
│   └── validators.py           # Validaciones
│
├── tests/                      # 🧪 TESTS
│   ├── conftest.py             # Fixtures pytest
│   ├── test_*.py               # Tests unitarios
│   └── panel/                  # Tests del panel
│
├── scripts/                    # 📜 SCRIPTS DE MANTENIMIENTO
│   ├── create_timeline_event_type.py
│   ├── reindex_knowledge_base.py
│   └── diagnose_hubspot_errors.py
│
└── docs/                       # 📖 DOCUMENTACIÓN
    ├── README.md
    ├── api/                    # Documentación de API
    ├── architecture/           # Diagramas
    └── troubleshooting/        # Guías de solución
```

---

## 5. FLUJO DE MENSAJES

### 5.1 Mensaje Entrante (WhatsApp → Sistema)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     1. MENSAJE WHATSAPP ENTRANTE                            │
│                     (Twilio Webhook POST /whatsapp/webhook)                 │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                     2. WEBHOOK HANDLER                                       │
│  ├─ Extraer: From, Body, MediaUrl, ProfileName                              │
│  ├─ Normalizar teléfono a E.164 (+573...)                                   │
│  ├─ Detectar canal de origen (links en mensaje)                             │
│  └─ Verificar estado en Redis                                               │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
              ┌─────────────────────┴─────────────────────┐
              │                                           │
              ▼                                           ▼
┌───────────────────────────────┐         ┌───────────────────────────────────┐
│   ESTADO: BOT_ACTIVE           │         │   ESTADO: HUMAN_ACTIVE            │
│   (Sofía responde)             │         │   (Asesor atendiendo)             │
│                                │         │                                   │
│   → Orchestrator procesa       │         │   → Espejar mensaje a HubSpot     │
│   → LLM genera respuesta       │         │   → Guardar en MongoDB            │
│   → Twilio envía               │         │   → Notificar por WebSocket       │
│   → Log a HubSpot Timeline     │         │   → NO responder automáticamente  │
└───────────────────────────────┘         └───────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                     3. ORCHESTRATOR (agents/orchestrator.py)                 │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │                    RECEPTION AGENT                                      │ │
│  │  ├─ Clasificar intención del mensaje                                    │ │
│  │  ├─ Detectar links de portales (MetroCuadrado, FincaRaíz, etc.)        │ │
│  │  └─ Decidir: INFO (consulta) | CRM (lead) | HANDOFF (humano)            │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│                               │                                              │
│           ┌───────────────────┼───────────────────┐                         │
│           ▼                   ▼                   ▼                         │
│  ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐               │
│  │   INFO AGENT    │ │   CRM AGENT     │ │   HANDOFF       │               │
│  │   (RAG Query)   │ │   (Lead Data)   │ │   (Transfer)    │               │
│  │                 │ │                 │ │                 │               │
│  │  - Buscar en KB │ │  - Nombre       │ │  → HUMAN_ACTIVE │               │
│  │  - Responder    │ │  - Teléfono     │ │  → Notificar    │               │
│  │                 │ │  - Ubicación    │ │     asesor      │               │
│  │                 │ │  - Interés      │ │                 │               │
│  │                 │ │  → HubSpot Sync │ │                 │               │
│  └─────────────────┘ └─────────────────┘ └─────────────────┘               │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                     4. RESPUESTA + PERSISTENCIA                              │
│  ├─ Twilio: Enviar mensaje WhatsApp                                         │
│  ├─ MongoDB: Guardar en historial                                           │
│  ├─ HubSpot: Log en Timeline (background)                                   │
│  └─ Redis: Actualizar estado de sesión                                      │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 5.2 Mensaje Saliente (Panel → WhatsApp)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     PANEL DE ASESORES                                        │
│                     POST /whatsapp/panel/send-message                        │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                     OUTBOUND PANEL                                           │
│  ├─ Validar API Key                                                          │
│  ├─ Verificar ventana 24h (WhatsApp Policy)                                  │
│  ├─ Validar número de teléfono                                               │
│  └─ Detectar tipo de contenido (texto/audio/imagen)                          │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                     TWILIO API                                               │
│  ├─ Enviar mensaje WhatsApp                                                  │
│  └─ Callback de estado → /whatsapp/status                                    │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                     PERSISTENCIA (background)                                │
│  ├─ MongoDB: Guardar mensaje en historial                                    │
│  ├─ HubSpot: Log en Timeline del contacto                                    │
│  └─ Redis: Actualizar timestamp de actividad                                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 6. SISTEMA DE ESTADOS

### 6.1 Estados de Conversación (Middleware)

```python
class ConversationStatus(Enum):
    BOT_ACTIVE = "BOT_ACTIVE"           # Sofía responde automáticamente
    HUMAN_ACTIVE = "HUMAN_ACTIVE"       # Asesor atendiendo, bot silenciado
    IN_CONVERSATION = "IN_CONVERSATION" # Asesor en conversación activa
    PENDING_HANDOFF = "PENDING_HANDOFF" # Esperando transferencia
```

**Transiciones:**

```
                    ┌───────────────┐
                    │  BOT_ACTIVE   │ ← Estado inicial
                    └───────┬───────┘
                            │
         take_control() o   │   mensaje de asesor
         transfer_contact() │
                            ▼
                    ┌───────────────┐
                    │ HUMAN_ACTIVE  │
                    └───────┬───────┘
                            │
                close() o   │   timeout 24h
                devolver    │
                            ▼
                    ┌───────────────┐
                    │  BOT_ACTIVE   │
                    └───────────────┘
```

### 6.2 Estados del Orquestador

| Estado                   | Descripción                          |
| ------------------------ | ------------------------------------ |
| `RECEPTION_START`        | Inicio de conversación, clasificando |
| `AWAITING_CLARIFICATION` | Esperando aclaración del usuario     |
| `TRANSFERRED_INFO`       | Derivado a InfoAgent                 |
| `TRANSFERRED_CRM`        | Derivado a CRMAgent                  |
| `CRM_CONVERSATION`       | Recopilando datos de lead            |
| `WELCOME_SENT`           | Bienvenida enviada                   |

### 6.3 Claves Redis

| Clave                         | TTL     | Propósito                          |
| ----------------------------- | ------- | ---------------------------------- |
| `session:{session_id}`        | 30 días | Estado del orquestador (JSON)      |
| `conv_state:{phone}:{canal}`  | 24h     | Estado de middleware               |
| `conv_meta:{phone}:{canal}`   | 24h     | Metadatos (contact_id, advisor_id) |
| `active_conversations_sorted` | -       | ZSET ordenado por timestamp        |
| `last_client_msg:{phone}`     | 26h     | Ventana 24h WhatsApp               |
| `lead_creation_lock:{phone}`  | 5s      | Lock de deduplicación              |
| `panel_templates:*`           | -       | Templates guardados                |
| `lead_assigner:index:*`       | -       | Índice Round Robin                 |

---

## 7. BASE DE DATOS

### 7.1 MongoDB - Colecciones

**Base de datos:** `inmobiliaria_chat`

| Colección             | Propósito                   | Campos Principales                                       |
| --------------------- | --------------------------- | -------------------------------------------------------- |
| `messages`            | Historial de conversaciones | `phone`, `message`, `sender`, `timestamp`, `message_sid` |
| `contacts`            | Cache local de contactos    | `phone`, `hubspot_id`, `name`, `canal`                   |
| `appointments`        | Citas agendadas             | `contact_id`, `worker_id`, `appointment_dt`, `status`    |
| `appointment_workers` | Equipo de campo             | `name`, `active`, `created_at`                           |
| `panel_advisors`      | Asesores del panel          | `advisor_id`, `name`, `active`                           |

**Índices:**

```javascript
// messages
db.messages.createIndex({ phone: 1, timestamp: -1 });
db.messages.createIndex({ message_sid: 1 }, { unique: true, sparse: true });
db.messages.createIndex({ createdAt: 1 }, { expireAfterSeconds: 7776000 }); // 90 días TTL

// appointments
db.appointments.createIndex({ contact_id: 1, status: 1 });
db.appointments.createIndex({ appointment_dt: 1 });
```

### 7.2 PostgreSQL - RAG

**Extensión:** `pgvector`

| Tabla                     | Propósito                 |
| ------------------------- | ------------------------- |
| `langchain_pg_collection` | Colecciones de documentos |
| `langchain_pg_embedding`  | Embeddings vectoriales    |

**Parámetros RAG:**

- **Chunk Size:** 500 tokens
- **Chunk Overlap:** 100 tokens
- **Embedding Model:** text-embedding-3-small (1536 dimensiones)
- **Similarity:** Cosine

---

## 8. API ENDPOINTS

### 8.1 Webhooks (`/whatsapp/`)

| Método | Endpoint           | Propósito                      |
| ------ | ------------------ | ------------------------------ |
| POST   | `/webhook`         | Recepción de mensajes Twilio   |
| POST   | `/status`          | Callbacks de estado de entrega |
| POST   | `/hubspot/webhook` | Webhooks de HubSpot            |

### 8.2 Panel de Asesores (`/whatsapp/panel/`)

**UI:**
| Método | Endpoint | Propósito |
|--------|----------|-----------|
| GET | `/` | Interfaz web del panel |
| GET | `/static/{file}` | Archivos estáticos (JS, CSS) |

**Mensajería:**
| Método | Endpoint | Propósito |
|--------|----------|-----------|
| POST | `/send-message` | Envío (Form multipart) |
| POST | `/send-message-json` | Envío (JSON) |
| POST | `/send-template` | Envío de template |

**Contactos:**
| Método | Endpoint | Propósito |
|--------|----------|-----------|
| GET | `/contacts` | Lista contactos activos |
| GET | `/contacts/search` | Búsqueda en historial |
| POST | `/contacts/create` | Crear nuevo contacto |
| POST | `/contacts/{phone}/transfer` | Transferir a otro asesor |
| POST | `/contacts/{phone}/take-control` | Tomar control (human handoff) |
| DELETE | `/contacts/{phone}/close` | Cerrar conversación |
| PATCH | `/contacts/{contact_id}/name` | Editar nombre |
| PATCH | `/contacts/{contact_id}/stage` | Cambiar etapa pipeline |

**Historial:**
| Método | Endpoint | Propósito |
|--------|----------|-----------|
| GET | `/conversations/{phone}` | Historial por teléfono |
| GET | `/history/{contact_id}` | Historial por contact_id |
| GET | `/window-status/{phone}` | Estado ventana 24h |

**Templates:**
| Método | Endpoint | Propósito |
|--------|----------|-----------|
| GET | `/templates` | Listar templates |
| POST | `/templates` | Crear template |
| PUT | `/templates/{id}` | Actualizar |
| DELETE | `/templates/{id}` | Eliminar |

**Citas:**
| Método | Endpoint | Propósito |
|--------|----------|-----------|
| POST | `/contacts/{contact_id}/appointments` | Crear cita |
| GET | `/contacts/{contact_id}/appointments` | Listar citas |
| PATCH | `/appointments/{id}/cancel` | Cancelar cita |

**Equipo:**
| Método | Endpoint | Propósito |
|--------|----------|-----------|
| GET | `/advisors` | Listar asesores |
| PATCH | `/advisors/{id}` | Actualizar nombre |
| GET | `/workers` | Listar workers |
| POST | `/workers` | Crear worker |
| PATCH | `/workers/{id}` | Actualizar worker |
| DELETE | `/workers/{id}` | Eliminar worker |

**Métricas:**
| Método | Endpoint | Propósito |
|--------|----------|-----------|
| GET | `/metrics` | Dashboard |
| GET | `/metrics/export` | Exportar JSON |
| GET | `/metrics/export-excel` | Exportar Excel |

**WebSocket:**
| Método | Endpoint | Propósito |
|--------|----------|-----------|
| WS | `/ws/{advisor_id}` | Notificaciones tiempo real |
| GET | `/ws/stats` | Estadísticas conexiones |

---

## 9. PANEL DE ASESORES (Frontend)

### 9.1 Funcionalidades

**Gestión de Contactos:**

- Lista de contactos activos en tiempo real
- Filtros por tiempo (24h, 48h, 1 semana, personalizado)
- Búsqueda local + servidor
- Segregación por equipos (canal de origen)
- Deep links con auto-selección de contacto

**Chat:**

- Historial completo de mensajes
- Envío de texto, imágenes, audios
- Grabación de audio en navegador
- Templates predefinidos editables
- Indicador de ventana 24h de WhatsApp

**Pipeline de Ventas:**

- Visualización de etapas del deal
- Cambio de etapa desde el panel
- Sincronización bidireccional con HubSpot

**Citas:**

- Agendar citas con equipo de campo
- Selección de trabajador
- Confirmación automática por WhatsApp
- Cancelación con notificación

**Transferencias:**

- Transferir contacto a otro asesor
- Lista de asesores disponibles
- Registro en HubSpot

**Configuración:**

- Editar nombre del asesor (desde el panel)
- Gestionar equipo de campo (workers)

**Notificaciones Tiempo Real:**

- WebSocket con reconexión automática
- Badge de mensajes no leídos
- Sonido de notificación
- Ping cada 15 segundos

### 9.2 Segregación de Equipos

```javascript
// URLs de acceso directo por asesor
/whatsapp/panel/?key=API_KEY&advisor=89096380  // Asesor Portales
/whatsapp/panel/?key=API_KEY&advisor=89096378  // Asesor Directo
/whatsapp/panel/?key=API_KEY&advisor=82598814  // Marketing (métricas)
/whatsapp/panel/?key=API_KEY&advisor=89096379  // Asesor Respaldo
```

### 9.3 Deep Links (url_chat)

Cada contacto en HubSpot tiene una propiedad `url_chat` que enlaza directamente al panel con el contacto pre-seleccionado:

```
https://[domain]/whatsapp/panel/?key=API_KEY&advisor=89096380&phone=%2B573001234567
```

---

## 10. SISTEMA DE ASIGNACIÓN DE LEADS

### 10.1 Configuración de Equipos

```python
OWNERS_CONFIG = {
    # Portales inmobiliarios
    "equipo_portales": [
        {"name": "Asesor Portales", "id": "89096380", "active": True},
    ],

    # Directo + Redes Sociales
    "equipo_directo": [
        {"name": "Asesor Directo", "id": "89096378", "active": True},
    ],

    # Marketing (solo métricas)
    "equipo_marketing": [
        {"name": "Equipo de Marketing", "id": "82598814", "active": False},
    ],

    # Respaldo (transferencias manuales)
    "equipo_respaldo": [
        {"name": "Asesor Respaldo", "id": "89096379", "active": False},
    ],

    # Fallback (round robin)
    "default": [
        {"name": "Asesor Portales", "id": "89096380", "active": True},
        {"name": "Asesor Directo", "id": "89096378", "active": True},
    ],
}
```

### 10.2 Mapeo de Canales

| Canal            | Equipo          | Asesor ID   |
| ---------------- | --------------- | ----------- |
| MetroCuadrado    | equipo_portales | 89096380    |
| Finca Raíz       | equipo_portales | 89096380    |
| Mercado Libre    | equipo_portales | 89096380    |
| LinkedIn         | equipo_portales | 89096380    |
| WhatsApp Directo | equipo_directo  | 89096378    |
| Página Web       | equipo_directo  | 89096378    |
| Facebook         | equipo_directo  | 89096378    |
| Instagram        | equipo_directo  | 89096378    |
| YouTube          | equipo_directo  | 89096378    |
| TikTok           | equipo_directo  | 89096378    |
| Ciencuadras      | equipo_directo  | 89096378    |
| Desconocido      | default         | Round Robin |

---

## 11. INTEGRACIÓN HUBSPOT

### 11.1 Propiedades de Contacto

| Propiedad                  | Tipo     | Propósito                  |
| -------------------------- | -------- | -------------------------- |
| `phone`                    | string   | Teléfono normalizado E.164 |
| `canal_origen`             | string   | Fuente del lead            |
| `chatbot_timestamp`        | datetime | Primer contacto con Sofía  |
| `chatbot_score`            | number   | Score de calificación      |
| `chatbot_location`         | string   | Ubicación de interés       |
| `chatbot_urgency`          | string   | Nivel de urgencia          |
| `sofia_ultima_interaccion` | datetime | Última interacción         |
| `sofia_status`             | string   | Estado en el bot           |
| `url_chat`                 | string   | Deep link al panel         |
| `hubspot_owner_id`         | string   | ID del asesor asignado     |

### 11.2 Pipeline de Ventas

| ID         | Etapa            | Descripción           |
| ---------- | ---------------- | --------------------- |
| 1275156339 | Nuevo Lead       | Lead recién capturado |
| 1275156340 | En conversación  | Asesor atendiendo     |
| 1275156341 | Visita agendada  | Cita programada       |
| 1279054635 | Visita realizada | Post-visita           |
| 1275312311 | Propuesta        | Negociación           |
| 1279054636 | En estudio       | Evaluación            |
| 1275156342 | Cerrado ganado   | Venta exitosa         |
| 1279054637 | Cerrado vendido  | Completado            |

### 11.3 Timeline Events

Los mensajes se registran en el Timeline del contacto usando la **Notes API** con formato:

```
[WhatsApp] Mensaje de Sofía
─────────────────────────
{contenido del mensaje}

📅 Hora: 2026-03-03 14:32:05 COT
```

---

## 12. CONFIGURACIÓN Y DEPLOY

### 12.1 Variables de Entorno

```env
# LLM
OPENAI_API_KEY=sk-...

# HubSpot
HUBSPOT_API_KEY=pat-...
HUBSPOT_PIPELINE_ID=854756009
HUBSPOT_DEAL_STAGE=1275156339

# Twilio
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_PHONE_NUMBER=+573...

# Redis
REDIS_URL=redis://...
REDIS_PUBLIC_URL=redis://... (local)

# MongoDB
MONGO_URL=mongodb://...
MONGO_PUBLIC_URL=mongodb://... (local)

# PostgreSQL (RAG)
DATABASE_URL=postgresql://...

# Panel
ADMIN_API_KEY=...
PANEL_BASE_URL=https://...

# Opcionales
FOLLOWUP_ENABLED=false
FOLLOWUP_DELAY_HOURS=24
SESSION_TTL=2592000
```

### 12.2 Railway Deployment

**Archivos de configuración:**

- `railway.json` - Configuración Railway
- `Procfile` - Comando de inicio
- `nixpacks.toml` - Build configuration
- `Aptfile` - Dependencias del sistema

**Comando de inicio:**

```bash
python startup.py
```

---

## 13. MÉTRICAS Y ESTADÍSTICAS

El panel incluye un dashboard de métricas con:

- **Total de conversaciones** por período
- **Mensajes enviados/recibidos**
- **Tiempo de respuesta promedio**
- **Conversiones por etapa del pipeline**
- **Distribución por canal de origen**
- **Actividad por asesor**

Exportación disponible en JSON y Excel.

---

## 14. ESTADÍSTICAS DEL CÓDIGO

| Componente                              | Líneas de Código |
| --------------------------------------- | ---------------- |
| `middleware/outbound_panel.py`          | ~4,340           |
| `middleware/webhook_handler.py`         | ~1,900           |
| `middleware/PanelAsesores/index.js`     | ~3,110           |
| `database/mongodb_client.py`            | ~930             |
| `integrations/hubspot/lead_assigner.py` | ~660             |
| `agents/orchestrator.py`                | ~500             |
| **Total estimado**                      | **~15,000+**     |

---

## 15. PRÓXIMOS PASOS SUGERIDOS

1. **Monitoreo**: Implementar alertas de leads huérfanos
2. **Escalabilidad**: Considerar workers para procesamiento async
3. **Testing**: Aumentar cobertura de tests automatizados
4. **Documentación**: Swagger/OpenAPI para endpoints

---

**Documento generado:** 3 de marzo de 2026  
**Autor:** GitHub Copilot (Claude Opus 4.5)  
**Proyecto:** AgenteConversacional_Practica v2.0
