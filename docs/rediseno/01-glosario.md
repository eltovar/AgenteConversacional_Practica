> Estado: ⚠️ Borrador v0 — construido por ingeniería inversa del código, no por decisión.
Cómo usar este documento: cada término tiene lo que el *código* dice hoy. Donde hay ambigüedad hay un bloque `❓ DECISIÓN PENDIENTE`. Responde solo esos bloques; el resto es solo para que valides que no me equivoqué.
Por qué este documento va primero: de los 6 problemas arquitectónicos de la auditoría, 3 son problemas de lenguaje, no de código (`canal` vs `equipo` vs `owner` acoplados 1:1:1; `whatsapp` vs `whatsapp_directo`; enum `PortalOrigen` desalineado con `CHANNEL_TO_TEAM`). No se arreglan refactorizando. Se arreglan decidiendo qué significa cada palabra.

---


## 1. Entidades del dominio


### Contacto


Persona que escribe por WhatsApp. Identificada por número de teléfono normalizado (`PhoneNormalizer.normalize()`).

Hoy existe simultáneamente como tres registros distintos:

| Sistema | Representación | Identificador |
|---|---|---|
| Redis | `conv_meta:{phone}:{canal}` | teléfono + canal |
| MongoDB | documento en `conversations` | teléfono |
| HubSpot | `contact` | `contact_id` |

> ✅ RESUELTO — Un Contacto es UNA PERSONA (un telefono). Si vuelve meses despues por otro inmueble, es el mismo contacto con un `Interes` nuevo. La clave `conv_meta:{phone}:{canal}` debe cambiar.
> *(Bloque original conservado como rastro de la discusion:)*
> ❓ Pregunta original
La clave de Redis incluye el canal: `conv_meta:{phone}:{canal}`. Eso significa que hoy, una misma persona que escribe por Instagram y luego por Finca Raíz produce dos registros, con dueños potencialmente distintos.
- ¿Un Contacto es una persona (un teléfono) o una persona-por-canal?
- Si una persona escribe por dos canales, ¿es un contacto con dos orígenes, o dos contactos?
- Si es uno solo: ¿quién es el dueño cuando los dos canales tienen owners distintos (Luisa en Finca Raíz, Jubeny en Instagram)?
*No respondas técnicamente. Respóndeme qué esperaría ver la asesora en el panel.*

### Conversación


Hilo de mensajes entre un Contacto y el sistema.
> ✅ RESUELTO — No habra modulo de archivado. Una conversacion no se cierra ni se archiva.
> ❓ Pregunta original
¿Una Conversación termina alguna vez? Hoy no: el registro es perpetuo y el estado hace ping-pong. En un CRM real suele haber conversaciones cerradas y archivadas.
Si el cliente vuelve a escribir 8 meses después, ¿es la misma conversación o una nueva?

### Propiedad / Inmueble

> ✅ RESUELTO — La entidad Propiedad NO entra al alcance. Entra `Interes`, que referencia un codigo de inmueble sin modelar la propiedad completa.
> ❓ Contexto original: NO EXISTE COMO ENTIDAD HOY. Solo hay detección de *códigos* de propiedad en texto (`utils/property_code_detector.py`). Un CRM inmobiliario sin entidad Propiedad no es un CRM inmobiliario.
¿Entra en el alcance del rediseño? ¿De dónde salen los datos: HubSpot, portal, carga manual?

### Cita / Visita


Agendamiento de visita a un inmueble. Gestionada por `middleware/appointment_manager.py`. Refleja dos etapas del pipeline: `marketingqualifiedlead` (agendada) → `salesqualifiedlead` (realizada), con auto-transición 1h30m después de la cita.

---


## 2. El nudo: Canal, Equipo, Owner


Esta es la zona de mayor confusión del sistema actual. Hoy los tres conceptos están acoplados rígidamente.

### Canal


Origen por el que llegó el Contacto. Definido en `utils/channels_registry.py` (16 canales).

| Categoría | Canales |
|---|---|
| `PORTAL` | `finca_raiz`, `metrocuadrado`, `mercado_libre`, `ciencuadras`, `pagina_web` |
| `SOCIAL` | `instagram`, `facebook`, `linkedin`, `youtube`, `tiktok` |
| `DIRECT` | `whatsapp_directo`, `whatsapp` |
| Otros | `desconocido`, `google_ads`, `referido`, `default` |


Cada canal declara: `team`, `owner_id`, `category`, `score_bonus`, `link_patterns`.
> ✅ RESUELTO — Se fusionan en `whatsapp_directo`. `whatsapp` queda como alias de migracion.
> ❓ Pregunta original — `whatsapp` vs `whatsapp_directo`
Son dos canales distintos en el registry pero se tratan diferente según el archivo (problema #6 de la auditoría). ¿Cuál es la diferencia de negocio, o son el mismo y hay que fusionarlos?
> ❓ DECISIÓN PENDIENTE — `default` y `desconocido`
Ambos existen como canales y ambos apuntan a Jubeny. ¿`default` es un canal real o un valor de fallback que se coló en la lista? Un valor de fallback no debería ser un canal.

### Equipo


Agrupación de asesoras. ⚠️ Corregido 2026-08-26: se declara en `utils/advisors_registry.py`, de donde `lead_assigner.py::OWNERS_CONFIG` la deriva: `equipo_portales`, `equipo_directo`, `equipo_marketing`, `equipo_respaldo`, `default`.
> ⚠️ Los equipos hoy no agrupan a nadie: cada uno tiene exactamente una persona.
`equipo_portales` = Jubeny. `equipo_directo` = Luisa. `equipo_respaldo` = Mónica. `equipo_marketing` = Marketing.
El concepto "Equipo" es una capa de indirección que no hace nada — hereda de cuando había round-robin, que ya se eliminó.
✅ RESUELTO — El concepto "Equipo" queda ELIMINADO. La relacion canal→persona es directa y configurable.
> ❓ Pregunta original: ¿Se conserva "Equipo" en el CRM nuevo (porque van a crecer y habrá varias asesoras por equipo), o se elimina y el canal apunta directo a la persona?

### Owner / Propietario


La asesora responsable de un Contacto. Se materializa como `hubspot_owner_id`.

| Owner ID | Nombre | Equipo | Asignación automática |
|---|---|---|---|
| `89096378` | Jubeny | `equipo_portales` | Sí — 14 canales |
| `89096380` | Luisa | `equipo_directo` | Sí — `finca_raiz`, `metrocuadrado` |
| `89096379` | Mónica | `equipo_respaldo` | No — solo transferencia manual |
| `82598814` | Equipo de Marketing | `equipo_marketing` | No — solo métricas |

> ✅ RESUELTO — Se separan en cuatro conceptos: Usuario / Rol / Canal asignado / Propietario del contacto.
> ❓ Pregunta original — Owner vs Rol
`82598814` "Equipo de Marketing" es un owner que no es una persona. Y Mónica es una persona que no recibe leads.
En el CRM nuevo, ¿"Owner" sigue siendo lo mismo que "usuario del sistema"? Yo diría que no: necesitas separar Usuario (quien inicia sesión), Rol (qué puede hacer) y Owner (de quién es el contacto). Hoy los tres son el mismo campo.

### Colaborador


Segunda asesora con acceso a un Contacto sin ser su Owner. Existe en Redis (modo `collaborative` de `transfer_contact`), no en HubSpot — HubSpot solo admite un `hubspot_owner_id` por contacto.
> ✅ RESUELTO — Los colaboradores NO se conservan. Un contacto tiene un solo dueno.
> ❓ Pregunta original: con HubSpot como backbone, los colaboradores no tienen dónde persistirse. ¿Se conservan? Si sí, viven solo en la base propia y HubSpot nunca los conoce — hay que declararlo en la política de fuentes de verdad (D-11).

---


## 3. Estado y flujo


### Estado conversacional


Enum en `conversation_state.py:70-73`, guardado en `conv_state:{phone}:{canal}`:

| Estado | Significado en código |
|---|---|
| `BOT_ACTIVE` | SofIA responde con IA |
| `PENDING_HANDOFF` | SofIA anunció que un asesor atenderá; queda en silencio |
| `HUMAN_ACTIVE` | La asesora tiene el control |
| `IN_CONVERSATION` | La asesora está chateando activamente |


Más un SET aparte: `bot_controlled_conversations` (contactos en modo bot con >48h de inactividad, fuera del ZSET pero visibles).
> ✅ RESUELTO — La pregunta se disuelve: desaparece el modelo de 4 estados, sustituido por asignatario + historial de eventos.
> ❓ Pregunta original — `HUMAN_ACTIVE` vs `IN_CONVERSATION`
¿Cuál es la diferencia de negocio? Desde el código parecen lo mismo con distinta antigüedad. Si la asesora no las distingue en el panel, son un solo estado y estamos manteniendo complejidad gratis.
> ✅ RESUELTO — Separados: la *etapa* es negocio, el *asignatario* es operativo.
> ❓ Pregunta original — estados de negocio vs estados tecnicos
Estos 4 estados describen *quién está hablando*. Un CRM también necesita estados de *dónde va el negocio* (etapa del embudo). Hoy están mezclados. ¿Los separamos formalmente?

### Handoff


Traspaso del control de SofIA a una asesora. Disparado por: análisis del LLM (`immediate`/`high`), 14 palabras clave, detección de código de propiedad, o flag `sofia_activa=false` en HubSpot.

### Transferencia


Cambio de Owner de un Contacto: de una asesora a otra. Pasa por `transfer_ownership()`.
> ⚠️ Handoff y Transferencia son cosas distintas y el sistema las nombra parecido. Handoff = bot → humano. Transferencia = humano → humano. En el CRM nuevo deben tener nombres inconfundibles. Propuesta: "Escalamiento" (bot→humano) y "Reasignación" (humano→humano).
❓ ¿Aceptas esos nombres, o el equipo ya usa otras palabras en el día a día? Manda la palabra que ya usan las asesoras.

---


## 4. Embudo y etapas


### Embudo / Pipeline / Etapa / Stage / lifecyclestage


Cinco palabras para dos conceptos. En HubSpot el campo es `lifecyclestage`. El sistema es contact-centric: no usa Deals.

### ✅ Inventario real — verificado en la API de HubSpot el 2026-08-05


Hay 20 etapas, no 16.

| # | Etapa | Valor |
|---|---|---|
| 1 | En conversacion | `1326623075` |
| 2 | Visita agendada | `marketingqualifiedlead` |
| 3 | Visita Realizada | `salesqualifiedlead` |
| 4 | En estudio | `opportunity` |
| 5 | Aprobado | `lead` |
| 6 | Cerrado Ganado | `customer` |
| 7 | Cerrado Perdido | `evangelist` |
| 8 | No Responde | `other` |
| 9 | Seguimiento | `1407668893` |
| 10 | Hasta 1.5M | `1326623067` |
| 11 | Hasta 2M | `1326631573` |
| 12 | Hasta 2.5M | `1326632625` |
| 13 | De 3M en adelante | `1326631574` |
| 14 | Local o Bodega | `1326623539` |
| 15 | Ventas | `1353539189` |
| 16 | Otros Municipios | `1326632628` |
| 17 | Propietarios | `1326623069` |
| 18 | Otras Areas | `1326632209` |
| 19 | Ya encontro | `1326623541` |
| 20 | Reubicados | `subscriber` |

> 🔴 Hallazgo 1 — `Nuevo Lead` (`1326631578`) ya no existe en HubSpot.
Estaba registrada como etapa activa en la documentación previa. La columna "Nuevo" del Kanban de los wireframes no tiene hoy ninguna etapa que la respalde.
✅ RESUELTO — Se CREA la etapa `Nuevo Lead` en HubSpot.
> ❓ Pregunta original: ¿Se recrea la etapa en HubSpot, o "Nuevo" pasa a ser un estado del sistema (contacto sin etapa asignada)? → punto R-8 de D-02
> ✅ RESUELTO — la colisión "Seguimiento" rol vs. etapa.
La etapa `1407668893` se renombra a `Post Cita`. Nuevo significado: clientes que tras la visita decidieron no quedarse con el inmueble.
A partir de aquí, "Seguimiento" designa exclusivamente al rol *Asesora de Seguimiento*. Ninguna etapa se llama así.
> ✅ RESUELTO — `Nuevo Lead` se recrea. Es una etapa nueva en HubSpot, no la recuperación del ID viejo. La usan los dos roles de asesora.
> ✅ RESUELTO — visibilidad por rol **en el sistema actual**: Interna ve 11 etapas, Seguimiento **19**. Matriz en [D-02 §6](02-actores-y-roles.md).
> ⚠️ **Corregido el 2026-08-28:** decía 17. Son **19**.
> 🔑 **En el sistema destino esto ya no aplica:** son **7 etapas comunes + 3 grupos de propiedades** — ver [D-06 §4](06-matriz-permisos.md).
🔴 Dos etapas quedaron huérfanas (ningún rol las ve): `Hasta 2M` (`1326631573`) y `No Responde` (`other`). La segunda es grave: el sistema escribe en ella mediante el auto-cierre, así que un contacto que caiga ahí desaparece del CRM sin que nadie pueda recuperarlo.

---


## 5. Interfaz


| Término | Qué es hoy |
|---|---|
| Panel | La aplicación web que usan las asesoras (`middleware/PanelAsesores/`) |
| Inbox / Bandeja | Lista de conversaciones activas — ZSET Redis `active_conversations_sorted` |
| Badge rojo | Contador de no leídos, en memoria del frontend |
| Punto azul | Marca manual, DOM directo, se limpia al seleccionar |
| Campana | Notificación persistente — ZSET `advisor_notifications:{id}`, TTL 30 días |

> ⚠️ Tres mecanismos de notificación con tres persistencias distintas (memoria, DOM, Redis). En el CRM nuevo esto debería ser un modelo de notificación con distintos tipos de presentación.

---


## 6. "SofIA" — el término más sobrecargado


Hoy significa cuatro cosas a la vez:
1. El agente de IA que responde por WhatsApp (`sofia_brain.py`)
2. El panel que usan las asesoras
3. El sistema completo (bot + panel + integraciones)
4. A partir de A-02, el CRM que reemplaza la interfaz de HubSpot
> ✅ RESUELTO — El producto se llama **SofIA CRM**. `SofIA` designa solo al agente de IA.
> ❓ Pregunta original — la que mas impacto tiene en la claridad de todos los demás documentos
Propongo separar:
- SofIA → solo el agente conversacional de IA (un actor del sistema)
- [nombre del producto] → el CRM que usan las asesoras
Si el CRM y el bot se llaman igual, cada caso de uso va a tener que aclarar de cuál habla, y la matriz de permisos se vuelve ilegible (SofIA-agente tiene permisos; SofIA-producto *es* el que otorga permisos).
¿Cómo se llama el producto?

---


### Interés 🆕 ✅


Un inmueble concreto por el que un Contacto preguntó, con su fecha. Un Contacto tiene 1..N Intereses.

Nace de un caso real: un cliente pregunta por varios inmuebles el mismo día, y vuelve semanas después preguntando por otro. Sin esta entidad no hay dónde guardar esa historia.

| Campo | Ejemplo |
|---|---|
| `codigo_inmueble` | Código de la propiedad |
| `fecha_deteccion` | Cuándo se detectó |
| `origen` | link · código en el mensaje · lo dijo el cliente |
| `estado` | activo · descartado · convertido en cita |

> No es un Deal. Es ligero: sin importe, sin probabilidad, sin embudo propio. La arquitectura sigue siendo contact-centric.
`utils/property_code_detector.py` ya detecta estos códigos en los mensajes, pero no los guarda. Persistirlos crea la entidad casi sin coste.
Desarrollo completo en [D-02 §17-ter](02-actores-y-roles.md).

### Historial de Notas ✅


Registro de las notas que cada asesor ha escrito sobre un Contacto. Responde a *"¿quién ha anotado qué, y cuánto?"*. Es una lista simple por autor.

### Historial de Contacto ✅


El movimiento del Contacto desde que entró al CRM. Es un timeline de eventos, de naturaleza distinta al Historial de Notas:

| Tipo de evento | Origen |
|---|---|
| Cambios de embudo | Sistema y asesoras |
| Intereses detectados | SofIA / el sistema, a partir de links y códigos enviados por el cliente |
| Citas: programada · editada · cancelada · completada | Acciones de las asesoras |

> ✅ Decisión: son DOS listas separadas, no un timeline único con filtro. Descarta la recomendación del benchmark (patrón #4 de D-17) — el usuario prefiere separarlas porque responden a preguntas distintas.
⚠️ Aun así, el Historial de Contacto es un registro de eventos, y eso obliga a un catálogo de eventos de dominio (D-09).

### Ticket del portal 🆕


Etiqueta visual que viaja con el lead indicando por qué portal entró. Aparece en la tarjeta del Kanban en color diferenciado (`MetroCuadrado`, `FincaRaiz`) y como columna `Portal` en las tablas del Dashboard.
> ✅ DECIDIDO: nivel 1 — es la etiqueta del portal de origen. Dice por dónde llegó el cliente, no qué le interesa. Lo que le interesa lo cubre la entidad Interés (arriba).

---


## 7. Términos ya resueltos — referencia rápida


| Término | Significado acordado |
|---|---|
| Seguimiento | Solo el rol *Asesora de Seguimiento*. Nunca una etapa |
| Post Cita | Etapa: cliente que tras la visita no se quedó con el inmueble |
| Nuevo Lead | Etapa inicial, se crea en HubSpot. Primera columna del Kanban |
| Equipo | ❌ Eliminado. Era la indirección canal→persona; ahora es directa y configurable |
| Canal / Portal | Origen del lead. Determina el dueño inicial. Dato administrable, no código |
| Rol | Qué puede hacer y qué ve un usuario. Independiente del canal |
| Transferencia | Cambio de dueño manual de A. Interna a A. Seguimiento |
| Ticket del portal | Etiqueta visual del canal de origen en tarjetas y tablas. Nivel 1: dice por dónde llegó, no qué le interesa |
| Interés 🆕 | Inmueble por el que un contacto ha preguntado, con fecha y origen. Un contacto tiene N intereses. Ver [D-02 §17-ter](02-actores-y-roles.md) |
| Historial de Notas | Las notas que cada asesora ha escrito sobre el contacto. Aporte humano, agrupado por autor |
| Historial de Contacto | Registro de eventos del recorrido del contacto: etapas, intereses detectados, citas. Hecho objetivo, no opinión |
| Contacto | Una persona = un teléfono. Si vuelve meses después por otro inmueble, es el mismo contacto con un `Interés` nuevo |
| Usuario / Rol / Propietario | Tres conceptos separados. Antes eran el mismo campo `hubspot_owner_id` |
| Estado conversacional | ❌ Se elimina el modelo de 4 estados. Se sustituye por *asignatario + historial de eventos* |


### Bloques de decisión ya cerrados


Estos `❓ DECISIÓN PENDIENTE` del cuerpo del documento quedan resueltos y se conservan solo como rastro de la discusión:

| Bloque | Resolución |
|---|---|
| §1 · ¿Contacto es persona o persona-por-canal? | Persona. La clave `conv_meta:{phone}:{canal}` tiene que cambiar |
| §2 · ¿Se conserva "Equipo"? | No. Eliminado — la relación canal→persona es directa y configurable |
| §2 · Owner vs Rol | Separados en Usuario / Rol / Canal asignado / Propietario |
| §3 · `HUMAN_ACTIVE` vs `IN_CONVERSATION` | La pregunta se disuelve: desaparecen los estados |
| §3 · Estados de negocio vs técnicos | Separados: la *etapa* es negocio, el *asignatario* es operativo |
| §4 · La columna "Nuevo" del Kanban | Se crea la etapa `Nuevo Lead` en HubSpot |
| §1 · ¿Entra la entidad Propiedad? | No como tal. Entra `Interés`, que referencia un código de inmueble sin modelar la propiedad completa |


---


## Términos por definir en próximas rondas


`chatbot_score` · `url_chat` · `embudo` (¿= pipeline?) · `masivo` / campaña · `plantilla` (template Twilio) · `ventana de 24h` · `recordatorio` · `seguimiento` · `asesora` vs `asesor` vs `advisor` vs `owner` (4 palabras, ¿un concepto?)

---

## 8. Decisiones cerradas — ronda 2026-08-15

### 🔑 El producto se llama **SofIA CRM**

Queda resuelta la sobrecarga del término. A partir de aquí, en toda la documentación:

| Término | Significa exclusivamente |
|---|---|
| **SofIA CRM** | El producto completo: el sistema que usan las asesoras |
| **SofIA** | El **agente de IA** que conversa por WhatsApp. Un actor del sistema, no el sistema |
| **Panel** | ❌ Término retirado. Era el nombre del sistema viejo |

### Otras decisiones

| Término | Resolución |
|---|---|
| **Colaboradores** | ❌ **No se conservan.** Un contacto tiene un solo dueño. Simplifica el modelo y elimina el problema de que HubSpot no puede persistirlos |
| **Archivado** | ❌ **No habrá módulo de archivado.** Una conversación no se cierra ni se archiva; permanece accesible siempre |
| **`whatsapp` / `whatsapp_directo`** | Designan **todos los canales distintos de los portales comunes** — el contacto que escribe directo sin venir de un portal |
| **`Interés`** | ✅ **Entidad aprobada.** Estructura el dato de qué inmuebles le interesan a cada cliente |

> ✅ **DECIDIDO: se fusionan.** Queda un único canal, `whatsapp_directo`. `whatsapp` pasa a ser **alias de migración** — se acepta al leer datos antiguos, nunca se escribe.
> Cierra el problema #6 de la auditoría (*"'whatsapp' vs 'whatsapp_directo' ambigüedad"*), que llevaba abierto desde el análisis inicial.
> ⚠️ **Requiere script de migración**: los contactos existentes con `canal_origen = "whatsapp"` deben reescribirse. Va a D-18.

---

## 9. Términos de la ronda 4

| Término | Definición |
|---|---|
| **Formulario** | **No es un canal.** Es la plantilla `experiencia_cita` (`HX1f96c23a505ea1b292401dbd7d0de13d`) que se envía automáticamente X tiempo después de una cita para calificar al asesor externo y la experiencia de atención. `Fecha de formulario` = cuándo se envió |
| **Link enviado** | URL que el cliente manda por WhatsApp. **N por contacto**, guardada como **evento del Historial de Contacto**, pulsable. Distinta del `Ticket del portal`, que es 1 por contacto |
| **Ticket del portal** | Confirmado en **nivel 1**: etiqueta del canal de origen. No guarda enlaces |

> 🔑 **Regla de alcance de Marketing:** ve **solo redes sociales** (Instagram, Facebook, TikTok), **salvo** los contactos que **tuvieron cita** — de esos ve **todos los portales**.

