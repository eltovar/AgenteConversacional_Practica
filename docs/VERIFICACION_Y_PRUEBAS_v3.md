# Verificación y Plan de Pruebas — Panel de Asesores v3.0

**Fecha:** 28 de Febrero de 2026  
**Relacionado:** `docs/PLAN_IMPLEMENTACIONES_PANEL_v3.md`, `docs/RESUMEN_IMPLEMENTACIONES_v3.md`

---

# PARTE 1: VERIFICACIÓN DE IMPLEMENTACIONES

## ✅ Estado de Verificación

| #   | Implementación                        | Código OK | Ubicación Verificada                                          |
| --- | ------------------------------------- | --------- | ------------------------------------------------------------- |
| 1   | Bug Fix `conversation_status`         | ✅        | `conversation_state.py:201` — `"conversation_status": status` |
| 2   | Endpoint `/contacts/create`           | ✅        | `outbound_panel.py:1190`                                      |
| 3   | Endpoint `/contacts/{phone}/transfer` | ✅        | `outbound_panel.py:1534`                                      |
| 4   | Endpoint `/advisors`                  | ✅        | `outbound_panel.py:1647`                                      |
| 5   | ZSET `active_conversations_sorted`    | ✅        | `conversation_state.py:102,158,362,405`                       |
| 6   | Método `transfer_contact()`           | ✅        | `conversation_state.py:441-522`                               |
| 7   | WebSocket Manager                     | ✅        | `websocket_manager.py` (306 líneas)                           |
| 8   | Endpoint WebSocket `/ws/{advisor_id}` | ✅        | `outbound_panel.py`                                           |
| 9   | Frontend: Modal crear contacto        | ✅        | `index.html` + `index.js:2259`                                |
| 10  | Frontend: Modal transferir            | ✅        | `index.html` + `index.js:2396`                                |
| 11  | Frontend: WebSocket client            | ✅        | `index.js:2008`                                               |

## ✅ Bugs Corregidos (Sesión 28-Feb-2026)

| #   | Bug                               | Estado | Solución                                         |
| --- | --------------------------------- | ------ | ------------------------------------------------ |
| 1   | Plantillas lentas                 | ✅     | Connection pool Redis + caché templates 60s      |
| 2   | Hora incorrecta                   | ✅     | Eliminado componente `lastUpdate`                |
| 3   | Contador "4 en espera" incorrecto | ✅     | `active_count` solo cuenta `HUMAN_ACTIVE`        |
| 4   | Hora mostrada no exacta           | ✅     | Eliminado del HTML                               |
| 5   | Transferir no funciona            | ✅     | `currentName` asignado en `selectContact()`      |
| 6   | Falta opción "otro"               | ✅     | Agregado en `property_type` y `operation_type`   |
| 7   | Búsqueda limitada                 | ✅     | Ahora busca en `canal_origen` y `handoff_reason` |

---

# PARTE 2: PLAN DE PRUEBAS EN POSTMAN

## ⚠️ IMPORTANTE: Orden de Ejecución

```
┌─────────────────────────────────────────────────────────────┐
│  1. PRIMERO: Tests en LOCAL (http://127.0.0.1:8001)         │
│     → Validar que todo funciona antes de subir              │
│                                                             │
│  2. DESPUÉS: Tests en PRODUCCIÓN (Railway)                  │
│     → Solo cuando LOCAL pase al 100%                        │
└─────────────────────────────────────────────────────────────┘
```

## Configuración Inicial

### Variables de Entorno (Postman)

Crear **dos entornos** en Postman:

**Entorno: `Panel-LOCAL`**

```json
{
  "base_url": "http://127.0.0.1:8001/whatsapp/panel",
  "api_key": "protect_admin_2024_xK9mP3qR",
  "test_phone": "+573147842399",
  "advisor_luisa": "87367331",
  "advisor_yubeny": "88251457"
}
```

**Entorno: `Panel-RAILWAY`**

```json
{
  "base_url": "https://tu-app.railway.app/whatsapp/panel",
  "api_key": "tu_admin_api_key_produccion",
  "test_phone": "+573147842399",
  "advisor_luisa": "87367331",
  "advisor_yubeny": "88251457"
}
```

### Headers Comunes (aplicar a toda la colección)

```
X-API-Key: {{api_key}}
```

---

# 🔴 TEST SUITE 0: DIAGNÓSTICO DE TRANSFERENCIA (PRIORITARIO)

> **Ejecutar PRIMERO** si la transferencia no funciona visualmente

## Test 0.1: Debug Redis — Ver metadata completa

**Propósito:** Ver el estado real en Redis del contacto

**Request:**

```http
GET {{base_url}}/debug/redis
X-API-Key: {{api_key}}
```

**Qué buscar en la respuesta:**

```json
{
  "meta_keys": [
    {
      "key": "conv_meta:+573147842399:whatsapp_directo",
      "value": "{\"assigned_owner_id\": \"87367331\", ...}",
      "ttl": 85432
    }
  ]
}
```

**Verificar:**

- ✅ `assigned_owner_id` debe ser el ID del asesor DESTINO después de transferir
- ✅ `transfer_history` debe mostrar el registro de la transferencia

---

## Test 0.2: Contactos del asesor ORIGEN (Yubeny)

**Propósito:** Verificar que el contacto YA NO aparece aquí después de transferencia exclusiva

**Request:**

```http
GET {{base_url}}/contacts?filter_time=24h&advisor=88251457
X-API-Key: {{api_key}}
```

**Esperado después de transferir a Luisa:**

- El contacto `+573147842399` NO debe aparecer en esta lista

---

## Test 0.3: Contactos del asesor DESTINO (Luisa)

**Propósito:** Verificar que el contacto SÍ aparece aquí después de transferencia

**Request:**

```http
GET {{base_url}}/contacts?filter_time=24h&advisor=87367331
X-API-Key: {{api_key}}
```

**⚠️ PROBLEMA CONOCIDO:**
Si el contacto NO aparece aquí, la causa probable es **SEGREGACIÓN POR CANAL**.

Verificar en la respuesta:

```json
{
  "contacts": [] // ← Si está vacío, revisar Test 0.4
}
```

---

## Test 0.4: Contactos SIN filtro de advisor

**Propósito:** Ver TODOS los contactos sin filtro de segregación

**Request:**

```http
GET {{base_url}}/contacts?filter_time=24h
X-API-Key: {{api_key}}
```

**Buscar el contacto transferido y verificar:**

```json
{
  "contacts": [
    {
      "phone": "+573147842399",
      "canal_origen": "whatsapp_directo", // ← Este es el canal
      "owner_id": "87367331", // ← Debe ser Luisa
      "assigned_owner_id": "87367331" // ← Debe ser Luisa
    }
  ]
}
```

---

## Test 0.5: Ver asesores y sus canales permitidos

**Propósito:** Entender qué canales puede ver cada asesor

**Request:**

```http
GET {{base_url}}/advisors
X-API-Key: {{api_key}}
```

**Respuesta esperada:**

```json
{
  "advisors": [
    {
      "id": "87367331",
      "name": "Luisa",
      "team": "equipo_luisa"
    },
    {
      "id": "88251457",
      "name": "Yubeny",
      "team": "equipo_yubeny"
    }
  ]
}
```

**Canales por equipo (verificar en logs del servidor):**

- `equipo_luisa`: `finca_raiz`, `mercado_libre`, `metrocuadrado`
- `equipo_yubeny`: `ciencuadras`, `facebook`, `instagram`, `pagina_web`, `whatsapp`, `whatsapp_directo`

---

## Test 0.6: Ejecutar transferencia desde Postman

**Propósito:** Transferir un contacto y ver la respuesta completa

**Request:**

```http
POST {{base_url}}/contacts/{{test_phone}}/transfer
X-API-Key: {{api_key}}
Content-Type: application/x-www-form-urlencoded
```

**Body (x-www-form-urlencoded en Postman):**
| KEY | VALUE |
|-----|-------|
| to_owner_id | 87367331 |
| mode | exclusive |
| reason | Test desde Postman |
| canal | whatsapp_directo |

**Respuesta esperada (200):**

```json
{
  "status": "success",
  "from_owner": "88251457",
  "to_owner": "87367331",
  "mode": "exclusive",
  "phone": "+573147842399",
  "hubspot_updated": true,
  "transfer_history": [
    {
      "from": "88251457",
      "to": "87367331",
      "mode": "exclusive",
      "reason": "Test desde Postman",
      "timestamp": "2026-02-28T13:45:00-05:00"
    }
  ]
}
```

**Validación Postman:**

```javascript
pm.test("Transferencia exitosa", function () {
  pm.response.to.have.status(200);
  var jsonData = pm.response.json();
  pm.expect(jsonData.status).to.eql("success");
  pm.expect(jsonData.to_owner).to.eql("87367331");
});

pm.test("HubSpot actualizado en modo exclusivo", function () {
  var jsonData = pm.response.json();
  if (jsonData.mode === "exclusive") {
    pm.expect(jsonData.hubspot_updated).to.be.true;
  }
});
```

---

## 🔍 Diagnóstico: ¿Por qué el contacto no aparece después de transferir?

| Síntoma                                   | Causa                            | Solución                           |
| ----------------------------------------- | -------------------------------- | ---------------------------------- |
| Test 0.3 vacío, Test 0.4 muestra contacto | Segregación por canal            | Ver sección "Solución Segregación" |
| Test 0.4 no muestra `owner_id` correcto   | Transferencia no guardó en Redis | Revisar logs del endpoint transfer |
| `hubspot_updated: false`                  | Error API HubSpot                | Verificar `HUBSPOT_API_KEY`        |

### Solución: Problema de Segregación por Canal

El contacto entró por `whatsapp_directo` pero Luisa solo puede ver:

- `finca_raiz`, `mercado_libre`, `metrocuadrado`

**Opciones:**

1. **Agregar canal a Luisa** → Modificar `LeadAssigner.OWNERS_CONFIG`
2. **Ignorar segregación en transferencias** → Modificar lógica de filtrado

---

## Test Suite 1: Bug Fix — Badges de Estado

### Test 1.1: Verificar `conversation_status` en respuesta

**Request:**

```http
GET {{base_url}}/contacts?filter_time=24h
X-API-Key: {{api_key}}
```

**Expected Response (200):**

```json
{
  "contacts": [
    {
      "phone": "+573001234567",
      "conversation_status": "HUMAN_ACTIVE",
      "status": "HUMAN_ACTIVE"
    }
  ]
}
```

**Validación en Postman (Tests tab):**

```javascript
pm.test("conversation_status no es 'active' hardcodeado", function () {
  var jsonData = pm.response.json();
  if (jsonData.contacts && jsonData.contacts.length > 0) {
    var contact = jsonData.contacts[0];
    pm.expect(contact.conversation_status).to.be.oneOf([
      "HUMAN_ACTIVE",
      "IN_CONVERSATION",
      "PENDING_HANDOFF",
      "BOT_ACTIVE",
    ]);
    pm.expect(contact.conversation_status).to.not.equal("active");
  }
});
```

---

## Test Suite 2: Creación Manual de Contactos

### ⚠️ Configuración IMPORTANTE en Postman

Para estos tests, el endpoint usa **form-urlencoded**, NO JSON.

**En Postman:**

1. Seleccionar tab **Body**
2. Seleccionar **x-www-form-urlencoded** (NO raw, NO form-data)
3. Agregar cada campo como key-value:

| KEY             | VALUE            |
| --------------- | ---------------- |
| firstname       | Carlos           |
| phone           | 3009998877       |
| lastname        | Rodriguez        |
| property_type   | apartamento      |
| operation_type  | compra           |
| budget          | 300000000        |
| characteristics | 3 habitaciones   |
| canal           | whatsapp_directo |

### Test 2.1: Crear contacto nuevo (Happy Path)

**Request:**

```http
POST {{base_url}}/contacts/create
X-API-Key: {{api_key}}
Content-Type: application/x-www-form-urlencoded
```

**Body (x-www-form-urlencoded en Postman):**
| KEY | VALUE |
|-----|-------|
| firstname | Carlos |
| phone | 3009998877 |
| lastname | Rodriguez |
| property_type | apartamento |
| operation_type | compra |
| budget | 300000000 |
| characteristics | 3 habitaciones, cerca al metro |
| canal | whatsapp_directo |

**Expected Response (200):**

```json
{
  "status": "success",
  "contact_id": "12345678",
  "deal_id": "87654321",
  "phone": "+573009998877",
  "display_name": "Carlos Rodriguez",
  "owner_id": "88251457"
}
```

**Validación:**

```javascript
pm.test("Contacto creado exitosamente", function () {
  pm.response.to.have.status(200);
  var jsonData = pm.response.json();
  pm.expect(jsonData.status).to.eql("success");
  pm.expect(jsonData.contact_id).to.exist;
  pm.expect(jsonData.deal_id).to.exist;
  pm.expect(jsonData.phone).to.include("+57");
});

// Guardar contact_id para pruebas posteriores
var jsonData = pm.response.json();
pm.environment.set("created_contact_id", jsonData.contact_id);
pm.environment.set("created_phone", jsonData.phone);
```

### Test 2.2: Intentar crear contacto duplicado

**Request:**

```http
POST {{base_url}}/contacts/create
X-API-Key: {{api_key}}
Content-Type: application/x-www-form-urlencoded
```

**Body (x-www-form-urlencoded en Postman):**
| KEY | VALUE |
|-----|-------|
| firstname | Carlos Duplicado |
| phone | 3009998877 |
| canal | whatsapp_directo |

**Expected Response (409 Conflict):**

```json
{
  "status": "exists",
  "message": "El contacto ya existe: Carlos Rodriguez",
  "contact_id": "12345678",
  "phone": "+573009998877",
  "suggestion": "¿Deseas tomar control de este contacto?"
}
```

**Validación:**

```javascript
pm.test("Detecta duplicado correctamente", function () {
  pm.response.to.have.status(409);
  var jsonData = pm.response.json();
  pm.expect(jsonData.status).to.eql("exists");
  pm.expect(jsonData.suggestion).to.include("tomar control");
});
```

### Test 2.3: Teléfono inválido

**Request:**

```http
POST {{base_url}}/contacts/create
X-API-Key: {{api_key}}
Content-Type: application/x-www-form-urlencoded
```

**Body (x-www-form-urlencoded en Postman):**
| KEY | VALUE |
|-----|-------|
| firstname | Test |
| phone | 123 |
| canal | whatsapp_directo |

**Expected Response (400):**

```json
{
  "detail": "Número de teléfono inválido: 123"
}
```

### Test 2.4: Sin API Key

**Request (SIN header X-API-Key):**

```http
POST {{base_url}}/contacts/create
Content-Type: application/x-www-form-urlencoded
```

**Body (x-www-form-urlencoded en Postman):**
| KEY | VALUE |
|-----|-------|
| firstname | Test |
| phone | 3001112233 |

**Expected Response (401):**

```json
{
  "detail": "API Key inválida"
}
```

---

## Test Suite 3: Transferencia de Contactos

### ⚠️ Configuración en Postman

Al igual que Suite 2, usar **x-www-form-urlencoded** en el Body.

### Test 3.1: Transferencia exclusiva (Yubeny → Luisa)

**Pre-requisito:** Contacto `+573147842399` activo y asignado a Yubeny

**Request:**

```http
POST {{base_url}}/contacts/+573147842399/transfer
X-API-Key: {{api_key}}
Content-Type: application/x-www-form-urlencoded
```

**Body (x-www-form-urlencoded en Postman):**
| KEY | VALUE |
|-----|-------|
| to_owner_id | 87367331 |
| mode | exclusive |
| reason | Reasignación por carga de trabajo |
| canal | whatsapp_directo |

**Expected Response (200):**

```json
{
  "status": "success",
  "from_owner": "88251457",
  "to_owner": "87367331",
  "mode": "exclusive",
  "phone": "+573147842399",
  "hubspot_updated": true
}
```

**Validación:**

```javascript
pm.test("Transferencia exclusiva exitosa", function () {
  pm.response.to.have.status(200);
  var jsonData = pm.response.json();
  pm.expect(jsonData.status).to.eql("success");
  pm.expect(jsonData.mode).to.eql("exclusive");
  pm.expect(jsonData.hubspot_updated).to.be.true;
});
```

### Test 3.2: Transferencia colaborativa (Luisa → Yubeny)

**Pre-requisito:** Contacto `+573147842399` activo y asignado a Luisa (después de Test 3.1)

**Request:**

```http
POST {{base_url}}/contacts/+573147842399/transfer
X-API-Key: {{api_key}}
Content-Type: application/x-www-form-urlencoded
```

**Body (x-www-form-urlencoded en Postman):**
| KEY | VALUE |
|-----|-------|
| to_owner_id | 88251457 |
| mode | collaborative |
| reason | Colaboración para cierre de venta |
| canal | whatsapp_directo |

**Expected Response (200):**

```json
{
  "status": "success",
  "from_owner": "87367331",
  "to_owner": "88251457",
  "mode": "collaborative",
  "phone": "+573147842399",
  "hubspot_updated": false
}
```

**Validación:**

```javascript
pm.test("Transferencia colaborativa no actualiza HubSpot", function () {
  var jsonData = pm.response.json();
  pm.expect(jsonData.mode).to.eql("collaborative");
  pm.expect(jsonData.hubspot_updated).to.be.false;
});
```

### Test 3.3: Modo inválido

**Request:**

```http
POST {{base_url}}/contacts/+573001234567/transfer
X-API-Key: {{api_key}}
Content-Type: application/x-www-form-urlencoded
```

**Body (x-www-form-urlencoded en Postman):**
| KEY | VALUE |
|-----|-------|
| to_owner_id | 87367331 |
| mode | invalid_mode |

**Expected Response (400):**

```json
{
  "detail": "Modo debe ser 'exclusive' o 'collaborative'"
}
```

---

## Test Suite 4: Listar Asesores

### Test 4.1: Obtener lista de asesores disponibles

**Request:**

```http
GET {{base_url}}/advisors
X-API-Key: {{api_key}}
```

**Expected Response (200):**

```json
{
  "advisors": [
    { "id": "87367331", "name": "Luisa", "active": true },
    { "id": "88251457", "name": "Yubeny", "active": true }
  ]
}
```

**Validación:**

```javascript
pm.test("Lista de asesores no vacía", function () {
  var jsonData = pm.response.json();
  pm.expect(jsonData.advisors).to.be.an("array");
  pm.expect(jsonData.advisors.length).to.be.at.least(1);
});

pm.test("Cada asesor tiene id y name", function () {
  var jsonData = pm.response.json();
  jsonData.advisors.forEach(function (advisor) {
    pm.expect(advisor.id).to.exist;
    pm.expect(advisor.name).to.exist;
  });
});
```

---

## Test Suite 5: WebSocket Stats (Debug)

### Test 5.1: Verificar conexiones WebSocket

**Request:**

```http
GET {{base_url}}/ws/stats
X-API-Key: {{api_key}}
```

**Expected Response (200):**

```json
{
  "total_connections": 0,
  "advisors_connected": 0,
  "phones_registered": 0
}
```

---

## Test Suite 6: Reordenamiento ZSET

### Test 6.1: Verificar orden por actividad

**Flujo de prueba manual:**

1. Crear contacto A (timestamp T1)
2. Crear contacto B (timestamp T2, T2 > T1)
3. Llamar `GET /contacts`
4. Verificar que B aparece antes que A

**Request:**

```http
GET {{base_url}}/contacts?filter_time=24h
X-API-Key: {{api_key}}
```

**Validación:**

```javascript
pm.test("Contactos ordenados por actividad reciente", function () {
  var jsonData = pm.response.json();
  if (jsonData.contacts && jsonData.contacts.length >= 2) {
    // El primero debe tener last_activity más reciente
    var first = new Date(jsonData.contacts[0].last_activity);
    var second = new Date(jsonData.contacts[1].last_activity);
    pm.expect(first.getTime()).to.be.at.least(second.getTime());
  }
});
```


---

# PARTE 3: FLUJOS PASO A PASO

## Flujo 1: Crear Contacto Manualmente

### ¿Quién lo hace?

La asesora desde el Panel de Asesores.

### ¿Dónde se crea?

**Primero en HubSpot**, luego automáticamente aparece en el Panel.

### Pasos

```
┌─────────────────────────────────────────────────────────────────┐
│ PASO 1: Asesora abre el Panel de Asesores                       │
│         URL: /whatsapp/panel?advisor=87367331                   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ PASO 2: Click en botón verde "+ Nuevo" (esquina superior)       │
│         Se abre el modal "Crear Nuevo Contacto"                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ PASO 3: Llenar formulario                                       │
│         • Nombre* (obligatorio)                                 │
│         • Teléfono* (obligatorio, sin código país)              │
│         • Apellido (opcional)                                   │
│         • Tipo inmueble (dropdown)                              │
│         • Tipo operación (dropdown)                             │
│         • Presupuesto (texto libre)                             │
│         • Notas adicionales (textarea)                          │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ PASO 4: Click en "Crear Contacto"                               │
│         El sistema:                                             │
│         1. Normaliza el teléfono (+57...)                       │
│         2. Busca si ya existe en HubSpot (whatsapp_id)          │
└─────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
┌─────────────────────────┐     ┌─────────────────────────┐
│ SI NO EXISTE:           │     │ SI YA EXISTE:           │
│ • Crea contacto HubSpot │     │ • Muestra warning       │
│ • Crea deal asociado    │     │ • "El contacto ya       │
│ • Activa HUMAN_ACTIVE   │     │   existe: [nombre]"     │
│ • Aparece en panel      │     │ • Botón "Tomar Control" │
└─────────────────────────┘     └─────────────────────────┘
              │                               │
              ▼                               ▼
┌─────────────────────────────────────────────────────────────────┐
│ PASO 5: Contacto visible en:                                    │
│         ✅ Panel de Asesores (inmediato, badge verde)           │
│         ✅ HubSpot CRM (nuevo contacto + deal)                  │
│         ✅ Pipeline "Comercial" etapa "Nuevo Lead"              │
└─────────────────────────────────────────────────────────────────┘
```

### ¿Qué pasa después?

- El contacto queda en estado **HUMAN_ACTIVE** con badge verde "En espera"
- La asesora puede hacer click para abrir el chat
- Al enviar mensaje, cambia a **IN_CONVERSATION** (badge azul)
- El contacto también es visible en HubSpot bajo el owner asignado

---

## Flujo 2: Transferir Contacto a Otra Asesora

### ¿Cuándo usarlo?

- Cuando la asesora está ocupada y otra puede atender mejor
- Cuando el cliente pide ser atendido por alguien específico
- Para colaboración en cierres de venta complejos

### Pasos

```
┌─────────────────────────────────────────────────────────────────┐
│ PASO 1: Seleccionar el contacto en el panel                     │
│         (click en la lista de la izquierda)                     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ PASO 2: Click en botón "⇄ Transferir"                           │
│         (aparece en el header del chat junto a "Cerrar")        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ PASO 3: En el modal "Transferir Contacto"                       │
│         • Ver info del contacto (solo lectura)                  │
│         • Seleccionar asesora destino (dropdown)                │
│         • Elegir modo:                                          │
│           ○ Exclusivo: pasa 100% a la otra                      │
│           ○ Colaborativo: ambas ven el contacto                 │
│         • Escribir motivo (opcional)                            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ PASO 4: Click en "Transferir"                                   │
└─────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
┌─────────────────────────┐     ┌─────────────────────────┐
│ MODO EXCLUSIVO:         │     │ MODO COLABORATIVO:      │
│ • Redis: cambia owner   │     │ • Redis: agrega owner   │
│ • HubSpot: actualiza    │     │ • HubSpot: NO cambia    │
│   hubspot_owner_id      │     │ • Ambas ven el contacto │
│ • Desaparece del panel  │     │ • Permanece en ambos    │
│   de la asesora origen  │     │   paneles               │
└─────────────────────────┘     └─────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ PASO 5: Notificaciones WebSocket (instantáneas)                 │
│         • Asesora origen: "Contacto X transferido a Y"          │
│         • Asesora destino: "Nuevo contacto transferido: X"      │
│         • Sonido de notificación en ambos paneles               │
└─────────────────────────────────────────────────────────────────┘
```

### Diferencia entre modos

| Aspecto          | Exclusivo               | Colaborativo      |
| ---------------- | ----------------------- | ----------------- |
| Ver contacto     | Solo nueva asesora      | Ambas asesoras    |
| Owner en HubSpot | Se actualiza            | No cambia         |
| Panel origen     | Desaparece el contacto  | Sigue visible     |
| Uso típico       | Reasignación definitiva | Trabajo en equipo |

---

## Flujo 3: Recibir Notificación de Nuevo Mensaje (WebSocket)

### ¿Cómo funciona?

```
┌─────────────────────────────────────────────────────────────────┐
│ EVENTO: Cliente envía mensaje por WhatsApp                      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Twilio webhook → webhook_handler.py                             │
│ (procesa el mensaje entrante)                                   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ conversation_state.py → update_activity()                       │
│ Actualiza el score en el ZSET (timestamp actual)                │
│ → El contacto sube al tope de la lista                          │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ websocket_manager.py → notify_new_message()                     │
│ Envía evento a todas las conexiones de la asesora asignada      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ PANEL DE LA ASESORA (navegador):                                │
│ 1. 🔊 Suena notificación de audio                               │
│ 2. 🔔 Aparece notificación del navegador (si está permitida)    │
│ 3. 📋 Lista de contactos se recarga                             │
│ 4. ⬆️ El contacto sube al primer lugar                          │
│ 5. 💬 Si el chat está abierto, el mensaje aparece               │
└─────────────────────────────────────────────────────────────────┘
```

### ¿Qué ve la asesora?

1. **Si tiene el chat abierto:** El mensaje aparece en el hilo inmediatamente
2. **Si NO tiene el chat abierto:**
   - Sonido de notificación
   - El contacto sube al tope de la lista con badge pulsante
   - Notificación del navegador (si está permitida)

### Fallback si WebSocket falla

El polling automático cada 3-10 segundos sigue funcionando. Los contactos se recargan periódicamente aunque no haya WebSocket activo.

---

## Flujo 4: Diferenciación Visual de Estados

### Estados y Badges

| Estado Redis      | Badge Visual           | Color    | Significado                  |
| ----------------- | ---------------------- | -------- | ---------------------------- |
| `HUMAN_ACTIVE`    | "En espera" (pulsante) | 🟢 Verde | Cliente esperando respuesta  |
| `IN_CONVERSATION` | "En conversación"      | 🔵 Azul  | Asesora está chateando       |
| `PENDING_HANDOFF` | "En espera"            | 🟢 Verde | Sofía solicitó transferencia |
| `BOT_ACTIVE`      | "Bot"                  | ⚪ Gris  | Sofía está respondiendo      |

### ¿Cuándo cambian?

```
┌────────────────┐     Sofía hace handoff     ┌────────────────┐
│   BOT_ACTIVE   │ ─────────────────────────▶ │  HUMAN_ACTIVE  │
│   (gris)       │                            │  (verde)       │
└────────────────┘                            └────────────────┘
                                                      │
                                          Asesora envía mensaje
                                                      │
                                                      ▼
                                              ┌────────────────┐
                                              │IN_CONVERSATION │
                                              │    (azul)      │
                                              └────────────────┘
                                                      │
                                          Asesora cierra chat
                                          (click "Cerrar")
                                                      │
                                                      ▼
                                              ┌────────────────┐
                                              │   BOT_ACTIVE   │
                                              │    (gris)      │
                                              └────────────────┘
```

---

## Flujo 5: ¿Dónde se Visualiza Cada Contacto?

### Contacto creado por Sofía (automático)

| Ubicación         | ¿Visible? | Condición                               |
| ----------------- | --------- | --------------------------------------- |
| Panel de Asesores | ✅ Sí     | Si está en HUMAN_ACTIVE/IN_CONVERSATION |
| HubSpot CRM       | ✅ Sí     | Siempre (contacto + deal creados)       |
| MongoDB           | ✅ Sí     | Historial de mensajes                   |

### Contacto creado manualmente desde el Panel

| Ubicación         | ¿Visible? | Condición                     |
| ----------------- | --------- | ----------------------------- |
| Panel de Asesores | ✅ Sí     | Inmediatamente (HUMAN_ACTIVE) |
| HubSpot CRM       | ✅ Sí     | Contacto + deal creados       |
| MongoDB           | ❌ No     | Sin mensajes aún              |

### Contacto después de "Cerrar conversación"

| Ubicación         | ¿Visible? | Condición                    |
| ----------------- | --------- | ---------------------------- |
| Panel de Asesores | ❌ No     | Sale del índice Redis        |
| HubSpot CRM       | ✅ Sí     | Permanece (datos históricos) |
| MongoDB           | ✅ Sí     | Historial preservado         |

---

# PARTE 4: CHECKLIST DE VERIFICACIÓN POST-DEPLOY

## Antes del Deploy

- [ ] Tests de Postman pasan en local
- [ ] `git status` limpio
- [ ] Variables de entorno configuradas en Railway

## Verificación Funcional

### 1. Bug Fix Badges

- [ ] Abrir panel con contacto en HUMAN_ACTIVE → Badge verde "En espera"
- [ ] Enviar mensaje → Badge cambia a azul "En conversación"

### 2. Creación Manual

- [ ] Crear contacto nuevo desde panel → Éxito
- [ ] Verificar en HubSpot → Contacto + Deal existen
- [ ] Intentar crear duplicado → Mensaje de alerta 409

### 3. Transferencia

- [ ] Transferir modo exclusivo → Contacto desaparece del panel origen
- [ ] Transferir modo colaborativo → Contacto visible en ambos paneles
- [ ] Verificar HubSpot → Owner actualizado (solo exclusivo)

### 4. WebSocket

- [ ] Abrir panel → Ver en logs: "Asesor X conectado"
- [ ] Enviar mensaje de prueba desde WhatsApp → Suena notificación
- [ ] Cerrar y reabrir panel → Reconexión automática

### 5. Reordenamiento

- [ ] Enviar mensaje a contacto antiguo → Sube al tope de la lista

---

# PARTE 5: TROUBLESHOOTING COMÚN

## Error 401: API Key inválida

**Causa:** Header `X-API-Key` faltante o incorrecto
**Solución:** Verificar variable `ADMIN_API_KEY` en Railway

## Error 409: Contacto ya existe

**Causa:** whatsapp_id duplicado en HubSpot
**Solución:** Usar botón "Tomar Control" o buscar contacto existente

## WebSocket no conecta

**Causa:** Proxy bloqueando WS, o CORS
**Solución:** Verificar logs del navegador; el polling funciona como fallback

## Contacto no aparece en panel

**Causa:** Estado en BOT_ACTIVE o expiró el TTL de Redis
**Solución:** Verificar `GET /contacts` para ver el estado real

## Badge siempre verde

**Causa:** Bug no aplicado, `conversation_status` sigue siendo "active"
**Solución:** Verificar línea 201 de `conversation_state.py`

---

_Documento generado para validación y pruebas del Panel de Asesores v3.0_
