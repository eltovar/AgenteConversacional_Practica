# D-11 · Politica de fuente de verdad por entidad

> **Estado:** borrador v0 — semilla creada el 2026-08-20 con la respuesta a D11-1.
> **Por que este documento es el riesgo n.o 1:** hoy hay 3 fuentes de verdad desincronizadas para **una** dimension (ownership) y ya genero fallos en produccion. El CRM completo tiene ~8 dimensiones.

---

## 1. Aclaracion previa — NTP, SNTP y PTP no aplican aqui

Se plantearon NTP, SNTP y PTP como posibles respuestas. **Resuelven un problema distinto.**

| | Que sincroniza |
|---|---|
| **NTP · SNTP · PTP** | **El reloj** de los servidores — que todos coincidan en que son las 14:32:07 |
| **Lo que pregunta D11-1** | **Los datos** entre Redis, MongoDB y HubSpot — cuanto tiempo pueden estar en desacuerdo sobre quien es el dueno de un contacto |

> No hace falta ningun protocolo de reloj: Railway, MongoDB Atlas y HubSpot ya mantienen sus relojes sincronizados por NTP de fabrica.
> **El problema real es de propagacion de datos**, y se resuelve con arquitectura, no con protocolos de tiempo.
>
> ⚠️ Lo unico relacionado con relojes que **si** afecta a este sistema esta documentado aparte: Mongo devuelve fechas sin zona horaria y compararlas con hora local produce errores de 5 horas. Es un problema de **manejo de zonas horarias**, no de sincronizacion de reloj.

---

## 2. Los cuatro niveles de retardo

| Nivel | Retardo | Como funciona | Coste |
|---|---|---|---|
| **S · Sincrono** | 0 s | La escritura no termina hasta que los 3 sistemas confirman | Si HubSpot esta lento o caido, **la asesora no puede trabajar** |
| **C · Casi inmediato** | < 5 s | Escritura local sincrona + propagacion en segundo plano con reintentos | Bajo |
| **D · Diferido** | minutos | Se encola y se procesa por lotes | Muy bajo |
| **R · Reconciliacion** | horas | Un trabajo periodico compara y corrige | Minimo — **es lo que hay hoy: 6 h** |

> 🔑 **Principio rector:** la escritura en la base **propia** es siempre sincrona. Lo que se difiere es **la propagacion hacia HubSpot**.
> Asi la asesora nunca espera por HubSpot, y HubSpot nunca bloquea la operacion.
> **La reconciliacion deja de ser el mecanismo principal y pasa a ser la red de seguridad.**

---

## 3. Retardo recomendado por entidad

| Entidad | Nivel | Retardo | Por que |
|---|---|---|---|
| **Usuario · Rol · Permiso** | **S** | **0 s** | Es seguridad. Un permiso revocado tiene que ser efectivo al instante |
| **Propiedad del contacto (owner)** | **C** | **< 5 s** | Si diverge, dos asesoras ven el mismo contacto como suyo |
| **Etapa del contacto** | **C** | **< 5 s** | El Kanban es la pantalla principal; arrastrar y que rebote es inaceptable |
| **Mensaje / conversacion** | **C** local, **D** hacia HubSpot | inmediato / minutos | El chat no puede esperar a HubSpot. HubSpot solo necesita el historial |
| **Cita** | **C** | < 30 s | Dispara recordatorios y la plantilla post-cita |
| **Nota** | **D** | < 1 min | Nadie la consulta en tiempo real |
| **Interes / enlace** | **D** | minutos | Dato de analisis, no operativo |
| **Metricas agregadas** | **R** | horas | Se recalculan por lotes |

---

## 4. Que cambia respecto de hoy

| | Hoy | Propuesta |
|---|---|---|
| Mecanismo principal | Reconciliacion cada 6 h | **Propagacion en segundo plano con reintentos** |
| Papel de la reconciliacion | Corrige divergencias reales a diario | **Red de seguridad** — no deberia encontrar nada |
| Si HubSpot esta caido | Se acumulan divergencias silenciosas | La cola retiene y reintenta; la operacion sigue |
| Entidades cubiertas | 1 (ownership) | 8 |

> ⚠️ **Consecuencia:** hace falta una **cola de propagacion persistente** con reintentos y registro de fallos. Es la pieza tecnica que hoy no existe y que sostiene toda esta politica.

---

## 5. La cola de propagacion

> ✅ **Resuelto en [ADR-006](14-adrs.md).** Era la pieza que sostenia toda esta politica y la que faltaba.

**Decision:** patron *outbox*. Toda escritura hacia HubSpot se registra como fila en una coleccion propia (`PROPAGACION`) y un trabajador la entrega de forma asincrona con reintentos.

**Lo importante para esta politica** — el resto del diseno esta en el ADR:

| Garantia | Como |
|---|---|
| **Ninguna escritura se pierde** | La fila es durable. Si HubSpot esta caido, la cola retiene y reintenta |
| **La asesora no espera a HubSpot** | La escritura local es sincrona; la propagacion, diferida. Hoy un 429 cuesta **36 s de pantalla congelada** |
| **El orden por contacto se respeta** | `clave_orden = contacto_id`. Sin esto, `Visita Agendada` y `Visita Realizada` pueden entregarse al reves y dejar la etapa mal **de forma permanente** |
| **Un fallo permanente se ve** | Estado `abandonado`, visible para el Administrador. Nunca se descarta en silencio |
| **El retardo nunca empeora** | El ultimo escalon de reintento es de 6 h — exactamente el ciclo de la reconciliacion de hoy |

> 🔑 **No se disena desde cero.** `bulk_campaigns` ya es una cola de trabajo durable con reclamo atomico, arrendamiento, idempotencia y exclusion mutua. ADR-006 la generaliza.

### Metricas de salud de la sincronizacion

Cuatro numeros, definidos en ADR-006 §3.5:

| Indicador | Alarma |
|---|---|
| Edad del elemento pendiente mas viejo | 🔴 > 1 h |
| Profundidad de la cola por estado | 🟠 crece dos ciclos seguidos |
| Elementos abandonados sin resolver | 🔴 cualquiera > 0 |
| Reclamos caducados rescatados por ciclo | 🟠 > 0 sostenido |

### Comportamiento ante conflicto

| Caso | Quien gana |
|---|---|
| Entidad **compartida** (contacto, etapa, propietario, nota, cita) | **HubSpot.** Es lo que ya hace `reconcile_owner_ids` y no hay motivo para cambiarlo |
| Entidad **de la base propia** (usuario, rol, canal, interes, evento) | La base propia. HubSpot no las conoce — ver §6 |
| Escritura local con propagacion **pendiente** | **La local.** La cola aun no ha entregado; HubSpot esta desactualizado por diseno, no en conflicto |

> ⚠️ **Queda pendiente** la tabla campo por campo de las entidades compartidas. Se cierra al escribir el esquema de HubSpot en la fase A de [D-18](18-migracion.md), no antes: hasta que no existan las propiedades no se sabe cuales son.

---

## 6. Hallazgo de D-10: tres entidades no caben en HubSpot

Al construir el modelo de datos aparecio un limite que matiza la decision A-02.

| Entidad | ¿Tiene sitio en HubSpot? |
|---|---|
| `INTERES` | ❌ **No.** Requeriria un objeto personalizado o Deals — ambos descartados |
| `EVENTO` | ❌ **No.** El timeline de HubSpot cubre una parte, no los 22 tipos |
| `CANAL_ASIGNADO` | ❌ **No.** Es configuracion del CRM propio, no dato de contacto |

> 🔑 **Matiz a la decision A-02.** HubSpot es el backbone **de los contactos**, no de todo el modelo. Tres entidades del rediseno viven **solo** en la base propia y no se replican.
>
> **No es un problema, es una precision necesaria.** Significa que:
> 1. La base propia deja de ser una cache de HubSpot y pasa a ser **autoritativa para una parte del dominio**
> 2. Si algun dia se quisiera prescindir de HubSpot, la parte nueva ya seria independiente
> 3. La reconciliacion solo aplica a las entidades **compartidas**: contacto, etapa, propietario, nota, cita

### Reparto de autoridad

| Grupo | Autoridad | Se reconcilia |
|---|---|---|
| Contacto, etapa, propietario, nota, cita | Compartida — HubSpot espeja | ✅ Si |
| Usuario, rol, canal asignado | **Base propia** | ❌ No aplica |
| Interes, evento | **Base propia** | ❌ No aplica |
| Conversacion y mensajes | Base propia; HubSpot recibe historial | Parcial |

> ❓ **Pregunta abierta:** ¿`presupuesto` y `segmento` se replican a HubSpot creando propiedades nuevas, o viven solo en la base propia? Si Marketing o direccion los consultan alguna vez desde HubSpot, hay que replicarlos.

