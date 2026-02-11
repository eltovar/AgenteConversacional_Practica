"""
Tests para endpoint administrativo /admin/reload-kb (Hot-Reload de RAG).

Utiliza TestClient de FastAPI para simular peticiones HTTP sin levantar servidor.
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from app import app


@pytest.fixture
def client():
    """Fixture que proporciona un cliente de prueba para FastAPI."""
    return TestClient(app)


# ===== TEST 1: Recarga exitosa (200 OK) =====

def test_reload_kb_success(client):
    """
    Test que verifica respuesta exitosa del endpoint /admin/reload-kb.
    Mockea agent.reload_knowledge_base() para retornar éxito.
    """
    # Mock del método reload_knowledge_base de InfoAgent
    mock_result = {
        "status": "success",
        "files_loaded": 14,
        "message": "Base de conocimiento actualizada. 14 archivos cargados."
    }

    with patch('app.agent.reload_knowledge_base', return_value=mock_result):
        response = client.post("/admin/reload-kb")

    # Verificar código de respuesta
    assert response.status_code == 200

    # Verificar estructura del JSON
    data = response.json()
    assert data["status"] == "success"
    assert data["files_loaded"] == 14
    assert "Base de conocimiento actualizada" in data["message"]


# ===== TEST 2: Error controlado desde RAGService (500) =====

def test_reload_kb_controlled_error(client):
    """
    Test que verifica manejo de errores controlados desde RAGService.
    Simula un fallo en la carga de archivos.
    """
    mock_result = {
        "status": "error",
        "message": "Fallo al recargar: No se encontraron archivos .txt"
    }

    with patch('app.agent.reload_knowledge_base', return_value=mock_result):
        response = client.post("/admin/reload-kb")

    # Verificar código de respuesta
    assert response.status_code == 500

    # Verificar mensaje de error
    data = response.json()
    assert "detail" in data
    assert "No se encontraron archivos" in data["detail"]


# ===== TEST 3: Excepción inesperada (500) =====

def test_reload_kb_unexpected_exception(client):
    """
    Test que verifica manejo de excepciones inesperadas.
    Simula un error crítico durante la ejecución.
    """
    with patch('app.agent.reload_knowledge_base', side_effect=RuntimeError("Error crítico simulado")):
        response = client.post("/admin/reload-kb")

    # Verificar código de respuesta
    assert response.status_code == 500

    # Verificar mensaje de error
    data = response.json()
    assert "detail" in data
    assert "Error interno" in data["detail"]


# ===== TEST 4: Endpoint existe y es POST =====

def test_reload_kb_method_not_allowed(client):
    """
    Test que verifica que el endpoint solo acepta POST.
    GET debe retornar 405 Method Not Allowed.
    """
    response = client.get("/admin/reload-kb")
    assert response.status_code == 405


# ===== TEST 5: Endpoint documentado en raíz =====

def test_admin_endpoint_listed_in_root(client):
    """
    Test que verifica que el endpoint administrativo está listado en GET /.
    """
    response = client.get("/")
    assert response.status_code == 200

    data = response.json()
    assert "endpoints" in data
    assert "admin_reload" in data["endpoints"]
    assert "/admin/reload-kb" in data["endpoints"]["admin_reload"]


# ===== TEST 6: Integración real (sin mock) =====

@pytest.mark.integration
def test_reload_kb_real_integration(client):
    """
    Test de integración que llama al endpoint real sin mocks.
    Requiere que knowledge_base/ exista con archivos.

    Marca: @pytest.mark.integration (ejecutar con: pytest -m integration)
    """
    response = client.post("/admin/reload-kb")

    # Verificar que la operación se completó (éxito o error controlado)
    assert response.status_code in [200, 500]

    data = response.json()

    if response.status_code == 200:
        # Caso éxito
        assert data["status"] == "success"
        assert "files_loaded" in data
        assert data["files_loaded"] >= 0
    else:
        # Caso error controlado
        assert "detail" in data