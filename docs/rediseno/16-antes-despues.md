> Estado: ⚠️ Borrador v0 — reconstruido.
Columna ANTES: ✅ verificada contra el código y la auditoría. Es hecho.
Columna DESPUÉS: 🔵 derivada de los wireframes y de las decisiones A-01/A-02. Es hipótesis hasta que la confirmes.
Depende de: [D-03 Visión](03-vision.md) · [D-17 Benchmark](17-benchmark-crms.md)

---


## 1. Cambio de fondo


|  | ANTES ✅ | DESPUÉS 🔵 |
|---|---|---|
| Qué es el producto | Bot de WhatsApp con panel de apoyo | CRM operativo con WhatsApp como un canal |
| Entidad central | Conversación (`conv_meta:{phone}:{canal}`) | Lead / Contacto |
| Identidad del contacto | Teléfono + canal → la misma persona en dos canales son dos registros | ✅ Teléfono → **una persona**, un registro, varios orígenes |
| Cómo nace un contacto | Solo cuando alguien escribe | Mensaje entrante o alta manual (`+Lead`) ✅ dibujado |
| Pantallas | Una: la bandeja | Tres: `Dashboard · Lead · WhatsApp` ✅ dibujado |
| Dónde trabaja la asesora | Panel para chatear + HubSpot para el resto | Solo SofIA |
| Rol de HubSpot | Fuente autoritativa consultada en vivo | Backbone invisible, sincronizado en segundo plano |


---


## 2. Organización del trabajo — la contradicción sin resolver


|  | ANTES ✅ | DESPUÉS 🔵 |
|---|---|---|
| Eje organizador | Canal de origen | Función en el ciclo de vida |
| Estructura | `Canal → Equipo → Owner`, acoplados 1:1:1 | `Rol → Vista → Cola de trabajo` |
| Jubeny | 14 canales (`equipo_portales`) | ¿"A. Interna"? |
| Luisa | `finca_raiz` + `metrocuadrado` (`equipo_directo`) | ¿"A. Seguimiento"? |
| Regla implícita | *"atiendes lo que llega por tu portal"* | *"atiendes lo nuevo / haces seguimiento"* ✅ dibujado en `img08` |

> ✅ V-4 RESUELTA — no eran incompatibles: son dos dimensiones ortogonales.
El canal define de quién es el lead *al entrar* (dato configurable por el Administrador). El rol define qué puede hacer y qué ve esa persona. La transferencia manual cambia el dueño después.
Un lead de Finca Raíz ya contactado sigue siendo de Luisa, porque ella es dueña del canal *y* tiene el rol de A. Seguimiento. Las dos cosas son verdad a la vez.
`channels_registry.py` sobrevive, pero deja de ser código y pasa a ser dato administrable. El concepto "Equipo" desaparece.
Desarrollo completo en [D-02 §1](02-actores-y-roles.md).

---


## 3. Roles y permisos


| Dimensión | ANTES ✅ | DESPUÉS 🔵 |
|---|---|---|
| Control de acceso | No existe. Quien entra al panel ve lo que el frontend decida | Autorización en el backend, por rol |
| Roles | Ninguno. Solo 4 "equipos" de una persona cada uno | ✅ `A. Interna` · `A. Seguimiento` · `Marketing` · `Administrador` |
| Autenticación | Enlace sin credenciales | ✅ Iniciar sesión con Google + tabla propia de usuarios vinculada por `owner_id` |
| Usuario / Rol / Propietario | Un solo campo: `hubspot_owner_id` | Tres conceptos separados (patrón #1 del benchmark) |
| Visibilidad de etapas | Todas para todos | ✅ Interna 11 · Seguimiento 19 — matriz verificada contra la API |
| Visibilidad de columnas | N/A | Por rol. *(El caso de `Último Seguimiento` se retiró, pero el principio sigue para las etapas del Kanban)* |
| Owner que no es persona | `82598814` "Equipo de Marketing" | Marketing es un rol, no un owner |
| Colaboradores | Solo en Redis; HubSpot no los conoce | ✅ **Se eliminan.** Un contacto tiene un solo dueño |


---


## 4. Configuración y operación


| Para cambiar… | ANTES ✅ | DESPUÉS 🔵 |
|---|---|---|
| Una asesora | Editar `OWNERS_CONFIG` en `lead_assigner.py` + desplegar | Pantalla de administración |
| Un canal | Editar `channels_registry.py` + desplegar | Pantalla de administración |
| Una etapa del embudo | Editar código en varios sitios + desplegar | Pantalla de administración |
| Regla de asignación | Editar código + desplegar | Configuración |
| Una automatización | Editar `app.py` (APScheduler) + desplegar | ❓ ¿flujos configurables o se acepta que siga en código? |

> ✅ Antes verificado: auditoría §7.1 problema #1, prioridad ALTA — *"owner_ids en 4+ archivos"*.
Este bloque es el objetivo O-3 y el patrón #3 del benchmark. Es el más caro de los tres fundacionales.

---


## 5. Datos y fuentes de verdad


| Dimensión | ANTES ✅ | DESPUÉS 🔵 |
|---|---|---|
| Fuentes de verdad | 3 (Redis / MongoDB / HubSpot), HubSpot autoritativa | 3, pero con política formal por entidad (D-11) |
| Sincronización | `transfer_ownership()` + reconciliación cada 6 h | Igual, extendida a etapas, notas y citas |
| Dimensiones sincronizadas | 1 (ownership) | ~8 (contacto, etapa, propiedad, cita, tarea, nota, asignación, permisos) |
| Divergencias | Silenciosas, corregidas hasta 6 h después | ❓ ¿se acepta el mismo retardo con 8 dimensiones? |
| Puntos de escritura | 13, de los cuales 6 aún escriben a un solo sistema | Uno por entidad |
| Lectura para el Kanban | N/A | Proyección local — no llamada en vivo a HubSpot (límites de tasa) |

> 🔴 El riesgo número uno del rediseño. Hoy hay 3 fuentes desincronizadas para *una* dimensión y ya generó bugs en producción. Multiplicar por 8 sin una política formal reproduce el problema a escala.

---


## 6. Conversación y escalamiento


| Dimensión | ANTES ✅ | DESPUÉS 🔵 |
|---|---|---|
| Modelo | 4 estados (`BOT_ACTIVE`, `PENDING_HANDOFF`, `HUMAN_ACTIVE`, `IN_CONVERSATION`) + SET `bot_controlled_conversations` | Asignatario + historial de eventos (patrón #5) |
| ¿Quién atiende? | Se deduce del estado | Campo explícito: SofIA o una persona |
| Escalamiento | Cambio de estado silencioso | Evento auditable: quién, cuándo, por qué |
| Fallo conocido | Handoff diferido indefinidamente sin nombre → pérdida de leads ✅ | Sin condición de nombre; con tiempo límite |
| Conversación sin dueño | Posible → difusión global por WebSocket | Imposible: siempre hay asignatario (patrón #6) |
| Posponer | No existe (se simula con inactividad de 48 h + recolector a 90 días) | ❓ ¿hace falta "posponer"? |


---


## 7. Interfaz


| Dimensión | ANTES ✅ | DESPUÉS 🔵 (✅ dibujado) |
|---|---|---|
| Navegación | Bandeja única | `Dashboard · Lead · WhatsApp` |
| Vista de embudo | No existe | Kanban: `Nuevo · En Conversación · Visita Agendada · Visita Realizada · En Estudio` |
| Vista de tabla | No existe | `ID · Nombre · Número · Email · Lead · Fecha Creación · Fecha Último Seguimiento*` |
| Conmutar vistas | No existe | Kanban ↔ Tabla sobre el mismo conjunto filtrado |
| Dashboard | No existe | ✅ Por rol. Interna: 2 KPIs + 1 cola · Seguimiento: 3 KPIs + 2 colas |
| Detalle del lead | Datos básicos en el panel lateral | ✅ Dos cosas distintas: `Historial de Notas` (aportes por asesora) + `Historial de Contacto` (registro de eventos: etapas, intereses, citas) |
| Inmuebles consultados | ❌ Se detectan y se descartan | 🔵 Entidad `Interés` — N por contacto, con fecha y origen ❓ P-12 |
| Notas | ❌ no existen | Entidad de primer nivel, visible en tarjeta, detalle y chat |
| Filtros y búsqueda | Parcial | Globales, y guardables como vista |
| Cita | Gestionada aparte | Tarjeta editable dentro del hilo |
| Plantillas | Existen en el backend, invisibles | Selector en el composer |
| Notificaciones | 3 mecanismos con 3 persistencias (memoria / DOM / Redis) | Un modelo, varias presentaciones |
| Alta manual | ❌ | `+Lead` y `Nuevo contacto` |


---


## 8. Lo que no cambia


Importante decirlo: evita que desarrollo crea que hay que tocar todo.

| Se conserva | Por qué |
|---|---|
| SofIA (motor de IA, RAG, multi-agente) | Es la ventaja competitiva real. El benchmark lo confirma: los CRM WhatsApp-first tienen IA muy inferior |
| Twilio como transporte de WhatsApp | Sin motivo para cambiar |
| HubSpot como persistencia | Decisión A-02 |
| Redis como estado operativo | Correcto para su función |
| PgVector + base de conocimiento | Independiente del rediseño |
| Detección de canal (`link_detector`) | Sigue siendo válido — pero como metadato de origen, no como eje organizador |
| Modelo contact-centric sin Deals | ✅ Confirmado por los wireframes: el Kanban mueve contactos, no negocios |


---


## 9. Magnitud del cambio


| Área | Cambio |
|---|---|
| Motor de IA | 🟢 ~0 % |
| Integraciones (Twilio, Bunny, PgVector) | 🟢 ~10 % |
| Sincronización con HubSpot | 🟡 ~40 % — se extiende a más entidades |
| Modelo de datos | 🔴 ~80 % — la entidad central cambia |
| Estado conversacional | 🔴 ~70 % — de estados a asignatario + eventos |
| Autorización | 🔴 100 % — no existe nada |
| Frontend | 🔴 ~85 % — de una pantalla a tres |
| Configuración operativa | 🔴 ~90 % — de código a datos |

> Esto valida el "80%": el motor se conserva, todo lo que rodea al motor se rehace.

---


## 10. Preguntas que este documento deja abiertas


| # | Pregunta | Bloquea |
|---|---|---|
| AD-1 | V-4: ¿canal o función como eje organizador? | Todo |
| AD-2 | ¿Un contacto es una persona o una persona-por-canal? | Modelo de datos |
| AD-3 | ✅ RESUELTO. Son dos listas separadas: *Historial de Notas* (quién anotó qué) y *Historial de Contacto* (timeline de embudos, intereses y citas) | Ver D-01 |
| AD-4 | ¿Las automatizaciones pasan a ser configurables o se quedan en código? | Alcance, esfuerzo |
| AD-5 | ¿Se conservan los colaboradores, sabiendo que HubSpot no puede persistirlos? | D-11 |
| AD-6 | ¿Qué retardo de sincronización es aceptable por entidad? | D-11 |
| AD-7 | ✅ RESUELTO indirectamente. El modelo pasa a *asignatario + eventos*; "posponer" deja de necesitar un estado propio | Ver [D-02 §18](02-actores-y-roles.md) |
| AD-8 🆕 | ¿Se aprueba la entidad Interés (un contacto, varios inmuebles)? | D-10 — bloquea la Fase 2 |
