# INFORME TÉCNICO: Análisis de Problemas de Audio y Mensajes No Entregados

**Fecha:** 2026-02-27  
**Versión:** 1.0  
**Autor:** Sistema de Análisis de Código  
**Severidad:** CRÍTICA

---

## RESUMEN EJECUTIVO

Se identificaron **tres problemas críticos** en el sistema:

| #   | Problema                                  | Impacto                                | Severidad  |
| --- | ----------------------------------------- | -------------------------------------- | ---------- |
| 1   | Audio del Panel no llega al cliente       | Asesores no pueden enviar notas de voz | 🔴 CRÍTICO |
| 2   | Respuesta de Sofia no entregada (timeout) | Clientes no reciben respuesta del bot  | 🔴 CRÍTICO |
| 3   | URL de Bunny.net sin protocolo HTTPS      | Twilio rechaza multimedia              | 🔴 CRÍTICO |

---

## 1️⃣ DIAGNÓSTICO TÉCNICO

### 1.1 Problema de Audio del Panel → Cliente (Error 400)

#### Flujo Esperado vs Flujo Real

```
┌─────────────────────────────────────────────────────────────────┐
│                       FLUJO ESPERADO                            │
├─────────────────────────────────────────────────────────────────┤
│ 1. Asesor graba audio en Panel (WebM/navegador)                 │
│ 2. Panel sube archivo a Bunny.net Storage                       │
│ 3. Bunny retorna URL: https://inmobiliaria-media.b-cdn.net/...  │
│ 4. Panel envía mensaje via Twilio con MediaUrl                  │
│ 5. Twilio descarga audio y lo envía al cliente                  │
│ 6. Cliente recibe audio en WhatsApp                             │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                       FLUJO REAL (CON ERROR)                    │
├─────────────────────────────────────────────────────────────────┤
│ 1. ✅ Asesor graba audio en Panel (WebM/navegador)              │
│ 2. ✅ Panel sube archivo a Bunny.net Storage                    │
│ 3. ❌ Bunny retorna URL SIN PROTOCOLO:                          │
│       inmobiliaria-media.b-cdn.net/audios_asesores/...          │
│ 4. ❌ Twilio rechaza URL (Error 21620: Invalid media URL)       │
│ 5. ❌ Panel retorna HTTP 500 Internal Server Error              │
│ 6. ❌ Cliente NO recibe audio                                   │
└─────────────────────────────────────────────────────────────────┘
```

#### Evidencia en Logs

```log
2026-02-27T19:08:44 - [Panel] 📁 Archivo recibido: nota_voz_1772219321338.webm, tipo=audio/webm;codecs=opus, tamaño=146153 bytes
2026-02-27T19:08:44 - WARNING - [MediaProcessor] Audio WebM recibido - puede no reproducirse en WhatsApp
2026-02-27T19:08:44 - [BunnyStorage] Archivo subido exitosamente: inmobiliaria-media.b-cdn.net/audios_asesores/573195652633_1772219324.webm
                                                                 ↑↑↑ FALTA https:// ↑↑↑
2026-02-27T19:08:44 - [TwilioClient] 📤 Enviando con MediaUrl: inmobiliaria-media.b-cdn.net/audios_asesores/573195652633_1772219324.webm...
2026-02-27T19:08:44 - ERROR - [TwilioClient] Error enviando mensaje: 400 - {"code":21620,"message":"Invalid media URL(s)"}
2026-02-27T19:08:44 - POST /whatsapp/panel/send-message HTTP/1.1" 500 Internal Server Error
```

#### Punto Exacto de Fallo

**Archivo:** `utils/media_processor.py`, línea 42 y línea 197

```python
# Línea 42 - Configuración
BUNNY_PULL_ZONE = os.getenv("BUNNY_PULL_ZONE_URL", "").rstrip('/')
# Si la variable de entorno no tiene https://, la URL queda incompleta

# Línea 197 - Construcción de URL
public_url = f"{BUNNY_PULL_ZONE}/{folder}/{clean_filename}"
# Resultado: "inmobiliaria-media.b-cdn.net/..." en lugar de "https://inmobiliaria-media.b-cdn.net/..."
```

---

### 1.2 Problema de Mensaje de Sofia No Entregado (Timeout)

#### Flujo Esperado vs Flujo Real

```
┌─────────────────────────────────────────────────────────────────┐
│                       FLUJO ESPERADO                            │
├─────────────────────────────────────────────────────────────────┤
│ 1. Cliente envía audio por WhatsApp                             │
│ 2. Twilio recibe y envía webhook (timeout: 15s)                 │
│ 3. Sistema descarga audio (~2s)                                 │
│ 4. Sistema transcribe con Whisper (~3s)                         │
│ 5. Sofia analiza y genera respuesta (~5s)                       │
│ 6. Sistema retorna TwiML con respuesta (~12s total)             │
│ 7. Twilio procesa TwiML y envía mensaje al cliente              │
│ 8. Cliente recibe respuesta ✅                                  │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                       FLUJO REAL (CON ERROR)                    │
├─────────────────────────────────────────────────────────────────┤
│ 1. ✅ Cliente envía audio por WhatsApp (19:11:21)               │
│ 2. ✅ Twilio recibe y envía webhook                             │
│ 3. ✅ Sistema descarga audio (~1s)                              │
│ 4. ✅ Sistema transcribe con Whisper (~4s)                      │
│ 5. ✅ Sofia analiza y genera respuesta (~6s)                    │
│ 6. ⚠️ Sistema detecta handoff y ejecuta lógica adicional (~6s)  │
│ 7. ❌ Webhook tarda 21 segundos (19:11:21 → 19:11:42)           │
│ 8. ❌ Twilio ya cerró la conexión (timeout a los ~15s)          │
│ 9. ❌ TwiML llega tarde - Twilio lo ignora                      │
│10. ❌ Cliente NO recibe respuesta                               │
│11. ⚠️ MongoDB/HubSpot guardan mensaje como "enviado"           │
└─────────────────────────────────────────────────────────────────┘
```

#### Evidencia en Logs

```log
# Audio recibido
2026-02-27T19:11:21 - [Webhook] Mensaje recibido de whatsapp:+573147842399: [Sin texto]... NumMedia=1

# Procesamiento completo
2026-02-27T19:11:22 - [BunnyStorage] Archivo subido exitosamente
2026-02-27T19:11:25 - [Whisper] Transcripción exitosa: Hola y buenas tardes, ayer me quedaron de contactar...
2026-02-27T19:11:26 - [Webhook] Estado de conversación: BOT_ACTIVE
2026-02-27T19:11:29 - [SofiaBrain] Procesando mensaje Single-Stream
2026-02-27T19:11:35 - [SofiaBrain] Single-Stream completado | Emoción: frustrado, Score: 4, Handoff: immediate

# Handoff processing (6+ segundos adicionales)
2026-02-27T19:11:41 - [Webhook] Handoff INMEDIATO detectado: emoción=frustrado, score=4
2026-02-27T19:11:41 - [Webhook] ✅ Contacto +573147842399 agregado al panel (IMMEDIATE)

# Respuesta final - DEMASIADO TARDE
2026-02-27T19:11:42 - POST /whatsapp/webhook HTTP/1.1" 200 OK
#                     ↑↑↑ 21 segundos desde la petición original ↑↑↑

# PERO el mensaje se marca como enviado en background
2026-02-27T19:11:43 - [TimelineLogger] ✅ Nota creada: contact=204754684522, sender=client
2026-02-27T19:11:44 - [TimelineLogger] ✅ Nota creada: contact=204754684522, sender=bot
```

#### Punto Exacto de Fallo

**Problema arquitectónico:** El webhook NO valida que Twilio realmente envió el mensaje.

```
PROBLEMA: Guardamos como "enviado" ANTES de confirmación real
═══════════════════════════════════════════════════════════════

Flujo actual:
1. retornar TwiML  ← Asumimos que esto = "enviado"
2. background_tasks.add_task(guardar en MongoDB como "bot")
3. background_tasks.add_task(guardar en HubSpot como "bot")

Problema:
- Si Twilio cerró la conexión por timeout, el TwiML nunca se procesa
- PERO MongoDB/HubSpot ya guardaron el mensaje como "enviado"
- El panel muestra mensaje que NUNCA llegó al cliente
```

---

### 1.3 Comparación: Mensaje de Texto vs Audio

| Aspecto                     | Mensaje "Buenas tardes" | Mensaje Audio            |
| --------------------------- | ----------------------- | ------------------------ |
| **Tiempo de procesamiento** | ~12 segundos            | ~21 segundos             |
| **Resultado**               | ✅ Llegó al cliente     | ❌ NO llegó              |
| **Causa**                   | Dentro de timeout       | Excede timeout de Twilio |

---

## 2️⃣ CAUSAS PROBABLES

### 2.1 Causas Técnicas Confirmadas

| #   | Causa                                                    | Archivo              | Línea         | Severidad  |
| --- | -------------------------------------------------------- | -------------------- | ------------- | ---------- |
| 1   | Variable `BUNNY_PULL_ZONE_URL` sin `https://` en Railway | `.env` en Railway    | -             | 🔴 CRÍTICO |
| 2   | Timeout de webhook excedido (21s > 15s)                  | `webhook_handler.py` | Todo el flujo | 🔴 CRÍTICO |
| 3   | Sin validación de URL antes de enviar a Twilio           | `media_processor.py` | 197           | 🟡 ALTO    |
| 4   | Formato WebM no soportado por WhatsApp                   | `media_processor.py` | 507           | 🟡 ALTO    |
| 5   | Mensaje guardado antes de confirmación de entrega        | `webhook_handler.py` | 788+          | 🔴 CRÍTICO |

### 2.2 Causas Arquitectónicas

```
┌────────────────────────────────────────────────────────────────┐
│              PROBLEMAS DE DISEÑO IDENTIFICADOS                 │
├────────────────────────────────────────────────────────────────┤
│                                                                │
│ 1. SIN VALIDACIÓN DE DELIVERY STATUS                           │
│    ─────────────────────────────────                           │
│    • El sistema asume que retornar TwiML = mensaje entregado   │
│    • No se verifica el webhook de status de Twilio             │
│    • El endpoint /status existe pero solo loguea, no actualiza │
│                                                                │
│ 2. OPERACIONES SÍNCRONAS EN WEBHOOK                            │
│    ───────────────────────────────                             │
│    • Transcripción de audio (Whisper): ~4-5 segundos           │
│    • Análisis de Sofia (LLM): ~5-6 segundos                    │
│    • Lógica de handoff: ~5+ segundos                           │
│    • Total: >15 segundos (excede timeout de Twilio)            │
│                                                                │
│ 3. FALTA DE VALIDACIÓN DE URLs                                 │
│    ─────────────────────────────                               │
│    • URLs de Bunny.net no se validan antes de usar             │
│    • No hay verificación de protocolo https://                 │
│    • No hay fallback si la URL es inválida                     │
│                                                                │
│ 4. INCONSISTENCIA PANEL vs REALIDAD                            │
│    ──────────────────────────────────                          │
│    • Panel muestra mensajes como "enviados"                    │
│    • WhatsApp no los recibió                                   │
│    • No hay mecanismo de reconciliación                        │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

---

## 3️⃣ SOLUCIÓN PROPUESTA

### 3.1 Fix Inmediato #1: URL de Bunny.net

**Archivo:** `utils/media_processor.py`

```python
# ANTES (línea 42)
BUNNY_PULL_ZONE = os.getenv("BUNNY_PULL_ZONE_URL", "").rstrip('/')

# DESPUÉS - Validación y normalización de URL
def _get_bunny_pull_zone() -> str:
    """Obtiene y valida la URL del Pull Zone de Bunny.net."""
    url = os.getenv("BUNNY_PULL_ZONE_URL", "").strip().rstrip('/')

    if not url:
        logger.warning("[BunnyStorage] BUNNY_PULL_ZONE_URL no configurada")
        return ""

    # Asegurar que tenga protocolo https://
    if not url.startswith("https://"):
        if url.startswith("http://"):
            url = url.replace("http://", "https://", 1)
        else:
            url = f"https://{url}"
        logger.info(f"[BunnyStorage] URL normalizada a: {url}")

    return url

BUNNY_PULL_ZONE = _get_bunny_pull_zone()
```

**También en Railway:** Verificar que `BUNNY_PULL_ZONE_URL=https://inmobiliaria-media.b-cdn.net`

---

### 3.2 Fix Inmediato #2: Validación de URL antes de Twilio

**Archivo:** `utils/twilio_client.py`, método `send_whatsapp_message`

```python
async def send_whatsapp_message(
    self,
    to: str,
    body: str,
    media_url: Optional[str] = None
) -> dict:
    """Envía un mensaje de WhatsApp usando la API de Twilio."""

    # NUEVA VALIDACIÓN: Verificar URL de media antes de enviar
    if media_url:
        if not media_url.startswith("https://"):
            logger.error(f"[TwilioClient] URL de media inválida (falta https://): {media_url}")
            return {
                "status": "error",
                "message": f"URL de media debe comenzar con https://. Recibido: {media_url[:50]}..."
            }

        # Verificar que la URL sea accesible (opcional pero recomendado)
        # ...resto del código igual
```

---

### 3.3 Fix Arquitectónico #1: Envío Asíncrono para Audios

**Problema:** El webhook tarda más de 15 segundos cuando hay audio.

**Solución:** Responder inmediatamente a Twilio y enviar respuesta de forma asíncrona.

```python
# webhook_handler.py - Flujo propuesto para audios

@router.post("/webhook")
async def whatsapp_webhook(background_tasks: BackgroundTasks, ...):
    # Si hay multimedia (audio/imagen)
    if num_media > 0:
        # 1. Responder INMEDIATAMENTE a Twilio (vacío)
        # 2. Procesar en background
        background_tasks.add_task(
            _process_media_message_async,
            phone_normalized,
            media_url,
            content_type,
            contact_info,
            final_channel
        )

        # Retornar vacío - Twilio no enviará nada
        return Response(content="", media_type="text/xml")

    # Para texto simple, continuar flujo normal (rápido)
    # ...

async def _process_media_message_async(phone, media_url, content_type, contact_info, channel):
    """Procesa mensaje multimedia de forma asíncrona."""
    try:
        # 1. Descargar y subir a Bunny.net
        # 2. Transcribir con Whisper (si es audio)
        # 3. Procesar con Sofia
        # 4. ENVIAR RESPUESTA VÍA API (no TwiML)
        result = await twilio_client.send_whatsapp_message(
            to=phone,
            body=response_text
        )

        # 5. VERIFICAR que se envió correctamente
        if result["status"] == "success":
            # Guardar en MongoDB/HubSpot como "enviado"
            await _save_message_as_sent(...)
        else:
            # Guardar como "fallido"
            await _save_message_as_failed(...)

    except Exception as e:
        logger.error(f"[AsyncMedia] Error: {e}")
```

---

### 3.4 Fix Arquitectónico #2: Confirmación de Entrega Obligatoria

**Problema:** Se guarda como "enviado" antes de confirmación real.

**Solución:** Usar el webhook de status de Twilio para confirmar.

```python
# webhook_handler.py - Mejorar endpoint /status

# Agregar tracking de mensajes pendientes (Redis)
PENDING_MESSAGES_KEY = "pending_messages:{message_sid}"

@router.post("/status")
async def whatsapp_status_callback(
    MessageSid: str = Form(...),
    MessageStatus: str = Form(...),
    ErrorCode: Optional[str] = Form(None),
    ErrorMessage: Optional[str] = Form(None),
):
    """
    Callback de estado de mensajes de Twilio.

    Estados posibles:
    - queued: En cola
    - sent: Enviado al carrier
    - delivered: Entregado al dispositivo ✅
    - read: Leído por el usuario ✅
    - failed: Error en envío ❌
    - undelivered: No entregado ❌
    """
    logger.info(f"[StatusCallback] {MessageSid}: {MessageStatus}")

    # Actualizar estado en MongoDB
    mongo_manager = get_mongo_manager()

    if MessageStatus in ["delivered", "read"]:
        # Confirmar entrega
        await mongo_manager.update_message_delivery_status(
            message_sid=MessageSid,
            status="delivered",
            delivered_at=datetime.utcnow()
        )
        logger.info(f"[StatusCallback] ✅ Mensaje {MessageSid} confirmado como entregado")

    elif MessageStatus in ["failed", "undelivered"]:
        # Marcar como fallido
        await mongo_manager.update_message_delivery_status(
            message_sid=MessageSid,
            status="failed",
            error_code=ErrorCode,
            error_message=ErrorMessage
        )
        logger.error(f"[StatusCallback] ❌ Mensaje {MessageSid} falló: {ErrorCode} - {ErrorMessage}")

        # Notificar al panel que el mensaje NO llegó
        # (para que el asesor pueda reenviar)

    return Response(content="", media_type="text/xml")


# Configurar webhook en Twilio Console:
# Status Callback URL: https://tu-dominio.railway.app/whatsapp/status
```

---

### 3.5 Fix para Formato WebM

**Problema:** Los navegadores graban en WebM, WhatsApp no lo soporta.

**Solución:** Convertir WebM a formato compatible antes de enviar.

```python
# media_processor.py - Agregar conversión de audio

async def upload_outgoing_media(
    self,
    file_bytes: bytes,
    content_type: str,
    phone: str
) -> str:
    """Sube archivo enviado por la ASESORA desde el Panel a Bunny.net."""

    content_lower = content_type.lower()

    # NUEVA LÓGICA: Convertir WebM a OGG para compatibilidad
    if "webm" in content_lower:
        logger.info("[MediaProcessor] Detectado WebM - convirtiendo a OGG para WhatsApp")

        try:
            file_bytes = await self._convert_webm_to_ogg(file_bytes)
            content_type = "audio/ogg"
            logger.info("[MediaProcessor] ✅ Conversión WebM→OGG exitosa")
        except Exception as e:
            logger.error(f"[MediaProcessor] Error convirtiendo WebM: {e}")
            # Continuar con WebM original (puede fallar en WhatsApp)

    # ...resto del código

async def _convert_webm_to_ogg(self, webm_bytes: bytes) -> bytes:
    """Convierte audio WebM a OGG usando ffmpeg."""
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix='.webm', delete=False) as webm_file:
        webm_file.write(webm_bytes)
        webm_path = webm_file.name

    ogg_path = webm_path.replace('.webm', '.ogg')

    try:
        # Requiere ffmpeg instalado en Railway
        subprocess.run([
            'ffmpeg', '-i', webm_path,
            '-c:a', 'libvorbis', '-q:a', '4',
            ogg_path
        ], check=True, capture_output=True)

        with open(ogg_path, 'rb') as f:
            return f.read()
    finally:
        import os
        os.unlink(webm_path)
        if os.path.exists(ogg_path):
            os.unlink(ogg_path)
```

**Alternativa (más simple):** Modificar el frontend del panel para grabar en formato OGG/WAV.

---

## 4️⃣ PLAN DE VERIFICACIÓN

### 4.1 Verificación de Recepción de Audio

```bash
# 1. Verificar que el audio llega al webhook
curl -X POST "https://tu-app.railway.app/whatsapp/webhook" \
  -F "From=whatsapp:+573001234567" \
  -F "To=whatsapp:+15551234567" \
  -F "Body=" \
  -F "NumMedia=1" \
  -F "MediaUrl0=https://example.com/audio.ogg" \
  -F "MediaContentType0=audio/ogg"

# Verificar en logs:
# - [MediaProcessor] Media descargada de Twilio: X bytes
# - [Whisper] Transcripción exitosa: ...
```

### 4.2 Verificación de Transcripción

```bash
# Endpoint de prueba para verificar Whisper
curl -X POST "https://tu-app.railway.app/test/transcribe" \
  -H "Content-Type: multipart/form-data" \
  -F "audio=@test_audio.ogg"

# Verificar respuesta:
# {"status": "success", "transcription": "texto transcrito..."}
```

### 4.3 Verificación de Envío Manual (Postman)

```http
### Test: Enviar mensaje de texto
POST https://tu-app.railway.app/whatsapp/panel/send-message-json
Content-Type: application/json
X-API-Key: {{admin_api_key}}

{
    "phone": "+573001234567",
    "message": "Mensaje de prueba desde Postman",
    "canal": "whatsapp"
}

### Respuesta esperada:
{
    "status": "success",
    "message_sid": "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "mongo_id": "...",
    "sofia_paused": true
}
```

### 4.4 Verificación de Status de Mensajes

```http
### Simular callback de status de Twilio
POST https://tu-app.railway.app/whatsapp/status
Content-Type: application/x-www-form-urlencoded

MessageSid=SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
MessageStatus=delivered
From=whatsapp:+15551234567
To=whatsapp:+573001234567

### Verificar en logs:
# [StatusCallback] ✅ Mensaje SMxxx confirmado como entregado
```

### 4.5 Verificación de Delivery Receipts

```python
# Script de verificación de mensajes pendientes
import httpx
import asyncio

async def verify_message_delivery(message_sid: str):
    """Consulta el estado de un mensaje en Twilio."""

    account_sid = "ACxxxxxxxx"
    auth_token = "xxxxxxxx"

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages/{message_sid}.json"

    async with httpx.AsyncClient() as client:
        response = await client.get(url, auth=(account_sid, auth_token))
        data = response.json()

        print(f"Message SID: {data['sid']}")
        print(f"Status: {data['status']}")  # queued, sent, delivered, read, failed
        print(f"Error Code: {data.get('error_code')}")
        print(f"Error Message: {data.get('error_message')}")

        return data['status']

# Uso:
# asyncio.run(verify_message_delivery("SMxxxxxxxx"))
```

---

## 5️⃣ PRUEBAS CON POSTMAN

### 5.1 Colección de Pruebas

```json
{
  "info": {
    "name": "Sofia - Test Audio y Mensajes",
    "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
  },
  "item": [
    {
      "name": "1. Enviar Mensaje Texto",
      "request": {
        "method": "POST",
        "header": [
          { "key": "Content-Type", "value": "application/json" },
          { "key": "X-API-Key", "value": "{{admin_api_key}}" }
        ],
        "body": {
          "mode": "raw",
          "raw": "{\n    \"phone\": \"+573001234567\",\n    \"message\": \"Test desde Postman\",\n    \"canal\": \"whatsapp\"\n}"
        },
        "url": "{{base_url}}/whatsapp/panel/send-message-json"
      }
    },
    {
      "name": "2. Consultar Estado Mensaje",
      "request": {
        "method": "GET",
        "header": [
          { "key": "Authorization", "value": "Basic {{twilio_base64}}" }
        ],
        "url": "https://api.twilio.com/2010-04-01/Accounts/{{twilio_account_sid}}/Messages/{{message_sid}}.json"
      }
    },
    {
      "name": "3. Simular Webhook de Status",
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
            { "key": "MessageSid", "value": "{{message_sid}}" },
            { "key": "MessageStatus", "value": "delivered" },
            { "key": "From", "value": "whatsapp:+15551234567" },
            { "key": "To", "value": "whatsapp:+573001234567" }
          ]
        },
        "url": "{{base_url}}/whatsapp/status"
      }
    },
    {
      "name": "4. Simular Audio Entrante",
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
            { "key": "From", "value": "whatsapp:+573001234567" },
            { "key": "To", "value": "whatsapp:+15551234567" },
            { "key": "Body", "value": "" },
            { "key": "NumMedia", "value": "1" },
            { "key": "MediaUrl0", "value": "{{test_audio_url}}" },
            { "key": "MediaContentType0", "value": "audio/ogg" }
          ]
        },
        "url": "{{base_url}}/whatsapp/webhook"
      }
    },
    {
      "name": "5. Verificar Ventana 24h",
      "request": {
        "method": "GET",
        "header": [{ "key": "X-API-Key", "value": "{{admin_api_key}}" }],
        "url": "{{base_url}}/whatsapp/panel/window-status/+573001234567"
      }
    }
  ],
  "variable": [
    { "key": "base_url", "value": "https://tu-app.railway.app" },
    { "key": "admin_api_key", "value": "protect_admin_2024_xK9mP3qR" },
    { "key": "twilio_account_sid", "value": "ACxxxxxxxx" },
    { "key": "twilio_base64", "value": "base64(account_sid:auth_token)" },
    { "key": "message_sid", "value": "" },
    { "key": "test_audio_url", "value": "https://example.com/test.ogg" }
  ]
}
```

---

## 6️⃣ CHECKLIST DE IMPLEMENTACIÓN

### Prioridad Alta (Implementar HOY)

- [ ] **Fix 1:** Verificar/corregir `BUNNY_PULL_ZONE_URL` en Railway (agregar `https://`)
- [ ] **Fix 2:** Agregar validación de URL en `media_processor.py`
- [ ] **Fix 3:** Agregar validación de URL en `twilio_client.py`

### Prioridad Media (Implementar esta semana)

- [ ] **Fix 4:** Implementar procesamiento asíncrono para audios
- [ ] **Fix 5:** Mejorar endpoint `/status` para tracking de delivery
- [ ] **Fix 6:** Agregar campo `delivery_status` en MongoDB

### Prioridad Baja (Implementar próxima semana)

- [ ] **Fix 7:** Conversión WebM → OGG para compatibilidad
- [ ] **Fix 8:** Dashboard de mensajes fallidos
- [ ] **Fix 9:** Alertas cuando hay mensajes sin confirmar

---

## 7️⃣ MONITOREO RECOMENDADO

### Métricas a Trackear

```python
# Agregar métricas (ejemplo con prometheus)
from prometheus_client import Counter, Histogram

# Mensajes enviados vs confirmados
messages_sent = Counter('sofia_messages_sent_total', 'Total messages sent', ['channel', 'status'])
messages_confirmed = Counter('sofia_messages_confirmed_total', 'Messages confirmed delivered')

# Tiempo de respuesta del webhook
webhook_latency = Histogram('sofia_webhook_latency_seconds', 'Webhook response time')

# Errores de media
media_errors = Counter('sofia_media_errors_total', 'Media processing errors', ['type', 'error'])
```

### Alertas Recomendadas

1. **Webhook Latency > 10s:** Alerta cuando el webhook está cerca del timeout
2. **Delivery Failure Rate > 5%:** Alerta cuando muchos mensajes no se entregan
3. **Media Upload Errors:** Alerta cuando Bunny.net falla

---

## 8️⃣ CONCLUSIONES

### Problemas Críticos Encontrados

1. **URL de Bunny.net sin protocolo:** La variable de entorno `BUNNY_PULL_ZONE_URL` en Railway no tiene `https://`, causando que Twilio rechace todas las URLs de multimedia.

2. **Timeout de Webhook:** El procesamiento de audios (descarga + Whisper + Sofia + handoff) excede los 15 segundos de timeout de Twilio, causando que las respuestas no lleguen.

3. **Falsa Confirmación:** El sistema guarda mensajes como "enviados" antes de recibir confirmación real de Twilio, creando inconsistencia entre el panel y WhatsApp.

### Acciones Inmediatas Requeridas

1. ✅ Verificar y corregir `BUNNY_PULL_ZONE_URL` en Railway
2. ✅ Agregar validación de URLs antes de enviar a Twilio
3. ⚠️ Considerar procesamiento asíncrono para mensajes con audio

---

**Documento generado:** 2026-02-27  
**Próxima revisión:** 2026-03-06
