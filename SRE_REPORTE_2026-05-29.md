# 🔍 Reporte SRE — SofIA Inmobiliaria Proteger
**Fecha:** 2026-05-29  
**Ventana analizada:** 14:20:19 UTC → 14:25:09 UTC (~5 min de tráfico real)  
**Fuente:** `railway logs --lines 200`  
**Analizado por:** Claude (diagnóstico autónomo)

---

## ✅ Estado General del Sistema: SALUDABLE

No se detectaron errores de nivel ERROR ni CRITICAL en los 200 logs analizados. El servidor está operativo y procesando mensajes de clientes en producción.

---

## 📊 Resumen de Componentes

| Componente | Estado | Observaciones |
|---|---|---|
| **FastAPI / Gunicorn** | ✅ OK | Webhook 200 OK, procesamiento diferido activo |
| **Memory Watchdog** | ✅ OK | RSS = 267 MB (límite: 4096 MB) — muy por debajo |
| **Redis** | ✅ OK | `conv_state` leído y escrito sin errores |
| **MongoDB** | ✅ OK | Mensajes guardados y recuperados correctamente |
| **HubSpot API** | ✅ OK | Contactos encontrados, actualizados, timeline creado |
| **Twilio Webhook** | ✅ OK | Mensaje recibido y entregado — SID confirmado |
| **SofiaBrain** | ✅ OK | Single-Stream LLM completado sin errores |
| **APScheduler** | ✅ OK | 3 jobs corriendo puntualmente |
| **Panel Asesoras** | ⚠️ WARN | 1 warning por API Key faltante (ver detalle) |

---

## 🔎 Flujo Completo Trazado — Mensaje Real en Producción

### Entrada: 14:20:38 UTC | Cliente +573002133257

```
[Webhook] Mensaje recibido → +573002133257
  → canal: whatsapp
  → body: "Me indicas por favor en qué numero me puedo contactar 
           para realizar una pregunta sobre mi factura"
  → 200 OK devuelto de inmediato (anti-timeout Twilio ✅)

[DeferredProcess] Procesamiento diferido iniciado
  → HubSpotClient: contact_id = 224761414404 encontrado
  → Redis: Sin estado previo → BOT_ACTIVE por defecto

[MessageAggregator] Mensajes combinados (2 en buffer):
  → "Hola sofia" + "Me indicas por favor..."
  → Delay de 30s (debounce) respetado

[SofiaBrain] Single-Stream LLM ejecutado
  → Emoción: neutral | Score: 5 | Handoff: none
  → Respuesta generada: "Claro, puedes comunicarte con nuestro 
     equipo de caja al WhatsApp 604 444 63 64..."

[TwilioClient] Mensaje enviado
  → SID: SM349a83e921da52e31c0610287e025dc3 | status: queued

[StatusCallback] Entrega confirmada: delivered ✅
  → A: whatsapp:+573002133257

[TimelineLogger] 2 notas HubSpot creadas
  → sender=client: ✅
  → sender=bot: ✅
```

**Tiempo total respuesta:** ~16 segundos (38→54s) dentro del margen esperado con LLM.

---

## ⚠️ Anomalías Detectadas

### 1. WARNING — API Key no proporcionada en header
- **Línea:** 13 (14:20:28 UTC)
- **Log:** `[Panel] API Key no proporcionada en header`
- **Componente:** `outbound_panel.py` — endpoint GET `/whatsapp/panel/metrics`
- **Causa probable:** La llamada de diagnóstico SRE enviada por Claude usó el query param `?api_key=` en lugar del header `X-API-Key`. El panel espera el header.
- **Impacto:** NINGUNO en producción. El endpoint rechazó la solicitud pero no afectó operaciones.
- **Acción recomendada:** Documentar que el endpoint `/panel/metrics` requiere header `X-Admin-API-Key`, no query param.

### 2. OBSERVACIÓN — Luisa (89096380) tiene 23 mensajes sin leer
- **Línea:** 173 (14:25:01 UTC)
- **Log:** `[Inbox][Batch] advisor=89096380 no_leidos=23 leidos=27 total=53`
- **Componente:** Panel Asesoras — Luisa (equipo_directo)
- **Causa:** Acumulación de mensajes no respondidos. A las 14:25 también realizó un Take Control sobre +573014208281:finca_raiz.
- **Impacto:** Potencial demora en respuesta a 23 clientes.
- **Acción recomendada:** Verificar con Luisa si está activa; los 23 no-leídos pueden requerir atención.

### 3. OBSERVACIÓN — Jubeny (89096378) tiene 15 mensajes sin leer
- **Línea:** 5 (14:20:20 UTC)
- **Log:** `[Inbox][Batch] advisor=89096378 no_leidos=15 leidos=30 total=45`
- **Componente:** Panel Asesoras — Jubeny (equipo_portales)
- **Impacto:** Menor que Luisa, pero requiere seguimiento.

### 4. OBSERVACIÓN — HubSpot: firstname=None en contacto 224761414404
- **Línea:** 25-30
- **Log:** `Contacto encontrado con propiedades: 224761414404 (firstname: None)`
- **Componente:** `CRMAgent` / `contact_manager.py`
- **Causa:** El contacto existe en HubSpot pero no tiene nombre capturado aún.
- **Impacto:** Sin nombre, no se puede activar el handoff a asesora automáticamente. SofiaBrain continuó como BOT_ACTIVE correctamente.

---

## 📈 Métricas de Salud del Worker

| Métrica | Valor | Estado |
|---|---|---|
| **RSS Memory (14:20:46)** | 267 MB | ✅ Sawtooth bajo (ciclo ~1-2h) |
| **RSS Memory (14:22:46)** | 267 MB | ✅ Estable sin crecimiento |
| **RSS Memory (14:24:46)** | 267 MB | ✅ Sin fuga detectada |
| **Bulk campaign tick** | cada 15s | ✅ 8/8 ejecuciones exitosas |
| **check_scheduled_messages** | cada 60s | ✅ 3/3 ejecuciones exitosas |
| **memory_watchdog** | cada 120s | ✅ 3/3 ejecuciones exitosas |

**Interpretación Watchdog:** 267 MB es un nivel extremadamente bajo. El servidor está en el valle del patrón sawtooth, muy lejos del SIGTERM (4096 MB). Sin riesgo de reinicio en las próximas horas.

---

## 🏗️ Actividad por Asesora (ventana analizada)

| Asesora | Owner ID | No Leídos | Acción Detectada |
|---|---|---|---|
| **Jubeny** | 89096378 | 15 | Consultando panel, segregación equipo_portales OK |
| **Luisa** | 89096380 | 23 | Take Control activo sobre +573014208281 (finca_raiz) |
| **Monica** | 89096379 | 0 | 1 contacto (transferencia) — en modo respaldo |

**Acción tomada por Luisa a 14:25:04:**
```
[Inbox][Clear] advisor=89096380 phone=+573014208281 removed=1
[ConversationState] HUMAN_ACTIVE activado: +573014208281:finca_raiz
[Panel] Take Control: PENDING_HANDOFF -> HUMAN_ACTIVE
```
Luisa tomó control de una conversación de Finca Raíz exitosamente.

---

## 🔒 Verificación de Singletons (sin leaks)

Los siguientes singletons operaron correctamente sin instanciación paralela:
- `HubSpotClient` — 4 llamadas a través del singleton único
- `Redis` — estados leídos/escritos via `_redis_pool`
- `TwilioClient` — envío exitoso via cliente único
- `MongoDB` — guardado y recuperación de mensajes sin errores de conexión

---

## 🚦 Anomalías Críticas Monitoreadas — NINGUNA DETECTADA

| Anomalía | Detectada | Detalle |
|---|---|---|
| `MongoServerSelectionTimeoutError` | ❌ No | MongoDB operativo |
| `ConnectionError: Redis` | ❌ No | Redis operativo, estados correctos |
| `memory watchdog + SIGTERM` | ❌ No | RSS 267 MB, sin reinicio |
| `422 Unprocessable` en webhook | ❌ No | Webhook procesando correctamente |
| `HubSpot 429` | ❌ No | Sin rate limit |
| `OpenAI APIError` | ❌ No | LLM respondió sin errores |

---

## 📋 Acciones Recomendadas

1. **[Baja]** Documentar en CLAUDE.md que `/panel/metrics` requiere header `X-Admin-API-Key`, no query param.
2. **[Media]** Monitorear inbox de Luisa (23 no-leídos) — puede indicar sobrecarga o ausencia temporal.
3. **[Baja]** El contacto 224761414404 no tiene `firstname`; SofiaBrain operó correctamente pero el lead no puede hacer handoff automático hasta tener nombre.
4. **[Info]** Considerar añadir Railway MCP token al entorno (`RAILWAY_TOKEN`) para diagnósticos SRE autónomos futuros sin necesidad de ejecutar scripts .bat.

---

*Reporte generado automáticamente por diagnóstico SRE activo. Logs fuente: `railway_logs_output.txt` (2026-05-29)*
