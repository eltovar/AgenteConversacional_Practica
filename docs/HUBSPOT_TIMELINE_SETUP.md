# Configuración de Timeline Events en HubSpot

Esta guía explica cómo crear el Event Type personalizado para registrar
las conversaciones de Sofía en el Timeline de los contactos.

---

## Requisitos Previos

La Timeline Events API requiere credenciales de **desarrollador** (no Private App):

| Variable | Descripción |
|----------|-------------|
| `HUBSPOT_APP_ID` | El ID de tu aplicación de desarrollador |
| `HUBSPOT_DEVELOPER_API_KEY` | Tu Developer API Key |

---

## Paso 1: Obtener Credenciales de Desarrollador

### 1.1 Acceder a la cuenta de desarrollador

1. Ve a [https://developers.hubspot.com/](https://developers.hubspot.com/)
2. Inicia sesión con tu cuenta

### 1.2 Obtener el App ID

1. Ve a **Apps** en el menú lateral
2. Selecciona tu app **sofia-middleware** (creada con `hs get-started`)
3. El **App ID** está en:
   - La URL: `https://app.hubspot.com/developer/.../application/{APP_ID}`
   - O en los detalles de la aplicación

### 1.3 Obtener la Developer API Key

1. Ve a **Settings** (esquina superior derecha)
2. Selecciona **Developer API Key**
3. Crea una nueva key o copia la existente

### 1.4 Agregar a tu .env

```env
HUBSPOT_APP_ID=tu_app_id_aqui
HUBSPOT_DEVELOPER_API_KEY=tu_developer_api_key_aqui
```

---

## Paso 2: Habilitar App Events (Timeline Events)

### Opción A: Solicitar acceso a HubSpot

La función **App Events** está restringida en algunas cuentas. Si al ejecutar:

```bash
cd sofia-middleware
hs project add
```

Ves el mensaje *"This account doesn't have access to this feature"* para App Events,
debes solicitar acceso a HubSpot completando el formulario en la interfaz.

### Opción B: Usar la CLI (si tienes acceso)

```bash
cd sofia-middleware
hs project add --features app-event
```

Sigue las instrucciones para definir los tokens:
- `contenido` (string): Contenido del mensaje
- `emisor` (string): Emisor del mensaje
- `es_bot` (boolean): Si es mensaje del bot
- `timestamp` (string): Fecha y hora

---

## Paso 3: Crear Event Template via API

Si prefieres usar la API directamente (requiere Developer API Key):

```bash
curl -X POST "https://api.hubapi.com/crm/v3/timeline/{APP_ID}/event-templates?hapikey={DEVELOPER_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Mensaje WhatsApp Sofía",
    "objectType": "contacts",
    "headerTemplate": "{{#if es_bot}}🤖 Sofía (IA){{else}}👤 Cliente{{/if}}: Mensaje de WhatsApp",
    "detailTemplate": "**{{emisor}}**\n\n{{contenido}}\n\n---\n_Enviado: {{timestamp}}_",
    "tokens": [
      {
        "name": "contenido",
        "label": "Contenido del mensaje",
        "type": "string"
      },
      {
        "name": "emisor",
        "label": "Emisor",
        "type": "string"
      },
      {
        "name": "es_bot",
        "label": "Es mensaje del bot",
        "type": "boolean"
      },
      {
        "name": "timestamp",
        "label": "Fecha y hora",
        "type": "string"
      },
      {
        "name": "direccion",
        "label": "Dirección del mensaje",
        "type": "enumeration",
        "options": [
          {"value": "inbound", "label": "Entrante (Cliente)"},
          {"value": "outbound", "label": "Saliente (Sofía/Asesor)"}
        ]
      }
    ]
  }'
```

Reemplaza:
- `{APP_ID}` con tu App ID
- `{DEVELOPER_API_KEY}` con tu Developer API Key

---

## Paso 4: Guardar el Event Type ID

La respuesta incluirá un `id`. Guárdalo en tu `.env`:

```env
HUBSPOT_TIMELINE_EVENT_TYPE_ID=123456
```

---

## Paso 5: Verificar la configuración

```bash
# Listar event types existentes
curl "https://api.hubapi.com/crm/v3/timeline/{APP_ID}/event-templates?hapikey={DEVELOPER_API_KEY}"
```

---

## Alternativa: Usar Notes API (Fallback Automático)

Si no tienes acceso a App Events o no configuras el `HUBSPOT_TIMELINE_EVENT_TYPE_ID`,
el sistema usará automáticamente la **Notes API** como fallback.

Las notas aparecerán en el Timeline del contacto con el formato:

```
🤖 [Sofía - IA] ➡️

Contenido del mensaje aquí...

---
📅 2025-01-15 10:30:45
```

El código en `timeline_logger.py` detecta automáticamente si el
Event Type ID está configurado. Si no lo está, usa notas.

---

## Visualización en HubSpot

Una vez configurado con Timeline Events, los mensajes aparecerán así:

```
┌─────────────────────────────────────────┐
│ 🤖 Sofía (IA): Mensaje de WhatsApp      │
├─────────────────────────────────────────┤
│ **Sofía (IA)**                          │
│                                         │
│ ¡Hola! Soy Sofía, asesora virtual de    │
│ Inmobiliaria Proteger. ¿En qué puedo    │
│ ayudarte hoy?                           │
│                                         │
│ ---                                     │
│ _Enviado: 2025-01-15 10:30:45_          │
└─────────────────────────────────────────┘
```

---

## Notas Importantes

1. **Rate Limits**: La Timeline API tiene límite de 100 requests/10 segundos
2. **Retención**: Los eventos de Timeline son permanentes (no se borran automáticamente)
3. **Filtros**: Los asesores pueden filtrar por tipo de evento en el Timeline
4. **Permisos**: Se requiere el scope `timeline` en la aplicación

---

## Recursos

- [HubSpot Timeline Events API](https://developers.hubspot.com/docs/api/crm/timeline)
- [Sample Apps - Timeline Events](https://github.com/HubSpot/sample-apps-timeline-events)
- [HubSpot Developer Docs](https://developers.hubspot.com/docs)
- [HubSpot CLI Documentation](https://developers.hubspot.com/docs/getting-started/quickstart)