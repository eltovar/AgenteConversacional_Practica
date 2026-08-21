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


### Fase 0 — Fundamentos


| ID | Documento | Archivo | Estado |
|---|---|---|---|
| D-01 | Glosario / lenguaje ubicuo | `01-glosario.md` | ⚠️ Borrador v0 — requiere revisión |
| D-02 | Actores y roles | `02-actores-y-roles.md` | ⚠️ Borrador v0 — roles ✅ confirmados por wireframe |
| D-03 | Visión, objetivos y no-objetivos | `03-vision.md` | ⚠️ Borrador v0 — reconstruido, 7 preguntas abiertas |
| — | Wireframes recuperados (catálogo) | `wireframes/README.md` | ✅ Fuente primaria |


### Fase 1 — Comportamiento (el qué)


| ID | Documento | Estado |
|---|---|---|
| D-04 | Inventario de capacidades actuales (as-is) | ⬜ No iniciado |
| D-05 | Casos de uso + criterios de aceptación | ⬜ Existe borrador parcial en `DOCUMENTACION.docx` (solo imágenes) |
| D-06 | Matriz de permisos (rol × recurso × acción) | ⬜ Existe título en `DOCUMENTACION.docx`, sin contenido |
| D-07 | Journey maps (cliente / asesora) | ⬜ No iniciado |
| D-08 | Máquinas de estado | ⬜ No iniciado |
| D-09 | Catálogo de eventos de dominio | ⬜ No iniciado |


### Fase 2 — Estructura (el cómo)


| ID | Documento | Estado |
|---|---|---|
| D-10 | Modelo de dominio + ERD + diccionario de datos | ⬜ No iniciado |
| D-11 | Política de fuente de verdad por entidad | ⬜ No iniciado — máxima prioridad técnica |
| D-12 | Arquitectura objetivo (C4) | ⬜ No iniciado |
| D-13 | Contratos de API y eventos | ⬜ No iniciado |
| D-14 | ADRs (decisiones arquitectónicas) | ⬜ No iniciado — mínimo 5 |
| D-15 | Requisitos no funcionales (PII, memoria, costo, disponibilidad) | ⬜ No iniciado |


### Fase 3 — Transición


| ID | Documento | Estado |
|---|---|---|
| D-16 | Antes vs Después → `16-antes-despues.md` | ⚠️ Borrador v0 — "Antes" ✅ verificado, "Después" 🔵 hipótesis |
| D-17 | Benchmark de CRMs → `17-benchmark-crms.md` | ⚠️ Borrador v0 — reconstruido, 10 patrones propuestos |
| D-18 | Estrategia de migración, coexistencia y rollback | ⬜ No iniciado |
| D-19 | Matriz de riesgos | ⬜ No iniciado |
| D-20 | Roadmap + matriz de trazabilidad | ⬜ No iniciado |
| D-21 | Definition of Ready (contrato con desarrollo) | ⬜ No iniciado |


---


## Insumos faltantes (bloquean D-03, D-16, D-17)


| Insumo | Por qué se necesita | Cómo obtenerlo |
|---|---|---|
| `Planos_Rediseno_SofIA_v1.docx` | Confirmado perdido (2026-08-05). Definía fases F1–F6, casos de uso UC-xx, sprints. | ✅ Reconstruido en D-03 / D-16 / D-17 desde wireframes + código + auditoría |
| Puntos 3, 4, 5 del informe | Visión, antes/después, benchmark | ✅ Reconstruidos como borradores v0 |
| Roles "Interna" / "Seguimiento" |  | ✅ Confirmados por wireframe `img08` |


### 🔴 Bloqueo activo — la pregunta V-4


El código organiza el trabajo por CANAL. Los wireframes lo organizan por FUNCIÓN. Son incompatibles. Hasta resolverlo no se puede avanzar en D-06 (permisos), D-10 (modelo de datos) ni D-11 (fuentes de verdad). Detalle en D-03 §8 y [D-16 §2](16-antes-despues.md).

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

## Registro de preguntas abiertas — al 2026-08-20

> **Cómo leer los documentos.** Muchos bloques `❓` del cuerpo son **rastro de la discusión**, no preguntas vivas: se conservan para que se entienda cómo se llegó a cada decisión, y llevan encima la resolución marcada `✅ RESUELTO`.
> **Esta tabla es la única lista de preguntas realmente abiertas.** Si no está aquí, está decidido.

### Fase 0 — Fundamentos *(100 % · 2 preguntas menores)*

| # | Documento | Pregunta | Peso |
|---|---|---|---|
| G-1 | D-01 | ¿`default` y `desconocido` son canales reales o valores de reserva que se colaron en el registro? | 🟡 Menor |
| G-2 | D-01 | Nombres definitivos para *Escalamiento* (bot→humano) y *Reasignación* (humano→humano). Manda la palabra que ya usen las asesoras | 🟡 Menor |
| V-2b | D-03 | **Las metas** de los tres números. Los valores de "hoy" ya están medidos | 🟠 Del negocio |
| V-2c | D-03 | Tiempo de primera respuesta — requiere acceso a MongoDB, no está en HubSpot | 🟠 Técnico |

### Fase 2 — Estructura

| # | Documento | Pregunta | Peso |
|---|---|---|---|
| A-3 | ADR-001 | ¿Cuánto dura la sesión? Con 2 dispositivos, eterna es riesgo y corta es molestia | 🟠 |
| A-4 | ADR-001 | ¿Qué pasa si alguien pierde acceso a su Gmail? | 🟠 |
| A-5 | ADR-001 | ¿Hay intención de migrar a Google Workspace con dominio propio? | 🟡 |
| A-6 | ADR-001 | ¿Se permite que dos personas compartan un correo? Hoy "Publicidad Proteger" es un owner que no es persona | 🟡 |
| D11-1 | D-11 | ¿Qué retardo de sincronización se acepta por entidad? Hoy son 6 h para ownership | 🔴 **Bloquea D-11** |
| D15-1 | D-15 | ¿Las automatizaciones pasan a ser configurables o se quedan en código? | 🟠 |
| D08-1 | D-08 | ¿Hace falta "posponer" una conversación? Hoy se simula con inactividad de 48 h | 🟡 |

### Del negocio, no de la documentación

| # | Pregunta |
|---|---|
| N-1 | Metas de los tres números de referencia |
| N-2 | ¿O-6 (auditoría de lo que hace la IA) es objetivo o se retira? |
| N-3 | ¿Se confirma "no reescribir desde cero" y "sin app móvil nativa" como no-objetivos? |

**Total: 14 preguntas abiertas.** Solo **una** bloquea un documento (D11-1). Ninguna bloquea la extracción de Fase 1.

