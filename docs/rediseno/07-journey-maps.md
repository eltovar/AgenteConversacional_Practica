# D-07 · Journey maps

> **Estado:** v1 — construido sobre los wireframes, las decisiones cerradas y **los tiempos medidos en producción** (D-03 §12).
> **Depende de:** [D-04 Inventario](04-inventario-actual.md) · [D-05 Casos de uso](05-casos-de-uso.md) · [D-08 Máquinas de estado](08-maquinas-de-estado.md)

---

## 1. Journey del cliente

```mermaid
journey
    title Recorrido del cliente, del anuncio a la visita
    section Descubrimiento
      Ve el anuncio en el portal: 4: Cliente
      Pulsa el enlace de WhatsApp: 5: Cliente
    section Primer contacto
      Escribe su consulta: 5: Cliente
      SofIA responde en 42 s: 5: Cliente, SofIA
      SofIA pregunta nombre e interes: 4: Cliente, SofIA
    section Espera
      SofIA escala a la asesora: 3: SofIA
      Espera respuesta humana: 2: Cliente
    section Atencion
      La asesora responde: 4: Cliente, Asesora
      Acuerdan una visita: 5: Cliente, Asesora
    section Visita
      Recordatorio 24 h antes: 5: SofIA
      Realiza la visita: 5: Cliente
      Recibe encuesta de experiencia: 4: SofIA
```

### Los momentos que deciden

| # | Momento | Hoy | Riesgo |
|---|---|---|---|
| 1 | **Primer contacto** | SofIA responde en **42 s** · 95 % bajo 5 min | 🟢 Resuelto |
| 2 | **Espera tras el escalamiento** | Mediana 30 min · **p90 24,1 h** | 🔴 **El punto crítico** |
| 3 | Ventana de 24 h | Si el cliente tarda, solo plantillas | 🟠 Restricción externa |
| 4 | Recordatorio de cita | Automático, 24 h antes | 🟢 |
| 5 | Encuesta post-cita | `experiencia_cita`, automática | 🟢 |

> 🔴 **El único hueco real del recorrido del cliente es el momento 2.** SofIA le contesta en 42 segundos y le genera una expectativa; **1 de cada 10 veces la respuesta humana tarda más de 24 horas.** El contraste entre ambos es lo que hace que la espera se note tanto.

---

## 2. Journey de la Asesora Interna — un día

```mermaid
flowchart TD
    A["Abre SofIA CRM"] --> B["Continuar con Google"]
    B --> C["Dashboard<br/>2 KPIs + Clientes a Atender"]
    C --> D{"Hay contactos<br/>pendientes?"}
    D -->|"Si"| E["Abre el contacto<br/>desde la cola"]
    D -->|"No"| F["Revisa el Kanban<br/>11 etapas"]
    E --> G["Lee el Historial de Contacto<br/>intereses, etapas, citas"]
    G --> H["Responde por WhatsApp"]
    H --> I{"Que sigue?"}
    I -->|"Agendar"| J["Crea la cita<br/>desde el hilo"]
    I -->|"Anotar"| K["Registra una nota"]
    I -->|"Avanzar"| L["Mueve de etapa<br/>en el Kanban"]
    I -->|"Ya no es suyo"| M["Transfiere a Seguimiento<br/>con confirmacion"]
    J --> C
    K --> C
    L --> C
    M --> C
    F --> C

    style C fill:#e3f2fd,stroke:#1565c0,stroke-width:2px
    style M fill:#fff4e6,stroke:#e67e22,stroke-width:2px
```

### Qué cambia respecto de hoy

| Dolor documentado | Cómo se resuelve |
|---|---|
| *"Perdía media hora al día buscando contactos"* | La **cola `Clientes a Atender`** le dice qué atender. No busca: abre y trabaja |
| *"No me gusta cambiar de pestaña constantemente"* | Tres secciones en una sola aplicación. HubSpot desaparece de su día |
| *"No se visualizaban contactos con badge"* | El contacto pendiente está **en la cola**, no depende de un indicador visual |
| *"Había leads que nunca aparecían"* | Resuelto en la arquitectura actual; el asignatario explícito lo hace estructural |

> 🔑 **El cambio de fondo: pasa de *buscar* a *atender una cola*.** Hoy la asesora decide qué mirar; en el destino el sistema se lo dice.

---

## 3. Journey de la Asesora de Seguimiento

Su Dashboard tiene **dos colas porque tiene dos vías de entrada** — no es un capricho de diseño.

```mermaid
flowchart TD
    A["Dashboard de Seguimiento<br/>3 KPIs · 2 colas"] --> B{"Que cola?"}

    B -->|"Cola 1"| C["Clientes a Atender Nuevos<br/>llegaron por Finca Raiz o MetroCuadrado"]
    B -->|"Cola 2"| D["Clientes para Seguimiento<br/>transferidos por A. Interna"]

    C --> E["Contacto nuevo:<br/>SofIA ya hizo el filtro"]
    D --> F["Contacto con historia:<br/>lee que paso antes"]

    E --> G["Atiende"]
    F --> G
    G --> H["Registra seguimiento"]
    H --> I["Mueve de etapa<br/>19 etapas disponibles"]
    I --> A

    style C fill:#e8f5e9,stroke:#2e7d32
    style D fill:#fff4e6,stroke:#e67e22
```

**La diferencia entre las dos colas es de contexto, no de acción:**

| | Cola 1 — nuevos por canal | Cola 2 — transferidos |
|---|---|---|
| De dónde viene | Portal directo | A. Interna |
| Qué sabe la asesora | Solo el filtro de SofIA | **Todo el recorrido previo** |
| Qué mira primero | El enlace de origen | El `Historial de Contacto` |

> ⚠️ **Aquí es donde `E-04 Transferencia` gana valor.** Sin ese evento, un contacto transferido llega sin explicación de por qué. Con él, la asesora ve quién se lo pasó y cuándo.

---

## 4. Journey del Administrador

Dos recorridos distintos, y **ninguno incluye conversar**.

```mermaid
flowchart LR
    A["Dashboard Vista Total<br/>3 KPIs · 3 colas"] --> B{"Que necesita?"}
    B -->|"Supervisar"| C["Kanban TODOS LOS EMBUDOS<br/>21 etapas + dueno por tarjeta"]
    B -->|"Configurar"| D["Engranaje"]
    C --> E["Abre un contacto<br/>historial SIN chat"]
    D --> F["Team Members"]
    F --> G["Crear o desactivar usuario"]
    F --> H["Cambiar rol"]
    F --> I["Asignar portales"]
    F --> J["Cambiar correo"]

    style D fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px
```

> 🔑 **El recorrido de configuración es el que materializa el objetivo O-3.** Hoy cada una de esas cuatro acciones exige editar código y desplegar. En el destino son cuatro clics.
> Y hay un dato que lo hace más urgente: [D-04](04-inventario-actual.md) encontró que **cuatro sitios distintos definen hoy quién es una asesora**. Esta pantalla debe sustituir a los cuatro.

---

## 5. Journey de Marketing

```mermaid
flowchart LR
    A["Dashboard<br/>4 KPIs"] --> B{"Que analiza?"}
    B -->|"Origen de los leads"| C["Redes<br/>Instagram · Facebook · TikTok"]
    B -->|"Resultado comercial"| D["Citas<br/>todos los portales"]
    C --> E["Abre un contacto<br/>ve el enlace que envio"]
    D --> F["Revisa Fecha Formulario<br/>a quien se le envio la encuesta"]
    C --> G["Descarga Excel por portal"]
    D --> G

    style C fill:#e8f5e9,stroke:#2e7d32
    style D fill:#fff4e6,stroke:#e67e22
```

**Las dos secciones responden a dos preguntas distintas:**

| Sección | Pregunta que responde | Alcance |
|---|---|---|
| `Redes` | *¿Qué anuncio trae leads?* | Solo Instagram, Facebook y TikTok |
| `Citas` | *¿Cuántos llegaron a visita y a quién se le envió la encuesta?* | **Todos** los portales |

> ⚠️ Marketing **nunca lee conversaciones**. Ve el contacto, su historial, sus enlaces y sus citas — pero no el chat.

---

## 6. El recorrido de un lead, extremo a extremo

Cruce de los cinco journeys sobre un mismo contacto:

```mermaid
sequenceDiagram
    participant C as Cliente
    participant S as SofIA
    participant I as A. Interna
    participant G as A. Seguimiento
    participant M as Marketing

    C->>S: Escribe desde un anuncio
    Note over S: E-01 contacto creado<br/>E-07 enlace guardado
    S->>C: Responde en 42 s
    S->>C: Pregunta nombre e interes
    Note over S: E-06 interes detectado
    S->>I: E-03 escalamiento
    Note over I: Aparece en su cola
    I->>C: Atiende
    I->>I: E-14 nota · E-02 etapa
    I->>G: E-04 transferencia manual
    Note over G: Aparece en Clientes para Seguimiento
    G->>C: Da continuidad
    G->>G: E-08 cita agendada
    S->>C: E-12 recordatorio 24 h antes
    Note over G: E-11 cita realizada, auto 1h30
    S->>C: E-13 encuesta experiencia_cita
    Note over M: Ve la cita y la Fecha Formulario
```

> 🔑 **Cada paso del recorrido deja un evento.** Es lo que permite que Marketing y el Administrador reconstruyan la historia **sin leer una sola conversación** — y lo que hace posible medir el embudo, que hoy no se puede.

---

## 7. Puntos de fricción — ordenados por impacto

| # | Fricción | Evidencia | Se resuelve con |
|---|---|---|---|
| 1 | **Espera tras el escalamiento** | p90 = **24,1 h** medido | Colas por rol + `E-03` para poder medirlo |
| 2 | **Buscar contactos manualmente** | *"media hora al día"* | Cola `Clientes a Atender` |
| 3 | **Cambiar de pestaña** | Reportado por las asesoras | Tres secciones en una app |
| 4 | **Un cambio menor exige desplegar** | Owners en **4 sitios** | Pantalla *Team Members* |
| 5 | **Contexto perdido al transferir** | Sin evento de transferencia | `E-04` + Historial de Contacto |
| 6 | **Marketing no puede medir por anuncio** | La URL se detecta y se tira | `E-07` enlace guardado |

---

## 8. Pendiente

- Journey del cliente **que no responde** — qué pasa entre la ventana de 24 h y el cierre
- Journey de reactivación: cliente que vuelve meses después por otro inmueble
- Recorrido de error: qué ve la asesora si falla el envío de un mensaje
