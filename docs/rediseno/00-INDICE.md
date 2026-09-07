> Estado: Fase de planeación · Iniciado: 5 de agosto de 2026
Regla de oro: ningún documento pasa a "Cerrado" hasta que CyberTovar lo confirme explícitamente.
Los borradores marcados `⚠️` contienen suposiciones derivadas del código, no decisiones tomadas.

---


## Decisiones de alcance ya cerradas


| # | Decisión | Valor | Fecha |
|---|---|---|---|
| A-01 | Alcance del sistema destino | CRM propio para Inmobiliaria Proteger (un solo tenant). No es SaaS multi-inmobiliaria. | 2026-08-05 |
| A-02 | Rol de HubSpot | *Backbone invisible.* HubSpot es la capa de persistencia; SofIA es la única interfaz. Las asesoras nunca abren HubSpot. SofIA replica y filtra por rol la funcionalidad de HubSpot. | 2026-08-05 |
| A-03 | Formato de la documentación | Doble salida. Original: Markdown + Mermaid versionado en `docs/rediseno/`. Espejo legible: Word generado en `Desktop\Documentacion Re-estructura SofIA\Documentacion Faltante\`, organizado por fases. | 2026-08-05 |


### Cómo se mantiene el espejo en Word


```bash
python docs/rediseno/_tools/md2docx.py
```


Se ejecuta después de crear o modificar cualquier documento. Regenera todos los `.docx` desde el Markdown, así que los Word nunca se editan a mano — son salida, no fuente. Para añadir un documento nuevo al espejo, se registra en el diccionario `LAYOUT` del script (define carpeta de fase y nombre legible).

### Consecuencia crítica de A-02 (registrar en ADR)


HubSpot no filtra pipelines por rol — esa capacidad exige HubSpot Enterprise (~$800/mes). SofIA la resuelve por su cuenta. Eso convierte a SofIA en responsable de la autorización, no solo de la presentación. Implicaciones:
- El filtrado por rol no puede vivir en el frontend. Si vive en JS, cualquier asesora ve todo con abrir DevTools.
- Cada entidad nueva del CRM (etapa, propiedad, tarea, nota, cita) necesita su propia política de sincronización con HubSpot.
- Hoy existe reconciliación de 6h solo para ownership. Habrá que extenderla o rediseñarla.

---


## Backlog de documentos


### Fase 0 — Fundamentos · **100 %**


> ⚠️ **Estado actualizado el 2026-08-27.** El detalle archivo por archivo, con lo que está escrito pero desactualizado, vive en [AUDITORIA.md](AUDITORIA.md).

| ID | Documento | Archivo | Estado |
|---|---|---|---|
| D-01 | Glosario / lenguaje ubicuo | `01-glosario.md` | ✅ Escrito — 371 líneas |
| D-02 | Actores y roles | `02-actores-y-roles.md` | ✅ Escrito — 1.523 líneas, roles confirmados por wireframe |
| D-03 | Visión, objetivos y no-objetivos | `03-vision.md` | ✅ Escrito — métricas medidas en producción |
| — | Wireframes recuperados (catálogo) | `wireframes/README.md` | 🟠 29 imágenes · falta `wf32.png` |


### Fase 1 — Comportamiento (el qué) · **100 %**


| ID | Documento | Estado |
|---|---|---|
| D-04 | Inventario de capacidades actuales (as-is) | ✅ Escrito — **corregido el 26-ago** (fuentes de identidad de asesoras) |
| D-05 | Casos de uso + criterios de aceptación | ✅ **Cerrado el 27-ago** — **34 casos, todos con criterios**. Ya no quedan casos sin detallar |
| D-06 | Matriz de permisos (rol × recurso × acción) | ✅ **Cerrado el 27-ago** — §4 reescrita al modelo 7+3, auditoría definida |
| D-07 | Journey maps (cliente / asesora) | ✅ Escrito — 5 recorridos · ⚠️ **3 journeys sin escribir**, con material ya disponible (§8) |
| D-08 | Máquinas de estado | ✅ **Ampliado el 28-ago** — §3-bis documenta la transferencia automática real de hoy |
| D-09 | Catálogo de eventos de dominio | ✅ Escrito — 22 tipos, E-01…E-22 |


### Fase 2 — Estructura (el cómo) · **100 %** ✅


| ID | Documento | Estado |
|---|---|---|
| D-10 | Modelo de dominio + ERD + diccionario de datos | ✅ Escrito — **corregido el 26-ago** |
| D-11 | Política de fuente de verdad por entidad | ✅ **Cerrado el 27-ago** — la cola de propagación resuelta en ADR-006 |
| D-12 | Arquitectura objetivo (C4) | ✅ **Cerrado el 27-ago** — §7 con los módulos medidos, §8 con ADR-006. Falta solo el diagrama de despliegue |
| D-13 | Contratos de API y eventos | ✅ Escrito |
| D-14 | ADRs (decisiones arquitectónicas) | ✅ **Cerrado el 27-ago** — **ADR-001…006 completos** |
| D-15 | Requisitos no funcionales (PII, memoria, costo, disponibilidad) | ✅ **Cerrado el 28-ago** — disponibilidad, PII y retención, coste del LLM y 8 alertas mínimas |


### Fase 3 — Transición · **62 %**


| ID | Documento | Estado |
|---|---|---|
| D-16 | Antes vs Después → `16-antes-despues.md` | ✅ **Actualizado el 28-ago** — AD-1 a AD-8 resueltas · reparto de código medido |
| D-17 | Benchmark de CRMs → `17-benchmark-crms.md` | ✅ Escrito — 10 patrones propuestos |
| D-18 | Estrategia de migración, coexistencia y rollback → `18-migracion.md` | ✅ **Escrito el 26-ago** — análisis de módulos, 8 cargas, coexistencia, reversión |
| D-19 | Matriz de riesgos | 🔴 No iniciado |
| D-20 | Roadmap + matriz de trazabilidad | 🔴 No iniciado |
| D-21 | Definition of Ready (contrato con desarrollo) | 🔴 No iniciado |
| **D-22** | Investigación de herramientas de mensajería (XMPP · API Gateway · WS) | ✅ **Escrito el 27-ago** — 7 hallazgos, 2 de gravedad alta |


---


## Insumos faltantes (bloquean D-03, D-16, D-17)


| Insumo | Por qué se necesita | Cómo obtenerlo |
|---|---|---|
| `Planos_Rediseno_SofIA_v1.docx` | Confirmado perdido (2026-08-05). Definía fases F1–F6, casos de uso UC-xx, sprints. | ✅ Reconstruido en D-03 / D-16 / D-17 desde wireframes + código + auditoría |
| Puntos 3, 4, 5 del informe | Visión, antes/después, benchmark | ✅ Reconstruidos como borradores v0 |
| Roles "Interna" / "Seguimiento" |  | ✅ Confirmados por wireframe `img08` |


### ✅ RESUELTO — la pregunta V-4


> **Estuvo bloqueando D-06, D-10 y D-11. Ya no.**

El código organiza el trabajo por CANAL. Los wireframes lo organizan por FUNCIÓN. **No son incompatibles: son dos dimensiones ortogonales.** El canal decide **de quién es el lead al entrar**; el rol decide **qué puede hacer** esa persona con él. Finca Raíz sigue entrando a Luisa, y Luisa además tiene el rol A. Seguimiento.

Detalle en D-03 §8 y [D-16 §2](16-antes-despues.md). Los tres documentos que bloqueaba están escritos.

---


## Documentos técnicos preexistentes (fuera de git hasta hoy)


`/docs/` estaba en `.gitignore` (línea 36). Contenido recuperable como insumo para D-04:
- `INFORME_TECNICO_ESTADO_ACTUAL_2026-03-03.md`
- `PLAN_IMPLEMENTACIONES_PANEL_v3.md`
- `RESUMEN_IMPLEMENTACIONES_v3.md`
- `HUBSPOT_TIMELINE_SETUP.md`
- `deal_stage_tracking_setup.md`
> ⚠️ `CLAUDE.md` referencia `ESTADO_ACTUAL_PROYECTO.md` como "contexto maestro". Ese archivo no existe en el repositorio.

---

## Registro de preguntas abiertas — al 2026-08-28

> **Cómo leer los documentos.** Muchos bloques `❓` del cuerpo son **rastro de la discusión**, no preguntas vivas: se conservan para que se entienda cómo se llegó a cada decisión, y llevan encima la resolución marcada `✅ RESUELTO`.
> **Esta tabla es la única lista de preguntas realmente abiertas.** Si no está aquí, está decidido.

### Fase 0 — Fundamentos *(100 % · 2 preguntas menores)*

| # | Documento | Pregunta | Peso |
|---|---|---|---|
| G-1 | D-01 | ¿`default` y `desconocido` son canales reales o valores de reserva que se colaron en el registro? | 🟡 Menor |
| G-2 | D-01 | Nombres definitivos para *Escalamiento* (bot→humano) y *Reasignación* (humano→humano). Manda la palabra que ya usen las asesoras | 🟡 Menor |
| V-2b | D-03 | **Las metas** de los tres números. Los valores de "hoy" ya están medidos | 🟠 Del negocio |
| ~~V-2c~~ | D-03 | ✅ **Resuelta.** Medido en MongoDB: SofIA **0,7 min**; asesora **30 min mediana, p90 24,1 h** | ✅ |

### Fase 2 — Estructura

| # | Documento | Pregunta | Peso |
|---|---|---|---|
| A-3 | ADR-001 | ¿Cuánto dura la sesión? Con 2 dispositivos, eterna es riesgo y corta es molestia | 🟠 |
| A-4 | ADR-001 | ¿Qué pasa si alguien pierde acceso a su Gmail? | 🟠 |
| A-5 | ADR-001 | ¿Hay intención de migrar a Google Workspace con dominio propio? | 🟡 |
| A-6 | ADR-001 | ¿Se permite que dos personas compartan un correo? Hoy "Publicidad Proteger" es un owner que no es persona | 🟡 |
| D11-1 | D-11 | ✅ **Resuelto por [ADR-006](14-adrs.md).** Reintento con espera creciente y tope en ~9 h — nunca peor que las 6 h de hoy. Queda solo confirmar los parámetros (Q6-1 a Q6-3) | 🟡 Confirmar |
| D15-1 | D-15 | ¿Las automatizaciones pasan a ser configurables o se quedan en código? | 🟠 |
| **Q6-1** | ADR-006 | Un elemento abandonado en la cola, ¿avisa al Administrador de forma activa o espera a que abra la pantalla? *(recomiendo activa)* | 🟡 |
| **Q6-2** | ADR-006 | ¿Cuánto se guardan los elementos ya entregados? *(recomiendo 30 días)* | 🟡 |
| **Q6-3** | ADR-006 | ¿Se acepta la tabla de reintentos con tope en ~9 h? *(nunca es peor que las 6 h de hoy)* | 🟡 |
| ~~P-6.1~~ | D-06 | ✅ **Resuelta.** `Post Cita` SÍ es motivo de cierre — el cliente declinó tras la visita — y además dispara transferencia manual a Seguimiento | ✅ |
| ~~P-6.2~~ | D-06 | ✅ **Resuelta.** Hoy es **una sola acción**: la etapa dispara la transferencia (`STAGES_TRANSFER_TO_LUISA`) | ✅ |
| ~~P-6.3~~ | D-06 | ✅ **Resuelta.** El contacto desaparece de quien transfiere — verificado en código | ✅ |
| ~~P-6.4~~ | D-06 | ✅ **Resuelta.** Seguimiento custodia **y** trabaja activamente. Es cola viva | ✅ |
| ~~P-6.5~~ | D-06 | ✅ **Resuelta.** `motivo_cierre` convive con `Cerrado Ganado`. Al contar el embudo **manda la etapa** | ✅ |
| ~~P-6.6~~ | D-06 | ✅ **Resuelta.** `Cerrado Perdido` = sin interés definitivo | ✅ |
| ~~P-6.7~~ | D-06 | ✅ **Resuelta.** Botón explícito: cambiar una propiedad **no** transfiere. `STAGES_TRANSFER_TO_LUISA` desaparece | ✅ |
| ~~M-1~~ | D-22 | ✅ **Resuelta el 28-ago.** ⚠️ *La entrada anterior de esta tabla decía que no estaba desplegada — era falso.* Commit `8fdbc2a` en producción (deployment `65a8a8c9`), modo `log_only`, **4 de 4 firmas válidas**. Verificado contra Railway | ✅ |
| **M-1b** | D-22 | 🔴 **Nuevo:** ¿cuándo se pasa a `enforce`? Hoy valida pero **no bloquea**. Con 4 muestras conviene dejar correr más tráfico | 🟠 **Seguridad** |
| **M-2** | D-22 | ¿Las asesoras deben recibir avisos con el panel cerrado? Decide si entra Web Push | 🟠 |
| **N15-1** | D-15 | ¿99,5 % de disponibilidad en horario laboral, o basta "que no se note"? | 🟡 |
| **N15-2** | D-15 | ¿Las alertas van por WhatsApp al Administrador o por correo? *(recomiendo WhatsApp)* | 🟡 |
| **N15-3** | D-15 | ¿Hace falta derecho al olvido? Hoy **nadie puede borrar** contactos | 🟠 Añade alcance |
| D08-1 | D-08 | ¿Hace falta "posponer" una conversación? Hoy se simula con inactividad de 48 h | 🟡 |

### Del negocio, no de la documentación

| # | Pregunta |
|---|---|
| N-1 | Metas de los tres números de referencia |
| N-2 | ¿O-6 (auditoría de lo que hace la IA) es objetivo o se retira? |
| N-3 | ¿Se confirma "no reescribir desde cero" y "sin app móvil nativa" como no-objetivos? |

**Total: 19 preguntas abiertas — ninguna bloquea la documentación.** Se resolvieron M-1, V-2c y P-6.7; entran M-1b y las tres de D-15. **D-06 y D-16 quedan cerrados sin preguntas.** La única que añade alcance es **N15-3** (derecho al olvido); la única con reloj es **M-1b** (pasar la firma a `enforce`).

