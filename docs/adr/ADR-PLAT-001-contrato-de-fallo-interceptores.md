# ADR-PLAT-001 · Contrato de fallo de los interceptores transversales

**Estado:** propuesta · **Fecha:** 2026-09-05 · **Rev. 2:** 2026-09-05 (verificación en producción) · **Decide:** CyberTovar
**Ámbito:** ramas `dev_juanrodriguez` y `dev_salomon` (commits `4572128` … `e6f473b`) · **Track:** estabilización de plataforma
**Sustituye:** nada · **Relacionado:** `docs/staging/03_MATRIZ_VARIABLES_STAGING.md`, auditorías 4-sep y 5-sep 2026

> **Rev. 2 — el fallo de multimedia está resuelto y medido en producción.** Es el defecto **A4**
> (§«Causa raíz»), un `NameError` por un import incompleto. **No era ninguno de los cuatro puntos
> abiertos**, pero pertenece al bloque A y es la confirmación empírica de la regla **R1**. Esta
> revisión corrige además dos afirmaciones de la Rev. 1 que apuntaban en la dirección equivocada.

---

## Contexto

La rama `dev_juanrodriguez` introduce **tres interceptores transversales a la vez**, cada uno colocado sobre un camino que hasta ahora corría sin intercepción alguna:

| # | Interceptor | Dónde se inserta | Camino que intercepta |
|---|---|---|---|
| **I-1** | `SensitiveDataFilter` | `logging_config.py` | **Todo** `logger.*` del sistema (≈40 módulos hacen `from logging_config import logger`) |
| **I-2** | Safety gates de entorno | `utils/environment.py` → `twilio_client.py:644` | **Todo** saliente de WhatsApp: texto y multimedia |
| **I-3** | Clasificador de identidad | `whatsapp_identity.py` → `webhook_handler.py:1899` | **Todo** entrante de WhatsApp |

Cada uno resuelve un problema real y legítimo: fuga de PII en logs, envíos accidentales desde staging, y la fuga BSUID documentada en `memory/fuga_bsuid_webhook_ago2026.md`. **La calidad interna de los tres es buena** — el clasificador BSUID en particular corrigió en un día casi todos los defectos de la primera auditoría.

El problema no está dentro de ninguno de los tres. Está en **lo que ocurre cuando uno de ellos dice que no**.

### El defecto es el mismo en los tres

Un interceptor introduce un modo de fallo que antes no existía en ese camino. Los tres lo hicieron **sin extender el contrato de los consumidores aguas abajo**:

| Interceptor | Cómo señala el fallo | Qué pasa con esa señal |
|---|---|---|
| **I-1** Filtro de logs | **Lanza `TypeError` al llamador** | Nadie la espera. No hay contrato: un log mal formado tumba código que nadie tocó |
| **I-2** Safety gate | Devuelve `{"status": "blocked"}` | **3 consumidores de 25 no la conocen** → la interpretan como éxito |
| **I-3** Clasificador | `Response(content="")` sin persistir | **Invisible**: ni Mongo, ni panel, ni descarga del media. El mensaje deja de existir |

Tres instancias del mismo error arquitectónico: **se añadió un estado de fallo sin actualizar a quien lo consume**.

---

## 🔴 Causa raíz del fallo de multimedia — defecto **A4**, medido en producción

**El multimedia no se rompe por el fix de BSUID.** Se rompe por una línea de log del commit
`4572128` («sanitize operational logs») — el **mismo interceptor I-1** del bloque A.

### El defecto, en una línea

`middleware/outbound_panel.py` **usa** `safe_url()` en 3 sitios y **no la importa**:

```python
# linea 65 — el import que dejó fuera safe_url
from utils.safe_logging import safe_error, safe_id, safe_phone, safe_text
#                                                                        ^ falta safe_url

# linea 1980, dentro de send_message(), en el bloque de subida de multimedia
logger.info(f"[Panel] Bunny.net URL obtenida: {safe_url(permanent_media_url)}")
```

Es un **f-string**: se evalúa antes de llamar a `logger`. Lanza `NameError`, lo captura el
`except Exception` del bloque de media, y sale por la respuesta HTTP:

```python
raise HTTPException(status_code=500,
                    detail=f"Error procesando archivo multimedia: {str(e)}")
```

> **Lo que ve la asesora:**
> `HTTP 500: Error procesando archivo multimedia: name 'safe_url' is not defined`

Eso explica la contradicción reportada —*«el panel da error pero los logs dicen que son
compatibles»*—: **Bunny sube el archivo y el CDN responde 200 ANTES de que reviente la línea de
log.** El fallo ocurre después del éxito, en el registro del éxito.

### Traza de producción — las dos ramas, lado a lado

Deploy `88df277c` · rama **`dev_juanrodriguez`** (= `dev_salomon`, `e6f473b`):

```
14:46:19.242  [BunnyStorage] Subiendo con Content-Type: audio/mpeg
14:46:19.588  [BunnyStorage] Archivo subido exitosamente: url:https://inmobiliaria-media…#ac3708d2
14:46:19.627  [BunnyStorage] ✅ CDN verificado (intento 1): … status=200
14:46:19.627  [Panel] ERROR - Error procesando multimedia: name 'safe_url' is not defined
                     ↑ 5 microsegundos después del 200. Sin envío. Sin SID.
```

Deploy `1f3ef614` · rama **`main`** (producción actual):

```
15:09:05  [BunnyStorage] ✅ CDN verificado (intento 1): … status=200
15:09:05  [Panel] 📤 Bunny.net URL obtenida: https://inmobiliaria-media…mp3
15:09:05  [Panel] ✅ Multimedia subido a Bunny.net: audio -> https://…mp3
15:09:05  [TwilioClient] Mensaje enviado exitosamente. SID: MMe56a0d…, status: accepted
```

En `main` el envío llega a Twilio con SID **`MM…`** (mensaje con media) — verificado 5 veces
entre las 15:09 y las 15:24. En `dev` no hay ni una sola línea de `TwilioClient` tras el fallo.
**Bunny.net y Twilio están sanos en ambas ramas; el corte está en el panel.**

### Por qué solo afecta al multimedia

Las líneas 1980 y 1994 viven **dentro** de `if media_file and media_file.filename:`. Un mensaje
de texto no entra en ese bloque y nunca toca `safe_url`. De ahí el síntoma exacto: **el texto
funciona, el multimedia no.**

### Alcance completo — barrido AST sobre todo el repositorio

| Archivo | `safe_*` usados | Sin importar |
|---|---|---|
| `middleware/outbound_panel.py` | `safe_error`, `safe_id`, `safe_phone`, `safe_text`, **`safe_url`** | 🔴 **`safe_url`** |
| *(los demás 200+ archivos)* | — | ✅ ninguno |

**Un solo archivo, un solo nombre, tres usos** — y `main` tiene **cero** usos de `safe_url` en
ese archivo, por lo que la regresión es exclusiva de las ramas `dev`:

| Uso | Función | Efecto |
|---|---|---|
| 1980, 1994 | `send_message()` | 🔴 **Rompe todo envío de multimedia del panel** |
| 3766 | `update_contact_name()` | 🔴 Está **fuera** del `try` → 500 sin capturar al renombrar un contacto |

> **El segundo uso todavía no se ha reportado porque nadie renombró un contacto en esa ventana
> de deploy.** Está igual de roto.

### El arreglo — **APLICADO** en `dev_salomon` (5-sep-2026)

Una línea. `middleware/outbound_panel.py:65`:

```python
from utils.safe_logging import safe_error, safe_id, safe_phone, safe_text, safe_url
```

Acompañado de `tests/test_multimedia_regresion.py` (16 tests) y de una línea en
`.gitignore` — `!/tests/test_multimedia_regresion.py` — **sin la cual el test no se habría
commiteado jamás**: el repo ignora `tests/*` con una lista blanca de sólo 4 archivos.

**Verificación por mutación (dos veces, la segunda tras endurecer el guardia):** al retirar
`safe_url` del import fallan exactamente los 4 tests que deben, y el fix se restaura solo.
Un guardia que no falla cuando el defecto vuelve no prueba nada.

> **Lo que costó afinar el guardia — cinco hallazgos, ninguno un defecto del código.**
> La primera versión sólo inspeccionaba llamadas `objeto.metodo()`, así que **no miraba el
> patrón A2 real** (`mark_visit_completed(safe_id(x))`, una llamada simple). Al endurecerla
> salieron `sofia_brain.py:395` —un fragmento de log construido en una variable— y luego cuatro
> de `app.py`, cuyo logger se llama `_diag_logger` y no `logger`. Los cinco eran límites del
> guardia, no del código. La versión final exige **dos** condiciones para absolver —nombre de
> logger **y** método de logging—, de modo que un `mi_logger.guardar(safe_id(x))` seguiría
> disparando. El resultado es que la limpieza del repo frente al patrón A2 ahora está
> **demostrada**, no supuesta.

### Qué dice esto del ADR

**A4 no es ninguno de los cuatro puntos abiertos.** Es un quinto defecto, del bloque A, y es
**la confirmación empírica de R1**: *un interceptor de logging nunca puede tumbar al llamador*.
A1 predijo esta clase de fallo por `TypeError`; A4 la produjo por `NameError`. Mismo principio
violado, mismo bloque, distinta mecánica — y A4 **sí está activo**, mientras A1 sigue dormido.

Refuerza también la decisión de fondo: el problema de esta rama nunca fue el trabajo de BSUID,
que es bueno y funciona (`FEATURE_WHATSAPP_BSUID_SUPPORT=TRUE` en producción). **Fue la capa de
sanitización de logs, aterrizada sin red.**

---

## ⚠️ Correcciones a la Revisión 1

Dos afirmaciones de la Rev. 1 —heredadas de la auditoría del 5-sep— apuntaban al sitio
equivocado y hay que retirarlas:

| Rev. 1 decía | Realidad medida |
|---|---|
| *«`outbound_panel.py`: 1 línea de lógica, sólo el `import`. **Idéntico a `main`**»* | ❌ **Esa única línea ERA el defecto.** El conteo de «líneas de lógica» por AST trató el import como inocuo; era un import **incompleto** que rompe el multimedia |
| *«El código de media es byte-idéntico en lógica a `main`; solo M2 o A1 pueden romperlo»* | ❌ **Falso dilema.** No era ni M2 ni A1, sino A4 — invisible para ese método de conteo |

La lección de método: **un barrido que cuenta líneas de lógica no ve un import incompleto**, y
el barrido de guardia de la Rev. 1 buscaba `safe_*` *fuera* de un `logger` (el patrón de A2) —
no comprobaba que cada `safe_*` usado estuviera **ligado**. Son dos comprobaciones distintas y
hacen falta las dos (ver Paso 6, test de guardia).

---

## Verificación — qué se comprobó y cómo

Todo lo siguiente está **medido sobre el código de la rama**, no inferido.

### ✅ I-2 · El estado `blocked` es huérfano — confirmado, con alcance acotado

`send_whatsapp_message` devuelve tres estados (`success`, `error`, `blocked`); `blocked` es nuevo. De los **25 sitios** que consultan `status`:

- **22 usan `== "success"` o `!= "success"`** → un `blocked` falla correctamente. Sin riesgo.
- **3 usan `== "error"`**, es decir *"todo lo que no sea error es éxito"*:

| Sitio | Consecuencia de un `blocked` |
|---|---|
| `webhook_handler.py:411` (`_persistir_saliente`) | **Guarda en Mongo un mensaje que nunca salió** → el panel se lo muestra a la asesora como entregado |
| `webhook_handler.py:1115` | Loguea `✅ Respuesta enviada` |
| `app.py:1824` | Dispara el handoff pendiente |

El riesgo no es que falle: es que **miente**. La asesora ve en pantalla algo que el cliente no recibió.

### 🔄 I-2 · El gate NO afecta a producción — corrige la auditoría

La auditoría del 5-sep lo señaló como *"causa nº 1 candidata del fallo de multimedia"*. **Medido, no lo es en producción.** Matriz ejecutada sobre `utils/environment.py`:

| `APP_ENV` | `env_name()` | `is_staging()` | `require_twilio_outbound_allowed()` |
|---|---|---|---|
| *(sin definir)* | `local` | `False` | ✅ `(True, 'allowed')` |
| `production` | `production` | `False` | ✅ `(True, 'allowed')` |
| `local` | `local` | `False` | ✅ `(True, 'allowed')` |
| `staging` | `staging` | `True` | 🔴 `(False, 'TWILIO_OUTBOUND_ENABLED=false')` |

`twilio_outbound_enabled()` tiene default `not is_staging()`, y `is_twilio_recipient_allowed()` retorna `True` de entrada si no es staging. **En producción el gate es un no-op.** Solo muerde si el entorno se llama literalmente `staging`/`stage`/`preprod`/`preproduction`/`testing`.

> **Consecuencia diagnóstica:** si el multimedia falló en un entorno llamado `staging`, M2 lo explica por completo y **el arreglo es de configuración, no de código**. Si falló en producción, M2 queda descartado.

**Rev. 2 — M2 queda DESCARTADO con datos de producción.** Variables leídas en Railway
(proyecto `caring-balance`, entorno `production`):

| Variable | Valor real | Efecto |
|---|---|---|
| `RAILWAY_ENVIRONMENT_NAME` | `production` | `is_staging()` = **False** |
| `RAILWAY_ENVIRONMENT` | `production` | idem |
| `TWILIO_OUTBOUND_ENABLED` | `TRUE` | gate abierto explícitamente |
| `FEATURE_WHATSAPP_BSUID_SUPPORT` | `TRUE` | la rama BSUID **está activa** |
| `TWILIO_SIGNATURE_MODE` | `log_only` | sin cambios respecto de `main` |

`APP_ENV` y `ENVIRONMENT` **no están definidas**, así que `env_name()` cae en
`RAILWAY_ENVIRONMENT_NAME` = `production`. El gate es un no-op y **no aparece ni una línea
«Outbound bloqueado por safety gate»** en los logs. Confirmado: M2 no tuvo nada que ver.

Como `FEATURE_WHATSAPP_BSUID_SUPPORT=TRUE`, **B-b tampoco está activo en producción** — la rama
BSUID corre entera. B-a (`USERNAME`/`UNKNOWN`) sigue vigente.

### 🔴 I-1 · El filtro propaga excepciones — confirmado por las dos rutas

```
filter() directo          → TypeError: %d format: a real number is required, not str
logger.info('%d', 'x')    → TypeError propagado AL LLAMADOR
```

`Handler.handle()` envuelve `emit()` en `try/except`, pero **no `self.filter()`**. `SensitiveDataFilter` no existe en `main`: es nuevo de la rama. Es el único mecanismo capaz de romper código que nadie tocó, y su alcance es **todo el sistema**.

### 🔴🔑 Hallazgo nuevo · **A3 enmascara a A1**, y eso invierte el orden de los arreglos

Esto no aparecía en ninguna de las dos auditorías y **es el punto que reordena el plan**.

El filtro se instala vía `logging.basicConfig(handlers=[...])`, que es **no-op si el root logger ya tiene handlers** — exactamente lo que ocurre bajo Gunicorn/uvicorn. Simulado:

```
root con handlers previos (Gunicorn)
  → SensitiveDataFilter instalado: False
  → log mal formado: NO propaga (Python lo traga en handleError)
```

De donde se sigue, en cadena:

1. **En Railway, el filtro probablemente no está instalado.** Luego **A1 está dormido en producción** — no es la causa del fallo de multimedia.
2. **Pero entonces la protección anti-PII tampoco existe en producción.** El objetivo entero del commit `4572128` («sanitize operational logs») está anulado justo donde importa. La regla nº 2 del `CLAUDE.md` — *nunca loggear teléfonos completos en producción* — **no se está cumpliendo**, y el equipo cree que sí.
3. **🔴 Arreglar A3 primero convierte un bug dormido en una caída activa.** Instalar el filtro correctamente sobre los handlers de Gunicorn **activa A1 en producción**, en todos los módulos a la vez.

> **Restricción de secuencia, no negociable: A1 se arregla ANTES que A3. Nunca al revés, y nunca A3 solo.**

Como el enmascaramiento depende del orden de import, **no debe asumirse**: hay que medirlo en Railway exponiendo `configured_safety_summary()` y el estado real del filtro.

### 🟠 I-3 · Pérdida silenciosa de entrantes — confirmado

`USERNAME` y `UNKNOWN` hacen `return Response(content="")` sin `save_message`, sin panel y sin descargar el media. Y el gate **ya no depende del feature flag**: con el flag apagado, un BSUID que en `main` al menos intentaba el camino telefónico, hoy se descarta. Es un cambio de comportamiento **activo, no protegido por flag**.

`_PHONE_RE` (`^(?:whatsapp:)?\+?\d[\d\s().-]{6,}$`) no valida E.164: acepta `1......` y rechazará cualquier formato nuevo de Twilio. Todo lo que no case cae en `UNKNOWN` → desaparece.

*Matiz:* el safe-stop **en sí es correcto y hay que conservarlo** — cierra la rama del «10% inventa un teléfono» de `memory/bsuid_dos_ramas_rechazo_invento.md`. El defecto no es parar; es **parar sin dejar rastro**. Persistir el entrante y no procesarlo no son excluyentes.

### 🔄 M4 · La auditoría fue injusta con la documentación

La auditoría afirmó que `docs/staging/` *"promete protecciones que el código no aplica"*. **Falso.** `03_MATRIZ_VARIABLES_STAGING.md` las lista bajo el encabezado literal **«Variables nuevas recomendadas, pendientes de implementar»**. El documento es honesto.

El hecho real es distinto y menor: `a3ec03c` aterrizó **las definiciones** de 7 gates y cableó **una sola** (`require_twilio_outbound_allowed`). `require_bunny_upload_allowed`, `apply_bunny_prefix`, `require_hubspot_write_allowed`, `scheduler_outbound_enabled`, `rag_index_on_startup_enabled`, `staging_sentinel_enabled` y `configured_safety_summary` tienen **cero llamadas fuera de su propio archivo** (verificado por grep). Es **una capa a medio aterrizar**, no una promesa incumplida. Sigue siendo cierto que hoy staging escribe en Bunny y HubSpot reales.

---

### Las tres ramas, comparadas

| | `main` | `dev_juanrodriguez` | `dev_salomon` |
|---|---|---|---|
| Commit | `776cbb8` | `e6f473b` | **`e6f473b` — el mismo** |
| Publicada en `origin` | ✅ | ✅ | ❌ **solo local** |
| Commit `4572128` (sanitize logs) | ❌ no | ✅ sí | ✅ sí |
| `safe_url` usado en `outbound_panel` | **0 usos** | 3 usos | 3 usos |
| `safe_url` importado allí | n/a | 🔴 **no** | 🔴 **no** |
| Firma de Twilio (`TWILIO_SIGNATURE_MODE`) | ✅ presente | ✅ **idéntica a `main`** | ✅ **idéntica a `main`** |
| **Multimedia del panel** | ✅ funciona (SIDs `MM…`) | 🔴 **HTTP 500** | 🔴 **HTTP 500** |

**Hallazgos sobre `dev_salomon` que conviene fijar:**

1. **`dev_salomon` y `dev_juanrodriguez` son el mismo commit** (`e6f473b`). En código
   commiteado son indistinguibles: el fallo de multimedia es idéntico en ambas, por la misma
   línea.
2. **La «nueva firma de Twilio» no es una modificación de `dev_salomon`.** La validación de
   firma ya está en `main` (`TWILIO_SIGNATURE_MODE=log_only`) y las tres ramas la tienen igual;
   la rama que la introdujo (`claude/amazing-maxwell-kzd558`) ya es ancestro de `main`.
   **No hay tal «juntar dos cosas»: el multimedia se rompe con el commit de logs, solo.**
3. Los cambios **sin commitear** en el worktree de `dev_salomon` son documentación
   (`docs/rediseno/`, ~1.400 líneas) y **borrado de comentarios** en `outbound_panel.py`,
   `query_profiler.py` y otros — **cero cambios de lógica** (verificado por diff filtrado).
   No influyen en el fallo, pero borran comentarios que documentaban decisiones medidas en
   producción (el ZSCAN de 39,5 s, el `nx` del cache de etapas). Conviene no perderlos.

---

## Decisión

**Se adopta un contrato obligatorio para todo interceptor transversal**, y se aplica retroactivamente a los tres de esta rama.

> Un interceptor transversal es cualquier código que se inserta en un camino que antes corría sin condiciones, y que puede impedir que ese camino se complete: filtros de logging, safety gates, clasificadores de identidad, middlewares, decoradores de reintento.

### Las tres reglas

| # | Regla | Por qué |
|---|---|---|
| **R1 · No lanzar** | Un interceptor **nunca** propaga una excepción al llamador. Si su propia lógica falla, deja pasar y registra. | El llamador no pidió el interceptor y no sabe que existe. Un log mal formado no puede tumbar un pago, un envío ni un webhook. |
| **R2 · Un estado nuevo obliga a revisar a sus consumidores en el mismo commit** | Añadir un valor de retorno es un **cambio de contrato**. No se mergea sin recorrer los consumidores. | `blocked` nació huérfano y 3 sitios lo leen como éxito. |
| **R3 · Descartar ≠ no dejar rastro** | Un interceptor que corta un flujo de datos **persiste primero y corta después**. | Un entrante perdido es indistinguible de un cliente que nunca escribió. |

### Regla de secuencia asociada

**R4 · Cuando un defecto está enmascarado por otro, el arreglo va del enmascarado hacia la máscara.** Aquí: **A1 antes que A3**, siempre.

### Corolario de diseño

**Un gate que bloquea en silencio es indistinguible de una caída.** Todo gate debe ser observable en runtime (`/health`) y ruidoso al arrancar si va a bloquear.

---

## Opciones evaluadas

### Opción A · Mergear tras cuatro arreglos acotados ✅ **Recomendada**

| Dimensión | Valoración |
|---|---|
| Complejidad | **Baja** — A1 son 3 líneas; M1 son 3 condiciones; B-a/B-b es mover `save_message` antes del `return` |
| Coste | **≈1 día** de implementación + tests |
| Riesgo | **Bajo y acotado** — cada arreglo es local y testeable en aislamiento |
| Familiaridad | Alta — todo el código es de esta semana y está fresco |

**A favor:** conserva íntegro el trabajo BSUID, que es bueno y cierra una fuga real y documentada. La paridad con `main` ya está limpia (`git log dev..main` vacío, sin marcadores de conflicto).
**En contra:** exige disciplina de secuencia (R4). Un merge apresurado que toque A3 sin A1 empeora la situación.

### Opción B · Revertir la capa de interceptores y re-aterrizarla por partes

| Dimensión | Valoración |
|---|---|
| Complejidad | **Alta** — los tres interceptores están entretejidos con el merge `e6f473b` |
| Coste | 3-4 días |
| Riesgo | **Alto** — revertir arriesga perder el trabajo BSUID y la paridad ya lograda |

**A favor:** deja `main` incuestionablemente limpio.
**En contra:** desproporcionado. Los defectos son de contrato, no de diseño; se arreglan donde están. Y reintroduciría la fuga BSUID.

### Opción C · Mergear tal cual y mitigar por configuración

| Dimensión | Valoración |
|---|---|
| Complejidad | Nula |
| Coste | Minutos |
| Riesgo | 🔴 **Inaceptable** |

**En contra, decisivo:** M1 no se puede mitigar por configuración — el `blocked` huérfano miente a la asesora en cuanto el gate se active alguna vez. Y deja la protección anti-PII creída-pero-inexistente. **Descartada.**

---

## Análisis de trade-offs

El eje real no es *«mergear o no»*, sino **quién absorbe el coste de un contrato incompleto**.

- Hoy lo absorben **la asesora** (ve entregado lo que no salió) y **el cliente perdido** (escribe y nadie lo ve). Ambos costes son invisibles para quien despliega: no hay excepción, no hay alerta, no hay línea roja en los logs. Es el mismo patrón que `memory/bulk_deadlock_reclamacion_huerfana.md` — 3 campañas congeladas 33 días en silencio — y que `memory/envios_sin_persistir_webhook_sep2026.md`.
- Con la Opción A, el coste se paga **una vez, en el merge**, por quien tiene el contexto fresco.

La tensión secundaria es **seguridad vs. continuidad** en I-3. El safe-stop es correcto: es preferible perder un mensaje a envenenar HubSpot con un teléfono inventado. Pero es un falso dilema — **R3 disuelve la tensión**: persistir el entrante no reactiva la normalización errónea.

---

## Consecuencias

**Se vuelve más fácil**
- Añadir gates nuevos: R1-R3 dan la lista de comprobación.
- Diagnosticar staging: `configured_safety_summary()` en `/health` responde *«¿está bloqueado?»* sin leer logs.
- Confiar en el panel: lo que la asesora ve entregado, salió.

**Se vuelve más difícil**
- Añadir un estado de retorno: R2 obliga a recorrer los consumidores. Es intencional.
- Aterrizar gates a medias: o se cablean o se borran.

**Hallazgo operativo colateral — `tests/` está en `.gitignore`**

Descubierto al intentar commitear la regresión de multimedia. `.gitignore:42` dice `/tests/*`
con una lista blanca de **4 archivos**. Consecuencias medidas en el checkout de `dev_salomon`:

| | |
|---|---|
| Archivos de test en disco | **68** |
| Versionados por git | **32** |
| Locales, sólo en ese checkout | **36** |
| De ésos, que **ni siquiera importan** | **7** |

Los 7 rotos **abortan la colección**, así que `pytest tests/` no ejecuta ni un test en ese
repo; hay que pasarle la lista explícita de archivos. Y como cada worktree acumula los suyos,
«la suite» significa algo distinto en cada carpeta — el worktree de auditoría tenía 19 archivos
y 718 tests; el principal, 68 y 1.134.

Esto explica por qué **la regresión de multimedia «seguía sin existir»** en las dos auditorías:
es perfectamente posible que alguien la escribiera y nunca llegara al repositorio. Merece
decisión propia: o se invierte la regla (versionar `tests/` e ignorar lo que sobra), o se
limpian los 36 locales. Hoy la suite no es reproducible entre checkouts.

**Habrá que revisar**
- **La correlación en logs.** Con `phone`, `contact_id` y `message_sid` enmascarados, el método de 4 pasos de `memory/diagnostico_logs_railway.md` **ya no permite rastrear un contacto** sin recalcular el hash. Merece una utilidad de correlación — o el enmascaramiento habrá cambiado una fuga de PII por una ceguera operativa.
- **`_PHONE_RE` vs. E.164.** Clasificar por regex tolerante es deuda: acepta basura y rechazará formatos futuros de Twilio.
- **M4 — decidir explícitamente:** cablear los 6 gates restantes o borrarlos. No dejarlos definidos y muertos.

---

## Action items

Ordenados por **dependencia**, no por severidad. Los pasos 2-4 son la Opción A.

**Paso 0 · Diagnóstico — ✅ COMPLETADO (Rev. 2)**

- [x] Variables de Railway leídas → `RAILWAY_ENVIRONMENT_NAME=production`, `TWILIO_OUTBOUND_ENABLED=TRUE` → **M2 descartado**
- [x] Sin líneas «Outbound bloqueado por safety gate» en los logs → **M2 descartado**
- [x] Traceback real capturado en producción → **`name 'safe_url' is not defined`** → **es A4**

**Paso 1 · A4 — desbloquear el multimedia. Una línea, y va primero**

- [ ] `middleware/outbound_panel.py:65` → añadir `safe_url` al import de `utils.safe_logging`
- [ ] Verificar los 3 usos (1980, 1994, 3766) y que `update_contact_name()` deja de romper
- [ ] Test: enviar audio e imagen desde el panel y confirmar SID `MM…` en los logs

**Paso 2 · R1 — blindar el filtro (A1). Antes que A3, por R4**

- [ ] `try/except` alrededor de `record.getMessage()` en `SensitiveDataFilter.filter()`; ante fallo, `return True`
- [ ] Test: un `LogRecord` mal formado atraviesa el filtro y **no** lanza

**Paso 3 · R2 — cerrar el estado huérfano (M1)**

- [ ] `_persistir_saliente` (`webhook_handler.py:411`) → persistir solo si `status == "success"`
- [ ] `webhook_handler.py:1115` y `app.py:1824` → tratar `blocked` como no-éxito
- [ ] Documentar el contrato de 3 estados en el docstring de `send_whatsapp_message`
- [ ] Test: `blocked` → no persiste y no reporta éxito

**Paso 4 · R3 — no perder nunca el entrante (B-a, B-b)**

- [ ] `save_message(metadata=identity_metadata)` **antes** del `return` en `USERNAME`, `UNKNOWN` y en el safe-stop BSUID con flag apagado
- [ ] Test: los tres casos persisten el entrante

**Paso 5 · Hacer el gate observable (M2)**

- [ ] `CRITICAL` al arrancar si el gate va a bloquear
- [ ] `configured_safety_summary()` expuesto en el health check
- [ ] Test: matriz `env_name` × `TWILIO_OUTBOUND_ENABLED` × allowlist, **incluida la trampa «allowlist vacía bloquea todo»**

**Paso 6 · A3 — instalar el filtro de verdad. SOLO después del paso 2**

- [ ] Instalar sobre los handlers existentes del root logger, sin depender de `basicConfig`
- [ ] Test que reproduzca Gunicorn (root ya configurado)
- [ ] **Verificar en Railway que el filtro quedó activo** — hoy se cree activo y probablemente no lo está

**Paso 7 · Higiene**

- [ ] M3 — quitar el `lru_cache(maxsize=1)` de `_twilio_allowlist()` o darle TTL
- [ ] M4 — decidir: cablear los 6 gates o borrarlos
- [ ] B-c — auditar el pipeline BSUID con `contact_id = None` aguas abajo
- [ ] B-d — sustituir los 3 `locals()` por inicialización antes del `try`
- [ ] B-e — no aplicar `safe_phone()` sobre un `bsuid_<hex>`
- [ ] **Test de guardia · DOS comprobaciones distintas, hacen falta las dos:**
  - [ ] **(a)** ningún `safe_*` fuera de una llamada a `logger` — el patrón de A2 (corrupción de datos)
  - [ ] **(b)** 🔴 **todo `safe_*` usado está ligado en su módulo** — el patrón de A4 (`NameError`).
        Barrido AST sobre el repo; hoy pasa con el arreglo del Paso 1 y falla sin él

**Paso 8 · Puerta de merge**

- [ ] **Regresión de multimedia — sigue sin existir.** Entrante con `NumMedia > 0` en las 3 ramas del parser (JSON Conversations, form Conversations, legacy) + saliente con `media_url`
- [ ] `pytest tests/ -q` completo; suite del panel 51/51 en verde
- [ ] Foto y audio end-to-end, confirmados en panel y en Mongo
- [ ] Verificar que ningún mensaje bloqueado quedó guardado como entregado

---

## Veredicto (Rev. 2)

**No mergear todavía** — pero el bloqueo ya no es un misterio, y el defecto que más duele
**cuesta una línea**.

| # | Estado en producción | Acción |
|---|---|---|
| **A4** 🔴 | **ACTIVO y reproducido** — rompe todo el multimedia del panel y `update_contact_name()` | **Paso 1 · una línea** |
| **B-a** 🔴 | **ACTIVO** — `USERNAME`/`UNKNOWN` se descartan sin rastro | Paso 4 |
| **M1** 🔴 | Latente — miente en cuanto el gate se active alguna vez | Paso 3 |
| **A1** 🟠 | **Dormido**, porque A3 lo enmascara — se despierta si se arregla A3 primero | Paso 2, **antes que A3** |
| **M2** ✅ | **Descartado con datos**: `RAILWAY_ENVIRONMENT_NAME=production`, `TWILIO_OUTBOUND_ENABLED=TRUE` | Paso 5, solo observabilidad |
| **B-b** ✅ | **No activo**: `FEATURE_WHATSAPP_BSUID_SUPPORT=TRUE` | Paso 4, junto con B-a |

**Sobre las tres ramas:** la paridad con `main` está limpia, la firma de Twilio es idéntica en
las tres, y el trabajo de BSUID es bueno y **está funcionando en producción**. `dev_salomon` es
el mismo commit que `dev_juanrodriguez`, así que **no hay dos cosas que juntar**: el multimedia
lo rompe el commit de sanitización de logs, por sí solo.

Lo que falta son **cuatro arreglos acotados y una secuencia que respetar** — y el primero
desbloquea el multimedia hoy.

---

> **Estado de la verificación (Rev. 2).**
> **Verificado en producción:** el fallo de multimedia (traza de Railway en las dos ramas),
> las variables del gate, el barrido AST de todo el repositorio, y el `NameError` reproducido
> localmente con el mensaje exacto que ve el panel.
> **Sigue pendiente:** **no se ha ejecutado la suite de tests** (`pytest tests/ -q`, panel
> 51/51), y **no se ha aplicado ningún cambio de código** — Fase 4 del proceso de 6 fases
> requiere aprobación explícita antes de tocar la rama.
> **No verificable desde los logs:** el objeto en Bunny del deploy roto, porque `safe_url`
> enmascara la ruta (`…#ac3708d2`) y no permite reconstruirla. Es exactamente la ceguera
> operativa que advierte la sección «Consecuencias»; en este caso no hizo falta, porque el
> propio servidor hizo el `HEAD` al CDN y obtuvo `200`.
