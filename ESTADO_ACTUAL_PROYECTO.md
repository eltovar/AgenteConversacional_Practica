# ESTADO ACTUAL DEL PROYECTO — SofIA (Inmobiliaria Proteger)

> Ultima actualizacion: 10 de agosto de 2026

---

## Resumen General

Sistema de agente conversacional multi-agente en produccion sobre Railway Pro. Stack: FastAPI + Redis + MongoDB + HubSpot API v3 + Twilio WhatsApp + OpenAI GPT-4o-mini. Panel de asesores con WebSocket en tiempo real.

---

## Sesiones Recientes

### 13 de agosto de 2026 — Punto C: registro de asesoras (DESPLEGADO)

**Commits:** `c18725e`, `2e063c0`.

`lead_assigner.OWNERS_CONFIG` ya era un registro y cuatro consumidores lo
respetaban, pero **ocho puntos repetian los IDs a fuego**. La identidad se mueve a
`utils/advisors_registry.py`, espejo de channels_registry, y OWNERS_CONFIG pasa a
derivarse de ahi — lead_assigner y sus consumidores no se enteran.

Corrige ademas la direccion de la dependencia: antes, leer el nombre de una
asesora obligaba a importar el asignador de leads entero, que arrastra el registro
de canales.

**Dos conceptos que estaban mezclados y ahora son campos distintos:**
`receives_leads` (asignacion automatica) y `uses_panel` (tiene bandeja). Monica no
recibe leads pero atiende transferencias manuales, asi que su bandeja existe y el
job de las 3 AM debe limpiarla.

**Dos hallazgos al migrar:**
- `ADVISOR_PORTALS` era un mapa que no mapeaba nada — los dos IDs apuntaban a la
  misma lista. Eliminado.
- `from_advisor="89096378"` era un BUG: si transferia otra asesora, la
  notificacion decia que venia de Jubeny. Ahora resuelve el dueno actual.

**Un cambio de comportamiento que se colo y se corrigio (`2e063c0`):** los 10
embudos de transferencia pasaron a ocultarse tambien a Monica. Se verifico que la
LISTA fuera identica, pero no a QUIEN se le oculta. Detectado al verificar en
produccion tras el deploy.

**QA:** 22 tests, 12 mutaciones aplicadas y 12 detectadas. Incluye un barrido que
prohibe IDs de asesora en los 8 ficheros de produccion (ignorando comentarios) y
un test que exige que el equipo de cada asesora exista en channels_registry — un
typo ahi la dejaria sin canales, en silencio. Baseline intacto (32/7).


### 12 de agosto de 2026 — El badge vuelve a "no leido" (DESPLEGADO)

**Commit:** `ef9fef0`. Revierte la parte de frontend de `7e366b1`.

Las asesoras reportaron que el aviso no se apagaba tras contestar. Analizados los
9 contactos con aviso de su consola contra MongoDB: **el backend acertaba en los
9**. En cinco, el cliente habia vuelto a escribir DESPUES de que la asesora
contestara — 13 segundos en un caso, 7 en otro. No eran fantasmas, eran deudas
nuevas apareciendo tan rapido que se ven igual.

El defecto real era otro: la reconciliacion se salta el contacto abierto
(`_rp !== currentPhone`), asi que el aviso del chat en el que estas trabajando no
se limpiaba nunca tras contestar.

**Por que se revirtio y no se parcheo:** el argumento para cambiar la semantica
era que sin aviso el contacto se hundia y desaparecia. **El Punto 1.1 ya resolvio
eso unas horas antes** — un contacto con el cliente esperando entra siempre en la
lista, tenga aviso o no. Railway lo confirmaba: "0 unread + 6 esperando
respuesta". El aviso ya no sostenia la visibilidad, solo aportaba prominencia, y
esa prominencia salia cara.

Se revirtieron los 3 guards de `index.js` y NADA del backend. `pending_reply` y
`pending_rescued` siguen siendo el corte. Hay un test que ata las dos piezas para
que nadie borre `pending_reply` pensando que ya no se usa.

**Hallazgo pendiente:** `+573206768027` esta en el embudo "En conversacion" y fue
cerrado con el boton manual — el estado incoherente que define la regla del
usuario. Es el primer caso real para `scripts/reabrir_cerradas_con_cliente_esperando.py`,
pero su criterio actual (comparar `last_message_at` con `updated_at`) no lo marca.
Hay que cambiarlo al criterio del EMBUDO. La auditoria necesita el mismo cambio:
hoy cuenta cada cierre legitimo como invisible, y por eso subio de 2 a 11 en una
tarde mientras las asesoras trabajaban.

### 12 de agosto de 2026 — Badge permanente: regresion corregida (DESPLEGADO)

**Commit:** `7b40340`. Detectado en produccion a las 11:05: badges que no se apagaban
tras contestar. Regresion de `ba79089`, de la madrugada.

La regla de "quien espera respuesta" paso por tres versiones, dos de ellas erroneas:

1. **Colapsar por telefono** quedandose con un documento cualquiera (el ultimo del
   cursor) → escondia 3 clientes que llevaban 11 horas, 4 y 6 dias esperando.
2. **Basta con que ALGUN canal tenga `client`** → 20 avisos encendidos para siempre.
   El cliente escribe por el portal, la asesora contesta por WhatsApp —el canal real
   de mensajeria— y el documento del portal se queda con `client` sin que nada lo toque.
3. **Decide el documento MAS RECIENTE** de entre todos los canales. Correcta.

**Verificado en produccion:** 144 telefonos marcados con la regla vieja, 127 con la
nueva — **17 avisos falsos apagados**, sin perder ningun cliente que si espera.

La regla vive en `phones_awaiting_from_docs()`, funcion pura probada ejecutandola. La
lectura se trae a Python en vez de agregarla en MongoDB a proposito: es el corazon del
aviso y tiene que poder probarse.

**Dos lecciones de metodo, ambas con costo:**
- Un doble de pruebas que REIMPLEMENTA la regla prueba lo que su autor cree, no el
  codigo. El doble anterior codificaba la regla equivocada y sobrevivia a las
  mutaciones. Ahora delega en la funcion real.
- El arnes de mutacion escribe sobre los ficheros fuente y **dejo dos mutaciones sin
  restaurar** en el arbol de trabajo. Peor: la siguiente corrida toma el respaldo del
  fichero ya mutado y la corrupcion se vuelve permanente. No llego a produccion (HEAD
  estaba limpio). Se le anadieron dos guardias: aborta si un patron no aparece, y
  verifica por SHA-256 que cada fichero volvio a su estado.

25 mutaciones aplicadas y 25 detectadas. Baseline intacto (32/7), Ruff sin cambios.

**Pendiente de decidir:** la auditoria cuenta como invisible cualquier conversacion con
`last_message_sender=client`, sin mirar si fue CERRADA despues. Con eso, el numero sube
cada vez que una asesora cierra bien una conversacion — un indicador que empeora cuando
el equipo hace su trabajo. Tras el arreglo paso de 2 a 7 invisibles, y los 7 son cierres
deliberados de esta manana.

### 12 de agosto de 2026 — Punto B: los ultimos invisibles (DESPLEGADO)

**Commits:** `f34b57e`, `1cc4074`, `ba79089`.

| | Antes | Despues |
|---|---|---|
| Pendientes no visibles | 4 invisibles + 3 cortados | **2 invisibles + 0 cortados** |
| Visibles | 28 de 34 | **32 de 34** |

**Tres defectos encadenados, cada uno tapando al siguiente:**

1. **Sin dueno** (2 contactos, 22 en 30 dias). La lista filtra por dueno y no tener
   dueno es el estado NORMAL mientras Sofia atiende. Se atribuyen por canal AL LEER,
   sin escribir propiedad: asignar en el camino caliente marcaria 1.062 de las 1.176
   sin dueno. Atribuir por canal garantiza ademas que pasen la segregacion por equipos.
2. **El tope se aplicaba por posicion, no por recencia.** `advisor_contacts` llega en
   dos tramos (ZSET, luego respaldo de MongoDB) y entre ellos no hay orden. Un cliente
   de hace 11 horas perdia su plaza frente a otro de hace 40 dias.
3. **La deteccion de pendientes era CIEGA AL CANAL.** `conversations` guarda un
   documento por (telefono, canal); la resolucion reutilizaba `get_message_previews_batch`,
   que indexa por telefono y colapsa los documentos — ganaba el ultimo del cursor. Si
   era el de `advisor`, el cliente que esperaba en otro canal desaparecia.
   **232 telefonos tienen mas de un canal.** Sustituido por `find_phones_awaiting_reply()`.

**Lo que NO se hizo:** rehidratar el `BOT_CONTROLLED_SET` sin meta estaba en el plan.
Se implemento y se revirtio — sin meta no hay dueno y la lista filtra por dueno, asi
que no cambiaba nada de lo que ve nadie.

**Limitacion conocida:** el arreglo del cerrado-que-reescribe es hacia adelante. Los 2
contactos que quedan invisibles se reabriran cuando el cliente escriba de nuevo; su
estado actual no se sana solo.

**Latencia:** media 7,5 s, max 8,1 s — sin empeorar respecto a los 7-12 s del Punto 1.
El tope de 100 esconde 35 pendientes de Jubeny, todos de mas de 7 dias.

**Tests:** 22 nuevos, 22 mutaciones aplicadas y 22 detectadas. Dos sobrevivieron por un
motivo que conviene recordar: los tests usaban un doble que REIMPLEMENTABA la consulta,
asi que mutar la consulta real no les afectaba — probaban la comprension del autor, no
el codigo. Baseline intacto (32/7), Ruff F,E9 sin cambios.

### 11 de agosto de 2026 — Punto 1.2: el aviso pasa de "no leido" a "sin responder" (DESPLEGADO)

**Commit:** `7e366b1`. Abrir un chat quitaba el globito aunque el cliente siguiera
esperando. Ahora el aviso se apaga cuando la asesora RESPONDE.

La senal es `pending_reply`, calculada en cada carga desde
`conversations.last_message_sender`. No se guarda en ningun sitio: al contestar, el
remitente pasa a "advisor" y el aviso cae solo. **Se descarto invertir la semantica
de `advisor_inbox` en Redis** — habria obligado a revisar sus 5 puntos de borrado y
habria vuelto destructiva la limpieza de las 3 AM (app.py:684). Con este camino ese
job deja de ser relevante para el aviso, sin tocarlo.

**Dos marcas, no una:** `pending_reply` es la verdad (sin acotar, la lee el panel) y
`pending_rescued` es el pase (acotado por el tope de 100, lo leen los cortes 2 y 3).
Fundirlas anularia el tope.

**Verificado en produccion:** 178 contactos traen la senal, **173 de ellos con
`has_unread=False`** — son los que hasta hoy no tenian ninguna marca. La auditoria
no se movio (este punto cambia la senal, no la visibilidad).

**Consecuencia a vigilar:** Jubeny pasa a ver ~96 avisos y Luisa ~77, cuando antes
eran 3 y 11. No es un fallo: es la deuda acumulada hecha visible de golpe. Si
resulta ser ruido, la palanca es acotar por antiguedad, no volver atras.

**Tests:** 19 nuevos (13 frontend + 6 backend), 8 mutaciones aplicadas y 8
detectadas. Los de frontend extraen la condicion real de `index.js` y la EJECUTAN en
node contra objetos de prueba. Baseline intacto (32/7), Ruff F,E9 en 14.

### 11 de agosto de 2026 — Punto 1: los pendientes nunca se cortan (DESPLEGADO)

**Commits:** `179387a`, `5c3ffde`, `0d74661` en `claude/conversations-fallback-compliance`.

**Resultado medido con la auditoria, antes y despues:**

| | Antes | Despues |
|---|---|---|
| Pendientes no visibles | 16 de 40 | 4 de 33 |
| Cortados por el limite | 13 | 1 |
| Invisibles (causas de fondo) | 3 | 3 — son el Punto 3 |

El unico cortado que queda es un contacto `finca_raiz` cuyo dueno es Jubeny, cuyo
equipo no tiene ese canal: lo aparta la segregacion por equipos, no el corte.

**Hallazgo central — GET /contacts recorta en TRES sitios encadenados:**
seleccion (`_split_always_visible`), pre-limite antes de enriquecer con HubSpot
(`_cuenta_como_prioridad`) y respuesta final (`_nunca_se_corta`). Arreglar uno solo
no cambia NADA en pantalla. Costo tres deploys descubrirlo: el primero dejaba 110
en 62, el segundo 136 en 33. Los tests pasaban en verde porque cubrian la seleccion
y paraban ahi. Ahora hay uno que recorre la cadena hasta la respuesta y un barrido
por AST que exige que cualquier corte futuro respete el criterio.

**Regresion aceptada a proposito:** `GET /contacts` pasa de 3-5 s a 7-12 s. El
desglose la atribuye al coste por contacto en Python (`otro` 9-13 s), no a MongoDB
(~35 ms) ni HubSpot (~0-600 ms en caliente). Se ofrecio acotar por antiguedad
(14 o 7 dias) o bajar el tope a 25; **el usuario decidio dejarlo como esta**: ninguna
conversacion con el cliente esperando queda oculta, por antigua que sea. No reducir
la lista para ganar latencia sin volver a preguntar.

**Tests:** 26 casos en `tests/panel/test_contacts_pending_cut.py`, 13 mutaciones
aplicadas y 13 detectadas. Tres tests de `test_inbox_failsafe.py` se reescribieron
—asertaban sobre el texto fuente de `get_active_contacts` y el refactor los rompio—
para comprobar la conducta ejecutandola. Baseline intacto (32 en `tests/panel`, 7 en
`tests/middleware`), Ruff F,E9 sin cambios (14).

### 11 de agosto de 2026 — Auditoria de visibilidad del panel (2.3, MEDIDO)

**Contexto:** las asesoras reportan contactos que nunca aparecen en la barra y
conversaciones que aparecen dias despues. No habia ningun caso confirmado, solo la
sospecha. Antes de tocar el frontend (riesgo alto) se construyo la linea base.

**Entregable:** `scripts/audit_visibility.py` — SOLO LECTURA, se corre con
`railway run`. Cruza la verdad de campo (MongoDB `conversations` con
`last_message_sender="client"`) contra lo que devuelve realmente
`GET /whatsapp/panel/contacts`, y clasifica cada invisible por causa.
Garantia estructural: `ReadOnlyRedis` solo expone comandos de lectura, cualquier
escritura revienta con AttributeError. No existe modo `--execute`.

**Resultado (ventana de 7 dias, 3 corridas estables):**

| | pendientes | visibles | invisibles | solo fuera de 24h |
|---|---|---|---|---|
| Jubeny | 16 | 11 | 1 | 4 |
| Luisa | 25 | 17 | 0 | 8 |
| SIN_OWNER | 2 | 0 | 2 | 0 |

**El problema de visibilidad es real pero pequeno: 3 de 43. Ninguno es de frontend.**

1. `+573195652633` (mercado_libre) — meta sin `assigned_owner_id` y Mongo sin
   `owner_id`. Esta en el ZSET y en BOT_CONTROLLED_SET, pero no es de nadie.
2. `+573206946429` (whatsapp) — `archived=True` + `in_panel=False`: contacto cerrado
   que volvio a escribir y no regreso al ZSET (conversation_state.py:1529).
3. `+573116983989` (pagina_web) — en BOT_CONTROLLED_SET **sin meta**: el bucle de
   conversation_state.py:456 hace `continue` y el contacto desaparece. Ademas sin
   owner y con desajuste de canal. Multi-causal.

**12 de 43 son "solo fuera de 24h":** visibles con filtro de 1 semana, no con el
default de 24h del endpoint (outbound_panel.py:5101). No es un bug — es la ventana
deslizante. Puede explicar buena parte de la queja y es decision de producto.

**Correccion de una hipotesis previa.** Se creia que
`rebuild_zset_from_conversations` (app.py:616) era el que hacia "aparecer" las
conversaciones dias despues. Es falso: re-inserta con el timestamp REAL del mensaje
(app.py:663), asi que su huella es desfase cero. Lo que si se midio: 5 contactos con
score del ZSET 1,7 a 5,8 dias POSTERIOR a su ultimo mensaje real — algo que no fue un
mensaje (toma de control, recuperacion admin, cambio de etapa) los subio a la cabeza
de la lista. Ese es el sintoma real.

**Recomendacion:** el arreglo es de backend, no de frontend. **2.2 (coordinador de
sincronizacion en index.js) no se justifica** con estos numeros: es el cambio de mayor
riesgo del roadmap y no hay defecto medido que lo respalde.

**Tests:** `tests/panel/test_audit_visibility.py`, 28 casos, validados con 10
mutaciones (las 10 detectadas). Baseline intacto: 32 fallos en `tests/panel`, 7 en
`tests/middleware`. Ruff F,E9 limpio.

**Nota:** `scripts/` y `tests/` estan en .gitignore — ninguno de los dos archivos
viaja en el PR.

### 10 de agosto de 2026 — Informe Mensual de Julio 2026 (ENTREGADO)

**Entregable:** `INFORME_SOFIA_JULIO_2026.docx` (12 paginas, audiencia gerencia).

**Cifras verificadas del mes:**
- 12.977 mensajes | 914 clientes unicos | 653 usuarios nuevos | 828 contactos CRM
- Desglose emisor: client 6.440, advisor 4.352, bot 2.051, system 134
- Automatizacion: solo 47 de 914 conversaciones (5,1%) cerraron sin asesora
- Pico 15-jul: 302 clientes (7,5x mediana) y 213 contactos (10,4x) — campana masiva a propietarios
- Ventana 08-16 concentra 87,9% del trafico; hora pico 10:00 (16,2%)
- Fin de semana cae 65,8% frente a dias laborables
- Reparto asesoras: Luisa Munoz 541 (65,3%), Jubeny Ocampo 287 (34,7%)

**Hallazgos de auditoria:**
1. **193 contactos sin `canal_origen` (23,3%)** ≈ **195 en lifecycle "Propietarios" (23,6%)**.
   La carga masiva de propietarios crea contactos por una via que NO asigna canal_origen.
   Pendiente de corregir en el flujo de carga bulk.
2. **235 contactos CRM (28,4%) sin ningun mensaje** — esperado en campana saliente, no es fuga.
3. **Cobertura CRM 99,5%**: 650 de 653 usuarios nuevos generaron contacto. Sync HubSpot OK.
4. **`conversations` NO esta vacia** (2.552 docs, 2.052 de julio) — no requiere backfill.
   En cambio la coleccion `contacts` esta en 0 documentos: verificar si es remanente.
5. Instagram y Mercado Libre: 0 contactos en julio pese a estar habilitados.

**Limitaciones de datos:**
- **Twilio no disponible para julio.** A principios de agosto se creo una cuenta nueva
  desde cero y se migro el numero, tras el bloqueo del numero de verificacion por parte
  de Claro. La cuenta nueva no hereda historial. No afecta el informe: MongoDB conserva
  el mes completo. `datos_twilio_julio.json` quedo con ceros — BORRAR, es engañoso.
- Falta correr `extract_campana_julio.py` para cuantificar la campana con precision
  (envios, fallidos, tasa de respuesta desde `bulk_campaigns`).

**Scripts creados** (`scripts/informe_julio/`, todos solo lectura, PII hasheada SHA-256):
`_common.py`, `extract_mongo_julio.py`, `extract_hubspot_julio.py`,
`extract_twilio_julio.py`, `extract_campana_julio.py`, `diagnose_twilio.py`, `README.md`.

**Deuda detectada:** al token de la Private App de HubSpot le falta el scope
`crm.objects.owners.read` (403 en `/crm/v3/owners/`). Se uso directorio de respaldo.

### 10 de agosto de 2026 — Extractores para Informe Mensual de Julio 2026 (detalle tecnico)

**Contexto:** Se solicito un informe de rendimiento de julio 2026 cruzando MongoDB,
HubSpot y Twilio. El entorno de trabajo no tiene salida de red hacia MongoDB ni
Twilio, por lo que la extraccion se externalizo a scripts ejecutables con
`railway run` (corren en la maquina local con variables de Railway inyectadas —
NO dentro del worker, sin impacto en el memory watchdog).

**Trabajo realizado:**

1. **Paquete `scripts/informe_julio/`** (4 archivos, solo lectura)
   - `_common.py` — ventana temporal, normalizacion y hasheo de telefonos
   - `extract_mongo_julio.py` — agregados de `messages`, primera-vez por diferencia
     de conjuntos, cortes diario/horario en `America/Bogota` via `$dateToString`
   - `extract_twilio_julio.py` — log de Messages entrante/saliente, status, fallidos
   - `extract_hubspot_julio.py` — contactos y notas de julio, backoff ante 429
   - Ninguno usa los singletons de la app; abren y cierran sus propios clientes

2. **Politica PII del informe:** ningun archivo de salida contiene telefonos en
   claro. SHA-256 + salt fijo compartido sobre los ultimos 10 digitos, identico en
   las 3 fuentes, lo que permite el cruce sin exponer datos personales.

**Hallazgos verificados vias MCP de HubSpot (portal 50895383):**
- 828 contactos creados en julio 2026
- Desglose por `canal_origen` que suma exactamente 828 (verificacion cruzada OK):
  whatsapp_directo 368, finca_raiz 125, metrocuadrado 93, ciencuadras 36,
  pagina_web 12, facebook 1, mercado_libre 0, instagram 0
- **193 contactos (23.3%) sin `canal_origen`** — fuga de atribucion a investigar

**Pendiente:** ejecutar los 3 extractores, cruzar las fuentes y generar el .docx.

### 10 de agosto de 2026 — Seccion 3.4: hallazgos de la campana masiva

Se ejecuto `extract_campana_julio.py` y se anadio la seccion 3.4 al informe.

**CORRECCION IMPORTANTE de atribucion.** El informe afirmaba que el pico del 15 de
julio lo causo una campana a propietarios. Los datos de `bulk_campaigns` lo desmienten:

| Campana | Segmento | Contactos | Enviados | Estado |
|---------|----------|-----------|----------|--------|
| Luisa 15-jul | No responde | 538 | 538 | completed |
| Jubeny 15-jul | No responde | 771 | 766 | **in_progress** |
| Jubeny 16-jul | Propietarios | 108 | 0 | **pending** |
| Luisa 16-jul | Propietarios | 104 | 0 | **pending** |

- El pico lo causo una **reactivacion del segmento "No responde"** (1.304 envios), no propietarios.
- Las 2 campanas a **Propietarios (212 contactos) NUNCA SE ENVIARON** — llevan pendientes
  desde el 16 de julio. **Accion requerida: despachar o cancelar.**
- La campana de Jubeny sigue en `in_progress` con 5 contactos sin procesar desde el 15-jul.
- Los 195 contactos lifecycle "Propietarios" vienen de una carga de BD, no de esta campana.

**Metricas de la campana:**
- 1.304 enviados, **0 fallidos (0,0%)** — infraestructura solida ante 10x el volumen diario
- 333 de 1.521 destinatarios respondieron: 21,9% sobre cargados / 25,5% sobre enviados reales
- Representa el 20,4% de todos los salientes del mes y el 10,0% del volumen total
- **Comparativa con mayo: 649 enviados → 31,3% respuesta. Julio: 1.304 → 25,5%.**
  El volumen se duplico (+101%) pero la tasa cayo ~6 puntos. Revisar segmentacion
  antes de subir volumen (plan de 2 masivos/mes).

**Dato tecnico:** `mensajes_con_conversation_sid = 0` en julio → el flag
`TWILIO_CONVERSATIONS_ENABLED` NO estuvo activo en produccion durante el mes.
Trazabilidad: 100% de entrantes y 99,7% de salientes con `message_sid`.

**Edicion del .docx:** se preservaron las modificaciones del usuario editando
`word/document.xml` directamente (unzip → parche XML → zip), no regenerando. El script
de parcheo quedo en `outputs/patch.py`. Validado con `validate.py`: 402 → 476 parrafos,
sin errores nuevos de esquema. Nota: en `w:pPr` el orden es estricto, `pBdr` antes de
`spacing` e `ind` despues.

### Agosto 2026 — Auditoria de Campana Masiva de Mayo y Plan de Mensajes Masivos

**Contexto:** El 6 de mayo de 2026 se envio la primera campana masiva de WhatsApp a clientes del CRM usando el sistema bulk del panel (`outbound_panel.py`). Se requirio un analisis profundo de resultados para presentar a la gerencia.

**Trabajo realizado:**

1. **Script de auditoria MongoDB** (`scripts/audit_campana_mayo6.py`, 350 lineas)
   - Consulta mensajes bulk del 6 de mayo en MongoDB
   - Clasifica respuestas por keywords (positivo, negativo, saludo, spam)
   - Detecta menciones de cita/visita y patrones de no-match
   - Exporta resultados a JSON

2. **Script de auditoria detallada** (`scripts/audit_campana_detalle.py`, 357 lineas)
   - Clasificacion mejorada con variantes del espanol ("sii", "sip", "sib")
   - Numeros completos (sin enmascarar) para prueba visual a gerencia
   - Conteo por patron detectado (presupuesto, zona, tipo de inmueble)
   - 5 categorias de patron: Presupuesto bajo, Presupuesto limite, Zona no cubierta, Tipo no disponible, Precio alto

3. **Documento de analisis** (`ANALISIS_CAMPANA_MASIVA_MAYO_2026.docx`)
   - Generado con docx-js (~603 lineas JS)
   - Portada + 8 secciones: metricas, clasificacion, desenlace, patrones, conclusiones
   - Datos finales verificados manualmente por el usuario:
     - 648 enviados, 203 respondieron (31.3%)
     - 90 positivos, 58 negativos, 44 ambiguos, 11 negocios
     - 4 citas reales (verificado), 1 cancelacion, 36 dejaron de responder, 49 sin opciones
     - Costo por cita: ~$3.73 USD
   - Tabla de numeros completos de contactos clave
   - Embudo visual de conversion

4. **Plan de Mensajes Masivos** (`PLAN_MENSAJES_MASIVOS_PROTEGER.docx`)
   - Documento para gerencia con costos, formulas y recomendaciones
   - Precios Twilio Colombia (abril 2026): Marketing ~$0.0149 + $0.005 Twilio = ~$0.02/msg
   - Formula validada: Costo = N x $0.023 (incluye respuestas)
   - Resumen ejecutivo agregado via edicion XML del DOCX

5. **Prompt reutilizable** (`PROMPT_AUDITORIA_CAMPANA_MASIVA.md`)
   - Template con variables reemplazables para auditar futuras campanas
   - Reconstruido del prompt original que se uso para la auditoria de mayo

**Hallazgos clave de la campana de mayo:**
- 31.3% tasa de respuesta (buena para reactivacion)
- 54.4% de los positivos se perdieron por falta de opciones de inmueble (el mayor cuello de botella)
- Solo 0.6% conversion a cita (4/648)
- El problema no es el canal, es el inventario de propiedades

### Julio 2026 — Informe Mensual Junio + Email vs WhatsApp

- Generado `INFORME_SOFIA_JUNIO_2026.docx` (v8, 27KB, 658 parrafos)
- Seccion de Email vs WhatsApp con datos corregidos: 93 correos, 75 unicos, 43 ambos canales
- Script `audit_email_vs_whatsapp.py` para cruzar datos MongoDB

### Anteriores

- **Marzo 2026:** Informe tecnico completo (`docs/INFORME_TECNICO_ESTADO_ACTUAL_2026-03-03.md`, 777 lineas)
- **Sistema bulk:** Implementado en `outbound_panel.py` con batch processing, dedup, throttle, templates aprobados, auto-promocion de etapa

---

## Archivos de Alto Riesgo (Actualizado)

| Archivo | LOC aprox. | Riesgo | Nota |
|---------|-----------|--------|------|
| `middleware/outbound_panel.py` | 8,200+ | EXTREMO | Singletons, 16+ endpoints, bulk campaigns |
| `middleware/webhook_handler.py` | 1,900+ | EXTREMO | Entrada Twilio, ADMIN_API_KEY |
| `middleware/sofia_brain.py` | — | ALTO | Motor IA, memoria Redis L1 |
| `agents/CRMAgent/crm_agent.py` | 8,000+ | ALTO | Sync HubSpot |
| `middleware/conversation_state.py` | — | ALTO | Modelos Redis |

---

## Feature Flags Activos

| Flag | Ubicacion | Estado |
|------|-----------|--------|
| `TWILIO_CONVERSATIONS_ENABLED` | sofia_brain.py, webhook_handler.py | En migracion |
| `QUERY_PROFILER_ENABLED` | middleware/query_profiler.py | true (default) |
| `SERVER_TIMING_ENABLED` | middleware/query_profiler.py | true (default) |

---

## Investigaciones Pendientes (Arquitectura Mayor)

### 1. Rate limit 429 de HubSpot
Backoff bloqueante dentro del request (hasta 36s por GET). Investigar: cola asincrona, token bucket, circuit breaker, cache agresiva, webhooks HubSpot. Evaluar viabilidad de eliminar HubSpot.

### 2. Fuente unica de verdad
Lista de contactos del panel se arma desde 6 rutas de codigo. Investigar rediseno hacia fuente unica — variante con HubSpot y variante sin el.

---

## Tests

- Panel: 51/51 PASS (`tests/panel/`)
- Bulk campaigns: suite completa en `tests/panel/test_bulk_campaigns.py`

---

**Siguiente sesion:** Continuar con tareas de desarrollo pendientes. Si se realiza otra campana masiva, usar `PROMPT_AUDITORIA_CAMPANA_MASIVA.md` como template.
