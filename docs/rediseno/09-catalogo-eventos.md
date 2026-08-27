# D-09 · Catálogo de eventos de dominio

> **Estado:** v1 — extraído de [D-02 §19 y §25.2](02-actores-y-roles.md).
> **Depende de:** [D-08 Máquinas de estado](08-maquinas-de-estado.md) · [D-10 Modelo de datos](10-modelo-de-datos.md)

---

## 1. Por qué este documento existe

El `Historial de Contacto` no es una tabla de campos: es un **registro de eventos**. Cada entrada es un hecho con tipo, fecha y autor.

Definición acordada:

| | **Historial de Notas** | **Historial de Contacto** |
|---|---|---|
| Qué registra | Las notas que cada asesora escribe | El **recorrido** del contacto desde que llegó |
| Autoría | Humana, agrupada por asesora | Sistema y asesoras |
| Naturaleza | Aporte subjetivo | **Hecho objetivo** |

> 🔴 **Y hay una razón medible.** Hoy el sistema **no puede medir ni su embudo ni su tiempo de reacción**:
> - La conversión real no se puede calcular porque `lifecyclestage` guarda el **estado actual**, no el recorrido
> - El tiempo *handoff → respuesta humana* no se puede calcular porque **el handoff no se registra**
>
> Ambos huecos se cierran con este catálogo. **D-09 no es documentación: es la condición para que el CRM pueda medirse a sí mismo.**

---

## 2. Catálogo

| # | Evento | Lo emite | Datos que lleva |
|---|---|---|---|
| **E-01** | Contacto creado | Sistema · SofIA · Asesora | canal de origen, ticket del portal, fecha |
| **E-02** | Cambio de etapa | Asesora · SofIA · Sistema | etapa anterior, etapa nueva, autor |
| **E-03** | **Escalamiento (handoff)** | SofIA | motivo estructurado, confianza, disparador, a quién se asigna |
| **E-04** | **Transferencia** | A. Interna | de quién, a quién, confirmación |
| **E-05** | Reasignación | Administrador | de quién, a quién, motivo |
| **E-06** | **Interés detectado** | SofIA · Sistema | código de inmueble, origen (link / código / dicho) |
| **E-07** | **Enlace enviado** | Sistema | URL, portal, fecha |
| **E-08** | Cita agendada | Asesora | fecha y hora, inmueble asociado |
| **E-09** | Cita editada | Asesora | fecha anterior, fecha nueva |
| **E-10** | Cita cancelada | Asesora | motivo |
| **E-11** | Cita realizada | Sistema | auto 1 h 30 min después de la hora |
| **E-12** | Recordatorio enviado | SofIA | plantilla, fecha |
| **E-13** | **Formulario post-cita enviado** | SofIA | plantilla `experiencia_cita`, fecha → alimenta `Fecha Formulario` |
| **E-14** | Nota creada | Asesora | autor, texto |
| **E-15** | Mensaje recibido | Sistema | canal, marca de tiempo |
| **E-16** | Mensaje enviado | Asesora · SofIA | autor, canal, marca de tiempo |
| **E-17** | Inicio de sesión | Sistema | usuario, fecha, origen |
| **E-18** | Usuario creado · modificado · desactivado | Administrador | qué cambió, valor anterior |
| **E-19** | Portal asignado a usuario | Administrador | portal, usuario |
| **E-20** | Cambio de correo de un usuario | Administrador | correo anterior, correo nuevo |
| **E-21** | **Ventana de 24 h cerrada** | Sistema | fecha de cierre → a partir de aquí solo plantillas |
| **E-22** | Ventana de 24 h reabierta | Sistema | el cliente volvió a escribir |

**22 tipos de evento.**

> **E-21 y E-22** materializan la restricción de WhatsApp. Cambian el **modo de envío** de la conversación, no su asignatario — ver [D-08 §1-bis](08-maquinas-de-estado.md).

---

## 3. Estructura común

Todo evento comparte la misma forma:

| Campo | Descripción |
|---|---|
| `tipo` | Uno de los 20 del catálogo |
| `contacto_id` | A qué contacto pertenece — salvo E-17 a E-20, que son de sistema |
| `autor_tipo` | `asesora` · `SofIA` · `sistema` · `administrador` |
| `autor_id` | Quién exactamente |
| `fecha` | Marca de tiempo en UTC |
| `datos` | Campos propios del tipo de evento |

> ⚠️ **Fechas siempre en UTC.** El sistema ya sufrió errores de 5 horas por comparar fechas sin zona horaria de Mongo con hora local de Bogotá. **Se guarda en UTC y se convierte solo al mostrar.**

---

## 4. Qué evento alimenta qué

| Evento | Alimenta |
|---|---|
| E-02 Cambio de etapa | Kanban · conversión del embudo · Historial de Contacto |
| E-03 Escalamiento | **Tiempo de respuesta real** · auditoría de SofIA · colas del Dashboard |
| E-04 Transferencia | KPI `Clientes transferidos a seguimiento` · cola de A. Seguimiento |
| E-06 Interés | Lista de inmuebles consultados en el panel del contacto |
| E-07 Enlace | Columna `Link` · KPIs de Marketing por red social |
| E-11 Cita realizada | KPI `Citas de la semana` · sección `Citas` de Marketing |
| E-13 Formulario | Columna `Fecha Formulario` |
| E-15 · E-16 Mensajes | Tiempo de primera respuesta · KPI `Chat Nuevos` |
| E-17 · E-18 · E-20 | Registro de auditoría de seguridad |

> 🔑 **Los KPIs de Marketing se definen sobre eventos**, no sobre campos. El wireframe lo dice literalmente: *"número de contactos con links de Instagram **en su historial**"*. Eso solo se puede responder si E-07 existe.

---

## 5. Visibilidad de eventos por rol

| Evento | Asesora dueña | Marketing | Administrador |
|---|---|---|---|
| E-01 a E-13 *(recorrido del contacto)* | ✅ | ✅ | ✅ |
| **E-15 · E-16 (contenido de mensajes)** | ✅ | ❌ | ❌ |
| E-14 Notas | ✅ | ✅ | ✅ |
| E-17 a E-20 *(sistema y seguridad)* | ❌ | ❌ | ✅ |

> 🔑 **La línea está en E-15 y E-16.** Marketing y Administrador ven que hubo mensajes y cuándo, pero **no su contenido**. Es lo que permite que vean el historial sin leer el chat.

---

## 6. Los tres eventos que hoy no existen

| Evento | Estado actual | Qué desbloquea |
|---|---|---|
| **E-03 Escalamiento** | El motivo se calcula en `reason_parts` y **se descarta** | Medir *handoff → respuesta humana* · auditar a SofIA |
| **E-06 Interés** | `property_code_detector.py` detecta códigos y **no los guarda** | La entidad `Interés` · saber qué inmueble le interesa al cliente |
| **E-07 Enlace** | `link_detector.py` detecta el portal y **descarta la URL** | Columna `Link` · KPIs de Marketing por red social |

> **Los tres tienen la mitad construida.** El sistema ya detecta esa información y la tira. Persistirla es barato y desbloquea tres funciones del rediseño.

---

## 7. Pendiente

- Esquema exacto del campo `datos` por tipo de evento
- Política de retención: ¿cuánto tiempo se guardan?
- ¿Los eventos se replican a HubSpot o viven solo en la base propia? → D-11
- Motivos estructurados de E-03: catálogo cerrado de razones de escalamiento
