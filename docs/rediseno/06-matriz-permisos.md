# D-06 · Matriz de permisos

> **Estado:** v1 — extraído de [D-02 §6 y §13](02-actores-y-roles.md).
> **Depende de:** [D-02 Actores y roles](02-actores-y-roles.md) · [ADR-001 Autenticación](14-adrs.md)
> **Principio rector:** el rol se resuelve **en el backend** desde la sesión, y el aislamiento se aplica **en la consulta a base de datos**. Nunca en el frontend.

---

## 1. Los cuatro roles

| Rol | Quién es hoy | Naturaleza |
|---|---|---|
| **A. Interna** | Jubeny Ocampo · Monica Administracion | Opera: atiende el primer contacto |
| **A. Seguimiento** | Luisa Muñoz | Opera: da continuidad |
| **Marketing** | Publicidad Proteger ⚠️ | Solo lectura: métricas y datos, sin conversaciones |
| **Administrador** | Salo Tovar · Hector Guerra | Configura y supervisa. **No conversa** |

> ⚠️ "Publicidad Proteger" es hoy un owner que no es una persona. Con la regla de correo único (A-6) debe pasar a ser la cuenta de alguien real.

---

## 2. Navegación por rol

**La navegación es distinta por rol** — no es la misma pantalla con botones desactivados.

| Rol | Secciones |
|---|---|
| A. Interna · A. Seguimiento | `Dashboard` · `Lead` · `WhatsApp` |
| Administrador | `Dashboard` · `Lead` + ⚙ configuración |
| Marketing | `Dashboard` · `Redes` · `Citas` |

---

## 3. Matriz rol × recurso × acción

**Leyenda:** ✅ permitido · ❌ denegado · 🔵 solo los propios · — no aplica

| Recurso | Acción | A. Interna | A. Seguimiento | Marketing | Administrador | SofIA |
|---|---|---|---|---|---|---|
| **Contacto** | Ver | 🔵 propios | 🔵 propios | ✅ todos | ✅ todos | ✅ todos |
| | Crear | ✅ | ✅ | ❌ | ❌ | ✅ |
| | Editar datos | 🔵 | 🔵 | ❌ | ✅ | ✅ |
| | Eliminar | ❌ | ❌ | ❌ | ❌ | ❌ |
| | Reasignar propietario | ❌ | ❌ | ❌ | ✅ | ❌ |
| **Conversación** | Leer el hilo | 🔵 | 🔵 | ❌ | ❌ | ✅ |
| | Responder | 🔵 | 🔵 | ❌ | ❌ | ✅ acotado |
| **Historial de Contacto** | Ver | 🔵 | 🔵 | ✅ | ✅ | ✅ |
| **Historial de Notas** | Ver | 🔵 | 🔵 | ✅ | ✅ | ❌ |
| | Crear nota | ✅ | ✅ | ❌ | ❌ | ❌ |
| **Etapa** | Cambiar | 🔵 | 🔵 | ❌ | ✅ | ✅ acotado |
| **Cita** | Agendar · editar · cancelar | 🔵 | 🔵 | ❌ | ❌ | ❌ |
| | Ver | 🔵 | 🔵 | ✅ | ✅ | ✅ |
| **Transferencia** | A. Interna → A. Seguimiento | ✅ | ❌ | ❌ | — | ❌ |
| **Métricas** | Ver | ❌ | ❌ | ✅ | ✅ | ❌ |
| | Exportar a Excel | ❌ | ❌ | ✅ | ✅ | ❌ |
| **Usuario** | Crear · desactivar · editar correo | ❌ | ❌ | ❌ | ✅ | ❌ |
| | Asignar rol · asignar portales | ❌ | ❌ | ❌ | ✅ | ❌ |
| **Canal** | Configurar | ❌ | ❌ | ❌ | ✅ | ❌ |

### Reglas transversales

| # | Regla |
|---|---|
| 1 | **Aislamiento entre asesoras: total.** Ninguna ve los contactos de otra |
| 2 | **Marketing y Administrador nunca leen conversaciones.** Ven el contacto y su historial, no el chat |
| 3 | **El Administrador no tiene contactos asignados** y no puede responder |
| 4 | **Nadie elimina contactos.** Ni siquiera el Administrador |
| 5 | Solo A. Interna transfiere, y **solo en un sentido** |
| 6 | SofIA nunca reasigna propietarios ni mueve a etapas de cierre |

---

## 4. Matriz de etapas visibles por rol

Verificada contra la API de HubSpot el 2026-08-05.

| # | Etapa | ID HubSpot | Interna | Seguimiento | Admin |
|---|---|---|---|---|---|
| 1 | Nuevo Lead | ⚠️ crear | ✅ | ✅ | ✅ |
| 2 | En Conversación | `1326623075` | ✅ | ✅ | ✅ |
| 3 | Visita Agendada | `marketingqualifiedlead` | ✅ | ✅ | ✅ |
| 4 | Visita Realizada | `salesqualifiedlead` | ✅ | ✅ | ✅ |
| 5 | En Estudio | `opportunity` | ✅ | ✅ | ✅ |
| 6 | Aprobado | `lead` | ✅ | ✅ | ✅ |
| 7 | Cerrado Ganado | `customer` | ✅ | ✅ | ✅ |
| 8 | Cerrado Perdido | `evangelist` | ✅ | ✅ | ✅ |
| 9 | Ya encontró | `1326623541` | ✅ | ✅ | ✅ |
| 10 | Otras Áreas | `1326632209` | ✅ | ❌ | ✅ |
| 11 | Ventas | `1353539189` | ✅ | ❌ | ✅ |
| 12 | Post Cita | `1407668893` ⚠️ renombrar | ❌ | ✅ | ✅ |
| 13 | Hasta 1.5M | `1326623067` | ❌ | ✅ | ✅ |
| 14 | Hasta 2M | `1326631573` | ❌ | ✅ | ✅ |
| 15 | Hasta 2.5M | `1326632625` | ❌ | ✅ | ✅ |
| 16 | De 3M en adelante | `1326631574` | ❌ | ✅ | ✅ |
| 17 | Propietarios | `1326623069` | ❌ | ✅ | ✅ |
| 18 | Otros Municipios | `1326632628` | ❌ | ✅ | ✅ |
| 19 | Local o Bodega | `1326623539` | ❌ | ✅ | ✅ |
| 20 | Reubicados | `subscriber` | ❌ | ✅ | ✅ |
| 21 | No Responde | `other` | ❌ | ✅ | ✅ |

**Totales: Interna 11 · Seguimiento 19 · Administrador 21 · compartidas 9.**

> 🔑 **Al aplicar la Opción B** ([D-02 §25.1](02-actores-y-roles.md)), esta matriz se reduce: solo las **7 primeras** siguen siendo etapas. Las 14 restantes pasan a ser **propiedades del contacto** — presupuesto, segmento y motivo de cierre.
> **Entonces el permiso deja de ser "qué etapas ve" y pasa a ser "qué propiedades puede leer y escribir".** Esta matriz se reescribe cuando D-10 cierre el modelo.

---

## 5. Permisos a nivel de campo

| Campo | Quién lo ve |
|---|---|
| `Portal` (ticket de origen) | Todos los roles |
| `Link` (enlace de origen) | A. Interna · A. Seguimiento · Marketing · Administrador |
| `Fecha Formulario` | Marketing · Administrador |
| `Asignado` (propietario) | Administrador siempre; asesoras solo en sus contactos |
| Contenido de mensajes | Solo la asesora dueña y SofIA |

> El caso de `Fecha Último Seguimiento` se retiró del diseño, pero el **principio de permiso por campo sigue vigente** — lo demuestran `Fecha Formulario` y `Link`.

---

## 6. Dónde se aplica cada control

| Capa | Qué controla | Riesgo si se omite |
|---|---|---|
| **Sesión** (cookie `HttpOnly`) | Quién eres | Suplantación |
| **Backend — resolución de rol** | Qué puedes hacer | Un rol falsificado desde el cliente |
| **Consulta a base de datos** | Qué registros ves | 🔴 **Filtrar en el frontend = cualquiera ve todo con DevTools** |
| **Frontend** | Qué se dibuja | Solo comodidad. **Nunca es seguridad** |

---

## 7. Estado actual — punto de partida

| | Hoy | Destino |
|---|---|---|
| Control de acceso | **No existe** | Autorización en el backend por rol |
| Acceso al panel | Por enlace, sin credenciales | Google + sesión |
| Acceso a métricas | Clave de administrador **en la URL** | Rol Marketing con su propia sesión |
| Aislamiento entre asesoras | Depende del frontend | En la consulta a base de datos |
| Revocación | No existe | Inmediata al desactivar el usuario |

> 🔴 Hoy quien abre `/metrics/?key=…` tiene, de hecho, **credenciales de administrador**: es la misma `ADMIN_API_KEY` que da acceso total al panel.

---

## 8. Pendiente

- Reescribir §4 cuando D-10 cierre el modelo de etapas vs propiedades
- Definir permisos sobre la entidad `Interés`
- Política de auditoría: qué acciones se registran y quién puede consultarlas
