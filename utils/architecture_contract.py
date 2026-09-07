"""Contrato arquitectonico verificable de SofIA.

Este modulo no inicializa integraciones ni valida credenciales: solo declara la
red operativa esperada para que codigo, logs y documentacion hablen el mismo
idioma sin cambiar flujos de negocio.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Tuple

from utils.safe_logging import OBS_SOURCES, obs_event


Evidence = Tuple[str, ...]


@dataclass(frozen=True)
class ArchitectureComponent:
    key: str
    kind: str
    status: str
    evidence: Evidence


@dataclass(frozen=True)
class ArchitectureEdge:
    key: str
    source: str
    target: str
    domain: str
    evidence: Evidence


ARCHITECTURE_COMPONENTS: Dict[str, ArchitectureComponent] = {
    "twilio": ArchitectureComponent("twilio", "external", "codigo_verificado", ("utils.twilio_client", "middleware.webhook_handler")),
    "webhook": ArchitectureComponent("webhook", "internal", "codigo_verificado", ("middleware.webhook_handler",)),
    "sofia_brain": ArchitectureComponent("sofia_brain", "internal", "codigo_verificado", ("middleware.sofia_brain",)),
    "openai": ArchitectureComponent("openai", "external", "codigo_verificado", ("middleware.sofia_brain", "utils.media_processor")),
    "redis": ArchitectureComponent("redis", "state_cache_realtime", "codigo_verificado", ("middleware.conversation_state", "app.get_state_manager")),
    "mongo": ArchitectureComponent("mongo", "durable_history", "codigo_verificado", ("database.mongodb_client",)),
    "hubspot": ArchitectureComponent("hubspot", "external_crm", "codigo_verificado", ("integrations.hubspot", "middleware.contact_manager")),
    "bunny": ArchitectureComponent("bunny", "external_media", "codigo_verificado", ("utils.media_processor",)),
    "panel": ArchitectureComponent("panel", "internal_ui", "codigo_verificado", ("middleware.outbound_panel", "middleware.PanelAsesores")),
    "websocket": ArchitectureComponent("websocket", "realtime", "codigo_verificado", ("middleware.websocket_manager",)),
    "scheduler": ArchitectureComponent("scheduler", "internal_jobs", "codigo_verificado", ("app.scheduler", "utils.scheduler_observability")),
    "rag": ArchitectureComponent("rag", "internal_ai_context", "produccion_no_verificada", ("rag.rag_service", "agents.InfoAgent")),
    "pgvector": ArchitectureComponent("pgvector", "vector_store", "produccion_no_verificada", ("rag.vector_store", "app.startup_event")),
}


ARCHITECTURE_EDGES: Dict[str, ArchitectureEdge] = {
    "whatsapp_inbound": ArchitectureEdge(
        "whatsapp_inbound", "twilio", "webhook", "entrada_whatsapp",
        ("POST /whatsapp/webhook", "source=twilio", "tests/test_twilio_signature.py"),
    ),
    "bot_processing": ArchitectureEdge(
        "bot_processing", "webhook", "sofia_brain", "respuesta_automatica",
        ("middleware.webhook_handler.get_sofia_brain", "source=openai"),
    ),
    "manual_response": ArchitectureEdge(
        "manual_response", "panel", "twilio", "respuesta_manual",
        ("POST /whatsapp/panel/send", "utils.twilio_client", "source=twilio"),
    ),
    "panel_contacts": ArchitectureEdge(
        "panel_contacts", "panel", "redis", "estado_panel",
        ("GET /whatsapp/panel/contacts", "source=panel", "tests/test_panel_contacts_perf.py"),
    ),
    "history_persistence": ArchitectureEdge(
        "history_persistence", "webhook", "mongo", "historial",
        ("database.mongodb_client.save_message", "source=mongo"),
    ),
    "crm_sync": ArchitectureEdge(
        "crm_sync", "webhook", "hubspot", "crm",
        ("integrations.hubspot", "source=hubspot"),
    ),
    "media_storage": ArchitectureEdge(
        "media_storage", "webhook", "bunny", "multimedia",
        ("utils.media_processor.upload_to_bunny", "source=bunny"),
    ),
    "panel_realtime": ArchitectureEdge(
        "panel_realtime", "websocket", "redis", "realtime",
        ("middleware.websocket_manager", "Redis Pub/Sub", "source=redis"),
    ),
    "scheduled_jobs": ArchitectureEdge(
        "scheduled_jobs", "scheduler", "twilio", "jobs_operativos",
        ("app.scheduler.add_job", "utils.scheduler_observability", "source=scheduler"),
    ),
    "rag_startup": ArchitectureEdge(
        "rag_startup", "rag", "pgvector", "rag_startup",
        ("rag.rag_service.reload_knowledge_base", "rag.vector_store", "produccion_no_verificada"),
    ),
}


SOURCE_COMPONENTS = frozenset({
    "twilio",
    "hubspot",
    "bunny",
    "mongo",
    "redis",
    "openai",
    "panel",
    "scheduler",
    "server",
})


DOMAIN_SOURCES_OF_TRUTH = {
    "estado_conversacional": "redis",
    "historial_operativo": "mongo",
    "crm_owner_etapa": "hubspot",
    "realtime_panel": "redis",
    "multimedia": "bunny",
    "rag": "pgvector",
}


def component_keys() -> Tuple[str, ...]:
    return tuple(sorted(ARCHITECTURE_COMPONENTS))


def edge_keys() -> Tuple[str, ...]:
    return tuple(sorted(ARCHITECTURE_EDGES))


def validate_architecture_contract() -> Tuple[str, ...]:
    errors = []
    for key, edge in ARCHITECTURE_EDGES.items():
        if edge.source not in ARCHITECTURE_COMPONENTS:
            errors.append(f"{key}:source:{edge.source}")
        if edge.target not in ARCHITECTURE_COMPONENTS:
            errors.append(f"{key}:target:{edge.target}")
        if not edge.domain:
            errors.append(f"{key}:domain")
        if not edge.evidence:
            errors.append(f"{key}:evidence")

    missing_sources = SOURCE_COMPONENTS.difference(OBS_SOURCES)
    if missing_sources:
        errors.append(f"obs_sources:{','.join(sorted(missing_sources))}")

    return tuple(errors)


def architecture_contract_summary() -> str:
    errors = validate_architecture_contract()
    return obs_event(
        "server",
        "architecture",
        "contract_loaded",
        status="ok" if not errors else "invalid",
        component_count=len(ARCHITECTURE_COMPONENTS),
        edge_count=len(ARCHITECTURE_EDGES),
        components=";".join(component_keys()),
        edges=";".join(edge_keys()),
        errors=";".join(errors) if errors else "none",
    )


def find_edges_for_component(component: str) -> Tuple[ArchitectureEdge, ...]:
    return tuple(
        edge for edge in ARCHITECTURE_EDGES.values()
        if edge.source == component or edge.target == component
    )


def statuses_for(components: Iterable[str]) -> Dict[str, str]:
    return {
        key: ARCHITECTURE_COMPONENTS[key].status
        for key in components
        if key in ARCHITECTURE_COMPONENTS
    }
