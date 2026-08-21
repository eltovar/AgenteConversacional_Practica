> Estado: ⚠️ Borrador v0 — reconstruido, no dictado.
`Planos_Rediseno_SofIA_v1.docx` se perdió. Este documento se reconstruyó desde: los wireframes de `DOCUMENTACION.docx`, la auditoría de ownership, el código, el historial de git y las decisiones registradas.
Todo lo marcado 🔵 es hipótesis derivada de evidencia. Todo lo marcado ✅ está confirmado por artefacto.

---


## 1. La visión, en una frase

> 🔵 SofIA deja de ser un bot de WhatsApp con un panel de apoyo, y pasa a ser el único lugar donde una asesora de Inmobiliaria Proteger trabaja: el CRM operativo de la empresa, con HubSpot invisible por detrás y la IA como un miembro más del equipo.

Evidencia que la sostiene:
- ✅ Los wireframes definen una navegación de tres secciones — `Dashboard · Lead · WhatsApp`. Hoy el sistema solo tiene la tercera.
- ✅ Decisión A-02: las asesoras nunca abren HubSpot.
- ✅ Existe una vista Kanban de pipeline dibujada, con creación manual de leads (`+Lead`) — funcionalidad de CRM, no de bandeja de mensajes.

---


## 2. Qué es hoy vs. qué será


|  | Hoy | Destino |
|---|---|---|
| Naturaleza | Bandeja de mensajes de WhatsApp con IA | CRM operativo con WhatsApp como un canal |
| Entidad central | La conversación | El Lead / Contacto |
| Unidad de trabajo | "Responder el chat que llegó" | "Mover el lead por el embudo" |
| Origen de los datos | El mensaje entrante | El lead (venga de donde venga, incluido alta manual) |
| Dónde vive la verdad del negocio | Repartida entre Redis, MongoDB y HubSpot | HubSpot como backbone, SofIA como única interfaz |

> El cambio de fondo no es de funcionalidad, es de entidad central. Hoy todo cuelga de la conversación (por eso la clave de Redis es `conv_meta:{phone}:{canal}` y una persona en dos canales son dos registros). En el destino todo cuelga del Lead. Ese solo cambio justifica el "80% de rediseño".

---


## 3. Objetivos


Cada objetivo está anclado a evidencia documentada, no a intuición.

### O-1 · Que el trabajo comercial ocurra en un solo lugar


🔵 Hoy la asesora usa el panel para chatear y HubSpot para todo lo demás. El destino elimina el segundo. Medible por: número de veces que una asesora abre HubSpot por semana → 0.

### O-2 · Que cada rol vea solo lo que le corresponde


✅ Confirmado por wireframe `img08`: el Dashboard separa "Clientes a Atender (Rol A. Interna)" de "Clientes para Seguimiento (Rol A. Seguimiento)". ✅ Confirmado por decisión previa: Interna ve 10 etapas, Seguimiento ve 16. ⚠️ HubSpot no puede hacer esto sin licencia Enterprise (~$800/mes). Este objetivo es la razón económica del rediseño.

### O-3 · Que cambiar la operación no requiera desplegar código


✅ Problema #1 de la auditoría, prioridad ALTA: *"Config hardcoded sin single source of truth — owner_ids en 4+ archivos"*. Hoy, cambiar una asesora, un canal, una etapa o una regla de asignación exige editar Python y hacer deploy. Medible por: cambios operativos que requieren deploy → 0.

### O-4 · Que ningún lead se pierda por un fallo del sistema


✅ Documentado en la auditoría: *"el handoff se difiere indefinidamente [...] el usuario confirmó que genera pérdida de leads"*. ✅ Documentado como principio de UX: *"Los contactos deben aparecer inmediatamente cuando escriben. 'Llegar tarde' es el bug más reportado."*

### O-5 · Que el sistema sea invisible cuando falla


✅ Principio de UX registrado: reinicios completamente silenciosos, sin mensajes de error rojos a las asesoras — pero un mensaje que falló al enviarse sí debe ser visible. 🔵 Reforzado por el historial: crisis de memoria de 31 GB, 11 fugas en 4 rondas, `SIGKILL` cada ~2h15m. La estabilidad es un requisito de producto, no de infraestructura.

### O-6 · Que se pueda auditar qué hizo la IA


🔵 Inferido, no confirmado. SofIA escribe datos de negocio (crea contactos, cambia etapas). Si el CRM es la fuente operativa, tiene que registrar la autoría.
> ❓ ¿Es objetivo o lo saco?

---


## 4. No-objetivos (igual de importantes)


| No haremos | Por qué |
|---|---|
| Multi-tenant / SaaS para otras inmobiliarias | ✅ Decisión A-01. Un solo tenant: Inmobiliaria Proteger. |
| Reemplazar o migrar fuera de HubSpot | ✅ Decisión A-02. HubSpot se queda como backbone. |
| Sustituir a la asesora con IA | 🔵 SofIA atiende y escala. Los wireframes ponen a la persona en el centro. |
| Reescribir desde cero | 🔵 El sistema está vivo con clientes reales. ❓ Confirmar. |
| App móvil nativa | 🔵 Los wireframes son de escritorio. ❓ Confirmar — las asesoras en visita usan el celular. |


---


## 5. Alcance funcional derivado de los wireframes ✅


Esto no es hipótesis: está dibujado.

### 5.1 Dashboard (img08)

- Tarjetas KPI: Chat Nuevos (nº de contactos nuevos), Citas Hasta Ahora, y una tercera sin definir → ❓
- Tabla Clientes a Atender (Rol A. Interna): `ID · Nombre · Número · Lead · Portal · Fecha llegada`
- Tabla Clientes para Seguimiento (Rol A. Seguimiento): `ID · Nombre · Número · Lead · Fecha llegada · Último Seguimiento`

### 5.2 Lead — dos vistas conmutables (img02, img06, img01, img07, img09)


Vista Kanban con columnas: `Nuevo · En Conversación · Visita Agendada · Visita Realizada · En Estudio`
- Tarjeta: `Nombre · Fecha creación · Asignado · Nota`
- Punto rojo = no leído · Ícono de calendario en "Visita Agendada"
- Botón `Nuevo Lead` al pie de columna

Vista Tabla con columnas: `ID · Nombre · Número · Email · Lead · Fecha Creación · Fecha Último Seguimiento (si interfaz de Seguimiento)`
> ✅ Nota literal del wireframe: *"Fecha Último Seguimiento (Si interfaz de Seguimiento)"* — la columna aparece según el rol. Confirma O-2 a nivel de campo, no solo de registro.

Comunes: Buscar · Filtro · `+Lead` (alta manual) · conmutador Kanban ↔ Tabla Panel de detalle: `Nombre · Email · Número · Historial de Notas · Historial de Contacto` Chat embebido: desde el Kanban se abre un modal con Lead Info a la izquierda y la conversación a la derecha (`img04`)

### 5.3 WhatsApp (img05)

- Botón Nuevo contacto · Buscar · Filtro
- Lista de chats · Conversación · Panel derecho: `Asignado · Lead · Canal · Nota · Lead Info (Nombre, Número, Email)` + enlace "Ver historial de contacto"
- Composer: adjuntar, audio, plantillas
- Tarjeta de Cita embebida en el hilo: fecha límite, recordatorio, botón `EDITAR`

### 5.4 Navegación cruzada ✅


Los wireframes dibujan flechas explícitas: WhatsApp → "Ver historial de contacto" → detalle del Lead, y Kanban → abrir chat. Las tres secciones no son islas: son vistas del mismo Lead.

---


## 6. Capacidades nuevas que esto implica (y hoy no existen)


| Capacidad | ¿Existe hoy? |
|---|---|
| Vista Kanban de pipeline con arrastrar y soltar | ❌ |
| Vista tabla de leads con columnas por rol | ❌ |
| Dashboard con KPIs y colas por rol | ❌ |
| Alta manual de un Lead (`+Lead`) | ❌ — hoy un contacto solo nace de un mensaje entrante |
| Iniciar conversación con un contacto nuevo | ⚠️ Parcial (campañas masivas) |
| Notas como entidad | ❌ |
| Historial de Notas e Historial de Contacto separados | ❌ |
| Filtros y búsqueda global | ⚠️ Parcial |
| Roles y permisos | ❌ No existe control de acceso |
| Cita editable desde el hilo del chat | ⚠️ Parcial |

> 10 capacidades, 7 inexistentes. Esto confirma la magnitud del "80%": no es refactor, es producto nuevo sobre motor existente.

---


## 7. Lo que sigue faltando — y no está en ningún archivo


Esto no lo puedo reconstruir. Necesito que lo respondas.

| # | Pregunta | Por qué bloquea |
|---|---|---|
| V-1 | ¿Por qué ahora? ¿Qué pasó concretamente que hizo insostenible seguir así? | La visión sin el dolor que la origina no convence a nadie ni prioriza nada |
| V-2 | ¿Cómo sabremos que funcionó? Dame 3 números: hoy y meta. (leads/mes, % que llegan a visita, tiempo de primera respuesta…) | Sin métricas, O-1 a O-6 no son verificables |
| V-3 | Volúmenes reales: ¿cuántos leads entran al mes? ¿cuántas conversaciones activas simultáneas? | Determina si esto es un CRM para 2 personas o para 20 |
| V-4 | ¿Quiénes son "A. Interna" y "A. Seguimiento" hoy? ¿Son Jubeny y Luisa, o son funciones que hoy hace la misma persona? | El código tiene *equipos por canal*; los wireframes tienen *roles por función*. Son dos modelos distintos y hay que elegir uno |
| V-5 | ✅ RESUELTA. El Dashboard es por rol. En A. Seguimiento las tarjetas son `Chat Nuevos Finca Raíz` · `Chat Nuevos Metro Cuadrado` · `Citas Hasta Ahora`. La tercera tarjeta vacía era la del Dashboard de A. Interna, que sigue sin definir | Ver [D-02 §8](02-actores-y-roles.md) |
| V-6 | ¿Fecha objetivo y quién desarrolla? | Define el nivel de detalle de toda la documentación |
| V-7 | ¿El sistema sigue vivo durante el rediseño? | Determina si hace falta estrategia de coexistencia (D-18) |


---


## 8. Contradicción detectada — hay que resolverla ✅


Este es el hallazgo más importante de la reconstrucción:

El código organiza el trabajo por CANAL. Los wireframes lo organizan por FUNCIÓN.

|  | Modelo del código | Modelo de los wireframes |
|---|---|---|
| Eje | Canal de origen | Momento del ciclo de vida |
| Jubeny | 14 canales | ¿"A. Interna"? |
| Luisa | `finca_raiz` + `metrocuadrado` | ¿"A. Seguimiento"? |
| Regla | *"tú atiendes lo que llega por tu portal"* | *"tú atiendes lo nuevo, tú haces seguimiento"* |


Son incompatibles. Un lead de Finca Raíz que ya fue contactado, ¿es de Luisa (canal) o de la asesora de seguimiento (función)?
> ✅ V-4 RESUELTA (2026-08-05). No era «canal *o* función»: son dos dimensiones ortogonales que conviven.
El canal determina de quién es el lead *al entrar* (dato configurable). El rol determina qué puede hacer y qué ve esa persona. La transferencia manual cambia el dueño después.
`channels_registry.py` sobrevive, pero deja de ser código y pasa a ser dato administrable. El concepto "Equipo" desaparece.
Desarrollo completo en [D-02 §1](02-actores-y-roles.md).

---

## 9. Ejecución — confirmado 2026-08-15

| Pregunta | Respuesta |
|---|---|
| **¿Quién desarrolla?** | CyberTovar junto a Claude Code. Planeación y desarrollo con el mismo equipo |
| **¿El sistema sigue vivo?** | ✅ **Sí.** `main` continúa operando en producción sin interrupciones |
| **¿Dónde se construye?** | En una **rama separada**, mientras `main` sigue atendiendo clientes reales |

### Lo que esto implica para el plan

| Implicación | Detalle |
|---|---|
| **Coexistencia obligatoria** | Durante todo el rediseño hay dos sistemas: el que atiende clientes y el que se construye. → **D-18 estrategia de migración deja de ser opcional** |
| **La documentación es el contrato** | Al desarrollar con Claude Code, la documentación *es* la especificación de entrada. Cada ambigüedad que quede sin cerrar se convierte en una decisión improvisada dentro del código |
| **Entrega por partes** | La rama tiene que poder integrarse por trozos, no en un único salto. → afecta al roadmap **D-20** |
| **Sin ventana de congelamiento** | Nada de lo que rompa producción puede fusionarse. Los cambios en HubSpot (renombrar `Seguimiento`→`Post Cita`, crear `Nuevo Lead`) **afectan al sistema vivo** y hay que secuenciarlos con cuidado |

> 🔴 **Riesgo identificado:** los dos cambios pendientes en HubSpot tocan el pipeline **que hoy está en producción**. Renombrar una etapa que el código actual consulta por ID es seguro (el ID no cambia), pero crear `Nuevo Lead` y empezar a usarla **sí** altera el flujo vivo. Debe ir en D-18 con su plan de reversión.

---

## 10. ✅ Por qué ahora — respondido 2026-08-15

Las seis razones que originan el rediseño, en palabras de CyberTovar:

| # | Razón | Naturaleza |
|---|---|---|
| 1 | Las asesoras perdían **media hora al día buscando contactos** | Síntoma · coste de tiempo |
| 2 | **Un cambio pequeño exige un deploy**, cuando podría hacerse con datos | Síntoma · rigidez |
| 3 | Había **leads que nunca aparecían en el panel** | Síntoma · pérdida de negocio |
| 4 | A veces **no se veían los contactos con badge** en el panel | Síntoma · fallo de visibilidad |
| 5 | A las asesoras **no les gusta cambiar de pestaña constantemente** | Síntoma · fricción diaria |
| 6 | **Se está implantando una estrategia de ventas por roles y tareas.** Aplicarla exige cambiar la arquitectura y el diseño | 🔑 **Causa** |

### 🔑 Cinco son síntomas. La sexta es la causa

Las cinco primeras son problemas del sistema actual: se podrían arreglar uno a uno sin rediseñar nada. **La sexta es de otra naturaleza: no es un fallo, es un cambio en el negocio.**

> **La empresa cambió su forma de vender — a un modelo por roles y tareas — y el sistema actual no puede representarla.**

Eso es lo que convierte esto en un rediseño y no en una lista de correcciones. Un sistema construido alrededor de *"una bandeja de WhatsApp compartida"* no puede sostener una operación organizada en *A. Interna → A. Seguimiento → supervisión → marketing*, con visibilidad distinta por rol y configuración sin deploy.

### Cómo ordena esto el trabajo

| Razón | Objetivo que la recoge | Se cierra con |
|---|---|---|
| 1 y 5 · tiempo perdido, cambio de pestaña | **O-1** un solo lugar de trabajo | Las tres navegaciones por rol |
| 2 · deploy para cambios menores | **O-3** configurar sin desplegar | Pantalla *Team Members* + registro de canales |
| 3 y 4 · leads invisibles | **O-4** ningún lead se pierde | Colas por rol + asignatario siempre explícito |
| 6 · estrategia de ventas | **O-2** cada rol ve lo suyo | Roles, matriz de etapas, permisos en el backend |

> **Criterio de prioridad para el roadmap (D-20):** la razón 6 es la que justifica el proyecto ante el negocio; las razones 3 y 4 son las que **cuestan dinero cada día**. El orden de entrega debería atacar primero lo que evita perder leads, y en paralelo construir la base de roles que exige la estrategia.

> ⏳ **Sigue faltando:** los **tres números de referencia** (leads al mes, % que llega a visita, tiempo de primera respuesta), hoy y meta. Sin ellos, O-1 a O-6 no son verificables. Se pueden extraer de HubSpot y Twilio si se autoriza.

---

## 11. Metricas base — medidas contra HubSpot el 2026-08-20

Consulta directa a la API. **Son los numeros reales de la operacion**, no estimaciones.

### 11.1 Volumen

| Metrica | Valor |
|---|---|
| Contactos totales en la base | **3.209** |
| Leads creados en los ultimos 30 dias | **578** |
| Promedio diario | **~19 leads/dia** |
| Contactos **sin dueno asignado** | **0** |

> La ausencia total de contactos huerfanos indica que la asignacion automatica por canal **funciona**. El problema del sistema actual no es asignar: es **mostrar**.

### 11.2 Distribucion por etapa (estado actual)

| Etapa | Contactos | % de la base |
|---|---|---|
| **No Responde** | **1.220** | **38,0 %** |
| Visita Realizada | 81 | 2,5 % |
| En Conversacion | 80 | 2,5 % |
| Cerrado Ganado | 29 | 0,9 % |
| Visita Agendada | 21 | 0,7 % |
| Resto de etapas | 1.778 | 55,4 % |

### 11.3 `No Responde` — 1.220 contactos, comportamiento correcto

**`No Responde` concentra el 38 % de la base — 1.220 contactos.**

> ⚠️ **Correccion (2026-08-20).** En una version anterior de este documento se interpretó esta cifra como *"leads invisibles"* y se ligó a la razon n.o 3 del *"por que ahora"*. **Era una lectura equivocada.**
>
> CyberTovar aclaró que **es el comportamiento esperado**: el auto-cierre a `No Responde` está programado a propósito para cumplir el objetivo de roles con el sistema actual. Los *"leads invisibles"* de la razon n.o 3 eran otra cosa — contactos nuevos que no aparecían en el panel tras el handoff de SofIA — y **ese problema ya está resuelto** en la arquitectura actual.

**Qué sigue importando de esta cifra, y qué no:**

| | |
|---|---|
| ❌ **No hace falta** analizar cuántos son recuperables | Es una decisión comercial, no de arquitectura |
| ❌ **No hay riesgo** de inundar la bandeja de A. Seguimiento | Con la Opción B, `No Responde` deja de ser etapa y pasa a ser **propiedad de cierre**: no ocupa columna en el Kanban |
| ✅ **Sí importa como volumen de migración** | Es el **mayor bloque de datos** a reescribir en D-18: 1.220 registros cuyo `lifecyclestage` hay que repartir entre etapa y propiedad de cierre |

### 11.4 Los tres numeros de referencia (V-2)

| # | Metrica | **Hoy** | Meta | Origen |
|---|---|---|---|---|
| 1 | **Leads al mes** | **578** | pendiente | HubSpot |
| 2 | **% que llega a visita** | **6,1 %** *(35 de 578 en la cohorte de 30 dias)* | pendiente | HubSpot |
| 3 | **Tiempo de primera respuesta** | no disponible | pendiente | Requiere MongoDB (`messages`), no esta en HubSpot |

> **Como leer estos porcentajes.** `lifecyclestage` guarda el **estado actual**, no un acumulado: un contacto que paso por *Visita Realizada* y avanzo a *Cerrado Ganado* ya no cuenta en la primera. Por eso **6,1 % es un suelo, no una cifra exacta**.
> Medir bien la conversion exige el **historial de cambios de etapa**, que es justo lo que aporta el `Historial de Contacto` como registro de eventos (D-02 seccion 19). **Hoy el sistema no puede medir su propio embudo.**

> **Falta el numero 3.** El tiempo de primera respuesta esta en la coleccion `messages` de MongoDB. Necesita acceso a la base de produccion, no a HubSpot.
> **Faltan las metas.** Los valores de "hoy" ya estan; las metas las pone el negocio.

