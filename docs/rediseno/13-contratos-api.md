# D-13 · Contratos de API y eventos

> **Estado:** v1 — 2026-08-20.
> **Depende de:** [D-06 Permisos](06-matriz-permisos.md) · [D-10 Modelo de datos](10-modelo-de-datos.md) · [D-12 Arquitectura](12-arquitectura.md)
> **Punto de partida:** 72 endpoints en un solo archivo, sin autenticación. El destino reorganiza por recurso y aplica permisos en todos.

---

## 1. Convenciones

| # | Regla |
|---|---|
| 1 | **Todo endpoint exige sesión.** Ninguno acepta clave en la URL |
| 2 | Recursos en plural: `/contactos`, `/usuarios`, `/citas` |
| 3 | El **rol se resuelve del lado del servidor**. El cliente nunca lo envía |
| 4 | El **filtro de propietario se aplica en la consulta**, no como parámetro opcional |
| 5 | Fechas en **ISO 8601 UTC**. La conversión a Bogotá es cosa del frontend |
| 6 | Errores con la misma forma siempre |
| 7 | Listados paginados con `limite` y `cursor` |
| 8 | Operaciones que escriben exigen **token anti-CSRF** |

### Códigos de respuesta

| Código | Cuándo |
|---|---|
| `200` · `201` | Correcto |
| `400` | Petición mal formada |
| `401` | Sin sesión o caducada |
| `403` | Sesión válida, **rol sin permiso** |
| `404` | No existe **o no es visible para este rol** |
| `409` | Conflicto — p. ej. transferir un contacto que ya no es tuyo |
| `429` | Límite de peticiones |

> 🔑 **`404` cubre dos casos a propósito.** Si una asesora pide un contacto de otra, la respuesta es `404`, no `403`. Un `403` confirmaría que el contacto existe — eso ya es una fuga de información.

### Forma del error

```json
{
  "error": {
    "codigo": "PERMISO_DENEGADO",
    "mensaje": "No tienes acceso a este recurso",
    "detalle": null
  }
}
```

> ⚠️ En fallos de inicio de sesión el mensaje es **idéntico** siempre, para no permitir enumerar correos.

---

## 2. Autenticación

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/auth/google` | Inicia el flujo OIDC |
| `GET` | `/auth/google/callback` | Recibe el `id_token`, verifica la firma, comprueba la lista blanca, crea la sesión |
| `GET` | `/auth/sesion` | Devuelve usuario y rol actuales |
| `POST` | `/auth/salir` | Invalida la sesión |

**Respuesta de `/auth/sesion`:**

```json
{
  "user_id": "usr_001",
  "nombre": "Jubeny Ocampo",
  "rol": "interna",
  "navegacion": ["dashboard", "lead", "whatsapp"],
  "etapas_visibles": ["nuevo_lead", "en_conversacion", "..."]
}
```

> 🔑 **El frontend recibe qué puede dibujar, pero el servidor no confía en eso.** Cada petición se vuelve a autorizar.

---

## 3. Contactos

| Método | Ruta | Roles | Notas |
|---|---|---|---|
| `GET` | `/contactos` | Todos | Filtrado por rol automáticamente |
| `GET` | `/contactos/{telefono}` | Todos | `404` si no es visible |
| `POST` | `/contactos` | Asesoras | Alta manual |
| `PATCH` | `/contactos/{telefono}` | Asesora dueña · Admin | Nombre, email |
| `PATCH` | `/contactos/{telefono}/etapa` | Asesora dueña · Admin | Solo etapas de su rol |
| `PATCH` | `/contactos/{telefono}/propiedades` | Asesora dueña · Admin | Presupuesto, segmento, motivo de cierre |
| `PATCH` | `/contactos/{telefono}/propietario` | **Solo Admin** | Reasignación |
| `POST` | `/contactos/{telefono}/transferir` | **Solo A. Interna** | A Seguimiento, con confirmación |

**Parámetros de `GET /contactos`:** `etapa` · `presupuesto` · `segmento` · `canal` · `buscar` · `limite` · `cursor`

> ⚠️ **No existe `propietario` como parámetro.** El propietario lo impone el servidor según el rol. Aceptarlo como filtro abriría la puerta a pedir los contactos de otra.

**Contacto en respuesta:**

```json
{
  "telefono": "+57...",
  "nombre": "Maria Restrepo",
  "etapa": "visita_realizada",
  "presupuesto": "hasta_2m",
  "segmento": "propietarios",
  "motivo_cierre": null,
  "canal_origen": "finca_raiz",
  "propietario": {"user_id": "usr_002", "nombre": "Luisa Munoz"},
  "ultimo_enlace": "https://...",
  "creado_en": "2026-08-12T14:30:00Z"
}
```

> `ultimo_enlace` es **una proyección del evento `E-07`**, no un campo almacenado. El historial guarda todos; la tabla muestra el último.

---

## 4. Intereses, eventos y notas

| Método | Ruta | Roles |
|---|---|---|
| `GET` | `/contactos/{telefono}/intereses` | Asesora dueña · Marketing · Admin |
| `PATCH` | `/contactos/{telefono}/intereses/{id}` | Asesora dueña — cambiar estado |
| `GET` | `/contactos/{telefono}/eventos` | Asesora dueña · Marketing · Admin |
| `GET` | `/contactos/{telefono}/notas` | Asesora dueña · Marketing · Admin |
| `POST` | `/contactos/{telefono}/notas` | **Solo asesoras** |
| `PATCH` `DELETE` | `/contactos/{telefono}/notas/{id}` | Solo la autora |

**`GET /contactos/{telefono}/eventos`** acepta `tipo` y `desde`.

> 🔴 **Este endpoint aplica el filtro más delicado del sistema.** Para Marketing y Administrador **excluye `E-15` y `E-16`** — el contenido de los mensajes. Es lo que permite ver el historial sin leer el chat.
> El filtro va **en la consulta**, no en la serialización. Si se filtrara al serializar, el contenido ya habría salido de la base.

---

## 5. Conversaciones y mensajes

| Método | Ruta | Roles |
|---|---|---|
| `GET` | `/conversaciones/{telefono}` | **Solo asesora dueña** |
| `POST` | `/conversaciones/{telefono}/mensajes` | Solo asesora dueña |
| `POST` | `/conversaciones/{telefono}/plantillas` | Solo asesora dueña |
| `POST` | `/conversaciones/{telefono}/leido` | Solo asesora dueña |
| `POST` | `/conversaciones/{telefono}/tomar-control` | Solo asesora dueña |
| `GET` | `/plantillas` | Asesoras |

**Estado de la conversación:**

```json
{
  "telefono": "+57...",
  "asignado_a": "usr_002",
  "modo_envio": "solo_plantillas",
  "ventana_expira_en": null,
  "ultimo_mensaje_en": "2026-08-19T09:12:00Z"
}
```

> 🔑 **`modo_envio` es el que dibuja el aviso en el composer.** Con `solo_plantillas`, el frontend desactiva el texto libre y ofrece el selector. Hoy esa información existe en `/window-status/{phone}` pero **el panel no la usa para nada visible**.

---

## 6. Citas

| Método | Ruta | Roles |
|---|---|---|
| `GET` | `/contactos/{telefono}/citas` | Asesora dueña · Marketing · Admin |
| `POST` | `/contactos/{telefono}/citas` | Solo asesoras — admite `interes_id` |
| `PATCH` `DELETE` | `/citas/{id}` | Solo la asesora dueña |
| `GET` `POST` `PATCH` `DELETE` | `/trabajadores` | **Solo Admin** |

> ⚠️ Los trabajadores de campo pasan a ser configuración del Administrador. Hoy cualquiera con la clave puede crearlos.

---

## 7. Dashboard y métricas

| Método | Ruta | Devuelve |
|---|---|---|
| `GET` | `/dashboard` | **KPIs y colas del rol de la sesión** |
| `GET` | `/metricas/redes` | Solo Marketing y Admin |
| `GET` | `/metricas/citas` | Solo Marketing y Admin |
| `GET` | `/metricas/exportar` | Solo Marketing y Admin — Excel |

> 🔑 **`/dashboard` no lleva parámetro de rol.** Devuelve lo que corresponde a quien pregunta: 2 KPIs y 1 cola para Interna, 3 y 2 para Seguimiento, 3 y 3 para Admin, 4 KPIs para Marketing.
> Un solo endpoint, cuatro respuestas. Es lo que hace que **no haya cuatro interfaces sino una configurable**.

**Ventana temporal:** todos los contadores usan **lunes a sábado**, la misma para todos los roles.

---

## 8. Administración

| Método | Ruta | Roles |
|---|---|---|
| `GET` `POST` | `/usuarios` | **Solo Admin** |
| `PATCH` | `/usuarios/{id}` | Solo Admin — rol, correo, activo |
| `DELETE` | `/usuarios/{id}` | Solo Admin — desactiva, no borra |
| `GET` `PATCH` | `/usuarios/{id}/canales` | Solo Admin |
| `GET` `PATCH` | `/canales` | Solo Admin |
| `GET` `PATCH` | `/roles/{rol}/etapas` | Solo Admin |

> 🔴 **`DELETE /usuarios/{id}` nunca borra.** Desactiva, invalida la sesión al instante y **exige reasignar sus contactos antes**. Borrar un usuario con contactos dejaría huérfanos.

---

## 9. Eventos WebSocket

Un solo canal **por usuario**, no por conexión. Todas las pestañas y dispositivos del mismo usuario reciben lo mismo.

| Evento | Cuándo | Quién lo recibe |
|---|---|---|
| `mensaje.recibido` | Llega un mensaje | Solo la asesora dueña |
| `mensaje.enviado` | Se envía desde otro dispositivo | Todas las sesiones del usuario |
| `contacto.asignado` | Escalamiento o transferencia | Quien lo recibe |
| `contacto.transferido` | Sale de su cola | Quien lo entrega |
| `etapa.cambiada` | Cambio de etapa | Asesora dueña · Admin |
| `ventana.cerrada` | Se cumplen 24 h | Asesora dueña |
| `envio.fallido` | Falla un envío | **Siempre visible** |

> 🔑 **`mensaje.enviado` es lo que hace funcionar los dos dispositivos.** Si la asesora responde en el móvil, la pestaña del escritorio lo refleja sin recargar.
> 🔴 **`envio.fallido` nunca se silencia.** Regla registrada: *"cuando un mensaje falla, la asesora DEBE verlo"*.

---

## 10. Correspondencia con los 72 endpoints actuales

| Situación | Cuántos | Qué pasa |
|---|---|---|
| Se conservan con cambio de ruta y permisos | ~40 | Contactos, conversaciones, citas, notas, plantillas |
| Se reescriben | ~12 | Métricas — quitar la clave de la URL, ampliar a todos los canales |
| **Se retiran** | ~10 | Los 5 de recuperación, `debug/redis`, `diagnose`, `reset-bot`, `ws/stats` |
| **Nuevos** | ~15 | Autenticación, usuarios, canales, intereses, eventos, dashboard |

> Los de recuperación existían porque el estado se corrompía. Con la cola de propagación y el asignatario explícito, **deberían dejar de hacer falta** — pero se retiran solo tras verificarlo en producción.

---

## 11. Pendiente

- Esquema OpenAPI completo
- Formato del cursor de paginación
- Límites de peticiones por rol
- Versionado de la API
- Contrato de la cola de propagación → ADR
