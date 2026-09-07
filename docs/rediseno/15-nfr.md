# D-15 · Requisitos no funcionales

> **Estado:** ✅ **v2 — cerrado el 2026-08-28.** Los cuatro apartados que estaban declarados como pendientes están escritos.
> **Principio:** aquí no hay buenas intenciones. **Cada requisito tiene un número y una forma de comprobarlo.** Un NFR que no se puede medir no es un requisito, es un deseo.

---

## 1. Automatizaciones — decisión D15-1

> ✅ **Las automatizaciones se quedan en código.** Decidido el 2026-08-20.

### Qué costaría hacerlas configurables

| Nivel | Qué permite | Esfuerzo | Veredicto |
|---|---|---|---|
| **1 · Parámetros** | Sacar los **tiempos y umbrales** a configuración | **Bajo — días** | ✅ **Recomendado igualmente** |
| **2 · Interruptores** | Activar y desactivar cada automatización desde la interfaz | Bajo | ✅ Recomendado |
| **3 · Reglas simples** | Construir "si pasa X entonces haz Y" desde una pantalla | Medio-alto — semanas | ❌ No se justifica |
| **4 · Motor de flujos** | Equivalente a HubSpot Workflows | Muy alto — meses | ❌ No se justifica |

> **Quedarse en código, pero subir al nivel 1.** La mayoría de los cambios que se piden no son *"quiero una automatización nueva"* sino *"cambiemos las 24 horas a 12"*. Eso hoy exige editar Python y desplegar.

**Parámetros a externalizar:**

| Parámetro | Valor actual |
|---|---|
| Umbral de inactividad | 48 h |
| Recolector de metadatos huérfanos | 90 días |
| Reconciliación | cada 6 h |
| Rebuild nocturno | 3:00 AM |
| Auto-transición post-cita | 1 h 30 min |
| Ventana de recordatorio de cita | 24 h antes |
| Retención de notificaciones | 30 días |
| 🆕 Modo de validación de firma Twilio | `TWILIO_SIGNATURE_MODE` — **ya externalizado** |

> ✅ **El patrón ya existe y funciona.** `TWILIO_SIGNATURE_MODE` se cambia en Railway **sin desplegar**. Es exactamente el nivel 1 aplicado a un caso real, y sirve de plantilla para los demás.

---

## 2. Rendimiento

| Métrica | Valor actual | Objetivo |
|---|---|---|
| Respuesta de SofIA | **0,7 min** mediana · 95 % bajo 5 min | **Mantener** |
| Respuesta de asesora | 30 min mediana · **p90 24,1 h** | 🎯 **p90 bajo 4 h** |
| Mensajes almacenados | 56.377 | — |
| Conversaciones activas | 2.647 en 90 días | — |
| Leads nuevos | ~19/día · 578/mes | — |
| `GET /contacts` del panel | 7-12 s *(aceptado a propósito)* | 🎯 **bajo 3 s** |

> 🔑 **El único objetivo de rendimiento que importa de verdad es el p90 de la asesora.** SofIA ya cumple. Y ese número **no se arregla optimizando código**: se arregla con notificaciones que lleguen (UC-018) y con colas visibles (UC-020).

---

## 3. Límites externos

| Límite | Valor |
|---|---|
| WhatsApp — conversaciones/día | 250 → 1.000 → 10.000 → 100.000 según calidad |
| HubSpot — límite de peticiones | Backoff implementado; **36 s** por reintento en el peor caso → lo resuelve [ADR-006](14-adrs.md) |
| Railway | **2 workers** de Gunicorn, `--max-requests 500`, historial de fugas de memoria y `SIGKILL` |
| OpenAI | Sin alerta de saldo. **Ver §6** |

> ⚠️ **Corrección verificada el 2026-08-27.** Este documento y `CLAUDE.md` decían *"1 worker"*. El `Procfile` dice **`--workers 2`**. Importa: con 2 procesos el WebSocket de un navegador vive en **uno solo**, y por eso Redis Pub/Sub es obligatorio, no opcional.

---

## 4. Disponibilidad y ventana de mantenimiento

### 4.1 Qué significa "estar caído" aquí

No es una sola cosa. **Hay tres caídas distintas y solo una es visible:**

| Caída | Quién la sufre | ¿Se nota? |
|---|---|---|
| **La API no responde** | Asesoras y clientes | ✅ Inmediato |
| **SofIA no puede responder** | Solo clientes nuevos | 🔴 **Nadie se entera.** Pasó: 2 días |
| **La propagación a HubSpot se detiene** | Nadie, al principio | 🔴 Invisible hasta que los datos divergen |

> 🔑 **Las dos caídas invisibles son las que han hecho daño real.** Por eso el objetivo de disponibilidad no puede ser un solo porcentaje.

### 4.2 Objetivos

| # | Requisito | Número | Cómo se comprueba |
|---|---|---|---|
| **A-1** | Disponibilidad de la API en horario laboral | **99,5 %** lunes a sábado, 7:00-19:00 Bogotá | Sondeo externo cada minuto |
| **A-2** | **Un reinicio no puede perder mensajes** | 0 mensajes perdidos | El webhook responde 200 y encola; el trabajo pendiente sobrevive al reinicio |
| **A-3** | **Un reinicio debe ser invisible para la asesora** | Sin avisos rojos en pantalla | El frontend reintenta en silencio y conserva lo que ya tenía |
| **A-4** | Recuperación tras caída de un worker | **< 60 s** | El watchdog ya reinicia solo |
| **A-5** | SofIA nunca degrada hacia el silencio | **0 leads sin atender** por fallo de IA | Si el motor falla, **escala a un humano** (UC-061) |

> **A-3 es tu regla, no mía.** *"Si el sistema necesita reiniciarse, debe ser completamente silencioso. Las asesoras no deben notar ningún aviso tipo 'Error al cargar contactos'."*
>
> **A-5 ya está implementado** — el PR #14 introdujo `MessageAnalysis.analysis_failed` justo para esto. **Es un NFR que se ganó a base de un incidente**, y hay que escribirlo para no perderlo en el rediseño.

### 4.3 Memoria — el riesgo con más historial

| | |
|---|---|
| **Antecedente** | Crisis de **31 GB en 48 h** (abr-2026). **11 fugas** corregidas en 4 rondas |
| **Recaída** | 6-ago-2026: **2 SIGKILL en 2,5 h**, intervalo ~2 h 16 min — casi idéntico al de antes del arreglo |
| **Estado medido hoy** | **RSS 307 MB** de un límite de 3.300 MB · watchdog cada 2 min · **0 SIGKILL** en la ventana observada |

| # | Requisito | Número |
|---|---|---|
| **A-6** | RSS estable en régimen | **< 800 MB** sostenido |
| **A-7** | Sin patrón de sierra | Ninguna subida sostenida entre reinicios |
| **A-8** | **Ninguna conexión fuera de los singletons** | Redis · httpx · HubSpot |

> 🔴 **A-8 no es un consejo de estilo: es la causa raíz documentada de las 11 fugas.** Todo código nuevo del rediseño —la cola de propagación incluida— tiene que respetarlo.

### 4.4 Ventana de mantenimiento

| | |
|---|---|
| **Ventana** | **Domingo**, cualquier hora |
| **Por qué el domingo** | Es el único día **fuera** de la ventana operativa lunes-sábado. Ya está decidido así para las métricas |
| **Fuera de ventana** | Solo despliegues que **no rompan producción** — las fases A, B, D y E de [D-18](18-migracion.md) |
| 🔴 **Exige ventana** | **Solo la fase C** — el reparto de `lifecyclestage`. Es el punto de no retorno |

> ⚠️ **El domingo es también el día que no se cuenta en ninguna semana.** Conviene tenerlo presente: una cita en domingo hoy no se contabiliza (ver [D-02 §26](02-actores-y-roles.md)).

---

## 5. PII y retención de datos

### 5.1 El estado real, medido — no estimado

El 2026-08-28, sobre **19 minutos** de tráfico real en producción:

| | |
|---|---|
| ✅ **El log del webhook ya está limpio** | `[Webhook][RAW]` emite solo nombres de campo. Corregido en `8fdbc2a` |
| 🔴 **72 líneas con el teléfono completo** | En `ContactManager`, `HubSpotClient`, `should_bot_respond`, `DeferredProcess`, `TwilioClient` |
| 🔴 **Al menos una con el texto del cliente** | `[Checkpoint] MSG_PRE_PROCESS \| phone=… \| body='Buenas tardes'` |

> **Esto contradice una regla que el proyecto ya tiene escrita** en `CLAUDE.md`: *"Nunca loggear teléfonos completos ni nombres de clientes en producción."* No es un descubrimiento: es una regla que existe y no se cumple.

### 5.2 Requisitos

| # | Requisito | Cómo se comprueba |
|---|---|---|
| **P-1** | **Ningún log emite un teléfono completo.** Enmascarado o últimos 4 dígitos | Prueba automática que barre los logs buscando el patrón |
| **P-2** | **Ningún log emite contenido de mensajes** | Igual |
| **P-3** | Los identificadores externos (`MessageSid`, `contact_id`) **sí** se registran | Son necesarios para diagnosticar y no identifican a una persona por sí solos |
| **P-4** | Se registran **etiquetas, nunca valores** | Es el criterio que `query_profiler.py` ya aplica |
| **P-5** | Toda exportación queda registrada: quién, qué y cuándo | UC-043 |
| **P-6** | `utils/pii_validator.py` sigue siendo la **fuente única** de extracción y validación | Ninguna otra ruta valida PII por su cuenta |

> 🔑 **P-4 no hay que inventarlo.** `query_profiler.py` ya enmascara rutas (`/contacts/{id}/detail`), nunca registra el filtro de las consultas Mongo ni el query string de HubSpot. **El criterio existe y funciona; falta aplicarlo al resto.**

### 5.3 Retención

| Dato | Cuánto | Por qué |
|---|---|---|
| Contactos y conversaciones | **Indefinido** | Es el historial del cliente |
| Eventos de contacto | **Indefinido** | Es lo que hace medible el embudo |
| Eventos de acceso y configuración | **12 meses** | Auditoría |
| Elementos entregados de la cola de propagación | **30 días** | Q6-2 de [ADR-006](14-adrs.md) |
| Notificaciones | **30 días** | Ya es el valor actual |
| Registros de aplicación (Railway) | Lo que retenga la plataforma | ⚠️ **Por eso no pueden llevar PII**: no controlamos ni su borrado ni quién los lee |

> ⚠️ **El último punto es el que cierra el argumento de §5.1.** Un log con teléfonos vive en un sistema de terceros, con su propia retención y sus propios accesos. **No es un fallo de estilo, es una salida de datos personales fuera del perímetro.**

### 5.4 Lo que este sistema no hace

| | |
|---|---|
| ❌ No se borran contactos | Ni siquiera el Administrador ([D-06 §3](06-matriz-permisos.md), regla 4) |
| ❌ No hay derecho al olvido implementado | 🔵 **Si el negocio lo necesita, es trabajo aparte y hay que decidirlo** |
| ❌ No se cifra a nivel de campo | Se confía en el cifrado en reposo de MongoDB Atlas y HubSpot |

---

## 6. Coste del LLM

### 6.1 El incidente que define este requisito

El **7-ago-2026** la cuenta de OpenAI agotó sus créditos.

| Día | Handoffs |
|---|---|
| 6-ago | **8** |
| 7-ago | **0** |
| 8-ago | 1 |

> 🔴 **Dos días de leads muertos. No lo detectó el monitoreo: lo reportaron las asesoras, 48 horas después.**
>
> Y parecía intermitente, lo que retrasó el diagnóstico: los contactos con una asesora al mando tenían el bot silenciado, así que nunca llamaban a OpenAI y nunca fallaban.

### 6.2 Requisitos

| # | Requisito | Número |
|---|---|---|
| **C-1** | 🔴 **Alerta de saldo de OpenAI** | Aviso al **20 %** restante |
| **C-2** | Coste medido y visible por período | Debe poder responderse *"¿cuánto costó el mes?"* sin abrir la consola de OpenAI |
| **C-3** | Un fallo del LLM **nunca** degrada al silencio | Escala a humano — ya implementado (PR #14) |
| **C-4** | El coste por lead debe ser conocido | Instrumentar tokens por conversación |

> 🔴 **C-1 sigue sin implementarse.** El PR #14 arregló la *consecuencia* —que los leads se perdieran— pero **no la causa**: nadie se entera de que el saldo se agota. Un aviso al 20 % habría evitado el incidente por completo.

**No pongo aquí una cifra de coste mensual porque no la he medido.** Con ~19 leads/día es estimable, pero un NFR con un número inventado es peor que uno sin número. **C-4 existe justamente para que ese número aparezca.**

---

## 7. Observabilidad y alertas

### 7.1 Lo que ya existe

| Pieza | Estado |
|---|---|
| `query_profiler.py` | ✅ Desglose de latencia por petición · cabecera `Server-Timing` · **enmascarado de PII incluido** |
| Memory watchdog | ✅ Cada 2 min, límite 3.300 MB |
| Logs de Railway | ✅ Consultables por CLI |
| `ws_manager.get_stats()` | ✅ Métricas de conexión en tiempo real |

> 🔑 **La observabilidad del rediseño no se estrena: ya está construida.** `query_profiler` es la pieza a la que se acoplan las métricas nuevas.

### 7.2 Lo que falta — y es lo que ha dolido

**Todos los incidentes graves de este sistema los detectó una persona, no el monitoreo.**

| Incidente | Quién lo detectó | Tardanza |
|---|---|---|
| OpenAI sin créditos | Las asesoras | **2 días** |
| 217 envíos congelados | Una revisión manual | **33 días** |
| Contactos invisibles en el panel | Las asesoras | Desconocida |
| Usernames de WhatsApp rompiendo respuestas | 🔴 **Esta auditoría** | Semanas |

> 🔴 **Este es el NFR más importante del documento.** No es un problema de herramientas: es que **no hay ninguna alerta configurada**. El sistema puede fallar en silencio durante semanas y lo hace con regularidad.

### 7.3 Alertas mínimas exigidas

| # | Alerta | Umbral | Qué incidente habría evitado |
|---|---|---|---|
| **O-1** | Saldo de OpenAI bajo | 20 % restante | OpenAI sin créditos (2 días) |
| **O-2** | Elemento más viejo de la cola de propagación | > 1 h | Divergencias silenciosas |
| **O-3** | Elementos abandonados en la cola | Cualquiera > 0 | Pérdida silenciosa de escrituras |
| **O-4** | Trabajo encolado sin avanzar | 2 ciclos seguidos | **Los 217 envíos de 33 días** |
| **O-5** | Errores de envío de Twilio | > 3 en 10 min | **Los usernames de WhatsApp** |
| **O-6** | Firmas de Twilio inválidas | Cualquiera en modo `enforce` | Ataque o mala configuración |
| **O-7** | `SIGKILL` o RSS creciente | RSS > 2 GB | Recaída de fugas de memoria |
| **O-8** | Leads entrantes sin respuesta de SofIA | > 15 min | Fallo del motor de IA |

> **O-4 y O-5 son las dos que más rentabilidad tienen**, porque cubren la clase de fallo que este sistema comete: **encolar trabajo que nadie procesa** y **fallar al entregar sin que nadie mire**.

### 7.4 Dónde viven las alertas

🔵 **Propuesta:** empezar por lo más simple que funcione — un trabajo programado que evalúe los ocho umbrales y avise por WhatsApp al Administrador, reutilizando el envío que ya existe.

| A favor | En contra |
|---|---|
| No añade servicios ni coste | Si el sistema está caído del todo, la alerta tampoco sale |
| Reutiliza APScheduler y Twilio | Requiere una salvaguarda externa mínima |

> ⚠️ **Por eso A-1 pide un sondeo externo**: es el único que sobrevive a que el sistema entero esté caído. Todo lo demás puede vivir dentro.

---

## 8. Resumen — los NFR en una tabla

| Grupo | # | Requisito | Estado hoy |
|---|---|---|---|
| **Disponibilidad** | A-1 | 99,5 % en horario laboral | 🔵 Sin medir |
| | A-2 | Un reinicio no pierde mensajes | ✅ Se cumple |
| | A-3 | Un reinicio es invisible | ⚠️ Parcial |
| | A-4 | Recuperación < 60 s | ✅ Watchdog |
| | A-5 | SofIA nunca degrada al silencio | ✅ PR #14 |
| | A-6/7/8 | Memoria estable y singletons | ✅ RSS 307 MB medido |
| **PII** | P-1 | Ningún teléfono completo en logs | 🔴 **72 líneas** |
| | P-2 | Ningún contenido de mensaje | 🔴 **Al menos 1** |
| | P-3/4 | Etiquetas, nunca valores | ⚠️ Solo en el webhook |
| | P-5 | Exportaciones registradas | 🔴 No existe |
| | P-6 | `pii_validator` fuente única | ✅ |
| **Coste** | C-1 | Alerta de saldo | 🔴 **No existe** |
| | C-2/4 | Coste medido y visible | 🔴 No existe |
| | C-3 | Fallo del LLM escala | ✅ PR #14 |
| **Observabilidad** | O-1…O-8 | Ocho alertas | 🔴 **Ninguna existe** |

> 🔑 **La lectura honesta de esta tabla:** disponibilidad razonablemente cubierta, **PII incumplida contra su propia regla**, y **observabilidad inexistente**. Los tres incidentes más caros del proyecto salen de esa última fila.

---

## 9. Preguntas

| # | Pregunta | Mi recomendación |
|---|---|---|
| **N15-1** | ¿99,5 % en horario laboral es el objetivo correcto, o basta con "que no se note"? | 99,5 % permite ~3 h al mes de caída. Suena mucho; en la práctica es una tarde mala |
| **N15-2** | ¿Las alertas por WhatsApp al Administrador, o prefieres correo? | **WhatsApp.** Es donde ya mira todo el mundo |
| **N15-3** | ¿Hace falta derecho al olvido (borrar un contacto a petición del cliente)? | Hoy **nadie puede borrar** contactos. Si el negocio lo necesita, es trabajo aparte |

> **Ninguna bloquea nada.** N15-3 es la única que podría añadir alcance.
