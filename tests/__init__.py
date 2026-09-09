# tests/__init__.py
"""
Suite de tests organizada por TEMA, no por paquete de codigo.

Estructura:
├── asignacion_ownership/   - Reparto de contactos, lead assigner, routing dirigido
├── badges_notificaciones/  - Avisos de no-leido y notificaciones a asesoras
├── bsuid_identidad/        - Identidades BSUID, migracion de clave, edicion de telefono
├── cache_estado/           - Caches, StateManager, pools y metricas de Redis
├── citaciones/             - Citar un mensaje anterior (reply/quote) y su traza
├── citas/                  - Appointments: agendado, confirmacion y metricas
├── contactos_pendientes/   - Corte de la lista de pendientes del panel
├── e2e/                    - Simulador manual contra el despliegue (no es pytest)
├── filtros_etapas/         - stage_filter y paginacion de etapas de HubSpot
├── links_canales/          - Deteccion de portal por link y asignacion de canal
├── multimedia/             - Bunny.net, sanitizacion de logs de media
├── panel_embudos/          - Embudos, cierre, take-control y flujos del panel
├── rag_llm/                - RAG, vector store, agentes, prompts, handoff
├── scheduler/              - Lock de liderazgo y eleccion continua del scheduler
├── telefonos/              - PhoneNormalizer (Colombia e internacional)
└── transversal/            - PII, safe_logging, firma Twilio, query profiler

IMPORTANTE: ninguna carpeta puede llamarse como un paquete de primer nivel
(middleware, utils, agents, rag, integrations, database, prompts). Cuando
existian `tests/middleware/` y `tests/utils/`, cualquier fichero que metiera
`tests/` en sys.path hacia que `import middleware` resolviera a `tests/middleware`
y 11 modulos dejaban de importar. Los nombres de arriba estan elegidos para que
esa colision no pueda repetirse.

Ejecutar todo:
    pytest tests/ -v

Ejecutar un tema:
    pytest tests/citaciones/ -v
    pytest tests/rag_llm/ -v
"""
