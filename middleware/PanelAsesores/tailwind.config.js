/** Configuración de Tailwind para el panel de asesoras.
 *
 * `content` debe listar TODOS los ficheros donde aparecen clases de Tailwind. El
 * escáner busca cadenas literales, así que el JS cuenta: index.js genera la mayor
 * parte del panel en runtime y ahí viven clases que no están en el HTML
 * (`bg-pink-100`, `text-amber-600`, `w-32`...). Si un fichero falta aquí, sus
 * clases no se compilan y esos elementos quedan sin estilo.
 *
 * Nota sobre clases dinámicas: el código usa `${cond ? 'bg-blue-50' : 'bg-white'}`
 * y variables que contienen nombres completos, que el escáner sí detecta. NO usar
 * nunca construcción por fragmentos tipo `bg-${color}-500`: sería indetectable y
 * la clase desaparecería del build sin aviso.
 */
module.exports = {
  content: [
    "./index.html",
    "./metrics.html",
    "./index.js",
    "./metrics.js",
  ],
  theme: {
    extend: {},
  },
  plugins: [],
};
