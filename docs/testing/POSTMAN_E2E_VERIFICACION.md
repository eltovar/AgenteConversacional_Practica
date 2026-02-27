# Guía de Pruebas E2E con Postman - Verificación de Fixes

**URL Base:** `https://agenteconversacionalpractica-production.up.railway.app`  
**Fecha:** 2026-02-27

---

## 📋 CHECKLIST DE VERIFICACIÓN

| #   | Test                    | Endpoint                                  | Verifica             |
| --- | ----------------------- | ----------------------------------------- | -------------------- |
| 1   | Health Check            | GET /health                               | Sistema activo       |
| 2   | Enviar Mensaje Texto    | POST /whatsapp/panel/send-message-json    | API funciona         |
| 3   | Verificar Ventana 24h   | GET /whatsapp/panel/window-status/{phone} | Estado ventana       |
| 4   | Simular Audio Entrante  | POST /whatsapp/webhook                    | Procesamiento audio  |
| 5   | Simular Status Callback | POST /whatsapp/status                     | Tracking delivery    |
| 6   | Listar Contactos Panel  | GET /whatsapp/panel/contacts              | Segregación funciona |

---

## 🔧 CONFIGURACIÓN DE POSTMAN

### Variables de Entorno (Environment)

```json
{
  "base_url": "https://agenteconversacionalpractica-production.up.railway.app",
  "admin_api_key": "protect_admin_2024_xK9mP3qR",
  "test_phone": "+573001234567",
  "advisor_id": "88251457"
}
```

### Crear Environment en Postman:

1. Click en "Environments" (icono de engranaje)
2. "Add" → Nombrar: "Sofia Railway Production"
3. Agregar variables:
   - `base_url` = `https://agenteconversacionalpractica-production.up.railway.app`
   - `admin_api_key` = `protect_admin_2024_xK9mP3qR`
   - `test_phone` = `+573001234567`
   - `advisor_id` = `88251457`

---

## 🧪 TEST 1: Health Check

**Propósito:** Verificar que el sistema está activo

```http
GET {{base_url}}/health
```

### Configuración en Postman:

- Method: `GET`
- URL: `{{base_url}}/health`
- Headers: ninguno

### Respuesta Esperada (200 OK):

```json
{
  "status": "healthy",
  "version": "2.0",
  "environment": "railway"
}
```

### Test Script (pestaña "Tests"):

```javascript
pm.test("Status code is 200", () => {
  pm.response.to.have.status(200);
});

pm.test("System is healthy", () => {
  const json = pm.response.json();
  pm.expect(json.status).to.eql("healthy");
});
```

---

## 🧪 TEST 2: Enviar Mensaje de Texto (JSON API)

**Propósito:** Verificar que el endpoint JSON funciona correctamente

```http
POST {{base_url}}/whatsapp/panel/send-message-json
Content-Type: application/json
X-API-Key: {{admin_api_key}}

{
    "phone": "{{test_phone}}",
    "message": "Test E2E desde Postman - {{$timestamp}}",
    "canal": "whatsapp"
}
```

### Configuración en Postman:

- Method: `POST`
- URL: `{{base_url}}/whatsapp/panel/send-message-json`
- Headers:
  - `Content-Type`: `application/json`
  - `X-API-Key`: `{{admin_api_key}}`
- Body (raw JSON):

```json
{
  "phone": "{{test_phone}}",
  "message": "Test E2E desde Postman - {{$timestamp}}",
  "canal": "whatsapp"
}
```

### Respuesta Esperada (200 OK):

```json
{
  "status": "success",
  "message_sid": "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "mongo_id": "...",
  "to": "+573001234567",
  "contact_id": "...",
  "canal": "whatsapp",
  "window_status": {
    "is_open": true,
    "time_remaining": 86400
  },
  "sofia_paused": true,
  "message_source": "Panel JSON API"
}
```

### Test Script:

```javascript
pm.test("Status code is 200", () => {
  pm.response.to.have.status(200);
});

pm.test("Message sent successfully", () => {
  const json = pm.response.json();
  pm.expect(json.status).to.eql("success");
  pm.expect(json.message_sid).to.match(/^SM/);
});

pm.test("Sofia was paused", () => {
  const json = pm.response.json();
  pm.expect(json.sofia_paused).to.be.true;
});

// Guardar message_sid para test de status
if (pm.response.code === 200) {
  pm.environment.set("last_message_sid", pm.response.json().message_sid);
}
```

### Posibles Errores:

| Error             | Causa            | Solución                       |
| ----------------- | ---------------- | ------------------------------ |
| 401 Unauthorized  | API Key inválida | Verificar `X-API-Key` header   |
| 400 Bad Request   | Mensaje vacío    | Agregar contenido en `message` |
| 422 Unprocessable | JSON mal formado | Verificar formato JSON         |

---

## 🧪 TEST 3: Verificar Ventana de 24 Horas

**Propósito:** Verificar estado de la ventana de conversación

```http
GET {{base_url}}/whatsapp/panel/window-status/{{test_phone}}
X-API-Key: {{admin_api_key}}
```

### Configuración en Postman:

- Method: `GET`
- URL: `{{base_url}}/whatsapp/panel/window-status/{{test_phone}}`
- Headers:
  - `X-API-Key`: `{{admin_api_key}}`

### Respuesta Esperada (200 OK):

```json
{
  "phone": "+573001234567",
  "is_open": true,
  "last_message_time": "2026-02-27T19:10:00Z",
  "time_remaining_seconds": 82800,
  "time_remaining_human": "23h 0m",
  "requires_template": false,
  "message": "Ventana abierta. Tiempo restante: 23h 0m"
}
```

### Test Script:

```javascript
pm.test("Status code is 200", () => {
  pm.response.to.have.status(200);
});

pm.test("Window status returned", () => {
  const json = pm.response.json();
  pm.expect(json).to.have.property("is_open");
  pm.expect(json).to.have.property("time_remaining_seconds");
});
```

---

## 🧪 TEST 4: Simular Webhook de Audio Entrante

**Propósito:** Verificar procesamiento de audio (simula lo que envía Twilio)

⚠️ **NOTA:** Este test simula el webhook de Twilio. En producción real, necesitas una URL de audio accesible.

```http
POST {{base_url}}/whatsapp/webhook
Content-Type: application/x-www-form-urlencoded

From=whatsapp:{{test_phone}}&To=whatsapp:+15551234567&Body=&NumMedia=0&MessageSid=SM_TEST_{{$timestamp}}
```

### Configuración en Postman:

- Method: `POST`
- URL: `{{base_url}}/whatsapp/webhook`
- Headers:
  - `Content-Type`: `application/x-www-form-urlencoded`
- Body (x-www-form-urlencoded):
  - `From`: `whatsapp:{{test_phone}}`
  - `To`: `whatsapp:+15551234567`
  - `Body`: `Hola, este es un test de verificación E2E`
  - `NumMedia`: `0`
  - `MessageSid`: `SM_TEST_{{$timestamp}}`

### Respuesta Esperada (200 OK):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>¡Hola! Soy Sofía...</Message>
</Response>
```

### Test Script:

```javascript
pm.test("Status code is 200", () => {
  pm.response.to.have.status(200);
});

pm.test("Response is TwiML XML", () => {
  pm.expect(pm.response.headers.get("Content-Type")).to.include("text/xml");
});

pm.test("Contains Message element", () => {
  pm.expect(pm.response.text()).to.include("<Message>");
});
```

---

## 🧪 TEST 5: Simular Status Callback de Twilio

**Propósito:** Verificar que el tracking de delivery funciona

```http
POST {{base_url}}/whatsapp/status
Content-Type: application/x-www-form-urlencoded

MessageSid={{last_message_sid}}&MessageStatus=delivered&From=whatsapp:+15551234567&To=whatsapp:{{test_phone}}
```

### Configuración en Postman:

- Method: `POST`
- URL: `{{base_url}}/whatsapp/status`
- Headers:
  - `Content-Type`: `application/x-www-form-urlencoded`
- Body (x-www-form-urlencoded):
  - `MessageSid`: `{{last_message_sid}}` (o un SID de prueba como `SM_TEST_12345`)
  - `MessageStatus`: `delivered`
  - `From`: `whatsapp:+15551234567`
  - `To`: `whatsapp:{{test_phone}}`

### Respuesta Esperada (200 OK):

```xml
(respuesta vacía - solo status 200)
```

### Test Script:

```javascript
pm.test("Status code is 200", () => {
  pm.response.to.have.status(200);
});
```

### Test con Status FAILED:

```
MessageSid=SM_TEST_FAIL_123&MessageStatus=failed&ErrorCode=30007&ErrorMessage=Message filtered
```

---

## 🧪 TEST 6: Listar Contactos del Panel

**Propósito:** Verificar segregación y listado de contactos

```http
GET {{base_url}}/whatsapp/panel/contacts?filter_time=24h&advisor={{advisor_id}}
X-API-Key: {{admin_api_key}}
```

### Configuración en Postman:

- Method: `GET`
- URL: `{{base_url}}/whatsapp/panel/contacts?filter_time=24h&advisor={{advisor_id}}`
- Headers:
  - `X-API-Key`: `{{admin_api_key}}`

### Respuesta Esperada (200 OK):

```json
{
  "contacts": [
    {
      "phone": "+573001234567",
      "name": "Carlos López",
      "contact_id": "205874207324",
      "canal": "whatsapp",
      "status": "IN_CONVERSATION",
      "last_message_time": "2026-02-27T19:10:00Z",
      "unread_count": 2
    }
  ],
  "total": 1,
  "active_count": 1
}
```

### Test Script:

```javascript
pm.test("Status code is 200", () => {
  pm.response.to.have.status(200);
});

pm.test("Response has contacts array", () => {
  const json = pm.response.json();
  pm.expect(json).to.have.property("contacts");
  pm.expect(json.contacts).to.be.an("array");
});
```

---

## 🧪 TEST 7: Verificar Fix de URL Bunny.net (CRÍTICO)

**Propósito:** Verificar que las URLs de multimedia tienen https://

### Paso 1: Enviar mensaje con Form-Data (simula panel real)

```http
POST {{base_url}}/whatsapp/panel/send-message
Content-Type: multipart/form-data
X-API-Key: {{admin_api_key}}

phone: {{test_phone}}
body: Test con imagen
canal: whatsapp
```

### Configuración en Postman:

- Method: `POST`
- URL: `{{base_url}}/whatsapp/panel/send-message`
- Headers:
  - `X-API-Key`: `{{admin_api_key}}`
- Body (form-data):
  - `phone`: `{{test_phone}}`
  - `body`: `Test de verificación E2E`
  - `canal`: `whatsapp`

### Respuesta Esperada (200 OK):

```json
{
  "status": "success",
  "message_sid": "SMxxxxxxxx",
  "message": "Mensaje enviado correctamente"
}
```

### Si hay error de URL:

```json
{
  "status": "error",
  "message": "URL de media debe comenzar con https://..."
}
```

---

## 🧪 TEST 8: Obtener Historial de Conversación

**Propósito:** Verificar que los mensajes se guardan en MongoDB

```http
GET {{base_url}}/whatsapp/panel/history/{contact_id}?limit=10&canal=whatsapp&phone={{test_phone}}
X-API-Key: {{admin_api_key}}
```

### Configuración en Postman:

- Method: `GET`
- URL: `{{base_url}}/whatsapp/panel/history/205874207324?limit=10&canal=whatsapp&phone={{test_phone}}`
- Headers:
  - `X-API-Key`: `{{admin_api_key}}`

### Respuesta Esperada (200 OK):

```json
{
  "messages": [
    {
      "id": "...",
      "content": "Test E2E desde Postman",
      "sender": "advisor",
      "timestamp": "2026-02-27T19:10:00Z",
      "delivery_status": "delivered"
    }
  ],
  "total": 1,
  "source": "mongodb"
}
```

---

## 📦 COLECCIÓN POSTMAN COMPLETA (Importar JSON)

Guarda este JSON y impórtalo en Postman:

```json
{
  "info": {
    "name": "Sofia E2E Tests - Railway",
    "_postman_id": "sofia-e2e-tests",
    "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
  },
  "item": [
    {
      "name": "1. Health Check",
      "request": {
        "method": "GET",
        "header": [],
        "url": {
          "raw": "{{base_url}}/health",
          "host": ["{{base_url}}"],
          "path": ["health"]
        }
      },
      "event": [
        {
          "listen": "test",
          "script": {
            "exec": [
              "pm.test('Status 200', () => pm.response.to.have.status(200));"
            ]
          }
        }
      ]
    },
    {
      "name": "2. Send Message JSON",
      "request": {
        "method": "POST",
        "header": [
          { "key": "Content-Type", "value": "application/json" },
          { "key": "X-API-Key", "value": "{{admin_api_key}}" }
        ],
        "body": {
          "mode": "raw",
          "raw": "{\n    \"phone\": \"{{test_phone}}\",\n    \"message\": \"Test E2E - {{$timestamp}}\",\n    \"canal\": \"whatsapp\"\n}"
        },
        "url": {
          "raw": "{{base_url}}/whatsapp/panel/send-message-json",
          "host": ["{{base_url}}"],
          "path": ["whatsapp", "panel", "send-message-json"]
        }
      },
      "event": [
        {
          "listen": "test",
          "script": {
            "exec": [
              "pm.test('Status 200', () => pm.response.to.have.status(200));",
              "pm.test('Success', () => pm.expect(pm.response.json().status).to.eql('success'));",
              "if(pm.response.code===200) pm.environment.set('last_message_sid', pm.response.json().message_sid);"
            ]
          }
        }
      ]
    },
    {
      "name": "3. Window Status",
      "request": {
        "method": "GET",
        "header": [{ "key": "X-API-Key", "value": "{{admin_api_key}}" }],
        "url": {
          "raw": "{{base_url}}/whatsapp/panel/window-status/{{test_phone}}",
          "host": ["{{base_url}}"],
          "path": ["whatsapp", "panel", "window-status", "{{test_phone}}"]
        }
      }
    },
    {
      "name": "4. Simulate Webhook Text",
      "request": {
        "method": "POST",
        "header": [
          {
            "key": "Content-Type",
            "value": "application/x-www-form-urlencoded"
          }
        ],
        "body": {
          "mode": "urlencoded",
          "urlencoded": [
            { "key": "From", "value": "whatsapp:{{test_phone}}" },
            { "key": "To", "value": "whatsapp:+15551234567" },
            { "key": "Body", "value": "Test E2E verificación" },
            { "key": "NumMedia", "value": "0" },
            { "key": "MessageSid", "value": "SM_TEST_{{$timestamp}}" }
          ]
        },
        "url": {
          "raw": "{{base_url}}/whatsapp/webhook",
          "host": ["{{base_url}}"],
          "path": ["whatsapp", "webhook"]
        }
      }
    },
    {
      "name": "5. Status Callback (Delivered)",
      "request": {
        "method": "POST",
        "header": [
          {
            "key": "Content-Type",
            "value": "application/x-www-form-urlencoded"
          }
        ],
        "body": {
          "mode": "urlencoded",
          "urlencoded": [
            { "key": "MessageSid", "value": "{{last_message_sid}}" },
            { "key": "MessageStatus", "value": "delivered" },
            { "key": "From", "value": "whatsapp:+15551234567" },
            { "key": "To", "value": "whatsapp:{{test_phone}}" }
          ]
        },
        "url": {
          "raw": "{{base_url}}/whatsapp/status",
          "host": ["{{base_url}}"],
          "path": ["whatsapp", "status"]
        }
      }
    },
    {
      "name": "6. List Panel Contacts",
      "request": {
        "method": "GET",
        "header": [{ "key": "X-API-Key", "value": "{{admin_api_key}}" }],
        "url": {
          "raw": "{{base_url}}/whatsapp/panel/contacts?filter_time=24h&advisor={{advisor_id}}",
          "host": ["{{base_url}}"],
          "path": ["whatsapp", "panel", "contacts"],
          "query": [
            { "key": "filter_time", "value": "24h" },
            { "key": "advisor", "value": "{{advisor_id}}" }
          ]
        }
      }
    },
    {
      "name": "7. Send Message Form",
      "request": {
        "method": "POST",
        "header": [{ "key": "X-API-Key", "value": "{{admin_api_key}}" }],
        "body": {
          "mode": "formdata",
          "formdata": [
            { "key": "phone", "value": "{{test_phone}}" },
            { "key": "body", "value": "Test E2E Form - {{$timestamp}}" },
            { "key": "canal", "value": "whatsapp" }
          ]
        },
        "url": {
          "raw": "{{base_url}}/whatsapp/panel/send-message",
          "host": ["{{base_url}}"],
          "path": ["whatsapp", "panel", "send-message"]
        }
      }
    }
  ],
  "variable": [
    {
      "key": "base_url",
      "value": "https://agenteconversacionalpractica-production.up.railway.app"
    },
    { "key": "admin_api_key", "value": "protect_admin_2024_xK9mP3qR" },
    { "key": "test_phone", "value": "+573001234567" },
    { "key": "advisor_id", "value": "88251457" }
  ]
}
```

---

## ⚡ EJECUCIÓN RÁPIDA

### Secuencia de Tests Recomendada:

1. **Test 1** - Health Check (verificar que el sistema está arriba)
2. **Test 6** - List Contacts (ver estado actual del panel)
3. **Test 2** - Send Message JSON (enviar mensaje de prueba)
4. **Test 3** - Window Status (verificar ventana)
5. **Test 5** - Status Callback (simular delivery)

### Ejecutar Todos los Tests (Collection Runner):

1. Click en "..." junto a la colección
2. "Run collection"
3. Seleccionar environment "Sofia Railway Production"
4. Click "Run Sofia E2E Tests"

---

## ✅ CRITERIOS DE ÉXITO

| Test              | Resultado Esperado               | ¿Pasa? |
| ----------------- | -------------------------------- | ------ |
| Health Check      | Status 200, healthy              | ⬜     |
| Send Message JSON | Status 200, message_sid presente | ⬜     |
| Window Status     | Status 200, is_open presente     | ⬜     |
| Webhook Text      | Status 200, TwiML response       | ⬜     |
| Status Callback   | Status 200                       | ⬜     |
| List Contacts     | Status 200, contacts array       | ⬜     |
| Send Message Form | Status 200, success              | ⬜     |

---

## 🐛 TROUBLESHOOTING

### Error 401 - Unauthorized

- Verificar que `X-API-Key` header está correcto
- Valor esperado: `protect_admin_2024_xK9mP3qR`

### Error 422 - Unprocessable Entity

- El JSON está mal formado
- Verificar comillas y estructura

### Error 500 - Internal Server Error

- Ver logs de Railway para detalles
- Posible problema con MongoDB o Redis

### Error 503 - Service Unavailable

- Twilio no está configurado
- Verificar variables de entorno en Railway

---

## 📊 VERIFICACIÓN DE FIXES ESPECÍFICOS

### Fix 1: URL Bunny.net (Error 21620)

Para verificar que el fix de URL funciona, revisar los logs de Railway después de enviar un mensaje con multimedia:

```
# ANTES (ERROR)
[BunnyStorage] inmobiliaria-media.b-cdn.net/...
[TwilioClient] Error 21620: Invalid media URL

# DESPUÉS (CORRECTO)
[BunnyStorage] URL normalizada con https://: https://inmobiliaria-media.b-cdn.net
[TwilioClient] ✅ Mensaje enviado exitosamente
```

### Fix 2: Delivery Status Tracking

Verificar que el endpoint `/status` actualiza MongoDB:

```
# Log esperado cuando se recibe callback
[StatusCallback] ✅ Mensaje SMxxx: delivered | To: +573001234567
[MongoDB] Delivery status actualizado: SMxxx -> delivered
```

---

**Documento creado:** 2026-02-27
