# Resumen Técnico de Implementaciones — Panel de Asesores v3.0

**Fecha:** 28 de Febrero de 2026
**Branch:** `FaseFinalDetalles`
**Plan de referencia:** `docs/PLAN_IMPLEMENTACIONES_PANEL_v3.md`

---

## Archivos Nuevos

| Archivo | Descripción |
|---------|-------------|
| `middleware/websocket_manager.py` | Gestor de conexiones WebSocket para notificaciones en tiempo real |
| `docs/RESUMEN_IMPLEMENTACIONES_v3.md` | Este documento |

## Archivos Modificados

| Archivo | Cambios |
|---------|---------|
| `middleware/conversation_state.py` | Bug fix + ZSET + modelo de transferencia + método `transfer_contact()` |
| `middleware/outbound_panel.py` | 3 nuevos endpoints: `/contacts/create`, `/contacts/{phone}/transfer`, `/advisors` + endpoint WebSocket |
| `middleware/PanelAsesores/index.html` | Botón "+ Nuevo", botón "Transferir", 2 modales, audio de notificación |
| `middleware/PanelAsesores/index.js` | ~500 líneas nuevas: creación manual, WebSocket client, transferencia |

## Archivos Eliminados

Ninguno. Los archivos de test ya estaban marcados para mover a `tests/` en el git status previo.

---

## 1. Bug Fix: `conversation_status` siempre devolvía `"active"`

### Problema
En `get_active_contacts()`, el campo `conversation_status` se construía con el literal hardcodeado `"active"` en lugar de usar el valor real almacenado en Redis. Esto hacía que el frontend nunca pudiera distinguir entre estados `HUMAN_ACTIVE` (badge verde "En espera") e `IN_CONVERSATION` (badge azul "En conversación").

### Causa Raíz
```python
# ANTES — conversation_state.py línea 175
"conversation_status": "active"   # siempre el mismo string
```

La variable `status` ya existía en scope (línea 151, resultado de `redis.get(state_key)`), pero no se usaba en ese campo.

### Solución
```python
# DESPUÉS
"conversation_status": status  # valor real: "HUMAN_ACTIVE" / "IN_CONVERSATION" / etc.
```

### Impacto en Frontend
El código de `index.js` ya tenía la lógica correcta:
```javascript
const isInConversation = status === 'IN_CONVERSATION';   // azul
const isHumanActive = status === 'HUMAN_ACTIVE' || status === 'PENDING_HANDOFF';  // verde
```
Con el fix, esa lógica ahora recibe el estado correcto y los badges se renderizan apropiadamente.

---

## 2. Creación Manual de Contactos

### Objetivo
Permitir a las asesoras crear contactos directamente desde el panel, sin esperar un handoff de Sofía.

### Backend — `outbound_panel.py`

Se agregó el endpoint `POST /contacts/create`. Flujo interno:

**Paso 1 — Normalización de teléfono**
```python
phone_normalized = PhoneNormalizer.normalize(phone)
```

**Paso 2 — Deduplicación en HubSpot**
Consulta `POST /crm/v3/objects/contacts/batch/read` usando `idProperty: "whatsapp_id"`. Si existe, retorna HTTP 409 con el `contact_id` existente y el mensaje `"¿Deseas tomar control?"`.

**Paso 3 — Asignación de owner (Round Robin)**
```python
from integrations.hubspot.lead_assigner import lead_assigner
owner_id = lead_assigner.get_next_owner(canal)
```
El canal de origen determina el equipo; el LeadAssigner rotula al siguiente asesor disponible de ese equipo.

**Paso 4 — Creación en HubSpot**
`POST /crm/v3/objects/contacts` con propiedades:
- `whatsapp_id` (identificador único para deduplicación futura)
- `phone`, `firstname`, `lastname`
- `hubspot_owner_id`, `canal_origen`, `lifecyclestage: "lead"`
- Opcionales: `tipo_inmueble`, `tipo_operacion`, `presupuesto`, `caracteristicas`

**Paso 5 — Creación de Deal**
`POST /crm/v3/objects/deals` con association al contacto recién creado. Pipeline Comercial (`1275156338`), etapa "Nuevo Lead" (`1275156339`).

**Paso 6 — Activar HUMAN_ACTIVE**
```python
await state_manager.activate_human(
    phone_normalized=phone_normalized,
    canal_origen=canal,
    owner_id=owner_id,
    reason="Creado manualmente desde panel",
    display_name=display_name,
    contact_id=contact_id
)
```
El contacto queda inmediatamente visible en el panel y en el índice Redis.

**Respuesta exitosa:**
```json
{
  "status": "success",
  "contact_id": "12345",
  "deal_id": "67890",
  "phone": "+573001234567",
  "display_name": "Carlos Rodriguez",
  "owner_id": "87367331"
}
```

### Frontend — `index.html`

- Botón `+ Nuevo` añadido al header del sidebar (esquina superior derecha, junto al badge "En vivo")
- Modal `#createContactModal` con campos:
  - **Nombre** (obligatorio), **Apellido** (opcional)
  - **Teléfono** (obligatorio, sin código de país)
  - **Tipo de inmueble** (select: apartamento, casa, local, oficina, bodega, lote, finca)
  - **Tipo de operación** (select: compra, arriendo, venta)
  - **Presupuesto** (text)
  - **Notas adicionales** (textarea)

### Frontend — `index.js`

Funciones añadidas:

| Función | Descripción |
|---------|-------------|
| `openCreateContactModal()` | Limpia el form y muestra el modal |
| `closeCreateContactModal()` | Oculta el modal |
| `createManualContact(event)` | Valida campos, hace `POST /contacts/create`, maneja respuesta 200/409 |
| `showCreateResult(type, msg)` | Renderiza feedback (success/error/warning) dentro del modal |
| `takeControlOfExisting(id, phone)` | Llama al endpoint `take-control` existente cuando se detecta duplicado |

Comportamiento en éxito: el modal se cierra a los 1.5s, se recarga la lista y se abre automáticamente el chat del nuevo contacto.

---

## 3. Reordenamiento Automático + WebSockets

### 3.1 Migración SET → ZSET

#### Problema anterior
El índice de contactos activos en Redis era un `SET` (`active_conversations_index`), que no garantiza ningún orden. Los contactos aparecían en orden arbitrario independientemente de cuándo había llegado el último mensaje.

#### Solución — Sorted Set (ZSET)
Se creó un nuevo índice `active_conversations_sorted` (ZSET) donde el **score es el Unix timestamp** de la última actividad. `ZREVRANGE` retorna los miembros ordenados de mayor a menor score (más reciente primero).

**Cambios en `conversation_state.py`:**

```python
ACTIVE_CONTACTS_SET  = "active_conversations_index"    # Legacy, se mantiene por compatibilidad
ACTIVE_CONTACTS_ZSET = "active_conversations_sorted"   # Nuevo ZSET ordenado
```

| Operación | Antes (SET) | Después (ZSET) |
|-----------|-------------|----------------|
| Agregar contacto | `SADD key member` | `ZADD key score member` |
| Leer ordenado | `SMEMBERS key` (sin orden) | `ZREVRANGE key 0 -1` (desc por score) |
| Remover contacto | `SREM key member` | `ZREM key member` |
| Actualizar posición | No posible | `ZADD key nuevo_score member` |

**Métodos actualizados:**
- `get_active_contacts()` — Usa `zrevrange`. Incluye migración automática: si el ZSET está vacío pero el SET legacy tiene datos, los migra en tiempo real con timestamp actual y continúa.
- `activate_human()` — Llama `zadd` con `score = get_bogota_now().timestamp()`
- `request_handoff()` — Ídem
- `activate_bot()` — Llama `zrem` para remover del ZSET
- `update_activity()` — **Clave para el reordenamiento:** actualiza el score en el ZSET cada vez que hay actividad. Esto sube el contacto al tope de la lista automáticamente.
- `cleanup_duplicate_states()` — Usa `zrange` como fuente principal, fallback a `smembers`

El SET legacy se mantiene sincronizado en paralelo para garantizar compatibilidad con cualquier código externo que aún lo lea.

### 3.2 WebSocket Manager

**Archivo nuevo:** `middleware/websocket_manager.py`

Implementa la clase `ConnectionManager` con:

**Estructura de datos interna:**
```python
active_connections: Dict[str, List[WebSocket]]  # advisor_id -> conexiones
phone_to_advisor: Dict[str, str]                # phone -> advisor_id
all_connections: Set[WebSocket]                  # todas las conexiones
```

**Métodos principales:**

| Método | Descripción |
|--------|-------------|
| `connect(ws, advisor_id)` | Acepta la conexión y la registra |
| `disconnect(ws, advisor_id)` | Remueve la conexión y limpia entradas vacías |
| `send_to_advisor(advisor_id, msg)` | Envía a todas las conexiones de un asesor específico; limpia las conexiones caídas |
| `notify_new_message(phone, canal, ...)` | Notifica al asesor asignado (o broadcast si no se conoce) |
| `notify_contact_transferred(...)` | Notifica al asesor origen ("salió") y al destino ("llegó") |
| `notify_status_change(...)` | Notifica cambios de estado |
| `broadcast(msg)` | Envía a todas las conexiones activas |
| `get_stats()` | Estadísticas de conexiones para monitoreo |

Se exporta la instancia singleton global `ws_manager` para ser usada desde cualquier endpoint.

### 3.3 Endpoint WebSocket — `outbound_panel.py`

```python
@router.websocket("/ws/{advisor_id}")
async def websocket_endpoint(websocket: WebSocket, advisor_id: str)
```

- Acepta la conexión vía `ws_manager.connect()`
- Loop de recepción maneja:
  - `{"type": "ping"}` → responde con `{"type": "pong"}`
  - `{"type": "watching", "phone": "..."}` → registra en `phone_to_advisor` para notificaciones dirigidas
- Maneja `WebSocketDisconnect` y errores desregistrando la conexión

Endpoint adicional:
```
GET /ws/stats  →  {"total_connections": N, "advisors_connected": N, ...}
```

### 3.4 Cliente WebSocket — `index.js`

**Variables de estado:**
```javascript
let ws = null;
let wsReconnectAttempts = 0;
const WS_MAX_RECONNECT_ATTEMPTS = 5;
const WS_RECONNECT_DELAY = 3000;
```

**Funciones añadidas:**

| Función | Descripción |
|---------|-------------|
| `connectWebSocket()` | Construye la URL (`ws://` o `wss://` según protocolo), conecta, define handlers |
| `handleWebSocketMessage(data)` | Router de mensajes: despacha por `data.type` |
| `handleNewMessageNotification(data)` | Reproduce sonido + notificación push + recarga lista/chat |
| `handleContactTransferred(data)` | Notificación push + recarga lista |
| `handleStatusChange(data)` | Recarga lista para actualizar badges |
| `playNotificationSound()` | Reproduce `#notificationSound` con `volume = 0.5` |
| `showBrowserNotification(title, body)` | Usa la Notifications API; pide permiso si no se ha concedido |
| `sendWebSocketPing()` | Envía `{"type": "ping"}` cada 30 segundos para keepalive |

**Reconexión automática:** `ws.onclose` lanza un `setTimeout(connectWebSocket, 3000)` hasta 5 intentos. Tras el 5to intento abandona y el polling sigue funcionando como fallback.

**Audio de notificación — `index.html`:**
```html
<audio id="notificationSound" preload="auto">
  <source src="https://assets.mixkit.co/..." type="audio/mpeg">
</audio>
```

---

## 4. Transferencia de Contactos

### Objetivo
Permitir que una asesora transfiera un contacto a otra, en dos modos:
- **Exclusive:** El contacto pasa completamente a la nueva asesora. Se actualiza `hubspot_owner_id` en HubSpot.
- **Collaborative:** Ambas asesoras ven el contacto. El owner principal cambia, pero el anterior queda en `assigned_owner_ids`. HubSpot no se modifica en este modo (solo Redis).

### 4.1 Modelo de Datos — `conversation_state.py`

Se extendió `ConversationMeta` con tres campos nuevos:

```python
@dataclass
class ConversationMeta:
    ...
    # Nuevos campos para transferencia
    assigned_owner_ids: Optional[List[str]] = None   # Lista de owners colaboradores
    primary_owner_id: Optional[str] = None           # Owner principal (sincronizado con HubSpot)
    transfer_history: Optional[List[dict]] = None    # Historial de transferencias
```

Formato de cada entrada en `transfer_history`:
```json
{
  "from": "87367331",
  "to": "88251457",
  "mode": "exclusive",
  "reason": "Cliente prefiere atención en inglés",
  "timestamp": "2026-02-28T10:30:00-05:00"
}
```

Los nuevos campos son `Optional` y tienen default `None`, lo que garantiza compatibilidad con metadata existente en Redis (el filtrado por `valid_fields` en `get_meta()` los acepta sin romper nada).

### 4.2 Método `transfer_contact()` — `conversation_state.py`

Lógica pura de Redis sin tocar HubSpot (esa responsabilidad queda en el endpoint):

```python
async def transfer_contact(phone, to_owner_id, from_owner_id=None,
                            canal="whatsapp", mode="exclusive", reason=None) -> dict
```

1. Lee la metadata actual del contacto en Redis
2. Construye el registro de transferencia y lo agrega a `transfer_history`
3. Según el modo:
   - **exclusive:** Sobreescribe `assigned_owner_id`, `primary_owner_id` y `assigned_owner_ids = [to_owner_id]`
   - **collaborative:** Agrega `to_owner_id` a la lista `assigned_owner_ids`; actualiza `primary_owner_id` al nuevo
4. Persiste la metadata actualizada con el mismo TTL
5. Retorna `{status, from_owner, to_owner, mode, phone, transfer_history}`

### 4.3 Endpoints — `outbound_panel.py`

**`POST /contacts/{phone}/transfer`**

Parámetros (Form):
- `to_owner_id` (obligatorio)
- `mode` — `"exclusive"` (default) o `"collaborative"`
- `reason` (opcional)
- `contact_id` — ID de HubSpot para actualizar owner
- `canal` — canal de la conversación

Flujo:
1. Normaliza el teléfono
2. Valida el modo
3. Llama a `state_manager.transfer_contact()` → actualiza Redis
4. Si `mode == "exclusive"` y hay `contact_id`: hace `PATCH /crm/v3/objects/contacts/{id}` en HubSpot con `hubspot_owner_id = to_owner_id`
5. Notifica vía `ws_manager.notify_contact_transferred()` a ambos asesores en tiempo real
6. Retorna resultado con `hubspot_updated: bool`

**`GET /advisors`**

Lee `LeadAssigner.OWNERS_CONFIG`, filtra por `active: true`, elimina duplicados por ID y retorna la lista plana de asesores disponibles para el selector del modal.

### 4.4 UI — `index.html`

**Botón de transferencia** en el header del chat (junto al botón rojo "Cerrar"):
```html
<button id="transferContactBtn" onclick="openTransferModal()" class="hidden ...">
  ⇄ Transferir
</button>
```
Aparece (`classList.remove('hidden')`) cuando se selecciona un contacto y se oculta al cerrar la conversación.

**Modal `#transferContactModal`:**
- Panel de info del contacto actual (nombre + teléfono, solo lectura)
- Campos ocultos: `phone`, `contact_id`, `canal`
- Select `#transferToOwner` — poblado dinámicamente desde `GET /advisors`
- Radio buttons para modo (exclusivo/colaborativo) con descripción
- Input de motivo (opcional)
- Área de resultado con colores por tipo

### 4.5 Funciones JS — `index.js`

| Función | Descripción |
|---------|-------------|
| `openTransferModal()` | Rellena info del contacto actual, muestra modal, llama a `loadAdvisorsList()` |
| `closeTransferModal()` | Oculta el modal |
| `loadAdvisorsList()` | `GET /advisors` → puebla `#transferToOwner`; cachea en `advisorsList[]` |
| `transferContact(event)` | `POST /contacts/{phone}/transfer` → maneja éxito (cierra modal + recarga lista) |
| `showTransferResult(type, msg)` | Feedback dentro del modal con colores (success/error/warning) |

Variable de estado añadida: `let advisorsList = []` para cache de asesores.

---

## Resultado Final

### Flujos Habilitados

```
ANTES:
  Asesora abre panel → Ve contactos en orden arbitrario → No sabe si están activos →
  No puede crear contactos propios → No puede transferir → Solo polling cada 3-10s

DESPUÉS:
  Asesora abre panel:
  ├── WebSocket conectado → Notificaciones instantáneas con sonido
  ├── Lista ordenada por actividad reciente (más nuevo arriba)
  ├── Badges correctos: verde "En espera" / azul "En conversación"
  ├── Botón "+ Nuevo" → Crea contacto con deduplicación + deal + HUMAN_ACTIVE
  └── Botón "Transferir" → Modo exclusive (HubSpot sync) o collaborative
```

### Resumen de Líneas de Código

| Archivo | Líneas añadidas aprox. |
|---------|----------------------|
| `conversation_state.py` | +95 |
| `outbound_panel.py` | +210 |
| `websocket_manager.py` | +230 (nuevo) |
| `index.html` | +95 |
| `index.js` | +480 |
| **Total** | **~1110 líneas** |

### Compatibilidad y Riesgos Mitigados

| Riesgo | Mitigación implementada |
|--------|------------------------|
| Datos legacy en SET de Redis | Migración automática en `get_active_contacts()`: si ZSET vacío, lee SET y migra al vuelo |
| Duplicados en HubSpot | `search_contact_by_phone()` antes de crear; retorna 409 con opción "tomar control" |
| WebSocket caído | Reconexión automática hasta 5 intentos; polling sigue funcionando como fallback |
| Campos nuevos en `ConversationMeta` rompen metadata antigua | Todos los campos nuevos son `Optional[...] = None`; el filtro `valid_fields` en `get_meta()` los ignora si no existen |
| HubSpot solo acepta 1 owner | Solo se sincroniza el `primary_owner_id` a HubSpot; los colaboradores viven exclusivamente en Redis |
