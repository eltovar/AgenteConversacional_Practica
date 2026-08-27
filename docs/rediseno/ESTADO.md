# Estado de la planeación — SofIA CRM

> **Instantánea al 2026-08-27.** Documento de continuidad: si se pierde el contexto de la sesión, **este archivo basta para retomar**.
> Índice navegable en [00-INDICE.md](00-INDICE.md) · preguntas abiertas al final.

---

## 1. Qué se está haciendo

Rediseño del ~80 % del sistema SofIA para Inmobiliaria Proteger. **Fase de planeación**: no se escribe código de producto, se produce la documentación que entra como especificación al desarrollo.

**El detonante** no fue un fallo técnico: la empresa cambió su forma de vender a un modelo **por roles y tareas**, y el sistema actual —construido como una bandeja de WhatsApp compartida— no puede representarla.

---

## 2. Decisiones de alcance

| # | Decisión |
|---|---|
| **A-01** | **CRM propio** para Inmobiliaria Proteger. Un solo tenant, no SaaS |
| **A-02** | **HubSpot como backbone invisible.** Las asesoras nunca lo abren |
| **A-03** | Documentación en Markdown (repo) + espejo Word (Desktop) |
| — | El producto se llama **SofIA CRM**. `SofIA` designa solo al agente de IA |
| — | Desarrollo por CyberTovar + Claude Code, en **rama separada** con `main` vivo |

---

## 3. El modelo, en seis decisiones

| # | Decisión | Consecuencia |
|---|---|---|
| 1 | **Canal y Rol son ortogonales** | El canal define de quién es el lead al entrar; el rol define qué puede hacer esa persona |
| 2 | **Un Contacto es una persona** | La clave es el teléfono. Muere `(teléfono, canal)` |
| 3 | **Usuario / Rol / Canal / Propietario separados** | Cuatro tablas donde hoy hay un campo |
| 4 | **7 etapas + 3 grupos de propiedades** | El embudo deja de ser excluyente. Kanban de 7 columnas, no 21 |
| 5 | **SofIA es un asignatario, no un estado** | Desaparecen los 4 estados conversacionales |
| 6 | **El historial es un registro de eventos** | 22 tipos de evento. Es lo que hace medible el sistema |

### Roles y usuarios

| Owner ID | Persona | Rol |
|---|---|---|
| `89096378` | Jubeny Ocampo | A. Interna |
| `89096379` | Monica Administracion | A. Interna — sin canales hoy |
| `89096380` | Luisa Muñoz | A. Seguimiento — Finca Raíz, MetroCuadrado |
| `82598814` | Publicidad Proteger | Marketing ⚠️ owner que no es persona |
| `86909130` | Salo Tovar | Administrador |
| `90889555` | Hector Guerra | Administrador |

**Etapas visibles:** Interna 11 · Seguimiento 19 · Administrador 21.
**Navegación:** asesoras `Dashboard · Lead · WhatsApp` · Admin `Dashboard · Lead` + ⚙ · Marketing `Dashboard · Redes · Citas`.

### Otras decisiones cerradas

Transferencia Interna → Seguimiento **manual y sin retorno** · reasignar un portal afecta **solo a leads nuevos** · Admin y Marketing **no leen conversaciones** · sin colaboradores · sin archivado · sin posponer · ventana de 24 h **cambia el modo de envío, no el dueño** · `whatsapp` y `whatsapp_directo` se fusionan · autenticación **con Google** · **monolito modular**, no microservicios · automatizaciones en código con parámetros configurables · ventana semanal **lunes a sábado** para todos los roles.

---

## 4. Lo medido en producción

| Métrica | Valor | Fuente |
|---|---|---|
| Contactos totales | 3.209 | HubSpot |
| Leads al mes | **578** (~19/día) | HubSpot |
| % que llega a visita | **6,1 %** *(suelo)* | HubSpot |
| Respuesta de **SofIA** | **0,7 min** · 95 % bajo 5 min | MongoDB |
| Respuesta de **asesora** | **30 min mediana · p90 24,1 h** | MongoDB |
| Contactos sin dueño | **0** | HubSpot |
| `No Responde` | 1.220 — 38 % de la base | HubSpot |
| Mensajes almacenados | 56.377 | MongoDB |

> 🔑 **SofIA cumple; el problema está en la cola de la distribución humana.** 1 de cada 4 turnos tarda más de 6,7 h y 1 de cada 10 más de 24 h.
> 🔴 **Hoy el sistema no puede medir ni su embudo ni su tiempo real de respuesta**, porque no registra el handoff ni el recorrido de etapas. Es la justificación de D-09.

---

## 5. Hallazgos técnicos que condicionan el rediseño

| # | Hallazgo |
|---|---|
| 1 | ✅ **Corregido 2026-08-26.** `advisors_registry.py` **ya es la fuente única** de identidad de asesoras: `OWNERS_CONFIG` se deriva de ella y `panel_advisors` es una colección muerta. Lo único duplicado que queda es el `owner_id` escrito a mano por canal en `channels_registry.py`. Detalle en [D-18 §2](18-migracion.md) |
| 1-bis | `advisors_registry` ya declara `receives_leads`, `uses_panel` y `receives_transfers` — **un modelo de roles embrionario**. `receives_transfers: True` en Luisa **es** el rol A. Seguimiento, ya codificado |
| 2 | **Dos arquitecturas de agente** y **dos máquinas de estado** conviven en producción |
| 3 | La clave de administrador **viaja en la URL** de `/metrics` — Marketing tiene de facto acceso total |
| 4 | `link_detector` y `property_code_detector` **detectan y descartan** justo los datos que necesita el rediseño |
| 5 | `outbound_panel.py` tiene **10.222 líneas y 72 endpoints** |
| 6 | **Cinco endpoints** existen solo para recuperarse de corrupción de estado |
| 7 | La colección `contacts` está **vacía** — código muerto |
| 8 | `INTERES`, `EVENTO` y `CANAL_ASIGNADO` **no caben en HubSpot** — viven solo en la base propia |
| 9 | **No existe banco de evaluación de prompts**: cada cambio es una apuesta a ciegas |

---

## 6. Avance

| Fase | Documentos | Avance |
|---|---|---|
| **Fase 0 — Fundamentos** | D-01 · D-02 · D-03 | **100 %** ✅ |
| **Fase 1 — Comportamiento** | D-04 · D-05 · D-06 · D-07 · D-08 · D-09 | **100 %** ✅ |
| **Fase 2 — Estructura** | D-10 · D-11 · D-12 · D-13 · D-14 · D-15 | **92 %** |
| **Fase 3 — Transición** | D-16 · D-17 · **D-18** ✅ · D-19 · D-20 · D-21 | **50 %** |
| **Total** | **19 de 21 documentos** · 5.892 líneas | **93 %** |

> 📋 **Auditoría completa archivo por archivo:** [AUDITORIA.md](AUDITORIA.md) — incluye los 9 puntos que están escritos pero desactualizados.

### Lo que falta escribir

| Doc | Prioridad |
|---|---|
| **ADR-006** cola de propagación | 🔴 Sostiene D-11 y D-12. El patrón ya existe en envíos masivos |
| **D-15** completar NFR de PII, memoria y disponibilidad | 🟠 Hoy son 67 líneas |
| D-19 Riesgos · D-20 Roadmap · D-21 Definition of Ready | 🟠 |

### El reparto real del rediseño *(medido con el grafo de código, D-18)*

| | % del sistema |
|---|---|
| ✅ Intacto | **~55 %** |
| 🟡 Se mueve o ajusta | ~15 % |
| 🔴 Se reescribe | **~30 %** |

> **Menos de un tercio del código se reescribe.** El "80 % de rediseño" es de **producto**, no de código.

---

## 7. Cargas de migración identificadas

| # | Qué | Volumen | Dificultad |
|---|---|---|---|
| 1 | Repartir `lifecyclestage` en 4 campos | **3.209 contactos** | 🔴 El dato de etapa ya se perdió en los clasificados |
| 2 | Colapsar `(teléfono, canal)` a `teléfono` | 3.146 conversaciones | 🟠 |
| 3 | Crear `USUARIO` desde `advisors_registry` | 6 owners | 🟢 **Trivial — el registro ya existe** |
| 4 | Sacar `owner_id` de `channels_registry` a `CANAL_ASIGNADO` | 16 canales | 🟢 |
| 5 | Fusionar `whatsapp` / `whatsapp_directo` | por contar | 🟢 |
| 6 | Renombrar `Seguimiento` → `Post Cita` en HubSpot | 1 etapa | 🟢 El ID no cambia |
| 7 | Crear etapa `Nuevo Lead` en HubSpot | — | 🟠 Toca producción |
| 8 | Eliminar colecciones muertas `contacts` y `panel_advisors` | 0 y 4 docs | 🟢 Cero consumidores |
| — | Poblar `EVENTO` con histórico | ❌ **Imposible** | Las métricas arrancan desde cero |

> 🔑 **La carga 1 tiene solución:** inferir la etapa de las **496 citas** de `appointments` — fuente objetiva. Detalle en [D-18 §6](18-migracion.md).

---

## 8. Preguntas abiertas

**Del negocio:** metas de los tres números · ¿O-6 auditoría de la IA es objetivo? · confirmar los no-objetivos.

**Técnicas menores:** duración de sesión (A-3 resuelto: 12 h) · pérdida de Gmail (A-4 resuelto) · `default` y `desconocido` como canales · nombres *Escalamiento* / *Reasignación* · ¿`presupuesto` y `segmento` se replican a HubSpot?

**Ninguna bloquea la escritura de los documentos que faltan.**

---

## 9. Cómo se trabaja

| | |
|---|---|
| **Fuente** | Markdown en `docs/rediseno/` |
| **Espejo** | Word en `Desktop\Documentacion Re-estructura SofIA\Documentacion Faltante\`, por fases |
| **Regenerar** | `python docs/rediseno/_tools/md2docx.py` |
| **Recuperar** | `python docs/rediseno/_tools/docx2md.py` reconstruye el Markdown desde el Word |
| **Marcas** | ✅ hecho · 🔵 hipótesis · ⚠️ borrador · ❓ pregunta · 🔴 bloqueo |

> ⚠️ **El Word es salida, no fuente.** Editarlo se pierde al regenerar.
> 🔴 **La documentación sigue sin commitear.** Ya se perdió una vez por eso.

```bash
git add .gitignore docs/rediseno
```
```bash
git commit -m "docs(rediseno): planeacion al 93 por ciento con D-18 y auditoria"
```
