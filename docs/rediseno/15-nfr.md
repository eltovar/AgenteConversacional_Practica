# D-15 · Requisitos no funcionales

> **Estado:** borrador v0 — semilla creada el 2026-08-20.

---

## 1. Automatizaciones — decision D15-1

> ✅ **Las automatizaciones se quedan en codigo.** Decidido el 2026-08-20.

### Que costaria hacerlas configurables

Preguntaste que tan complicado seria. Hay cuatro niveles, muy distintos entre si:

| Nivel | Que permite | Esfuerzo | Veredicto |
|---|---|---|---|
| **1 · Parametros** | Sacar los **tiempos y umbrales** a configuracion: las 24 h de inactividad, los 90 dias del recolector, la hora del rebuild, el 1 h 30 min de la auto-transicion de cita | **Bajo — dias** | ✅ **Recomendado igualmente** |
| **2 · Interruptores** | Activar y desactivar cada automatizacion desde la interfaz | Bajo | ✅ Recomendado |
| **3 · Reglas simples** | Construir "si pasa X entonces haz Y" desde una pantalla | Medio-alto — semanas | ❌ No se justifica |
| **4 · Motor de flujos** | Equivalente a HubSpot Workflows: ramas, esperas, condiciones anidadas | Muy alto — meses | ❌ No se justifica |

### Recomendacion

> **Quedarse en codigo, pero subir al nivel 1.**
>
> La mayoria de los cambios que se piden en la practica no son *"quiero una automatizacion nueva"* sino *"cambiemos las 24 horas a 12"*. Eso hoy exige editar Python y desplegar — justo la razon n.o 2 del "por que ahora".
>
> Sacar los tiempos a configuracion cuesta dias y elimina la mayor parte de esos despliegues. Los niveles 3 y 4 son productos en si mismos y no se justifican para 6 usuarios.

**Parametros a externalizar:**

| Parametro | Valor actual |
|---|---|
| Umbral de inactividad | 48 h |
| Recolector de metadatos huerfanos | 90 dias |
| Reconciliacion | cada 6 h |
| Rebuild nocturno | 3:00 AM |
| Auto-transicion post-cita | 1 h 30 min |
| Ventana de recordatorio de cita | 24 h antes |
| Retencion de notificaciones | 30 dias |

---

## 2. Rendimiento — medido el 2026-08-20

| Metrica | Valor actual | Objetivo |
|---|---|---|
| Respuesta de SofIA | 0,7 min mediana · 95 % bajo 5 min | **Mantener** |
| Respuesta de asesora | 30 min mediana · p90 24,1 h | **p90 bajo 4 h** |
| Volumen de mensajes | 56.278 en 90 dias | — |
| Conversaciones activas | 2.647 en 90 dias | — |
| Leads nuevos | ~19/dia | — |

## 3. Limites externos

| Limite | Valor |
|---|---|
| WhatsApp — conversaciones/dia | 250 → 1.000 → 10.000 → 100.000 segun calidad |
| HubSpot — limite de peticiones | Backoff ya implementado; 36 s por reintento en el peor caso |
| Railway | 1 worker de Gunicorn, historial de fugas de memoria y `SIGKILL` |

## 4. Pendiente de desarrollar

- Disponibilidad objetivo y ventana de mantenimiento
- Politica de PII y retencion de datos
- Presupuesto de coste por LLM
- Observabilidad y alertas
