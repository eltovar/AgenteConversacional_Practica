# Capa de servicios del panel CRM

Esta carpeta es la frontera de migracion entre HTTP y la logica de negocio.

## Estado actual

Los servicios son fachadas sobre `middleware.outbound_panel`. Esto conserva las
firmas originales que FastAPI usa para validar parametros, formularios, cuerpos
y dependencias.

## Regla

- `middleware/panel/*_routes.py` solo declara rutas HTTP.
- `services/panel/*_service.py` concentra la operacion de negocio del dominio.
- `middleware.outbound_panel` queda como implementacion legada temporal.

## Siguiente paso

Mover cuerpos internos desde `middleware.outbound_panel` hacia estos servicios
uno por uno, empezando por operaciones de bajo riesgo:

1. Lecturas puras: referencia, asesores, estado de ventana.
2. Consultas de panel: contactos, conversaciones, metricas.
3. Escrituras controladas: notas, citas, mensajes programados.
4. Operaciones sensibles: mensajes manuales, transferencias, campanas.
