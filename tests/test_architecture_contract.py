from utils.architecture_contract import (
    ARCHITECTURE_COMPONENTS,
    ARCHITECTURE_EDGES,
    DATA_DEPENDENCIES,
    DOMAIN_SOURCES_OF_TRUTH,
    EXTERNAL_PROVIDER_COMPONENTS,
    OWNED_DATA_COMPONENTS,
    SOURCE_COMPONENTS,
    architecture_contract_summary,
    data_dependency_keys_by_ownership,
    find_edges_for_component,
    validate_architecture_contract,
)
from utils.safe_logging import OBS_SOURCES


def test_architecture_contract_contains_critical_components():
    expected = {
        "twilio",
        "webhook",
        "sofia_brain",
        "openai",
        "redis",
        "mongo",
        "hubspot",
        "bunny",
        "panel",
        "websocket",
        "scheduler",
        "rag",
        "pgvector",
    }

    assert expected.issubset(ARCHITECTURE_COMPONENTS)


def test_architecture_edges_have_required_shape_and_evidence():
    assert validate_architecture_contract() == ()

    for edge in ARCHITECTURE_EDGES.values():
        assert edge.source in ARCHITECTURE_COMPONENTS
        assert edge.target in ARCHITECTURE_COMPONENTS
        assert edge.domain
        assert edge.evidence


def test_observability_sources_match_architecture_sources():
    assert SOURCE_COMPONENTS.issubset(OBS_SOURCES)


def test_rag_is_not_marked_as_production_validated():
    assert ARCHITECTURE_COMPONENTS["rag"].status == "produccion_no_verificada"
    assert ARCHITECTURE_COMPONENTS["pgvector"].status == "produccion_no_verificada"
    assert ARCHITECTURE_EDGES["rag_startup"].domain == "rag_startup"
    assert "produccion_no_verificada" in ARCHITECTURE_EDGES["rag_startup"].evidence


def test_architecture_summary_is_safe_and_filterable():
    summary = architecture_contract_summary()

    assert summary.startswith("[OBS] source=server component=architecture op=contract_loaded status=ok")
    assert "component_count=13" in summary
    assert "edge_count=10" in summary
    assert "components=text:len=" in summary
    assert "edges=text:len=" in summary
    assert "errors=none" in summary


def test_domain_sources_of_truth_are_explicit():
    assert DOMAIN_SOURCES_OF_TRUTH == {
        "estado_conversacional": "redis",
        "historial_operativo": "mongo",
        "crm_owner_etapa": "hubspot",
        "realtime_panel": "redis",
        "multimedia": "bunny",
        "rag": "pgvector",
    }


def test_component_lookup_connects_scheduler_and_panel():
    scheduler_edges = {edge.key for edge in find_edges_for_component("scheduler")}
    panel_edges = {edge.key for edge in find_edges_for_component("panel")}

    assert "scheduled_jobs" in scheduler_edges
    assert {"manual_response", "panel_contacts"}.issubset(panel_edges)


def test_data_dependencies_separate_owned_data_from_external_providers():
    assert OWNED_DATA_COMPONENTS == {"mongo", "pgvector", "redis"}
    assert EXTERNAL_PROVIDER_COMPONENTS == {"bunny", "hubspot", "openai", "twilio"}
    assert OWNED_DATA_COMPONENTS.isdisjoint(EXTERNAL_PROVIDER_COMPONENTS)

    for key, dependency in DATA_DEPENDENCIES.items():
        assert key in ARCHITECTURE_COMPONENTS
        assert dependency.role
        assert dependency.migration_policy
        assert dependency.evidence


def test_data_dependency_lookup_is_filterable_by_ownership():
    assert data_dependency_keys_by_ownership("owned") == ("mongo", "pgvector", "redis")
    assert data_dependency_keys_by_ownership("external") == ("bunny", "hubspot", "openai", "twilio")
