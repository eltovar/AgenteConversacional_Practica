# Panel CRM boundary

Este paquete es la frontera nueva del panel, sin cambiar la logica existente.

## Estado actual

`get_panel_router()` monta el router historico de `middleware/outbound_panel.py`.
Eso conserva exactamente los mismos endpoints publicos:

- `/whatsapp/panel/contacts`
- `/whatsapp/panel/conversations/{phone}`
- `/whatsapp/panel/send-message`
- `/whatsapp/panel/templates`
- `/whatsapp/panel/workers`
- `/whatsapp/panel/metrics`
- y el resto de rutas actuales.

## Regla de migracion

Cada grupo de rutas debe moverse aqui en piezas pequenas y con tests de
caracterizacion antes de tocar comportamiento.

Orden sugerido:

1. `workers_routes.py` - iniciado: las rutas ya viven aqui y llaman funciones legadas.
2. `templates_routes.py` - iniciado: envio, Content SIDs y CRUD de plantillas.
3. `notes_routes.py` - iniciado: CRUD de notas internas de contacto.
4. `appointments_routes.py` - iniciado: CRUD de citas y cancelacion.
5. `metrics_routes.py` - iniciado: dashboards y exportaciones de metricas.
6. `notifications_routes.py` - iniciado: listado y marcado de notificaciones.
7. `advisors_routes.py` - iniciado: listado y renombre de asesoras.
8. `reference_routes.py` - iniciado: datos de referencia como etapas.
9. `scheduled_messages_routes.py` - iniciado: mensajes de plantilla programados.
10. `bulk_campaigns_routes.py` - iniciado: endpoints publicos de campanas masivas.
11. `diagnostics_routes.py` - iniciado: diagnostico Redis, sistema y WebSocket.
12. `ui_routes.py` - iniciado: raiz HTML del panel.
13. `realtime_routes.py` - iniciado: WebSocket del panel.
14. `messages_routes.py` - iniciado: envio manual JSON/form y edicion/eliminacion.
15. `contacts_routes.py`
16. `conversations_routes.py`
17. `admin_routes.py` - iniciado: recuperacion y mantenimiento administrativo.

Mientras una ruta siga en `outbound_panel.py`, este paquete la expone por medio
del router legado. Cuando se migre un grupo, se retira del router legado y se
incluye su nuevo router aqui.

## Regla de no regresion

No se cambian URLs, payloads, codigos HTTP ni nombres de campos durante una
extraccion. Primero se mueve, despues se mejora.
