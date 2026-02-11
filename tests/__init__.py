# tests/__init__.py
"""
Suite de tests organizada por categorías.

Estructura:
├── agents/          - Tests de agentes (InfoAgent, ReceptionAgent, CRMAgent)
├── api/             - Tests de endpoints HTTP
├── e2e/             - Tests end-to-end de escenarios completos
├── middleware/      - Tests del middleware inteligente (Twilio/HubSpot)
├── orchestrator/    - Tests del orquestador y flujo de conversación
├── prompts/         - Tests de validación de prompts
├── rag/             - Tests del sistema RAG (vectores, búsqueda, indexación)
├── state/           - Tests de gestión de estado (StateManager, Redis)
└── utils/           - Tests de utilidades (link_detector, pii_validator)

Ejecutar todos los tests:
    pytest tests/ -v

Ejecutar tests por categoría:
    pytest tests/agents/ -v
    pytest tests/rag/ -v
    pytest tests/middleware/ -v
"""