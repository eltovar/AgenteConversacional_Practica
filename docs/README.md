# Documentación - Agente Conversacional Inmobiliaria Proteger

Bienvenido a la documentación técnica del sistema multi-agente conversacional.

## 📚 Índice de Documentación

### 🏗️ Arquitectura
- [Visión General del Sistema](architecture/system_overview.md) - Descripción de alto nivel del sistema
- [Máquina de Estados (FSM)](architecture/state_machine.md) - Diagrama y explicación de estados
- [Comunicación entre Agentes](architecture/agent_communication.md) - Protocolos de comunicación

### 🛠️ Implementación
- [PR 1: Refactor InfoAgent con bind_tools](implementation/pr1_info_agent_refactor.md) - Migración de parsing manual a bind_tools()
- [PR 2: Retry Logic en ReceptionAgent](implementation/pr2_retry_logic.md) - Implementación de resiliencia
- [PR 3: CRMAgent Stub](implementation/pr3_leadsales_stub.md) - Agente stub para handoff
- [Memoria de Sesión](implementation/session_memory.md) - Persistencia de contexto de usuario

### 📖 API de Componentes
- [ReceptionAgent](api/reception_agent.md) - Clasificación de intenciones y captura de PII
- [InfoAgent](api/info_agent.md) - Consultas informativas con RAG
- [CRMAgent](api/leadsales_agent.md) - Gestión de leads de ventas
- [LLMClient - Deuda Técnica](api/llm_client.md) - Wrapper de LangChain y análisis de duplicidad

### ✅ Reportes de Verificación
- [Verificación PR1](verification/pr1_verification.md) - Validación del refactor InfoAgent
- [Verificación PR2](verification/pr2_verification.md) - Validación de retry logic
- [Verificación PR3](verification/pr3_verification.md) - Validación de CRMAgent
- [Verificación Memoria de Sesión](verification/memory_verification.md) - Validación de persistencia de nombre

### 🧪 Testing
- [Pruebas Unitarias](testing/unit_tests.md) - Estrategia de unit tests
- [Pruebas de Integración](testing/integration_tests.md) - Tests end-to-end
- [Cobertura de Tests](testing/test_coverage.md) - Métricas de cobertura

### 🔧 Troubleshooting
- [Errores Comunes](troubleshooting/common_errors.md) - Problemas frecuentes y soluciones
- [Guía de Debugging](troubleshooting/debugging_guide.md) - Herramientas y técnicas

---

## 🚀 Inicio Rápido

Para entender el sistema rápidamente:

1. **Lee** [Visión General del Sistema](architecture/system_overview.md) para comprender la arquitectura
2. **Revisa** [Máquina de Estados](architecture/state_machine.md) para entender el flujo FSM
3. **Consulta** la documentación del agente que necesites modificar en la sección API

---

## 📌 Convenciones de Documentación

- **Diagramas**: Formato Mermaid (renderizables en GitHub)
- **Ejemplos de código**: Incluyen números de línea y referencias a archivos
- **Rutas de archivo**: Relativas a la raíz del proyecto
- **Formato**: Markdown con GitHub Flavored Markdown (GFM)

---

## 📂 Estructura del Proyecto

```
AgenteConversacional_Practica/
│
├── docs/                      # Esta documentación
│   ├── architecture/          # Diseño del sistema
│   ├── implementation/        # Guías de implementación
│   ├── api/                   # API de componentes
│   ├── verification/          # Reportes de verificación
│   ├── testing/               # Estrategias de testing
│   └── troubleshooting/       # Resolución de problemas
│
├── reception_agent.py         # Agente de clasificación
├── info_agent.py              # Agente de información (RAG)
├── crm_agent.py               # Agente CRM (stub)
├── main.py                    # Orquestador principal
├── state_manager.py           # Gestor de estado FSM
├── llm_client.py              # Wrapper de LangChain
└── tests/                     # Suite de pruebas
```

---

## 🔄 Estado Actual del Proyecto

**Última actualización**: 2025-11-12

### Implementaciones Completadas ✅

- ✅ PR 1: Refactor InfoAgent con `bind_tools()`
- ✅ PR 2: Retry logic en ReceptionAgent
- ✅ PR 3: CRMAgent stub implementation
- ✅ Memoria de sesión (persistencia de nombre de usuario)

### En Progreso 🔄

- 🔄 Documentación técnica completa
- 🔄 Pruebas de integración end-to-end

### Pendiente 📋

- 📋 Optimización de prompts para clasificación
- 📋 Implementación de historial de conversación
- 📋 Deploy en producción

---

## 🤝 Contribución

Para contribuir al proyecto:

1. Lee la documentación de arquitectura
2. Consulta las guías de implementación
3. Sigue las convenciones de código
4. Ejecuta tests antes de crear PRs
5. Actualiza la documentación si modificas funcionalidad

---

## 📞 Contacto y Soporte

Para preguntas o problemas:

- Consulta primero [Troubleshooting](troubleshooting/common_errors.md)
- Revisa los logs en `app.log`
- Utiliza el logging configurado en `logging_config.py`

---

**Versión de Documentación**: 1.0.0
**Última Revisión**: 2025-11-12
