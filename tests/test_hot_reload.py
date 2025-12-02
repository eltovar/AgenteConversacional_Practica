"""
Tests de integración para Hot-Reload de Base de Conocimiento.

Valida el flujo completo: RAGService → InfoAgent → Endpoint HTTP

NOTA: Tests refactorizados para arquitectura vectorial (PostgreSQL + pgvector).
      Los tests que validaban knowledge_map fueron eliminados (arquitectura obsoleta).
      La funcionalidad de hot-reload (A → B update) se valida en test_criterio2.py.
"""
import pytest
from rag.rag_service import rag_service
from Agents.InfoAgent.info_agent import agent
from fastapi.testclient import TestClient
from app import app


# ===== FIXTURES =====

@pytest.fixture
def client():
    """Cliente de prueba para FastAPI."""
    return TestClient(app)


# ===== TESTS UNITARIOS: RAGService =====

def test_ragservice_reload_method_exists():
    """Verificar que RAGService tiene método reload_knowledge_base()."""
    assert hasattr(rag_service, 'reload_knowledge_base')
    assert callable(rag_service.reload_knowledge_base)


def test_ragservice_reload_returns_dict():
    """
    Verificar que reload_knowledge_base() retorna diccionario con estructura correcta.

    NOTA: Este test requiere conexión a PostgreSQL (Railway).
          Se skipea automáticamente si se ejecuta localmente.
    """
    try:
        result = rag_service.reload_knowledge_base()

        assert isinstance(result, dict)
        assert "status" in result
        assert "chunks_indexed" in result
        assert "message" in result
        assert result["status"] in ["success", "error"]
    except ConnectionError as e:
        # Skip si no hay conexión a PostgreSQL (ejecución local)
        pytest.skip(f"Test requiere PostgreSQL (Railway). Error: {e}")


def test_ragservice_search_works_after_reload():
    """
    Test que verifica que search_knowledge() funciona correctamente después de reload.

    NOTA: Este test requiere conexión a PostgreSQL (Railway).
          Se skipea automáticamente si se ejecuta localmente.
    """
    try:
        # Recargar base de conocimiento
        rag_service.reload_knowledge_base()

        # Buscar en un documento conocido
        result = rag_service.search_knowledge(
            "knowledge_base/informacion_institucional.txt",
            "misión de la empresa"
        )

        # Verificar que no retorna error
        assert not result.startswith("[ERROR]")
        assert len(result) > 0
    except ConnectionError as e:
        # Skip si no hay conexión a PostgreSQL (ejecución local)
        pytest.skip(f"Test requiere PostgreSQL (Railway). Error: {e}")


# ===== TESTS UNITARIOS: InfoAgent =====

def test_infoagent_reload_method_exists():
    """Verificar que InfoAgent tiene método reload_knowledge_base()."""
    assert hasattr(agent, 'reload_knowledge_base')
    assert callable(agent.reload_knowledge_base)


def test_infoagent_reload_delegates_to_rag():
    """
    Verificar que InfoAgent.reload_knowledge_base() delega correctamente a RAGService.

    NOTA: Este test requiere conexión a PostgreSQL (Railway).
          Se skipea automáticamente si se ejecuta localmente.
    """
    try:
        result = agent.reload_knowledge_base()

        # Verificar estructura de retorno
        assert isinstance(result, dict)
        assert "status" in result
        assert "chunks_indexed" in result
        assert result["status"] == "success"
        assert result["chunks_indexed"] >= 0
    except ConnectionError as e:
        # Skip si no hay conexión a PostgreSQL (ejecución local)
        pytest.skip(f"Test requiere PostgreSQL (Railway). Error: {e}")


# ===== TESTS DE INTEGRACIÓN: API Endpoint =====

def test_api_endpoint_reload_success(client):
    """
    Test de integración que verifica endpoint /admin/reload-kb responde correctamente.

    NOTA: Este test requiere conexión a PostgreSQL (Railway).
          Se skipea automáticamente si se ejecuta localmente.
    """
    response = client.post("/admin/reload-kb")

    # Si el endpoint retorna 500, verificar si es por falta de conexión PostgreSQL
    if response.status_code == 500:
        error_detail = response.json().get("detail", "")
        if "could not translate host name" in error_detail or "PostgreSQL" in error_detail:
            pytest.skip(f"Test requiere PostgreSQL (Railway). Error: {error_detail}")
        else:
            # Si es otro tipo de error 500, fallar el test
            raise AssertionError(f"Endpoint retornó 500 con error inesperado: {error_detail}")

    # Verificar respuesta HTTP exitosa
    assert response.status_code == 200

    # Verificar estructura JSON
    data = response.json()
    assert data["status"] == "success"
    assert "chunks_indexed" in data
    assert data["chunks_indexed"] >= 0
    assert "message" in data


def test_api_endpoint_only_accepts_post(client):
    """Verificar que /admin/reload-kb solo acepta POST."""
    # GET debe fallar
    response_get = client.get("/admin/reload-kb")
    assert response_get.status_code == 405

    # PUT debe fallar
    response_put = client.put("/admin/reload-kb")
    assert response_put.status_code == 405

    # DELETE debe fallar
    response_delete = client.delete("/admin/reload-kb")
    assert response_delete.status_code == 405


# ===== TEST DE IDEMPOTENCIA =====

def test_reload_is_idempotent():
    """
    Test que verifica que múltiples recargas consecutivas producen el mismo resultado.

    NOTA: Este test requiere conexión a PostgreSQL (Railway).
          Se skipea automáticamente si se ejecuta localmente.
    """
    try:
        # Primera recarga
        result1 = rag_service.reload_knowledge_base()
        count1 = result1["chunks_indexed"]

        # Segunda recarga (sin cambios en disco)
        result2 = rag_service.reload_knowledge_base()
        count2 = result2["chunks_indexed"]

        # Tercera recarga
        result3 = rag_service.reload_knowledge_base()
        count3 = result3["chunks_indexed"]

        # Verificar que el contador es idéntico
        assert count1 == count2 == count3
        assert result1["status"] == result2["status"] == result3["status"] == "success"
    except ConnectionError as e:
        # Skip si no hay conexión a PostgreSQL (ejecución local)
        pytest.skip(f"Test requiere PostgreSQL (Railway). Error: {e}")
