# CLAUDE.md — Proyecto SofIA (Inmobiliaria Proteger)

> **Cerebro estático del repositorio.** Este archivo se carga automáticamente en cada sesión de Claude CLI. Lee `ESTADO_ACTUAL_PROYECTO.md` antes de hacer cambios profundos de arquitectura.

---

## Stack Técnico

- **Backend:** FastAPI (Python 3.12) + Gunicorn (1 worker, Railway Pro)
- **LLM:** OpenAI GPT-4o-mini + text-embedding-3-small
- **Estado:** Redis (redis.asyncio) — estados conversacionales + cache
- **Historial:** MongoDB (Motor async) — historial real-time del panel
- **Vectores:** PgVector (PostgreSQL) — knowledge base RAG
- **CRM:** HubSpot API v3 (sin Deals, contactos + timeline)
- **Mensajería:** Twilio WhatsApp (webhook + Conversations API migration)
- **Media:** Bunny.net Storage CDN
- **Orquestación IA:** LangChain + APScheduler (AsyncIO)

---

## Reglas Estrictas — Leer Antes de Tocar Código

### 1. Singletons y Memory Watchdog (CRÍTICO)
Antes de cualquier cambio, verificar impacto en los singletons del sistema:
```
_state_manager_singleton  → outbound_panel.py:334
_redis_pool               → outbound_panel.py:97  (_get_redis_client)
_httpx_client             → outbound_panel.py:100 (get_httpx_client)
hubspot_client            → integrations/__init__.py
```
El servidor corre con **memory watchdog activo en Railway**. Crear conexiones fuera de los singletons produce memory leaks que disparan reinicios de worker. **Nunca instanciar Redis/httpx/HubSpot fuera de estos singletons.**

### 2. Seguridad — PII y API Keys (CRÍTICO)
El sistema maneja datos sensibles de clientes (teléfonos, nombres, emails).
- Revisar **inyecciones de parámetros** y **sanitización de inputs** antes de cualquier deploy.
- Validar `ADMIN_API_KEY` en `webhook_handler.py` y `outbound_panel.py`.
- El módulo `utils/pii_validator.py` es la fuente de verdad para extracción/validación de PII.
- Nunca loggear teléfonos completos ni nombres de clientes en producción.

### 3. Archivos de Alto Riesgo
| Archivo | Riesgo | Motivo |
|---------|--------|--------|
| `middleware/outbound_panel.py` | 🔴 EXTREMO | 8,238 líneas, 16+ endpoints, singletons |
| `middleware/webhook_handler.py` | 🔴 EXTREMO | Punto de entrada Twilio, ADMIN_API_KEY |
| `middleware/sofia_brain.py` | 🟠 ALTO | Motor IA con memoria Redis L1 |
| `agents/CRMAgent/crm_agent.py` | 🟠 ALTO | Sincronización HubSpot, 8k+ LOC |
| `middleware/conversation_state.py` | 🟠 ALTO | Modelos Redis — cambios rompen estado live |

### 4. Redis Keys — No Renombrar sin Migración
```
conv_state:{phone}:{canal}       → BOT_ACTIVE | HUMAN_ACTIVE | IN_CONVERSATION
conv_meta:{phone}:{canal}        → JSON contacto
active_conversations_sorted      → ZSET inbox panel
last_client_msg:{phone}          → ventana 24h
phone_cache:{phone}              → contact_id HubSpot
```
Cambiar nombres de keys sin un script de migración borra el estado de todas las conversaciones activas.

### 5. Twilio — Feature Flag Activo
`TWILIO_CONVERSATIONS_ENABLED` controla la migración a Conversations API.
- Flag en `sofia_brain.py` y `webhook_handler.py`.
- No modificar el flujo de mensajería sin verificar el flag primero.

---

## Flujo Central (Resumen)

```
Cliente WhatsApp → Twilio POST /whatsapp/webhook
    → webhook_handler.py
        [BOT_ACTIVE]   → SofiaBrain → Orchestrator → Agents (Reception/Info/CRM)
        [HUMAN_ACTIVE] → outbound_panel.py → WebSocket → Panel Asesoras
    → HubSpot CRM (contactos, timeline)
    → MongoDB (historial)
    → Redis (estado)
```

---

## Reglas de Desarrollo

1. **Contexto maestro:** Leer `ESTADO_ACTUAL_PROYECTO.md` antes de refactors o cambios de arquitectura.
2. **Tests:** Suite en `tests/` — panel tiene 51/51 PASS. Correr antes de merge.
3. **Documentación:** Al terminar un cambio relevante, actualizar `ESTADO_ACTUAL_PROYECTO.md`.
4. **Agentes IA:** Los prompts viven en `prompts/`. Cambiar prompts sin actualizar el estado del proyecto puede causar regresiones silenciosas.
5. **RAG:** La knowledge base en `knowledge_base/*.txt` se indexa en startup. Cambios en chunking (500 tokens, 100 overlap) requieren re-indexación completa.
6. **Schedulers:** APScheduler corre sobre el mismo worker de Gunicorn. Jobs pesados deben ser async y cortos para evitar bloqueos.

---

## SRE — Monitoreo con Railway CLI

```bash
# Ver logs en tiempo real
railway logs --tail

# Ver últimas 200 líneas
railway logs --lines 200

# Filtrar errores críticos
railway logs --lines 200 | grep -E "(ERROR|CRITICAL|Exception|memory|MongoDB|Redis)"

# Ver variables de entorno activas
railway variables

# Estado del deploy actual
railway status
```

**Anomalías críticas a buscar en logs:**
- `MongoServerSelectionTimeoutError` → caída MongoDB
- `ConnectionError: Redis` → pérdida estado conversacional (conversaciones se resetean)
- `memory watchdog` + `SIGTERM` → reinicio worker (revisar singletons)
- `422 Unprocessable` en `/whatsapp/webhook` → cambio en schema Twilio
- `HubSpot 429` → rate limit CRM (tenacity retry activo, pero revisar volumen)
- `OpenAI APIError` → LLM caído, SofIA no responderá

---

## Variables de Entorno Requeridas (Railway)

```
OPENAI_API_KEY
HUBSPOT_ACCESS_TOKEN
TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN
TWILIO_PHONE_NUMBER
MONGODB_URI
REDIS_URL
PGVECTOR_DATABASE_URL
BUNNY_STORAGE_API_KEY
ADMIN_API_KEY
TWILIO_CONVERSATIONS_ENABLED   # feature flag migración
```
