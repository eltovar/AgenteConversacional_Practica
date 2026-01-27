# 📊 Configuración del Sistema de Seguimiento Automático de Etapas de Deals

**Fecha:** 27 de enero de 2026
**Versión:** 1.0
**Propósito:** Guía completa para configurar el seguimiento automático de etapas de deals en HubSpot

---

## 📋 ÍNDICE

1. [Resumen Ejecutivo](#1-resumen-ejecutivo)
2. [Prerequisitos](#2-prerequisitos)
3. [Obtener IDs de Etapas desde HubSpot](#3-obtener-ids-de-etapas-desde-hubspot)
4. [Configurar deal_tracker.py](#4-configurar-deal_trackerpy)
5. [Configurar Cron Job o Workflow](#5-configurar-cron-job-o-workflow)
6. [Testing](#6-testing)
7. [Troubleshooting](#7-troubleshooting)

---

## 1. RESUMEN EJECUTIVO

### ¿Qué hace este sistema?

Actualiza automáticamente la etapa de los Deals en HubSpot basándose en la actividad detectada:

| Etapa Actual | Condición | Nueva Etapa |
|--------------|-----------|-------------|
| Nuevo Lead | Trabajador responde (Call/Email/Note en últimas 24h) | En Conversación |
| En Conversación | Se detecta mención de "visita" o "agendar" | Visita Agendada |

### Componentes implementados

✅ **Código base:** `integrations/hubspot/deal_tracker.py` - Lógica de seguimiento
✅ **Endpoint admin:** `POST /admin/update-deal-stages` - Trigger manual
✅ **Configuración:** IDs de owners actualizados en `lead_assigner.py`
✅ **Campo canal_origen:** Agregado a `contact_properties` en `crm_agent.py`

---

## 2. PREREQUISITOS

### Variables de entorno requeridas

Verifica que estén configuradas en tu `.env`:

```env
# HubSpot API
HUBSPOT_API_KEY=pat-na1-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
HUBSPOT_PIPELINE_ID=default
HUBSPOT_DEAL_STAGE=appointmentscheduled  # ID de la etapa inicial "Nuevo Lead"

# Admin API (para endpoints protegidos)
ADMIN_API_KEY=tu-clave-secreta-aqui
```

### Propiedades personalizadas en HubSpot

Asegúrate de haber creado estas propiedades custom:

**En Contactos:**
- ✅ `canal_origen` (Dropdown select)
- ✅ `chatbot_email` (Single-line text)
- ✅ `chatbot_urgency` (Single-line text)
- ✅ `whatsapp_id` (Single-line text) - Para deduplicación
- ✅ `chatbot_timestamp` (Date picker)
- ✅ `chatbot_score` (Number)
- ✅ Otras propiedades del chatbot

**En Deals:**
- ✅ `chatbot_property_type` (Single-line text)
- ✅ `chatbot_location` (Single-line text)
- ✅ `chatbot_budget` (Number)
- ✅ `chatbot_score` (Number)
- ✅ `chatbot_urgency` (Single-line text)

---

## 3. OBTENER IDS DE ETAPAS DESDE HUBSPOT

### Opción A: Desde la UI de HubSpot (Más fácil)

1. **Navegar a configuración de pipeline:**
   ```
   Settings → Objects → Deals → Pipelines
   ```

2. **Seleccionar tu pipeline** (probablemente "Sales Pipeline" o "default")

3. **Copiar IDs de cada etapa:**
   - Haz clic en una etapa
   - El ID aparece en la URL: `https://app.hubspot.com/.../.../pipelines/{PIPELINE_ID}/stages/{STAGE_ID}`
   - Alternativamente, usa las Developer Tools del navegador para inspeccionar el HTML

4. **Crear un mapeo de etapas:**
   ```
   Nuevo Lead → 1275156339
   En Conversación → 1275156340
   Visita Agendada → 1275156341
   Visita Realizada → 1275156342
   Propuesta Enviada → 1275156343
   Negociación → 1275156344
   Cerrado Ganado → closedwon
   Cerrado Perdido → closedlost
   ```

### Opción B: Usando la API de HubSpot (Más preciso)

```bash
# Obtener todos los pipelines
curl -X GET "https://api.hubapi.com/crm/v3/pipelines/deals" \
  -H "Authorization: Bearer YOUR_HUBSPOT_API_KEY"
```

**Respuesta esperada:**
```json
{
  "results": [
    {
      "id": "default",
      "label": "Sales Pipeline",
      "stages": [
        {
          "id": "1275156339",
          "label": "Nuevo Lead",
          "displayOrder": 0
        },
        {
          "id": "1275156340",
          "label": "En Conversación",
          "displayOrder": 1
        },
        ...
      ]
    }
  ]
}
```

---

## 4. CONFIGURAR `deal_tracker.py`

### Paso 1: Actualizar IDs de etapas

Abre el archivo `integrations/hubspot/deal_tracker.py` y actualiza la sección `STAGE_IDS`:

```python
STAGE_IDS = {
    "nuevo_lead": "1275156339",  # ← Reemplazar con ID real de HubSpot
    "en_conversacion": "1275156340",  # ← Reemplazar con ID real
    "visita_agendada": "1275156341",  # ← Reemplazar con ID real
    "visita_realizada": "1275156342",  # ← Reemplazar con ID real
    "propuesta_enviada": "1275156343",  # ← Reemplazar con ID real
    "negociacion": "1275156344",  # ← Reemplazar con ID real
    "ganado": "closedwon",  # ✅ ID estándar de HubSpot
    "perdido": "closedlost",  # ✅ ID estándar de HubSpot
}
```

### Paso 2: Personalizar palabras clave de visita (opcional)

Si tu equipo usa términos diferentes para agendar visitas:

```python
VISIT_KEYWORDS = [
    "visita", "agendar", "agendada", "cita", "reunión",
    "ver el inmueble", "conocer la propiedad", "mostrar",
    # Agregar tus propios términos aquí
]
```

---

## 5. CONFIGURAR CRON JOB O WORKFLOW

Tienes 3 opciones para ejecutar el tracker automáticamente:

### Opción A: Cron Job Externo (Railway Cron - Recomendado)

**Crear archivo `railway.json` en la raíz del proyecto:**

```json
{
  "$schema": "https://railway.app/railway.schema.json",
  "build": {
    "builder": "NIXPACKS"
  },
  "deploy": {
    "startCommand": "uvicorn app:app --host 0.0.0.0 --port $PORT",
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 10
  },
  "crons": [
    {
      "name": "update-deal-stages",
      "schedule": "*/30 * * * *",
      "command": "curl -X POST https://tu-app.up.railway.app/admin/update-deal-stages -H 'X-API-Key: ${ADMIN_API_KEY}'"
    }
  ]
}
```

**Explicación:**
- `*/30 * * * *` = Cada 30 minutos
- El comando hace un POST al endpoint admin
- Railway ejecuta automáticamente el cron job

### Opción B: Servicio Externo (cron-job.org)

1. **Ir a:** [cron-job.org](https://cron-job.org)
2. **Crear cuenta gratuita**
3. **Configurar nuevo cronjob:**
   - **URL:** `https://tu-app.up.railway.app/admin/update-deal-stages`
   - **Method:** POST
   - **Headers:** `X-API-Key: tu-admin-api-key`
   - **Schedule:** Every 30 minutes
   - **Failure notifications:** Email (recomendado)

### Opción C: HubSpot Workflow (Trigger por actividad)

**Crear workflow en HubSpot:**

1. **Ir a:** Automation → Workflows → Create workflow → Deal-based
2. **Trigger:**
   ```
   Deal property "dealstage" is "Nuevo Lead"
   AND
   Associated contact has had activity in the last 1 day
   ```
3. **Action:** Webhook
   ```
   URL: https://tu-app.up.railway.app/admin/update-deal-stages
   Method: POST
   Headers: X-API-Key: ${ADMIN_API_KEY}
   Body:
   {
     "deal_id": "{{dealId}}",
     "contact_id": "{{associatedContactId}}"
   }
   ```

---

## 6. TESTING

### Test Manual desde la Terminal

```bash
# Test básico (sin parámetros - verificar endpoint)
curl -X POST https://tu-app.up.railway.app/admin/update-deal-stages \
  -H "X-API-Key: tu-admin-api-key"

# Test con deal específico
curl -X POST https://tu-app.up.railway.app/admin/update-deal-stages \
  -H "X-API-Key: tu-admin-api-key" \
  -H "Content-Type: application/json" \
  -d '{"deal_id": "123456789", "contact_id": "987654321"}'
```

### Test desde Postman

**Request:**
```
POST https://tu-app.up.railway.app/admin/update-deal-stages
Headers:
  X-API-Key: tu-admin-api-key
  Content-Type: application/json
Body (raw JSON):
{
  "deal_id": "DEAL_ID_FROM_HUBSPOT",
  "contact_id": "CONTACT_ID_FROM_HUBSPOT"
}
```

**Respuesta esperada (éxito):**
```json
{
  "status": "success",
  "message": "Deal 123456789 actualizado",
  "new_stage": "1275156340",
  "deal_id": "123456789"
}
```

**Respuesta esperada (sin cambios):**
```json
{
  "status": "no_change",
  "message": "Deal 123456789 no requiere actualización",
  "deal_id": "123456789"
}
```

### Test de Flujo Completo

1. **Crear un lead de prueba via chatbot:**
   - Envía mensaje a tu WhatsApp de Twilio
   - Sigue el flujo hasta registrar nombre
   - Anota el `deal_id` y `contact_id` del log

2. **Agregar actividad en HubSpot:**
   - Ve al contacto en HubSpot
   - Agrega una Nota, Email o Call
   - Guarda

3. **Ejecutar el tracker:**
   ```bash
   curl -X POST https://tu-app.up.railway.app/admin/update-deal-stages \
     -H "X-API-Key: tu-admin-api-key" \
     -H "Content-Type: application/json" \
     -d '{"deal_id": "TU_DEAL_ID", "contact_id": "TU_CONTACT_ID"}'
   ```

4. **Verificar en HubSpot:**
   - El Deal debe haber cambiado de "Nuevo Lead" a "En Conversación"

---

## 7. TROUBLESHOOTING

### Error: 401 Unauthorized

**Problema:** La API Key admin es incorrecta

**Solución:**
```bash
# Verificar que ADMIN_API_KEY esté configurada
echo $ADMIN_API_KEY

# Regenerar key segura
openssl rand -hex 32

# Actualizar en Railway:
# Settings → Variables → ADMIN_API_KEY → tu-nueva-key
```

### Error: 404 Not Found on HubSpot API

**Problema:** Los IDs de etapas en `STAGE_IDS` son incorrectos

**Solución:**
1. Obtener IDs correctos usando la API (ver sección 3)
2. Actualizar `deal_tracker.py`
3. Reiniciar el servidor

### Deal no se actualiza a "En Conversación"

**Posibles causas:**

1. **No hay actividad reciente:**
   - Verificar que haya Call/Email/Note en las últimas 24h
   - Revisar logs: `[DealStageTracker] Contacto X: 0 actividades encontradas`

2. **Deal ya está en otra etapa:**
   - El tracker solo actualiza deals en "Nuevo Lead"
   - Verificar etapa actual del deal en HubSpot

3. **Error de permisos en HubSpot:**
   - Verificar que el API Key tenga permisos de escritura en Deals
   - Settings → Integrations → Private Apps → Scopes → `crm.objects.deals.write`

### Logs útiles para debugging

```bash
# Ver logs en Railway
railway logs --tail

# Buscar logs del tracker
railway logs --tail | grep "DealStageTracker"

# Ver último error
railway logs --tail | grep "ERROR"
```

---

## 📊 RESUMEN DE CAMBIOS IMPLEMENTADOS

| Archivo | Cambios | Estado |
|---------|---------|--------|
| `integrations/hubspot/lead_assigner.py` | IDs de trabajadores actualizados | ✅ Completado |
| `agents/CRMAgent/crm_agent.py` | Agregado `canal_origen` a contact_properties | ✅ Completado |
| `integrations/hubspot/deal_tracker.py` | Creado sistema de seguimiento | ✅ Completado |
| `app.py` | Agregado endpoint `/admin/update-deal-stages` | ✅ Completado |
| **HubSpot UI** | Crear propiedad `canal_origen` | ⚠️ Pendiente manual |
| **HubSpot UI** | Obtener IDs de etapas | ⚠️ Pendiente manual |
| **Cron Job** | Configurar ejecución automática | ⚠️ Pendiente manual |

---

## 🚀 PRÓXIMOS PASOS

1. **Obtener IDs de etapas** desde HubSpot (sección 3)
2. **Actualizar `deal_tracker.py`** con IDs reales (sección 4)
3. **Configurar cron job** para ejecución automática (sección 5)
4. **Ejecutar test completo** (sección 6)
5. **Monitorear logs** durante las primeras 24h

---

**¿Necesitas ayuda?** Revisa los logs en Railway con `railway logs --tail` o contacta al equipo de desarrollo.
