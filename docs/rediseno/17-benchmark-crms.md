> Estado: ⚠️ Borrador v0 — reconstruido. El documento original se perdió y no consta qué CRMs habías mirado.
Criterio de selección: no elegí "los CRM más grandes". Elegí el CRM que mejor resuelve cada problema concreto documentado en la auditoría de SofIA. Un benchmark que no está anclado a tus problemas es turismo de producto.
Advertencia: precios y detalles de planes cambian. Verificar antes de citar en decisiones económicas.

---


## 0. Cómo leer este documento


Los problemas de SofIA ya están identificados. Este documento va al revés de lo normal: parte del problema y busca quién lo resolvió bien.

| # | Problema documentado | Fuente | Referente |
|---|---|---|---|
| P-1 | Cambiar asesora/canal/etapa exige editar código y desplegar | Auditoría §7.1 #1 (ALTA) | Attio · Airtable · HubSpot |
| P-2 | Cada rol debe ver distintas etapas y distintas columnas | Wireframe `img08` · Decisión A-02 | Salesforce · Attio |
| P-3 | Ownership desincronizado entre 3 sistemas | Auditoría §2 y §7 | Front · Intercom |
| P-4 | Escalamiento bot→humano se pierde o llega tarde | Auditoría §5 "Hallazgo crítico" | Intercom |
| P-5 | Automatizaciones hardcodeadas en APScheduler | Auditoría §3.6/3.7 | HubSpot Workflows · Salesforce Flow |
| P-6 | Notas e historial no existen como entidad | Wireframes `img09`, `img05` | HubSpot timeline |
| P-7 | La IA escribe datos sin dejar rastro de autoría | Inferido | Salesforce · Intercom |
| P-8 | WhatsApp es el canal real del negocio | Todo el sistema | Kommo · respond.io |


---


## 1. HubSpot — el que ya tienen


Se queda como backbone (decisión A-02). Aquí importa qué NO da.

Qué tomar:
- Timeline unificado de actividad. Un solo hilo cronológico por contacto donde caen mensajes, notas, cambios de etapa, correos y llamadas. Resuelve P-6 — y de paso resuelve una duda de los wireframes: dibujaste *"Historial de Notas"* e *"Historial de Contacto"* separados; HubSpot demuestra que un solo timeline con filtro por tipo funciona mejor que dos listas.
- Propiedades configurables por objeto. Crear un campo nuevo es configuración, no despliegue. Es la respuesta directa a P-1.
- Workflows declarativos — automatizaciones como datos editables por un no-programador (P-5).

Qué NO tomar / la trampa que los trajo aquí:
- Los pipelines son globales, no filtran por rol. Esa capacidad exige el plan Enterprise. Este es literalmente el motivo económico del rediseño — y significa que el filtrado por rol es responsabilidad de SofIA, no delegable.
- Rendimiento: la API tiene límites de tasa. Ya lo sufren (backoff de 429 implementado, `sleep` de 50 ms en la reconciliación). Cualquier vista que dependa de una llamada en vivo a HubSpot va a ser lenta.
> 🎯 Consecuencia de diseño: el Kanban de los wireframes no puede leer de HubSpot en vivo. Necesita una proyección local. Eso es una decisión de arquitectura (va a D-11/D-14), no un detalle.

---


## 2. Pipedrive — el Kanban como forma de pensar


Por qué está aquí: popularizó el pipeline visual como interfaz principal del CRM. Es exactamente lo que dibujaste en `img02`/`img06`.

Qué tomar:
- El embudo es la pantalla principal, no un reporte. El estado del negocio se ve de un vistazo.
- Arrastrar y soltar = cambio de estado. Una interacción, cero formularios.
- "Actividad pendiente" por tarjeta. Cada lead muestra cuál es su siguiente acción. Ataca P-4 desde el producto: un lead sin próxima acción está visualmente marcado como abandonado.
- Totales por columna (cuántos, cuánto valen).

Qué NO tomar:
- Está construido alrededor del *Deal*. SofIA es contact-centric y sin Deals — el Kanban debe mover contactos por `lifecyclestage`, no negocios. Copiar el modelo de Pipedrive obligaría a introducir Deals, contradiciendo la arquitectura actual.
> 🎯 Adoptar el patrón visual, rechazar el modelo de datos.

---


## 3. Intercom — bot y humano en la misma bandeja


El referente más directo de SofIA. Es el producto que mejor resolvió *"una IA atiende primero y escala a una persona"*.

Qué tomar:
- El escalamiento es un evento explícito y visible, no un cambio de estado silencioso. Cuando la IA entrega la conversación, queda registrado quién, cuándo y por qué. Ataca P-4 y P-7 a la vez.
- Reglas de asignación como configuración, no como código (P-1, P-3).
- La IA es un actor con nombre en el hilo. El cliente y el agente ven qué escribió la IA y qué escribió la persona. Es la respuesta de producto a P-7.
- Snooze / posponer. Una conversación puede "desaparecer hasta mañana". Hoy SofIA solo tiene activo/inactivo, y por eso necesita un `bot_controlled_conversations` y un recolector de basura a 90 días para simular el concepto.

Qué NO tomar:
- Modelo de precios por resolución de IA — irrelevante aquí.
- Su complejidad de configuración: sobra para 2–4 asesoras.
> 🎯 Lo más valioso de todo el benchmark: *el escalamiento es un evento auditable, no una transición de estado.* Eso reemplaza el enredo `BOT_ACTIVE / PENDING_HANDOFF / HUMAN_ACTIVE / IN_CONVERSATION` por un modelo mucho más simple: una conversación tiene un asignatario (SofIA o una persona) y un historial de reasignaciones.

---


## 4. Front — bandeja compartida con dueño


Por qué está aquí: resolvió el problema exacto de P-3 — varias personas sobre una misma bandeja sin pisarse.

Qué tomar:
- Asignación explícita y visible. Toda conversación tiene dueño, siempre; nunca hay estado "de nadie". Hoy SofIA tiene contactos sin `assigned_owner_id` y por eso existe el fallback de difusión global en el WebSocket.
- Comentarios internos en el hilo, invisibles para el cliente. Es la forma correcta de implementar la `Nota` de los wireframes: en el hilo, no en un cajón aparte (P-6).
- Indicador de "otra persona está escribiendo". Con 2 asesoras y contactos colaborativos, evita respuestas duplicadas.
- Reglas de bandeja configurables.

Qué NO tomar:
- Está centrado en correo electrónico; el modelo de hilos no encaja del todo con WhatsApp, que es un flujo continuo sin asunto.

---


## 5. Salesforce — el modelo de permisos


No tomar el producto. Tomar el modelo conceptual de autorización. Es el que hay que replicar porque A-02 convierte a SofIA en el responsable de la autorización.

Qué tomar:
- Separación de tres conceptos que hoy SofIA tiene fusionados en un solo campo:

| Concepto | Pregunta que responde | En SofIA hoy |
|---|---|---|
| Usuario | ¿Quién inicia sesión? | ⚠️ no existe login real |
| Perfil / Rol | ¿Qué tipo de cosas puede hacer? | ❌ no existe |
| Propietario del registro | ¿De quién es este contacto? | `hubspot_owner_id` |


Hoy los tres son `hubspot_owner_id`. Por eso "Equipo de Marketing" es un *owner que no es una persona*.
- Permisos a nivel de campo, no solo de registro. El wireframe `img07` ya lo pide: *"Fecha Último Seguimiento (Si interfaz de Seguimiento)"* — una columna que aparece según el rol. Eso es permiso de campo (P-2).
- Vistas de lista guardadas — filtro + columnas + orden, guardado como objeto y compartible por rol. Esta es la solución limpia a "Interna ve 10 etapas, Seguimiento ve 16": no se hardcodea, se configura (P-1 + P-2).
- Audit trail nativo: cada cambio guarda quién, qué y cuándo (P-7).

Qué NO tomar:
- Toda su complejidad de administración. Salesforce necesita un administrador dedicado; ustedes no lo tienen y no lo van a tener.
> 🎯 Regla: copiar el *modelo* (Usuario / Rol / Propietario / Vista guardada / Auditoría), no la *implementación*.

---


## 6. Attio y Airtable — configuración como datos


Por qué están aquí: son la respuesta más directa a P-1, el problema de prioridad ALTA.

Qué tomar:
- El esquema es dato, no código. Añadir una etapa, un campo o un canal es configuración en tiempo de ejecución. Hoy en SofIA cada uno de esos cambios es un despliegue.
- Vistas como objetos de primer nivel. Los wireframes ya piden dos vistas de lo mismo (Kanban y Tabla, `img02`). Attio demuestra que Kanban / tabla / calendario deben ser presentaciones intercambiables del mismo conjunto filtrado, no pantallas distintas con código distinto.

Qué NO tomar:
- Flexibilidad total = nadie sabe cómo usarlo. Para 2–4 asesoras, un esquema configurable por un administrador es correcto; un esquema que cualquiera puede cambiar es un desastre.
> 🎯 Punto medio recomendado: que etapas, canales, asesoras y reglas de asignación sean datos administrables. Que el modelo de dominio (qué es un Lead, qué es una Cita) siga siendo código.

---


## 6-bis. Leadsales — 🔴 el referente declarado, y el competidor más cercano


Referente elegido por CyberTovar para el *workplace* con perfiles por usuario y para el comportamiento multi-dispositivo. Investigado el 2026-08-05.

### Qué es


CRM conversacional que centraliza WhatsApp, Instagram y Facebook en un solo lugar y los organiza en embudos con etapas personalizables. Función multiagente: todo el equipo atiende desde el mismo número de WhatsApp simultáneamente. Incluye *Leadbot* (atiende el primer contacto y deriva al embudo correspondiente) y *Lead Agent* (agente de IA que lleva conversaciones completas consultando una base de conocimiento).
> Es prácticamente la misma propuesta que SofIA. Embudo Kanban + bandeja compartida + bot que escala a humano + base de conocimiento. Precio: 133–247 USD/mes.

### 🔑 El hallazgo que corrige una premisa del proyecto


La preocupación registrada era: *"cada Layout estará abierto en 2 dispositivos diferentes, hay que analizar cómo Leadsales evita límites y lentitud"*.

La limitación de 2 dispositivos es de la aplicación WhatsApp Business, no de un CRM. Leadsales la evita exactamente igual que SofIA: usando la API de WhatsApp en lugar de la app. Con la API, los usuarios y dispositivos simultáneos son ilimitados, con bandeja compartida y permisos por rol.
> SofIA ya tiene la misma arquitectura que Leadsales en este punto (Twilio WhatsApp API). El cuello de botella multi-dispositivo no viene de WhatsApp: viene del backend propio — sesiones, WebSocket y estado de lectura. Ver [D-02 §15](02-actores-y-roles.md).

### El límite de WhatsApp que sí aplica


Escalonado de conversaciones diarias: 250 → 1.000 → 10.000 → 100.000, subiendo automáticamente si se mantiene la calidad de la cuenta. Requisito no funcional real → D-15.

### Qué tomar

- Perfil de usuario dentro del espacio de trabajo de la empresa — el bloque `USER` de los wireframes
- Embudos con etapas personalizables — configuración, no código. Coincide con el principio rector del rediseño
- Un número de WhatsApp, muchos usuarios, con asignación explícita de conversaciones
- Bot de primer contacto que deriva al embudo correspondiente antes de pasar a humano

### Qué NO tomar

- Cobran por usuario adicional sobre la misma línea. Es un modelo de negocio, no una necesidad técnica
- Su IA es más superficial que el motor RAG multi-agente de SofIA
> 🎯 Lectura estratégica: Proteger está construyendo a medida lo que Leadsales vende empaquetado por ~1.600–3.000 USD/año. La justificación de construir en vez de comprar tiene que ser el filtrado por rol y la IA propia — que es exactamente donde Leadsales no llega. Si el rediseño no entrega eso, comprar sería más barato.

Fuentes: Leadsales — qué es y cómo funciona · WhatsApp multiagente · WhatsApp Business en varios dispositivos · Precios · respond.io — WhatsApp Business multiusuario

---


## 7. Kommo y respond.io — CRMs WhatsApp-first


Los competidores más honestos de SofIA. Son CRM construidos alrededor de WhatsApp, muy usados en Latinoamérica por inmobiliarias y concesionarios. Un asesor comercial de Proteger probablemente ya vio uno.

Qué tomar:
- WhatsApp es el objeto central, no un canal más. Ventana de 24 h, plantillas aprobadas y estados de entrega son conceptos de primera clase en la interfaz — no detalles técnicos escondidos. SofIA ya sufre esto (backoff de 429, `last_client_msg` para la ventana de 24 h, plantillas de Twilio) pero no lo muestra en el panel.
- Selector de plantillas en el composer, con vista previa. Ya está dibujado en `img05`.
- Aviso visible de ventana de 24 h cerrada. Directamente ligado a O-5: *"cuando un mensaje falla, la asesora DEBE verlo"*.
- Embudos por unidad de negocio.

Qué NO tomar:
- Su IA es superficial comparada con lo que ya tiene SofIA (RAG + multi-agente). Aquí SofIA va por delante — es su ventaja real.

---


## 8. Síntesis — los 10 patrones a adoptar


| # | Patrón | De | Resuelve | Costo | Prioridad |
|---|---|---|---|---|---|
| 1 | Usuario / Rol / Propietario como conceptos separados | Salesforce | P-2, P-3 | Alto | 🔴 Fundacional |
| 2 | Vistas guardadas (filtro + columnas + orden) por rol | Salesforce, Attio | P-1, P-2 | Medio | 🔴 Fundacional |
| 3 | Configuración operativa como datos, no código | Attio, HubSpot | P-1 | Alto | 🔴 Fundacional |
| 4 | Timeline unificado de actividad | HubSpot | P-6 | Medio | 🟠 Alta |
| 5 | Escalamiento como evento auditable, no como estado | Intercom | P-4, P-7 | Medio | 🟠 Alta |
| 6 | Asignación siempre explícita, nunca "de nadie" | Front | P-3 | Bajo | 🟠 Alta |
| 7 | Notas internas dentro del hilo | Front | P-6 | Bajo | 🟠 Alta |
| 8 | Kanban de contactos con arrastrar y soltar | Pipedrive | Wireframe | Medio | 🟡 Media |
| 9 | Ventana de 24 h y plantillas visibles en la interfaz | Kommo | O-5 | Bajo | 🟡 Media |
| 10 | Registro de auditoría con autoría de la IA | Salesforce, Intercom | P-7 | Medio | 🟡 Media |


Los tres 🔴 son fundacionales: si no entran en el diseño desde el principio, no se pueden añadir después sin rehacer el modelo de datos.

---


## 9. Anti-patrones — errores caros que estos productos ya cometieron


| Anti-patrón | Quién lo sufre | Por qué a ustedes les pega fuerte |
|---|---|---|
| Permisos en el frontend | — | Con HubSpot invisible, SofIA *es* la autorización. Filtrar en JS = cualquiera ve todo con abrir DevTools |
| Pipelines globales sin dimensión de rol | HubSpot | Es el problema que están rediseñando. No reproducirlo en la base propia |
| Un estado por cada matiz de la conversación | — | Ya tienen 4 estados + 1 SET auxiliar y no está claro que `HUMAN_ACTIVE` e `IN_CONVERSATION` se distingan. Intercom lo resuelve con *asignatario + eventos* |
| Configuración tan flexible que nadie la entiende | Airtable, Salesforce | 2–4 asesoras sin administrador dedicado. Configurable ≠ infinitamente configurable |
| Leer del CRM externo en tiempo real | — | Los límites de tasa de HubSpot ya les pegaron. El Kanban necesita proyección local |


---


## 10. Lo que falta de ti


| # | Pregunta |
|---|---|
| B-1 | ¿Qué CRMs miraste tú, y qué te gustó de cada uno? Puede que hayas visto algo que no está en esta lista |
| B-2 | ¿Alguien en Proteger usó antes otro CRM? Lo que odiaron vale más que lo que les gustó |
| B-3 | ¿Hay presupuesto para licencias, o todo se construye? Cambia si el patrón #3 se compra o se programa |
| B-4 | De los 10 patrones, ¿cuáles reconoces como "sí, eso es exactamente lo que necesito" y cuáles te suenan a exceso? |
