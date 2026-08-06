# Assets del panel — cómo regenerarlos

El panel ya no depende de ningún CDN externo. `tailwind.css` y `chart.umd.js` se
sirven desde `/whatsapp/panel/static/`, igual que `style.css` e `index.js`.

## Por qué

El panel cargaba Tailwind desde `https://cdn.tailwindcss.com`. Eso **no es una hoja
de estilos**: es un compilador JIT de ~400 KB que corre en el navegador y genera el
CSS en tiempo real. La documentación de Tailwind dice explícitamente que no es para
producción.

Cuando ese script no bajaba, el panel se renderizaba **sin una sola regla de layout**
— todo apilado en vertical— aunque el backend hubiera respondido perfectamente. Los
datos se veían; la maquetación no. Ocurría de forma intermitente y "se arreglaba
sola" al recargar, porque dependía del navegador, la red y la caché de cada asesora,
no del servidor.

Causas posibles de que fallara la descarga: caída o rate limit del CDN, bloqueadores
de anuncios, proxy corporativo, DNS, caché expirada, o que el script bajara pero
lanzara excepción al ejecutarse.

Sirviéndolo desde nuestro propio servidor: si Railway responde, el panel se ve bien.

## Regenerar `tailwind.css`

**Obligatorio cada vez que se añadan clases de Tailwind nuevas** al HTML o al JS. Si
no se regenera, esas clases no existirán en el CSS y esos elementos saldrán sin
estilo.

```bash
cd middleware/PanelAsesores
npx --yes tailwindcss@3 -c tailwind.config.js -i tailwind.input.css -o tailwind.css --minify
```

Se usa la **v3** porque es la versión que servía `cdn.tailwindcss.com`; la v4 cambia
la sintaxis de configuración y no es compatible con este `tailwind.config.js`.

### Qué ficheros escanea

`tailwind.config.js` lista `index.html`, `metrics.html`, `index.js` y `metrics.js`.

El JS **cuenta y no es opcional**: `index.js` genera la mayor parte del panel en
runtime y contiene clases que no aparecen en el HTML (`bg-pink-100`, `text-amber-600`,
`w-32`...). Si un fichero falta en `content`, sus clases desaparecen del build.

### ⚠️ Regla al escribir clases en JS

El escáner de Tailwind busca **cadenas literales**. Estos patrones funcionan:

```js
`class="${cond ? 'bg-blue-50' : 'bg-white'}"`        // ambas literales
const widths = ['w-32', 'w-48', 'w-40'];              // literales en un array
const canalColors = { instagram: 'bg-pink-100' };     // literales en un mapa
```

Este **no**, y la clase desaparecería del build sin ningún aviso:

```js
`bg-${color}-500`    // construida por fragmentos: indetectable
```

## Regenerar `chart.umd.js`

Solo lo usa `metrics.html`. Actualizar únicamente si hace falta una versión nueva:

```bash
cd middleware/PanelAsesores
curl -sL "https://cdn.jsdelivr.net/npm/chart.js" -o chart.umd.js
```

## Verificar antes de desplegar

Que las clases usadas existan realmente en el CSS compilado:

```bash
grep -c "\.flex{" tailwind.css        # utilidades base
grep -c "bg-\[#F8F7F4\]" tailwind.css # valores arbitrarios (JIT)
grep -c "bg-pink-100" tailwind.css    # clases que solo viven en index.js
```

Las tres deben devolver al menos 1. La segunda es la que confirma que los valores
arbitrarios —que antes solo existían gracias al JIT— quedaron compilados.
