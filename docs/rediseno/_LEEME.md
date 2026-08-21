Documentación de planeación — Rediseño SofIA, Inmobiliaria Proteger

---


## Empieza por aquí

1. `00 - Indice Maestro` — el mapa de todo: los 21 documentos, cuáles existen, cuáles faltan y qué está bloqueando qué. Si solo vas a abrir un archivo, abre ese.
2. `Wireframes / Catálogo de Wireframes` — las pantallas dibujadas. Es la fuente primaria del diseño.
3. Después, en orden: `01 Glosario` → `02 Actores y Roles` → `03 Visión`.

---


## La estructura


| Carpeta | Qué contiene |
|---|---|
| *(raíz)* | El índice maestro y este documento |
| Fase 0 - Fundamentos | Glosario, actores y roles, visión. Lo que hay que cerrar antes que nada |
| Fase 1 - Comportamiento | Casos de uso, permisos, journeys, máquinas de estado *(por crear)* |
| Fase 2 - Estructura | Modelo de datos, arquitectura, contratos, decisiones técnicas *(por crear)* |
| Fase 3 - Transición | Antes/después, benchmark, migración, riesgos, roadmap |
| Wireframes | Catálogo comentado + las 9 imágenes originales en `Imagenes/` |


El número del archivo es único y no cambia nunca. `03` siempre es la Visión, esté en la carpeta que esté.

---


## Cómo leer las marcas


Los documentos están escritos para poder distinguir lo que es un hecho de lo que es una propuesta mía. Esto importa: buena parte se reconstruyó después de perder `Planos_Rediseno_SofIA_v1.docx`.

| Marca | Significa |
|---|---|
| ✅ | Hecho verificado. Está en el código, en un wireframe o en una decisión ya tomada |
| 🔵 | Hipótesis derivada de evidencia. Es mi lectura, no tu decisión. Confírmala o corrígela |
| ⚠️ | Borrador. El documento aún no está cerrado |
| ❓ | Pregunta para ti. Requiere tu respuesta para poder avanzar |
| 🔴 | Bloqueo o riesgo alto. Hay trabajo detenido esperando esto |
| 🟠 🟡 | Prioridad alta / media |
| ⛔ | Bloqueado por falta de insumo |

> La forma más rápida de avanzar es buscar los ❓ y responderlos. Están redactados en lenguaje de negocio a propósito: no hace falta saber programar para contestarlos.

---


## De dónde sale cada cosa


Estos Word son una copia legible. El original vive en el repositorio, en `docs/rediseno/`, en formato Markdown y versionado con git — así desarrollo lo lee junto al código y queda historial de cambios.
- Para leer, revisar o presentar → esta carpeta
- Para desarrollar → el repositorio

Cada documento indica su archivo de origen debajo del título.
> ⚠️ No edites los Word. Se regeneran completos en cada actualización y tus cambios se perderían. Si algo hay que cambiar, dilo y se corrige en el original.

---


## El bloqueo activo


El código organiza el trabajo por CANAL. Los wireframes lo organizan por FUNCIÓN.
- Código: Jubeny atiende 14 canales, Luisa atiende Finca Raíz y MetroCuadrado.
- Wireframes: *A. Interna* atiende lo nuevo, *A. Seguimiento* hace seguimiento.

Un lead de Finca Raíz que ya fue contactado, ¿es de Luisa o de Seguimiento? Hasta resolverlo no se puede escribir el modelo de datos ni la matriz de permisos.

Está desarrollado en `03 - Visión` (§8) y `16 - Antes y Después` (§2).
