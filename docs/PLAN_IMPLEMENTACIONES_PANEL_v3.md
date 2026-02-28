# PLAN DE IMPLEMENTACIONES - Panel de Asesores v3.0

## Fecha: 27 de Febrero de 2026

## Autor: Arquitecto de Software Senior

---

# CONTEXTO DEL SISTEMA

## 🏢 Descripción General

**Inmobiliaria Proteger** opera un sistema de atención al cliente multicanal basado en un **chatbot conversacional inteligente llamado "Sofía"**. Este sistema integra:

- **WhatsApp Business API** (vía Twilio) como canal principal de comunicación
- **HubSpot CRM** para gestión de contactos, deals y pipeline de ventas
- **Panel de Asesores** (interfaz web) para que asesoras humanas atiendan leads transferidos por el bot
- **Sistema de agentes IA** (ReceptionAgent, InfoAgent, CRMAgent) que procesan conversaciones de forma inteligente

### Flujo Principal del Sistema

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Cliente   │────▶│   WhatsApp  │────▶│   Sofía     │────▶│   HubSpot   │
│  (WhatsApp) │     │   (Twilio)  │     │   (Bot IA)  │     │    (CRM)    │
└─────────────┘     └─────────────┘     └──────┬──────┘     └─────────────┘
                                               │
                                   ┌───────────▼───────────┐
                                   │  ¿Necesita humano?    │
                                   │  (handoff automático) │
                                   └───────────┬───────────┘
                                               │ Sí
                                   ┌───────────▼───────────┐
                                   │   Panel de Asesores   │
                                   │   (Atención humana)   │
                                   └───────────────────────┘
```

### Arquitectura de Componentes

| Componente             | Tecnología         | Función                                                        |
| ---------------------- | ------------------ | -------------------------------------------------------------- |
| Backend API            | FastAPI (Python)   | Endpoints REST, webhooks, lógica de negocio                    |
| Estado de Conversación | Redis              | Gestión de estados (BOT_ACTIVE, HUMAN_ACTIVE, IN_CONVERSATION) |
| Historial de Mensajes  | MongoDB            | Persistencia de conversaciones                                 |
| CRM                    | HubSpot API v3     | Contactos, deals, pipeline, owners                             |
| Almacenamiento Media   | Bunny.net CDN      | Audios e imágenes enviados por asesoras                        |
| Panel Frontend         | HTML/JS + Tailwind | Interfaz para asesoras comerciales                             |

### Estados de Conversación

```
BOT_ACTIVE ──────▶ HUMAN_ACTIVE ──────▶ IN_CONVERSATION
   │                (En espera)         (Asesora chateando)
   │                     │                     │
   │                     │                     │
   └─────────────────────┴─────────────────────┘
                    Bot reactivado
```

---

# OBJETIVO DEL PLAN

## 🎯 Meta Principal

Transformar el **Panel de Asesores** de una herramienta básica de visualización a una **plataforma completa de gestión de leads** que permita a las asesoras comerciales:

1. **Crear contactos manualmente** (no depender solo de leads generados por Sofía)
2. **Transferir contactos** entre asesoras para colaboración o reasignación
3. **Recibir notificaciones en tiempo real** cuando un cliente envía mensaje
4. **Identificar visualmente** el estado de cada lead (en espera vs siendo atendido)

## 📋 Requerimientos de Negocio

| #   | Requerimiento                                                                        | Solicitado por | Impacto |
| --- | ------------------------------------------------------------------------------------ | -------------- | ------- |
| 1   | "Quiero poder crear contactos desde el panel sin esperar a que lleguen por WhatsApp" | Asesoras       | Alto    |
| 2   | "Necesito transferir un lead a otra asesora cuando estoy ocupada"                    | Asesoras       | Medio   |
| 3   | "Los leads con mensajes nuevos deberían aparecer arriba con una notificación"        | Asesoras       | Alto    |
| 4   | "Quiero diferenciar visualmente quién ya fue atendido y quién está esperando"        | Asesoras       | Alto    |

## 🔧 Alcance Técnico

Este plan cubre:

- ✅ Nuevos endpoints REST en el backend
- ✅ Modificaciones a la estructura de datos en Redis
- ✅ Cambios en el frontend del panel (HTML/JS)
- ✅ Integración con HubSpot para sincronización de datos
- ✅ Implementación de WebSockets para tiempo real

Este plan **NO** cubre:

- ❌ Cambios en la lógica del chatbot Sofía
- ❌ Modificaciones al flujo de agentes IA
- ❌ Cambios en el webhook de Twilio
- ❌ Migraciones de base de datos MongoDB

---

# RESUMEN EJECUTIVO

| #   | Implementación                                              | Estado Actual             | Complejidad | Prioridad | Riesgo |
| --- | ----------------------------------------------------------- | ------------------------- | ----------- | --------- | ------ |
| 1   | Creación manual de contactos                                | No existe                 | Media-Alta  | Alta      | Medio  |
| 2   | Transferencia de contactos                                  | No existe                 | Alta        | Media     | Alto   |
| 3   | Reordenamiento automático + notificaciones                  | No existe (solo polling)  | Alta        | Alta      | Medio  |
| 4   | Búsqueda por palabras clave                                 | ✅ YA EXISTE              | N/A         | N/A       | N/A    |
| 🐛  | Bug: Diferenciación visual "En espera" vs "En conversación" | Parcialmente implementado | Baja        | Alta      | Bajo   |

---

# ANÁLISIS DETALLADO DEL CÓDIGO EXISTENTE

## 📁 Archivos Clave del Sistema

```
middleware/
├── outbound_panel.py          # Backend del panel (3208 líneas)
├── conversation_state.py      # Gestión de estados Redis (533 líneas)
├── contact_manager.py         # Gestión de contactos HubSpot (552 líneas)
├── PanelAsesores/
│   ├── index.html             # Frontend (284 líneas)
│   ├── index.js               # JavaScript del panel (1989 líneas)
│   └── style.css              # Estilos

integrations/hubspot/
├── hubspot_client.py          # Cliente HTTP HubSpot (367 líneas)
├── lead_assigner.py           # Asignación round-robin (650 líneas)
```

---

# 🐛 BUG: DIFERENCIACIÓN VISUAL "EN ESPERA" vs "EN CONVERSACIÓN"

## Estado Actual del Código

### Backend: conversation_state.py (Línea 71-76)

```python
class ConversationStatus(str, Enum):
    BOT_ACTIVE = "BOT_ACTIVE"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"       # ✅ Existe
    IN_CONVERSATION = "IN_CONVERSATION" # ✅ Existe
    PENDING_HANDOFF = "PENDING_HANDOFF"
```

### Backend: outbound_panel.py (Línea 505-510)

```python
if current_status in [ConversationStatus.HUMAN_ACTIVE, ConversationStatus.PENDING_HANDOFF]:
    # Ya está en espera, cambiar a IN_CONVERSATION (asesora está atendiendo)
    await state_manager.set_status(
        phone_normalized,
        ConversationStatus.IN_CONVERSATION,  # ✅ Se actualiza correctamente
        ...
    )
```

### Frontend: index.js (Línea 617-648)

```javascript
const isInConversation = status === "IN_CONVERSATION";
const isHumanActive = status === "HUMAN_ACTIVE" || status === "PENDING_HANDOFF";

// Determinar colores según estado
if (isInConversation) {
  bgClass = "bg-blue-50 border-l-4 border-blue-500";
  avatarClass = "bg-blue-500";
} else if (isHumanActive || isActive) {
  bgClass = "bg-green-50 border-l-4 border-green-500";
  avatarClass = "bg-green-500";
}
```

## 🔍 DIAGNÓSTICO DEL BUG

El código para la diferenciación visual **ESTÁ IMPLEMENTADO**, pero hay un problema en el flujo de datos:

### Problema Identificado

En `conversation_state.py`, línea 175, el campo `conversation_status` siempre se setea como "active":

```python
contacts.append({
    ...
    "conversation_status": "active"  # ← PROBLEMA: Siempre es "active", nunca "IN_CONVERSATION"
})
```

Pero en `index.js` se espera recibir el valor del estado real:

```javascript
const status = contact.conversation_status || contact.status || "";
const isInConversation = status === "IN_CONVERSATION"; // ← Nunca será true
```

### Solución Propuesta

**Archivo:** `middleware/conversation_state.py` (línea 175)

**Cambiar de:**

```python
"conversation_status": "active"  # Badge "En espera" del panel
```

**A:**

```python
"conversation_status": status  # Usar el estado real de Redis
```

### Verificación

1. Abrir panel con un contacto en HUMAN_ACTIVE → Debe mostrar badge verde "En espera"
2. Enviar mensaje desde el panel → Estado cambia a IN_CONVERSATION
3. Refrescar panel → Debe mostrar badge azul "En conversación"

### Riesgo: BAJO

- Cambio mínimo (1 línea)
- No afecta otros módulos

---

# 📋 IMPLEMENTACIÓN 1: CREACIÓN MANUAL DE CONTACTOS

## Análisis del Código Existente

### ✅ Lógica de deduplicación YA EXISTE:

**Archivo:** `integrations/hubspot/hubspot_client.py` (línea 182-197)

```python
async def search_contact_by_phone(self, phone: str) -> Optional[str]:
    """Busca ID de contacto usando whatsapp_id como identificador único."""
    endpoint = "/crm/v3/objects/contacts/batch/read"
    payload = {
        "properties": ["id", "firstname"],
        "idProperty": "whatsapp_id",
        "inputs": [{"id": phone}]
    }
    ...
```

### ✅ Creación de contacto YA EXISTE:

**Archivo:** `integrations/hubspot/hubspot_client.py` (línea 229-241)

```python
async def create_contact(self, properties: Dict[str, Any]) -> str:
    """Crea un nuevo contacto en HubSpot."""
    endpoint = "/crm/v3/objects/contacts"
    validated_props, _ = HubSpotPropertyValidator.validate_and_filter(properties)
    response = await self._request("POST", endpoint, {"properties": validated_props})
    contact_id = response["id"]
    logger.info(f"[HubSpotClient] Contacto creado: {contact_id}")
    return contact_id
```

### ✅ Asignación de owner YA EXISTE:

**Archivo:** `integrations/hubspot/lead_assigner.py` (línea 145-end)

```python
def get_next_owner(self, canal_origen: str = "default") -> Dict[str, Any]:
    """Retorna el siguiente owner disponible por Round Robin."""
    ...
```

## Lo que FALTA implementar

### 1. Nuevo endpoint en outbound_panel.py

```python
@router.post("/contacts/create")
async def create_manual_contact(
    firstname: str = Form(...),
    phone: str = Form(...),
    property_type: Optional[str] = Form(None),
    operation_type: Optional[str] = Form(None),
    budget: Optional[str] = Form(None),
    characteristics: Optional[str] = Form(None),
    canal: str = Form("whatsapp_directo"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Crea un contacto manualmente desde el panel de asesores.

    Flujo:
    1. Normalizar teléfono
    2. Verificar si existe (deduplicación)
    3. Si existe → Retornar error con opción de tomar control
    4. Si no existe → Crear contacto + deal en HubSpot
    5. Activar HUMAN_ACTIVE
    6. Agregar al índice del panel
    """
```

### 2. UI en index.html

Agregar botón "+ Nuevo Contacto" y modal con formulario:

- Nombre (obligatorio)
- Teléfono (obligatorio)
- Tipo de inmueble (opcional)
- Tipo de operación (opcional)
- Presupuesto (opcional)
- Características (opcional)

### 3. JavaScript en index.js

```javascript
async function createManualContact(formData) {
  // Validar campos obligatorios
  // POST a /contacts/create
  // Mostrar resultado
  // Refrescar lista de contactos
}
```

## Ubicación de Cambios

| Archivo                               | Cambio                          | Líneas aprox. |
| ------------------------------------- | ------------------------------- | ------------- |
| `middleware/outbound_panel.py`        | Endpoint `/contacts/create`     | +80 líneas    |
| `middleware/PanelAsesores/index.html` | Modal de creación               | +50 líneas    |
| `middleware/PanelAsesores/index.js`   | Función `createManualContact()` | +60 líneas    |

## Riesgos Potenciales

| Riesgo                         | Probabilidad | Mitigación                                   |
| ------------------------------ | ------------ | -------------------------------------------- |
| Duplicados en HubSpot          | Media        | Usar `search_contact_by_phone()` existente   |
| Rate limit HubSpot             | Baja         | Ya existe retry logic en `hubspot_client.py` |
| Asignación incorrecta de owner | Baja         | Usar `LeadAssigner` existente                |

## Orden de Implementación

1. Crear endpoint backend (testear con curl)
2. Crear modal UI (sin funcionalidad)
3. Conectar frontend con backend
4. Tests E2E

---

# 📋 IMPLEMENTACIÓN 2: TRANSFERENCIA DE CONTACTOS

## Análisis del Código Existente

### Estructura actual de metadata (conversation_state.py)

```python
@dataclass
class ConversationMeta:
    phone_normalized: str
    contact_id: Optional[str] = None
    status: str = "BOT_ACTIVE"
    last_activity: str = field(default_factory=get_bogota_now_iso)
    handoff_reason: Optional[str] = None
    assigned_owner_id: Optional[str] = None  # ← SINGULAR (solo 1 owner)
    canal_origen: str = "whatsapp"
    display_name: Optional[str] = None
    ...
```

### Limitación de HubSpot

HubSpot solo permite UN `hubspot_owner_id` por contacto. Los colaboradores deben manejarse en Redis.

## Diseño Propuesto

### Nuevo modelo de metadata

```python
@dataclass
class ConversationMeta:
    ...
    assigned_owner_id: Optional[str] = None         # Owner principal (sync con HubSpot)
    assigned_owner_ids: List[str] = field(default_factory=list)  # NUEVO: Lista de owners
    primary_owner_id: Optional[str] = None          # NUEVO: Owner principal original
    transfer_history: List[Dict] = field(default_factory=list)   # NUEVO: Historial
```

### Formato de transfer_history

```json
{
  "assigned_owner_ids": ["87367331", "88251457"],
  "primary_owner_id": "87367331",
  "transfer_history": [
    {
      "from": "87367331",
      "to": "88251457",
      "timestamp": "2026-02-27T10:30:00-05:00",
      "transferred_by": "87367331",
      "mode": "collaborative" // o "exclusive"
    }
  ]
}
```

## Endpoints Nuevos

### 1. Transferencia simple

```python
@router.post("/contacts/{contact_id}/transfer")
async def transfer_contact(
    contact_id: str,
    to_owner_id: str = Form(...),
    mode: str = Form("exclusive"),  # "exclusive" o "collaborative"
    x_api_key: str = Header(...),
):
    """
    Transfiere un contacto a otro asesor.

    Modos:
    - exclusive: El contacto pasa completamente al nuevo owner
    - collaborative: Ambos owners pueden ver y atender el contacto
    """
```

### 2. Listar transferencias

```python
@router.get("/contacts/{contact_id}/transfer-history")
async def get_transfer_history(contact_id: str):
    """Retorna el historial de transferencias de un contacto."""
```

## Cambios en UI

### index.js - Vista de contacto

```html
<div class="transfer-info">
  Asignado a: <strong>Yubeny</strong>
  <span class="text-gray-400">Transferido desde: Luisa</span>
  <button onclick="openTransferModal('${contactId}')">Transferir</button>
</div>
```

## Ubicación de Cambios

| Archivo                                  | Cambio                      | Líneas aprox. |
| ---------------------------------------- | --------------------------- | ------------- |
| `middleware/conversation_state.py`       | Extender `ConversationMeta` | +20 líneas    |
| `middleware/outbound_panel.py`           | Endpoints de transferencia  | +100 líneas   |
| `middleware/PanelAsesores/index.html`    | Modal de transferencia      | +40 líneas    |
| `middleware/PanelAsesores/index.js`      | Funciones de transferencia  | +80 líneas    |
| `integrations/hubspot/hubspot_client.py` | Método `update_owner()`     | +20 líneas    |

## Riesgos Potenciales

| Riesgo                       | Probabilidad | Mitigación                                 |
| ---------------------------- | ------------ | ------------------------------------------ |
| Inconsistencia Redis-HubSpot | Alta         | Solo sync owner principal a HubSpot        |
| Conflictos de mensajes       | Media        | Mostrar badge "Colabora" en panel          |
| Pérdida de historial         | Baja         | Guardar transfer_history en Redis          |
| Complejidad en segregación   | Alta         | Modificar filtro de panel para multi-owner |

## Dependencias

- Requiere que Implementación 1 esté completa (para tener contactos que transferir)

---

# 📋 IMPLEMENTACIÓN 3: REORDENAMIENTO AUTOMÁTICO + NOTIFICACIONES

## Análisis del Código Existente

### Índice actual: SET (sin orden)

**Archivo:** `middleware/conversation_state.py` (línea 101)

```python
ACTIVE_CONTACTS_SET = "active_conversations_index"  # ← SET, no ZSET

# Añadir al índice (sin timestamp)
await self.redis.sadd(self.ACTIVE_CONTACTS_SET, index_member)

# Leer (sin orden garantizado)
members = await self.redis.smembers(self.ACTIVE_CONTACTS_SET)
```

### Polling actual: 3-10 segundos

**Archivo:** `middleware/PanelAsesores/index.js` (línea 5-6)

```javascript
const POLLING_INTERVAL_IDLE = 10000; // 10 segundos cuando no hay chat activo
const POLLING_INTERVAL_ACTIVE = 3000; // 3 segundos cuando hay chat abierto
```

## Diseño Propuesto

### Fase 1: Migrar de SET a ZSET

**Nueva estructura Redis:**

```
active_conversations_sorted (ZSET)
├── score = timestamp_unix (mayor = más reciente)
├── member = "phone:canal"
└── Orden por ZREVRANGE (más reciente primero)
```

**Comandos:**

```python
# Añadir/actualizar posición
ZADD active_conversations_sorted NOW phone:canal

# Leer ordenados (más reciente primero)
ZREVRANGE active_conversations_sorted 0 50 WITHSCORES
```

### Fase 2: Implementar WebSockets

**Nueva arquitectura:**

```
┌─────────────┐         ┌─────────────┐         ┌─────────────┐
│   Cliente   │◄────────│  WebSocket  │◄────────│   Webhook   │
│   (Panel)   │  WS     │   Manager   │         │   Handler   │
└─────────────┘         └─────────────┘         └─────────────┘
                               │
                               ▼
                        ┌─────────────┐
                        │    Redis    │
                        │    ZSET     │
                        └─────────────┘
```

## Nuevos Archivos

### 1. middleware/websocket_manager.py

```python
from fastapi import WebSocket
from typing import Dict, List
import asyncio

class WebSocketManager:
    """Gestor de conexiones WebSocket para notificaciones en tiempo real."""

    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, advisor_id: str):
        await websocket.accept()
        if advisor_id not in self.active_connections:
            self.active_connections[advisor_id] = []
        self.active_connections[advisor_id].append(websocket)

    async def disconnect(self, websocket: WebSocket, advisor_id: str):
        self.active_connections[advisor_id].remove(websocket)

    async def broadcast_to_advisor(self, advisor_id: str, message: dict):
        """Envía mensaje a todas las conexiones de un asesor."""
        for connection in self.active_connections.get(advisor_id, []):
            await connection.send_json(message)

    async def broadcast_new_message(self, phone: str, canal: str):
        """Notifica a los asesores relevantes sobre un nuevo mensaje."""
        # Determinar owner del contacto
        # Enviar a sus conexiones activas
```

### 2. Endpoint WebSocket en outbound_panel.py

```python
@router.websocket("/ws/{advisor_id}")
async def websocket_endpoint(websocket: WebSocket, advisor_id: str):
    await websocket_manager.connect(websocket, advisor_id)
    try:
        while True:
            # Mantener conexión viva
            data = await websocket.receive_text()
            # Procesar ping/pong
    except WebSocketDisconnect:
        await websocket_manager.disconnect(websocket, advisor_id)
```

### 3. Frontend: Conexión WebSocket

```javascript
// index.js
let ws = null;

function connectWebSocket() {
  ws = new WebSocket(
    `wss://${window.location.host}/whatsapp/panel/ws/${ADVISOR_ID}`,
  );

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);

    if (data.type === "new_message") {
      // Reproducir sonido
      playNotificationSound();

      // Mover contacto al inicio
      moveContactToTop(data.phone);

      // Mostrar notificación visual
      showNotificationBadge(data.phone);
    }
  };

  ws.onclose = () => {
    // Reconectar después de 5 segundos
    setTimeout(connectWebSocket, 5000);
  };
}
```

## Ubicación de Cambios

| Archivo                               | Cambio                   | Líneas aprox. |
| ------------------------------------- | ------------------------ | ------------- |
| `middleware/websocket_manager.py`     | CREAR - Gestor WebSocket | +120 líneas   |
| `middleware/conversation_state.py`    | Migrar a ZSET            | +50 líneas    |
| `middleware/outbound_panel.py`        | Endpoint WebSocket       | +30 líneas    |
| `middleware/webhook_handler.py`       | Trigger de notificación  | +10 líneas    |
| `middleware/PanelAsesores/index.js`   | Cliente WebSocket        | +80 líneas    |
| `middleware/PanelAsesores/index.html` | Audio notificación       | +5 líneas     |

## Riesgos Potenciales

| Riesgo                                 | Probabilidad | Mitigación                             |
| -------------------------------------- | ------------ | -------------------------------------- |
| Pérdida de datos en migración SET→ZSET | Media        | Script de migración con rollback       |
| WebSocket no soportado (proxies)       | Baja         | Fallback a polling                     |
| Sobrecarga de conexiones               | Media        | Limitar conexiones por asesor          |
| Latencia en Railway                    | Baja         | WebSocket es más eficiente que polling |

## Plan de Migración SET → ZSET

```python
# scripts/migrate_redis_index.py
async def migrate_active_contacts():
    """Migra active_conversations_index (SET) a active_conversations_sorted (ZSET)."""

    # 1. Leer todos los miembros del SET actual
    members = await redis.smembers("active_conversations_index")

    # 2. Para cada miembro, obtener su timestamp de last_activity
    for member in members:
        phone, canal = member.split(":", 1)
        meta = await redis.get(f"conv_meta:{phone}:{canal}")
        timestamp = meta.get("last_activity", datetime.now().isoformat())
        score = datetime.fromisoformat(timestamp).timestamp()

        # 3. Agregar al ZSET
        await redis.zadd("active_conversations_sorted", {member: score})

    # 4. Verificar migración
    # 5. Eliminar SET antiguo (después de validación)
```

---

# 📋 IMPLEMENTACIÓN 4: BÚSQUEDA POR PALABRAS CLAVE

## ✅ YA IMPLEMENTADO

### Frontend: index.html (línea 73-77)

```html
<div class="mt-2">
  <input
    type="text"
    id="contactSearch"
    placeholder="Buscar por nombre o telefono..."
    class="w-full px-3 py-2 text-sm border rounded-lg..."
    oninput="filterContacts(this.value)"
  />
</div>
```

### JavaScript: index.js (línea 44-57)

```javascript
function filterContacts(searchTerm) {
  const term = searchTerm.toLowerCase().trim();

  if (!term) {
    renderContactsList(allContacts);
    return;
  }

  const filtered = allContacts.filter((contact) => {
    const name = (contact.display_name || "").toLowerCase();
    const phone = (contact.phone || "").toLowerCase();
    return name.includes(term) || phone.includes(term);
  });

  renderContactsList(filtered);
}
```

## Mejora Sugerida (Opcional)

Extender la búsqueda para incluir:

- Email
- Razón del handoff
- Canal de origen
- Etapa del pipeline

```javascript
function filterContacts(searchTerm) {
  const term = searchTerm.toLowerCase().trim();

  if (!term) {
    renderContactsList(allContacts);
    return;
  }

  const filtered = allContacts.filter((contact) => {
    const searchableFields = [
      contact.display_name || "",
      contact.phone || "",
      contact.email || "",
      contact.handoff_reason || "",
      contact.canal_origen || "",
      PIPELINE_STAGES.find((s) => s.id === contact.current_stage)?.name || "",
    ]
      .join(" ")
      .toLowerCase();

    return searchableFields.includes(term);
  });

  renderContactsList(filtered);
}
```

---

# 📊 RESUMEN DE IMPACTO

## Archivos a Modificar

| Archivo                 | Imp. 1 | Imp. 2 | Imp. 3 | Bug Fix |
| ----------------------- | ------ | ------ | ------ | ------- |
| `conversation_state.py` | -      | ✏️     | ✏️     | ✏️      |
| `outbound_panel.py`     | ✏️     | ✏️     | ✏️     | -       |
| `index.html`            | ✏️     | ✏️     | ✏️     | -       |
| `index.js`              | ✏️     | ✏️     | ✏️     | -       |
| `hubspot_client.py`     | -      | ✏️     | -      | -       |
| `webhook_handler.py`    | -      | -      | ✏️     | -       |
| `websocket_manager.py`  | -      | -      | 🆕     | -       |

## Orden Recomendado de Implementación

```
1️⃣ [URGENTE] Bug Fix: Diferenciación visual (1 línea)
     └── Validar que los badges funcionen correctamente

2️⃣ [ALTA] Implementación 4: Búsqueda (YA EXISTE - solo mejoras opcionales)

3️⃣ [ALTA] Implementación 1: Creación manual de contactos
     └── Reutiliza toda la lógica existente de HubSpot

4️⃣ [ALTA] Implementación 3: Reordenamiento + Notificaciones
     └── Puede hacerse en fases (primero ZSET, luego WebSocket)

5️⃣ [MEDIA] Implementación 2: Transferencia de contactos
     └── Más compleja, requiere rediseño de metadata
```

## Estimación de Tiempo

| Implementación               | Tiempo Estimado |
| ---------------------------- | --------------- |
| Bug Fix                      | 30 minutos      |
| Mejora búsqueda (opcional)   | 1 hora          |
| Creación manual de contactos | 4-6 horas       |
| Reordenamiento ZSET          | 3-4 horas       |
| WebSocket                    | 4-6 horas       |
| Transferencia de contactos   | 6-8 horas       |

---

# 🔒 PRECAUCIONES ANTES DE IMPLEMENTAR

## Backup

```bash
# Antes de cualquier cambio en Redis
redis-cli BGSAVE

# Antes de cambios en código
git stash
git checkout -b feature/panel-improvements-v3
```

## Tests a No Romper

```
tests/
├── panel/test_human_active_flow.py     # Flujo HUMAN_ACTIVE
├── test_conversation_integration.py    # Estados de conversación
├── test_sender_detection.py            # Detección de mensajes
```

## Módulos que NO deben modificarse sin precaución

1. **webhook_handler.py** - Punto de entrada de todos los mensajes
2. **sofia_brain.py** - Lógica del chatbot
3. **agents/orchestrator.py** - Orquestación de agentes

---

# ✅ CHECKLIST ANTES DE COMENZAR

- [ ] Hacer commit de cambios pendientes
- [ ] Crear branch `feature/panel-improvements-v3`
- [ ] Backup de Redis (`BGSAVE`)
- [ ] Ejecutar tests existentes (`pytest tests/ -v`)
- [ ] Confirmar prioridades con usuario
- [ ] Documentar cualquier cambio de API

---

_Documento generado automáticamente para planificación de desarrollo._
_No modificar código hasta recibir autorización._
