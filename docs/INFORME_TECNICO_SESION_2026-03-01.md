# Informe Técnico — Sesión de Desarrollo

## Panel de Asesores v3.0

**Fecha:** 1 de Marzo de 2026  
**Branch:** `FaseFinalDetalles`  
**Entorno:** Railway (Producción)

---

## 1. Descripción del Proyecto

### 1.1 ¿Qué es el Agente Conversacional?

Es un sistema de atención al cliente automatizado para una inmobiliaria que integra:

- **Sofía (Bot IA):** Agente conversacional basado en Claude que atiende consultas iniciales por WhatsApp
- **Panel de Asesores:** Interfaz web para que asesoras humanas tomen control de conversaciones
- **HubSpot CRM:** Sistema de gestión de contactos y deals
- **Redis:** Almacenamiento de estado de conversaciones en tiempo real
- **MongoDB:** Historial de mensajes
- **Twilio:** Gateway de WhatsApp

### 1.2 Flujo de Atención

```
Cliente WhatsApp → Twilio → Sofía (Bot) → [Si necesita humano] → Panel Asesoras
                                                                      ↓
                                                              HubSpot + Redis
```

### 1.3 Estados de Conversación

| Estado            | Descripción                        |
| ----------------- | ---------------------------------- |
| `BOT_ACTIVE`      | Sofía está atendiendo              |
| `PENDING_HANDOFF` | Sofía solicitó transferir a humano |
| `HUMAN_ACTIVE`    | Esperando respuesta de asesora     |
| `IN_CONVERSATION` | Asesora chateando activamente      |

---

## 2. Problemas Identificados y Solucionados

### 2.1 TTL de 24 Horas Insuficiente para Fines de Semana

**Problema:**  
Los contactos que entraban viernes después de las 6 PM, sábado o domingo expiraban antes de que las asesoras llegaran el lunes a las 8:30 AM.

**Causa:**  
TTL fijo de 24 horas en Redis para todas las conversaciones.

**Solución Implementada:**  
Nuevo método `_calculate_dynamic_ttl()` en `conversation_state.py`:

```python
def _calculate_dynamic_ttl(self) -> int:
    """
    Calcula TTL dinámico basado en día/hora:
    - Lunes a Viernes (antes de 6PM): 24 horas
    - Viernes después de 6PM, Sábado, Domingo: Hasta lunes 9AM
    """
    colombia_tz = pytz.timezone("America/Bogota")
    now = datetime.now(colombia_tz)
    weekday = now.weekday()  # 0=Lunes, 6=Domingo
    hour = now.hour

    # Fin de semana o viernes tarde
    if weekday == 5 or weekday == 6 or (weekday == 4 and hour >= 18):
        # Calcular hasta el próximo lunes 9AM
        days_until_monday = (7 - weekday) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        next_monday_9am = now.replace(hour=9, minute=0, second=0)
        next_monday_9am += timedelta(days=days_until_monday)
        ttl_seconds = int((next_monday_9am - now).total_seconds())
        return min(ttl_seconds, 259200)  # Máximo 72 horas

    return 86400  # 24 horas estándar
```

**Commit:** `82761a3`

---

### 2.2 Contactos No Suben al Tope al Recibir Mensajes

**Problema:**  
Cuando un cliente enviaba un mensaje, el contacto no subía al primer lugar de la lista en el Panel.

**Diagnóstico Inicial:**  
Se pensaba que el ZSET (Redis Sorted Set) no se estaba actualizando.

**Investigación:**

1. Se agregaron logs para verificar actualizaciones del ZSET
2. Los logs confirmaron que el ZSET **SÍ se actualizaba correctamente**
3. Se descubrió que el problema era un `sorted()` en Python que destruía el orden

**Causa Real:**  
En `get_active_contacts()` de `outbound_panel.py`:

```python
# ANTES (problemático)
contacts_sorted = sorted(
    filtered_contacts,
    key=lambda c: c.get("activated_at", ""),
    reverse=True
)
```

Este sort por `activated_at` sobrescribía el orden correcto del ZSET que ya venía ordenado por `last_activity`.

**Solución:**

```python
# DESPUÉS (correcto)
# El ZSET ya viene ordenado por last_activity (más reciente primero)
# NO reordenar aquí para preservar ese orden
contacts_sorted = filtered_contacts
```

**Commits:**

- `dc938ac` — Actualización de ZSET score en funciones de timestamp
- `c49a4cf` — Cambio de logs debug a INFO para visibilidad
- `36b5224` — Eliminación del sort destructivo

---

### 2.3 Errores 429 (Rate Limit) de HubSpot

**Problema:**  
Al cargar múltiples contactos, las llamadas paralelas a HubSpot para enriquecimiento causaban errores 429 (Too Many Requests).

**Solución:**  
Se agregó un semáforo para limitar concurrencia:

```python
# En outbound_panel.py
_hubspot_semaphore = asyncio.Semaphore(3)  # Máximo 3 llamadas concurrentes

async def _enrich_contact_from_hubspot(contact_data, phone):
    async with _hubspot_semaphore:
        # ... llamada a HubSpot
```

**Commit:** `dc938ac`

---

### 2.4 Logs No Visibles en Railway

**Problema:**  
Los logs de debug (`logger.debug()`) no aparecían en Railway para diagnosticar problemas.

**Solución:**  
Se cambiaron los logs críticos de `debug` a `info`:

```python
# Antes
logger.debug(f"[ZSET] Actualizando score...")

# Después
logger.info(f"[ZSET] Actualizando score...")
```

**Commit:** `c49a4cf`

---

## 3. Cambios Técnicos Detallados

### 3.1 Archivos Modificados

| Archivo                            | Cambios                                        |
| ---------------------------------- | ---------------------------------------------- |
| `middleware/conversation_state.py` | TTL dinámico, actualización ZSET en timestamps |
| `middleware/outbound_panel.py`     | Semáforo HubSpot, eliminación sort destructivo |

### 3.2 Funciones Modificadas

#### `conversation_state.py`

1. **`_calculate_dynamic_ttl()`** — Nueva función
   - Calcula TTL basado en día de la semana y hora
   - Extiende TTL para fines de semana

2. **`update_advisor_message_timestamp()`** — Modificada
   - Ahora actualiza el score del ZSET además de la metadata

3. **`update_client_message_timestamp()`** — Modificada
   - Ahora actualiza el score del ZSET además de la metadata

4. **Todas las funciones con TTL** — Modificadas
   - Usan `_calculate_dynamic_ttl()` en lugar de valor fijo

#### `outbound_panel.py`

1. **`get_active_contacts()`** — Modificada
   - Eliminado `sorted()` por `activated_at`
   - Preserva orden del ZSET (por `last_activity`)

2. **`_enrich_contact_from_hubspot()`** — Modificada
   - Agregado semáforo para rate limiting
   - Máximo 3 llamadas concurrentes a HubSpot

---

## 4. Commits Realizados

| Hash      | Mensaje                      | Descripción                           |
| --------- | ---------------------------- | ------------------------------------- |
| `82761a3` | TTL dinámico implementado    | TTL 24h weekdays, ~72h weekends       |
| `dc938ac` | ZSET score update + semáforo | Actualiza scores + rate limit HubSpot |
| `c49a4cf` | Logs debug → INFO            | Visibilidad en Railway                |
| `36b5224` | Eliminar sort destructivo    | Preserva orden ZSET                   |

---

## 5. Estructura de Datos Redis

### 5.1 ZSET `active_conversations_sorted`

```
Key: active_conversations_sorted
Members: +573001234567:whatsapp_directo
Score: Unix timestamp de última actividad
```

**Ordenamiento:** Score descendente (más reciente primero)

### 5.2 Hash `conv_meta:{phone}:{canal}`

```json
{
  "status": "HUMAN_ACTIVE",
  "assigned_owner_id": "88251457",
  "display_name": "Juan Pérez",
  "canal_origen": "whatsapp_directo",
  "activated_at": "2026-03-01T10:30:00-05:00",
  "last_activity": "2026-03-01T11:45:00-05:00",
  "advisor_last_msg": "2026-03-01T11:40:00-05:00",
  "client_last_msg": "2026-03-01T11:45:00-05:00"
}
```

---

## 6. Endpoints del Panel

| Método | Endpoint                     | Descripción             |
| ------ | ---------------------------- | ----------------------- |
| GET    | `/contacts`                  | Lista contactos activos |
| POST   | `/contacts/create`           | Crear contacto manual   |
| POST   | `/contacts/{phone}/transfer` | Transferir contacto     |
| DELETE | `/contacts/{phone}/close`    | Cerrar conversación     |
| GET    | `/advisors`                  | Lista de asesoras       |
| GET    | `/debug/redis`               | Debug estado Redis      |

---

## 7. Script de Limpieza de Pruebas

```powershell
$API_KEY = "protect_admin_2024_xK9mP3qR"
$BASE_URL = "https://agenteconversacionalpractica-production.up.railway.app/whatsapp/panel"

$NUMEROS_PRUEBA = @(
    "+573009998877",
    "+573001234567",
    "+573001112235"
)

foreach ($phone in $NUMEROS_PRUEBA) {
    $encoded = [System.Web.HttpUtility]::UrlEncode($phone)
    $url = "$BASE_URL/contacts/$encoded/close?canal=whatsapp_directo"
    Invoke-RestMethod -Uri $url -Method DELETE -Headers @{"X-API-Key"=$API_KEY}
}
```

---

## 8. Verificación de Contactos Activos

```powershell
$API_KEY = "protect_admin_2024_xK9mP3qR"
$url = "https://agenteconversacionalpractica-production.up.railway.app/whatsapp/panel/contacts?filter_time=all"

$response = Invoke-RestMethod -Uri $url -Method GET -Headers @{"X-API-Key"=$API_KEY}
$response.contacts | ForEach-Object {
    Write-Host "$($_.phone) - $($_.display_name) - $($_.conversation_status)"
}
```

---

## 9. Diagrama de Flujo de Ordenamiento

```
┌─────────────────────────────────────────────────────────────────┐
│ Cliente envía mensaje por WhatsApp                              │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ webhook_handler.py recibe mensaje                               │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ conversation_state.update_client_message_timestamp()            │
│   1. Actualiza metadata (client_last_msg)                       │
│   2. Actualiza ZSET score con timestamp actual  ← NUEVO         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ GET /contacts (Panel solicita lista)                            │
│   1. Lee ZSET ordenado por score (más reciente primero)         │
│   2. Enriquece con metadata de cada contacto                    │
│   3. NO reordena (preserva orden ZSET)  ← FIX                   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Panel muestra contactos en orden correcto                       │
│ (actividad más reciente arriba)                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 10. Conclusiones

### Problemas Resueltos

- ✅ TTL dinámico para fines de semana
- ✅ Ordenamiento correcto por actividad reciente
- ✅ Rate limiting para HubSpot API
- ✅ Logs visibles en producción

### Lecciones Aprendidas

1. El orden de un ZSET debe preservarse en todo el pipeline
2. Un `sorted()` en Python puede destruir el orden correcto de Redis
3. Los logs deben ser INFO o superior para verse en producción
4. El rate limiting es esencial para APIs externas

### Estado Final

El Panel de Asesores v3.0 está operativo con todos los fixes aplicados y desplegado en Railway branch `FaseFinalDetalles`.

---

_Documento generado: 1 de Marzo de 2026_
