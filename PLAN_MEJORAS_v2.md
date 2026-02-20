# PLAN DE MEJORAS - Panel de Asesores v2.0

## Resumen Ejecutivo

| # | Mejora | Estado Actual | Complejidad | Prioridad |
|---|--------|---------------|-------------|-----------|
| 1 | Templates multiples | Solo 1 template hardcodeado | Media | Alta |
| 2 | Workflows + Estados visuales (En espera - En conversacion) | Solo "En espera" | Media-Alta | Alta |
| 3 | Templates de citas | No existe | Media | Alta |
| 4 | Template seguimiento automatico 24h | Script manual existe | Media | Media |
| 5 | Foto de perfil | No existe (solo iniciales) | Baja-Media | Baja |
| 6 | Importar notas manuales de HubSpot | Parcial (se muestran como "Sistema") | Baja | Media |
| 7 | Verificar filtros 24h/48h/1 semana | Funciona pero sin paginacion | Baja | Media |
| 8 | Editar nombre en panel | No existe | Baja | Media |
| 9 | Verificar modulo redes sociales | 75% funcional | Media | Alta |

---

# FASE 1: Sistema de Templates Multiples

## Estado Actual

**Archivo:** `middleware/outbound_panel.py` (lineas 306-387)

Actualmente existe UN SOLO template hardcodeado:
```python
template_message = (
    "Hola! Soy del equipo de Inmobiliaria Proteger. "
    "Sigues interesado/a en nuestros servicios inmobiliarios? "
    "Estamos aqui para ayudarte."
)
```

## Plan de Implementacion

### 1.1 Crear archivo de configuracion de templates

**Nuevo archivo:** `config/whatsapp_templates.py`

```python
from enum import Enum
from dataclasses import dataclass
from typing import List, Optional

class TemplateCategory(str, Enum):
    REACTIVACION = "reactivacion"
    CITA = "cita"
    SEGUIMIENTO = "seguimiento"
    RECORDATORIO = "recordatorio"
    PROMOCION = "promocion"

@dataclass
class WhatsAppTemplate:
    id: str
    name: str
    category: TemplateCategory
    body: str
    variables: List[str]  # ["nombre", "fecha", "hora"]
    twilio_content_sid: Optional[str] = None  # Para templates oficiales de Twilio

TEMPLATES = {
    "reactivacion_general": WhatsAppTemplate(
        id="reactivacion_general",
        name="Reactivacion General",
        category=TemplateCategory.REACTIVACION,
        body="Hola {nombre}! Soy del equipo de Inmobiliaria Proteger. Sigues interesado/a en nuestros servicios inmobiliarios?",
        variables=["nombre"]
    ),
    "cita_confirmacion": WhatsAppTemplate(
        id="cita_confirmacion",
        name="Confirmacion de Cita",
        category=TemplateCategory.CITA,
        body="Hola {nombre}! Te confirmamos tu cita para el {fecha} a las {hora}. Te esperamos en {direccion}. Nos confirmas tu asistencia?",
        variables=["nombre", "fecha", "hora", "direccion"]
    ),
    "cita_recordatorio": WhatsAppTemplate(
        id="cita_recordatorio",
        name="Recordatorio de Cita",
        category=TemplateCategory.CITA,
        body="Hola {nombre}! Te recordamos que manana {fecha} tienes cita a las {hora}. Te esperamos!",
        variables=["nombre", "fecha", "hora"]
    ),
    "seguimiento_visita": WhatsAppTemplate(
        id="seguimiento_visita",
        name="Seguimiento Post-Visita",
        category=TemplateCategory.SEGUIMIENTO,
        body="Hola {nombre}! Esperamos que la visita al inmueble haya sido de tu agrado. Te gustaria agendar otra visita o tienes alguna pregunta?",
        variables=["nombre"]
    ),
    "seguimiento_24h": WhatsAppTemplate(
        id="seguimiento_24h",
        name="Seguimiento 24 horas",
        category=TemplateCategory.SEGUIMIENTO,
        body="Hola {nombre}! Pudiste revisar la informacion que te enviamos? Estamos aqui para resolver cualquier duda.",
        variables=["nombre"]
    ),
}
```

### 1.2 Modificar endpoint /send-template

**Archivo:** `middleware/outbound_panel.py`

```python
@router.post("/send-template")
async def send_template_message(
    background_tasks: BackgroundTasks,
    to: str = Form(...),
    template_id: str = Form(...),  # NUEVO: ID del template
    variables: str = Form("{}"),   # NUEVO: JSON con variables
    contact_id: Optional[str] = Form(None),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Envia un template especifico."""
    from config.whatsapp_templates import TEMPLATES
    import json

    template = TEMPLATES.get(template_id)
    if not template:
        raise HTTPException(status_code=400, detail=f"Template '{template_id}' no existe")

    # Parsear variables
    vars_dict = json.loads(variables)

    # Reemplazar variables en el body
    message = template.body.format(**vars_dict)

    # Enviar...
```

### 1.3 Agregar selector de templates en UI

**Archivo:** `middleware/outbound_panel.py` (HTML del panel)

Agregar dropdown con categorias:
- Reactivacion
- Citas
- Seguimiento
- Recordatorios

### 1.4 Archivos a modificar

| Archivo | Cambio |
|---------|--------|
| `config/whatsapp_templates.py` | **CREAR** - Definicion de templates |
| `middleware/outbound_panel.py` | Modificar `/send-template`, agregar UI |
| `requirements.txt` | Sin cambios necesarios |

---

# FASE 2: Estados Visuales y Workflows

## Estado Actual

**Estados existentes en Redis:**
```python
class ConversationStatus(Enum):
    BOT_ACTIVE = "BOT_ACTIVE"           # Sofia responde
    HUMAN_ACTIVE = "HUMAN_ACTIVE"       # Asesor intervino (TTL 2h)
    PENDING_HANDOFF = "PENDING_HANDOFF" # Esperando handoff
    CLOSED = "CLOSED"                   # Conversacion cerrada
```

**Visual actual:**
- `HUMAN_ACTIVE` -> Badge verde "En espera" (pulsante)
- `BOT_ACTIVE` -> Badge gris "Bot"
- Historico -> Badge gris "Historial"

## Plan de Implementacion

### 2.1 Agregar nuevo estado: IN_CONVERSATION

**Archivo:** `middleware/conversation_state.py`

```python
class ConversationStatus(str, Enum):
    BOT_ACTIVE = "BOT_ACTIVE"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"       # Esperando que asesora atienda
    IN_CONVERSATION = "IN_CONVERSATION" # NUEVO: Asesora esta chateando
    PENDING_HANDOFF = "PENDING_HANDOFF"
    CLOSED = "CLOSED"
```

### 2.2 Transicion automatica de estados

```
+------------------+     Cliente escribe    +------------------+
|   BOT_ACTIVE     | ---------------------->|  HUMAN_ACTIVE    |
|   (Sofia)        |     (handoff)          |  "En espera"     |
+------------------+                        |  Verde           |
                                            +---------+--------+
                                                      |
                                       Asesora envia mensaje
                                                      |
                                                      v
                                            +------------------+
                                            | IN_CONVERSATION  |
                                            | "En conversacion"|
                                            |  Azul            |
                                            +---------+--------+
                                                      |
                                       2h sin actividad (TTL)
                                                      |
                                                      v
                                            +------------------+
                                            |   BOT_ACTIVE     |
                                            |   (Sofia)        |
                                            +------------------+
```

### 2.3 Modificar /send-message para cambiar estado

**Archivo:** `middleware/outbound_panel.py`

```python
@router.post("/send-message")
async def send_message(...):
    # ... codigo existente ...

    # Cambiar estado de HUMAN_ACTIVE a IN_CONVERSATION
    try:
        current_status = await state_manager.get_status(phone_normalized)
        if current_status == ConversationStatus.HUMAN_ACTIVE:
            await state_manager.set_status(
                phone_normalized,
                ConversationStatus.IN_CONVERSATION,
                ttl=state_manager.HANDOFF_TTL_SECONDS
            )
            logger.info(f"[Panel] Estado cambiado a IN_CONVERSATION para {phone_normalized}")
    except Exception as e:
        logger.warning(f"[Panel] Error cambiando estado: {e}")
```

### 2.4 Colores y badges en UI

| Estado | Badge | Color | Icono |
|--------|-------|-------|-------|
| HUMAN_ACTIVE | "En espera" | Verde (#10B981) | pulsante |
| IN_CONVERSATION | "En conversacion" | Azul (#3B82F6) | chat |
| BOT_ACTIVE | "Bot" | Gris (#6B7280) | robot |
| Historico | "Historial" | Gris claro (#9CA3AF) | clipboard |

### 2.5 Archivos a modificar

| Archivo | Cambio |
|---------|--------|
| `middleware/conversation_state.py` | Agregar `IN_CONVERSATION` |
| `middleware/outbound_panel.py` | Cambiar estado al enviar mensaje + UI |

---

# FASE 3: Templates de Citas

## Estado Actual

Sofia detecta intencion de cita via `intencion_visita: true` en el analisis, pero no hay templates especificos para gestion de citas.

## Plan de Implementacion

### 3.1 Templates de citas (ya incluidos en FASE 1)

```python
"cita_confirmacion": "Te confirmamos tu cita para el {fecha} a las {hora}..."
"cita_recordatorio": "Te recordamos que manana tienes cita a las {hora}..."
"cita_cancelacion": "Lamentamos informarte que la cita del {fecha} ha sido cancelada..."
"cita_reagendar": "Te gustaria reagendar tu cita? Tenemos disponibilidad en..."
```

### 3.2 Agregar propiedades de cita en HubSpot

**Propiedades a crear en HubSpot:**
- `chatbot_cita_fecha` (date)
- `chatbot_cita_hora` (string)
- `chatbot_cita_estado` (enum: pendiente, confirmada, cancelada, completada)
- `chatbot_cita_direccion` (string)

### 3.3 Integrar con calendario (FUTURO)

- Posible integracion con Google Calendar o HubSpot Meetings
- Envio automatico de recordatorio 24h antes

---

# FASE 4: Template de Seguimiento Automatico 24h

## Estado Actual

**Existe script manual:** `scripts/follow_up_scheduler.py`

```python
FOLLOWUP_MESSAGE_TEMPLATE = """Hola {nombre}!
Esperamos que la visita al inmueble haya sido de tu agrado...
"""
```

**Problema:** Se ejecuta manualmente, no hay automatizacion.

## Plan de Implementacion

### 4.1 Opcion A: Cron Job en Railway

**Crear archivo:** `scripts/cron_followup.py`

```python
# Ejecutar cada hora via Railway Cron Jobs
# https://docs.railway.app/reference/cron-jobs

async def check_and_send_followups():
    """
    1. Buscar contactos donde last_message_time > 24h
    2. Verificar que no tienen followup reciente
    3. Enviar template de seguimiento
    """
    redis_client = await get_redis()

    # Buscar keys de ventana 24h
    async for key in redis_client.scan_iter("last_client_msg:*"):
        phone = key.replace("last_client_msg:", "")
        last_msg = await redis_client.get(key)

        if is_older_than_24h(last_msg):
            # Verificar si ya enviamos followup
            followup_key = f"followup_sent:{phone}"
            if not await redis_client.exists(followup_key):
                await send_followup_template(phone)
                await redis_client.set(followup_key, "1", ex=7*24*60*60)  # TTL 7 dias
```

### 4.2 Opcion B: APScheduler en app.py

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler()

@app.on_event("startup")
async def start_scheduler():
    scheduler.add_job(check_and_send_followups, 'interval', hours=1)
    scheduler.start()
```

### 4.3 Variables de entorno

```env
FOLLOWUP_ENABLED=true
FOLLOWUP_DELAY_HOURS=24
FOLLOWUP_TEMPLATE_ID=seguimiento_24h
```

### 4.4 Archivos a modificar

| Archivo | Cambio |
|---------|--------|
| `scripts/follow_up_scheduler.py` | Refactorizar para usar templates |
| `app.py` | Agregar APScheduler (opcion B) |
| `requirements.txt` | Agregar `APScheduler>=3.10.0` |

---

# FASE 5: Foto de Perfil

## Estado Actual

**NO se importan fotos.** El panel muestra avatares de iniciales:
```html
<div class="w-10 h-10 bg-green-500 rounded-full">
    {{ displayName.charAt(0).toUpperCase() }}
</div>
```

## Investigacion

### Fuentes posibles de fotos:

| Fuente | Disponible | Notas |
|--------|------------|-------|
| **Twilio WhatsApp** | NO | Twilio no proporciona foto de perfil |
| **HubSpot** | Parcial | Solo si se sube manualmente al contacto |
| **Gravatar** | SI | Basado en email (si existe) |
| **UI Avatars** | SI | Genera avatares bonitos por nombre |

### Recomendacion: UI Avatars + HubSpot fallback

```python
def get_avatar_url(contact):
    # 1. Intentar HubSpot (propiedad hs_avatar_filemanager_key)
    if contact.get("avatar_url"):
        return contact["avatar_url"]

    # 2. Fallback: UI Avatars (servicio gratuito)
    name = contact.get("display_name", "?")
    return f"https://ui-avatars.com/api/?name={name}&background=random&color=fff&size=128"
```

## Plan de Implementacion

### 5.1 Usar UI Avatars (servicio externo gratuito)

**Modificar UI del panel:**
```html
<img
    src="https://ui-avatars.com/api/?name={{displayName}}&background=10B981&color=fff&size=40&rounded=true"
    class="w-10 h-10 rounded-full"
    alt="Avatar"
>
```

### 5.2 Archivos a modificar

| Archivo | Cambio |
|---------|--------|
| `middleware/outbound_panel.py` | Cambiar avatar de iniciales a img con UI Avatars |

**Complejidad:** Baja (solo cambio de HTML)

---

# FASE 6: Importar Notas Manuales de HubSpot

## Estado Actual

**Las notas SI se importan**, pero las manuales (sin prefijo) se clasifican como "Sistema".

**Archivo:** `integrations/hubspot/timeline_logger.py` (lineas 589-607)

```python
if "telefono" in body_prefix or "cliente" in body_prefix:
    sender = "client"
elif "robot" in body_prefix or "sofia" in body_prefix:
    sender = "bot"
elif "persona" in body_prefix or "asesor" in body_prefix:
    sender = "advisor"
else:
    sender = "system"  # <- Notas manuales caen aqui
    sender_name = "Sistema"
```

## Plan de Implementacion

### 6.1 Mejorar deteccion de notas manuales

```python
# Agregar deteccion de notas manuales de HubSpot
elif "nota" in body_prefix or not any(emoji in body for emoji in ["telefono", "robot", "persona"]):
    # Si no tiene emoji del chatbot, es nota manual
    sender = "manual_note"
    sender_name = "Nota HubSpot"
    align = "left"
```

### 6.2 Agregar estilo visual diferenciado

```css
.bubble-manual-note {
    background: #FEF3C7;  /* Amarillo claro */
    border-left: 3px solid #F59E0B;  /* Borde naranja */
}
```

### 6.3 Archivos a modificar

| Archivo | Cambio |
|---------|--------|
| `integrations/hubspot/timeline_logger.py` | Mejorar `_format_notes_as_chat` |
| `middleware/outbound_panel.py` | Agregar estilo para notas manuales |

---

# FASE 7: Verificar Filtros de Tiempo

## Estado Actual

**Codigo de filtros (lineas 707-721):**
```python
if filter_time == "24h":
    since = now - timedelta(hours=24)
elif filter_time == "48h":
    since = now - timedelta(hours=48)
elif filter_time == "1week":
    since = now - timedelta(weeks=1)
```

**Problemas identificados:**

1. **Sin paginacion** - Limite de 100 contactos en HubSpot
2. **Filtro custom** - No se pasa correctamente a HubSpot

## Plan de Implementacion

### 7.1 Agregar paginacion

```python
async def get_contacts_with_advisor_activity(
    since: datetime,
    until: datetime,
    limit: int = 100,
    after: str = None  # NUEVO: cursor de paginacion
):
    payload = {
        "filterGroups": [...],
        "limit": limit,
    }
    if after:
        payload["after"] = after

    # ... hacer request ...

    return {
        "contacts": results,
        "paging": {
            "next_after": data.get("paging", {}).get("next", {}).get("after")
        }
    }
```

### 7.2 Arreglar filtro custom

```python
# Asegurar que since/until se usen en la busqueda HubSpot
since_ms = int(since.timestamp() * 1000)
until_ms = int(until.timestamp() * 1000)

filters = [
    {"propertyName": "createdate", "operator": "GTE", "value": since_ms},
    {"propertyName": "createdate", "operator": "LTE", "value": until_ms},
]
```

### 7.3 Archivos a modificar

| Archivo | Cambio |
|---------|--------|
| `integrations/hubspot/timeline_logger.py` | Agregar paginacion |
| `middleware/outbound_panel.py` | Usar paginacion en `/contacts` |

---

# FASE 8: Editar Nombre en Panel

## Estado Actual

**NO existe funcionalidad de edicion.** El nombre se obtiene READ-ONLY de HubSpot.

## Plan de Implementacion

### 8.1 Crear endpoint de actualizacion

**Archivo:** `middleware/outbound_panel.py`

```python
@router.patch("/contacts/{contact_id}/name")
async def update_contact_name(
    contact_id: str,
    firstname: str = Form(...),
    lastname: str = Form(""),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Actualiza el nombre del contacto en HubSpot."""
    if not _validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key invalida")

    import httpx

    url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
    payload = {
        "properties": {
            "firstname": firstname,
            "lastname": lastname
        }
    }

    async with httpx.AsyncClient() as client:
        response = await client.patch(
            url,
            headers={"Authorization": f"Bearer {os.getenv('HUBSPOT_API_KEY')}"},
            json=payload,
            timeout=10.0
        )

        if response.status_code == 200:
            return {"status": "success", "message": "Nombre actualizado"}
        else:
            raise HTTPException(status_code=response.status_code, detail=response.text)
```

### 8.2 Agregar UI de edicion

```html
<!-- Boton de editar junto al nombre -->
<button onclick="editName('{{contact_id}}', '{{firstname}}', '{{lastname}}')"
        class="text-gray-400 hover:text-gray-600">
    Editar
</button>

<!-- Modal de edicion -->
<div id="editNameModal" class="hidden fixed inset-0 bg-black bg-opacity-50">
    <div class="bg-white p-6 rounded-lg">
        <input id="editFirstname" placeholder="Nombre">
        <input id="editLastname" placeholder="Apellido">
        <button onclick="saveNameChange()">Guardar</button>
    </div>
</div>
```

### 8.3 Archivos a modificar

| Archivo | Cambio |
|---------|--------|
| `middleware/outbound_panel.py` | Agregar endpoint PATCH + UI modal |

---

# FASE 9: Verificar Modulo de Redes Sociales

## Estado Actual

**Dashboard:** `/whatsapp/panel/metrics/` - 75% funcional

**Problemas identificados:**

1. Sin validacion de errores HubSpot (retorna vacio silenciosamente)
2. Sin paginacion (maximo 100 contactos)
3. Filtro de fechas custom no funciona en busqueda HubSpot
4. Sin exportacion CSV/PDF

## Plan de Implementacion

### 9.1 Agregar manejo de errores

```python
if response.status_code != 200:
    logger.error(f"[Metrics] HubSpot error: {response.status_code} - {response.text}")
    raise HTTPException(
        status_code=503,
        detail=f"Error consultando HubSpot: {response.status_code}"
    )
```

### 9.2 Agregar exportacion CSV

```python
@router.get("/metrics/export")
async def export_metrics_csv(
    days: int = Query(7),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Exporta metricas a CSV."""
    import csv
    from io import StringIO

    data = await get_social_media_metrics(days, x_api_key)

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Canal", "Leads", "Porcentaje"])

    total = data["total_leads"]
    for canal, count in data["leads_by_channel"].items():
        pct = (count / total * 100) if total > 0 else 0
        writer.writerow([canal, count, f"{pct:.1f}%"])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=metricas_{days}d.csv"}
    )
```

### 9.3 Archivos a modificar

| Archivo | Cambio |
|---------|--------|
| `middleware/outbound_panel.py` | Agregar manejo errores + exportacion CSV |
| `integrations/hubspot/timeline_logger.py` | Agregar paginacion a busquedas |

---

# Orden de Implementacion Recomendado

## Sprint 1: Core (Alta prioridad)

| Orden | Fase | Descripcion | Archivos |
|-------|------|-------------|----------|
| 1 | FASE 2 | Estados visuales (En espera - En conversacion) | conversation_state.py, outbound_panel.py |
| 2 | FASE 1 | Sistema de templates multiples | config/whatsapp_templates.py, outbound_panel.py |
| 3 | FASE 9 | Arreglar modulo redes sociales | outbound_panel.py |

## Sprint 2: Mejoras (Media prioridad)

| Orden | Fase | Descripcion | Archivos |
|-------|------|-------------|----------|
| 4 | FASE 4 | Template seguimiento automatico 24h | app.py, scripts/follow_up_scheduler.py |
| 5 | FASE 6 | Mejorar importacion de notas | timeline_logger.py |
| 6 | FASE 7 | Arreglar filtros de tiempo | timeline_logger.py, outbound_panel.py |

## Sprint 3: Nice-to-have (Baja prioridad)

| Orden | Fase | Descripcion | Archivos |
|-------|------|-------------|----------|
| 7 | FASE 8 | Editar nombre en panel | outbound_panel.py |
| 8 | FASE 5 | Fotos de perfil (UI Avatars) | outbound_panel.py |
| 9 | FASE 3 | Templates de citas (extension de FASE 1) | whatsapp_templates.py |

---

# Verificacion

## Tests manuales por fase

| Fase | Test | Resultado Esperado |
|------|------|-------------------|
| 1 | Seleccionar template - Enviar | Mensaje con variables reemplazadas |
| 2 | Asesora envia mensaje | Badge cambia de verde a azul |
| 3 | Enviar template de cita | Variables {fecha}, {hora} se reemplazan |
| 4 | Esperar 24h sin respuesta | Cliente recibe template automatico |
| 5 | Ver lista de contactos | Avatares con colores por nombre |
| 6 | Crear nota manual en HubSpot | Aparece con estilo amarillo en panel |
| 7 | Filtrar por "Ultima semana" | Muestra contactos correctos |
| 8 | Editar nombre - Guardar | Nombre actualizado en HubSpot y panel |
| 9 | Abrir dashboard metricas | Graficos cargan sin errores |

---

# Dependencias Nuevas

```txt
# requirements.txt (agregar)
APScheduler>=3.10.0  # Para seguimiento automatico
```

---

# Variables de Entorno Nuevas

```env
# Seguimiento automatico
FOLLOWUP_ENABLED=true
FOLLOWUP_DELAY_HOURS=24
FOLLOWUP_TEMPLATE_ID=seguimiento_24h

# Ya existentes (verificar que esten)
HUBSPOT_API_KEY=xxx
ADMIN_API_KEY=xxx
REDIS_URL=xxx
```

---

# PLANES ANTERIORES (Referencia)

## Plan: Fix Bug - CRMAgent pide datos despues de link

**Estado:** PENDIENTE

Agregar en `agents/CRMAgent/crm_agent.py:270`:
```python
state.metadata["link_procesado"] = True
```

---

## Plan: Soporte de Audios en WhatsApp

**Estado:** PENDIENTE

- Transcripcion con OpenAI Whisper (~$9/mes)
- Almacenamiento con Cloudinary (gratis)
- Ver detalles en plan original (archivo de plan)