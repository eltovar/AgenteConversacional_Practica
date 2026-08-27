# D-04 · Inventario de capacidades actuales

> **Estado:** v1 — levantado el 2026-08-20 directamente del código y de la base de producción.
> **Para qué sirve:** que desarrollo sepa **qué ya existe** antes de reconstruir. Buena parte del rediseño es reorganizar, no crear.

---

## 1. Tamaño del sistema

| | Valor |
|---|---|
| Líneas de Python | **31.579** |
| Endpoints del panel | **72** |
| Trabajos programados (APScheduler) | **15** |
| Colecciones en MongoDB | 9 |
| Mensajes almacenados | 56.377 |
| Conversaciones | 3.145 |

### Módulos por tamaño

| Archivo | Líneas | Riesgo |
|---|---|---|
| `middleware/outbound_panel.py` | **10.222** | 🔴 Extremo — 72 endpoints y los singletons |
| `middleware/webhook_handler.py` | 2.561 | 🔴 Punto de entrada de Twilio |
| `app.py` | 2.242 | 🟠 Arranque, schedulers, reconciliación |
| `middleware/conversation_state.py` | 2.055 | 🟠 Estado en Redis |
| `integrations/hubspot/timeline_logger.py` | 1.215 | 🟡 |
| `utils/media_processor.py` | 1.200 | 🟡 |
| `utils/twilio_client.py` | 1.000 | 🟡 |
| `middleware/contact_manager.py` | 962 | 🟠 |
| `agents/CRMAgent/crm_agent.py` | 887 | 🟠 |
| `middleware/appointment_manager.py` | 793 | 🟡 |
| `middleware/sofia_brain.py` | 650 | 🟠 Motor de IA |

> 🔴 **`outbound_panel.py` concentra un tercio del sistema.** Es el archivo que el rediseño tiene que descomponer.

---

## 2. Capacidades que YA existen

### 2.1 Mensajería

| Capacidad | Dónde vive |
|---|---|
| Recepción por webhook de Twilio | `webhook_handler.py` |
| Envío de texto libre | `POST /send-message` · `/send-message-json` |
| Envío de plantillas aprobadas | `POST /send-template` |
| Gestión de plantillas | `GET/POST/DELETE /templates` |
| Audio, imágenes y documentos | `media_processor.py` + Bunny CDN |
| Agregación de mensajes seguidos | `message_aggregator.py` |
| Citar y responder mensajes | `reply_quote_formatter.py` |
| **Estado de la ventana de 24 h** | `GET /window-status/{phone}` |
| Reintentos ante límite de Twilio | Backoff 2/4/8 s |

### 2.2 Inteligencia artificial

| Capacidad | Dónde vive |
|---|---|
| Respuesta + análisis en **una sola llamada** | `sofia_brain.py` — Single-Stream |
| Base de conocimiento con búsqueda semántica | PgVector + `knowledge_base/` |
| Memoria conversacional con recorte | `RedisChatMessageHistory` |
| Detección de portal por enlace | `link_detector.py` |
| Detección de código de inmueble | `property_code_detector.py` |
| Extracción y validación de PII | `pii_validator.py` |
| Interpretación de fechas en lenguaje natural | `date_parser.py` |
| Horario comercial | `business_hours.py` |
| Escalamiento a humano | 4 disparadores en `webhook_handler.py` |

### 2.3 Panel de asesoras

| Capacidad | Endpoint |
|---|---|
| Lista de contactos | `GET /contacts` |
| Búsqueda | `GET /contacts/search` |
| Detalle e hidratación | `GET /contacts/{phone}/detail` · `/hydrate` |
| Hilo de conversación | `GET /conversations/{phone}` |
| Tomar control del bot | `POST /contacts/{phone}/take-control` |
| Devolver al bot | `POST /reset-bot/{phone}` |
| Marcar como leído | `POST /contacts/{phone}/mark-read` |
| Notificaciones | `GET /notifications` · `/{id}/read` · `/read-all` |
| Tiempo real | `websocket_manager.py` |
| Cerrar contacto | `DELETE /contacts/{phone}/close` |

### 2.4 Contactos y CRM

| Capacidad | Endpoint |
|---|---|
| Crear contacto manualmente | `POST /contacts/create` |
| Editar nombre | `PATCH /contacts/{id}/name` |
| Cambiar canal | `PATCH /contacts/{phone}/canal` |
| **Cambiar etapa** | `PATCH /contacts/{id}/stage` |
| Listar etapas disponibles | `GET /stages` |
| **Notas** — crear, editar, borrar | `/contacts/{id}/notes` |
| Historial del contacto | `GET /history/{contact_id}` |

### 2.5 Propiedad y transferencias

| Capacidad | Endpoint / módulo |
|---|---|
| Transferencia directa | `POST /contacts/{phone}/transfer` |
| Flujo con aprobación | `transfer-request` · `transfer-accept` · `transfer-reject` |
| Función centralizada | `transfer_ownership()` |
| Asignación por canal | `lead_assigner.py` + `channels_registry.py` |
| Reconciliación de propietarios | `reconcile_owner_ids()` cada 6 h |
| Sincronización manual | `POST /admin/sync-owners` · `/sync-owners-mongo` |

### 2.6 Citas

| Capacidad | Endpoint / módulo |
|---|---|
| Crear, editar, cancelar | `/contacts/{id}/appointments` |
| Trabajadores de campo | `/workers` — CRUD, 8 registros |
| Recordatorios | Job `apt_reminders` |
| Seguimiento post-cita | Jobs `apt_followups` · `apt_followup2` |
| Auto-transición a *Visita Realizada* | 1 h 30 min después |

### 2.7 Campañas masivas

| Capacidad | Endpoint |
|---|---|
| Crear campaña por embudo | `POST /bulk-campaigns` |
| Previsualizar destinatarios | `POST /bulk-campaigns/preview` |
| Consultar campaña | `GET /bulk-campaigns/{id}` · `/last/{stage_id}` |
| Procesador en cola | Job `bulk_campaign_processor` |
| Depuración del embudo | `job_depuracion_masivos.py` |

### 2.8 Métricas

6 endpoints — ver [D-02 §13-bis](02-actores-y-roles.md) para su análisis crítico.

### 2.9 Recuperación y mantenimiento

| Capacidad | Endpoint |
|---|---|
| Limpiar bandeja obsoleta | `POST /admin/cleanup-stale-inbox` |
| Recuperar conversaciones perdidas | `POST /admin/recover-lost-conversations` |
| Recuperar tras una caída | `POST /admin/recover-outage` |
| Restaurar el panel | `POST /admin/restore-panel` |
| Diagnóstico | `GET /diagnose` · `/debug/redis` · `/ws/stats` |

> ⚠️ **Cinco endpoints dedicados a recuperarse de fallos.** Es una medida indirecta de cuántas veces el estado se ha corrompido en producción.

---

## 3. Los 15 trabajos programados

| Job | Qué hace |
|---|---|
| `rebuild_zset_nightly` | Reconstruye la bandeja + recolector de basura, 3:00 AM |
| `reconcile_owner_ids` | Cruza Redis / MongoDB / HubSpot cada 6 h |
| `apt_reminders` · `apt_followups` · `apt_followup2` | Ciclo de citas |
| `advisor_24h_notifications` | Avisa de contactos sin respuesta |
| `followup_24h` | Seguimiento a las 24 h |
| `conv_timeouts` | Caducidad de conversaciones |
| `rescue_stalled_leads` | Rescata leads atascados |
| `aprobados_daily_reminder` | Recordatorio diario de aprobados |
| `bulk_campaign_processor` | Cola de campañas |
| `scheduled_messages` | Mensajes programados |
| `lead` | Procesamiento de leads |
| `memory_watchdog` | Vigila el consumo de memoria |
| `scheduler_lock_heartbeat` | Evita que corran dos schedulers a la vez |

---

## 4. Estado de la base de datos

| Colección | Documentos | Observación |
|---|---|---|
| `messages` | 56.377 | |
| `conversations` | 3.145 | Clave por `(teléfono, canal)` |
| `appointments` | 495 | |
| `contact_notes` | 164 | Las notas ya existen |
| `scheduled_messages` | 20 | |
| `bulk_campaigns` | 9 | |
| `appointment_workers` | 8 | Trabajadores de campo |
| `panel_advisors` | 4 | ⚠️ **Ver abajo** |
| `contacts` | **0** | 🔴 **Colección vacía — código muerto** |

### 🔴 Dos hallazgos

**`contacts` está vacía.** Existe la colección pero no tiene ni un documento. O nunca se usó o quedó abandonada. Hay que confirmar si algún código escribe en ella antes de eliminarla.

**`panel_advisors` es una colección muerta.** ⚠️ **Corregido el 2026-08-21** — ver [D-18 §2](18-migracion.md).

La afirmación original de este documento decía que **cuatro fuentes** definían quién es una asesora. **Era incorrecta.** El índice del grafo de código y la lectura del repositorio lo desmienten:

| Fuente supuesta | Realidad verificada |
|---|---|
| `OWNERS_CONFIG` en `lead_assigner.py` | ✅ **Se deriva** de `advisors_registry` — `lead_assigner.py:28-29` |
| `utils/advisors_registry.py` | ✅ **Es la fuente única declarada.** La usan `outbound_panel.py:48` y `app.py:706` |
| `panel_advisors` en MongoDB | 🔴 **Colección muerta — cero consumidores en Python** |
| Owners de HubSpot | Sistema externo, autoritativo |

> ✅ **La unificación de la identidad de las asesoras ya está hecha.**
> ⚠️ **Lo que sí sigue duplicado:** `channels_registry.py` mantiene `owner_id` escrito a mano por canal. La asignación **canal → owner** es lo que la pantalla *Team Members* debe sustituir — un problema mucho más pequeño.

---

## 5. Capacidades que NO existen

Contraste con lo que exigen los wireframes:

| Capacidad | ¿Existe? |
|---|---|
| **Autenticación y usuarios** | ❌ Nada |
| **Roles y permisos** | ❌ Nada |
| **Dashboard con KPIs** | ❌ Nada |
| **Kanban de embudo** | ❌ Nada |
| **Tabla de leads con columnas por rol** | ❌ Nada |
| **Pantalla de configuración** | ❌ Nada |
| **Entidad `Interés`** | ❌ Se detecta y se descarta |
| **Registro de eventos del contacto** | ❌ Nada |
| **Enlace de origen guardado** | ❌ Se detecta y se descarta |
| Historial de notas | ⚠️ Existen las notas, falta la vista agrupada por asesora |
| Alta manual de contacto | ✅ Existe |
| Cambio de etapa | ✅ Existe |
| Notificaciones | ⚠️ Existen, con tres persistencias distintas |

> **10 capacidades exigidas, 9 inexistentes.** Coincide con lo estimado en D-03.

---

## 6. Lo que se conserva íntegro

| Se conserva | Por qué |
|---|---|
| `sofia_brain.py` Single-Stream | Motor de IA — la ventaja competitiva |
| RAG con PgVector | Independiente del rediseño |
| Twilio y `twilio_client.py` | Transporte de WhatsApp |
| `media_processor.py` + Bunny | Multimedia resuelto |
| `appointment_manager.py` | Ciclo de citas completo |
| Detectores (`link`, `property_code`, `pii`, `date`) | Ya funcionan; solo hay que **guardar** lo que detectan |
| Campañas masivas | Funcionalidad completa |
| `websocket_manager.py` | Tiempo real |

> 🔑 **Los detectores son el mejor ejemplo de reorganizar en vez de crear.** `link_detector` y `property_code_detector` ya extraen la información que necesitan la entidad `Interés` y los eventos E-06 y E-07. Hoy la usan para decidir y la tiran. **Persistirla es el cambio más barato del rediseño.**

---

## 7. Deuda técnica registrada

| # | Deuda | Estado |
|---|---|---|
| 1 | Asignación **canal → owner** escrita a mano en `channels_registry.py` | 🟠 Abierta — la identidad ya está unificada en `advisors_registry` |
| 2 | `LeadAssigner` con Redis síncrono fuera de los singletons | 🔴 Abierta |
| 3 | **Dos arquitecturas de agente** conviviendo | 🔴 Abierta |
| 4 | **Dos máquinas de estado** sin coordinación | 🔴 Abierta |
| 5 | Clave de administrador **en la URL** de `/metrics` | 🔴 Abierta |
| 6 | `SOCIAL_MEDIA_CHANNELS` duplicada en `outbound_panel.py:6235` | 🟠 Abierta |
| 7 | Colección `contacts` vacía | 🟡 Nueva |
| 8 | Sin banco de evaluación de prompts | 🔴 Abierta |
| 9 | `_transfer_to_luisa` sin migrar a `transfer_ownership()` | 🟠 Abierta |
| 10 | Claves `conv_was_panel` heredadas | 🟡 Abierta |

---

## 8. Pendiente

- Confirmar si algún código escribe en `contacts` antes de eliminarla
- Inventariar el frontend (`middleware/PanelAsesores/index.js`)
- Mapear cuáles de los 72 endpoints sobreviven al rediseño y cuáles se retiran
