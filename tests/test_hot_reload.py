"""
Tests de integración para Hot-Reload de Base de Conocimiento.

Valida el flujo completo: RAGService → InfoAgent → Endpoint HTTP
"""
import pytest
import os
from rag import rag_service
from info_agent import agent
from fastapi.testclient import TestClient
from app import app


# ===== FIXTURES =====

@pytest.fixture
def client():
    """Cliente de prueba para FastAPI."""
    return TestClient(app)


@pytest.fixture
def temp_knowledge_file():
    """
    Fixture que crea un archivo temporal en knowledge_base/ para testing.
    Se limpia automáticamente después del test.
    """
    test_file = "knowledge_base/test_hotreload_fixture.txt"

    # Setup: crear archivo
    with open(test_file, 'w', encoding='utf-8') as f:
        f.write("CONTENIDO DE PRUEBA HOTRELOAD\n")
        f.write("Palabra clave única: FIXTURE_HOTRELOAD_2024\n")
        f.write("Este archivo es temporal para testing.")

    yield test_file  # Proveer path al test

    # Teardown: eliminar archivo si existe
    if os.path.exists(test_file):
        os.remove(test_file)


# ===== TESTS UNITARIOS: RAGService =====

def test_ragservice_reload_method_exists():
    """Verificar que RAGService tiene método reload_knowledge_base()."""
    assert hasattr(rag_service, 'reload_knowledge_base')
    assert callable(rag_service.reload_knowledge_base)


def test_ragservice_reload_returns_dict():
    """Verificar que reload_knowledge_base() retorna diccionario con estructura correcta."""
    result = rag_service.reload_knowledge_base()

    assert isinstance(result, dict)
    assert "status" in result
    assert "files_loaded" in result
    assert "message" in result
    assert result["status"] in ["success", "error"]


def test_ragservice_reload_detects_new_file(temp_knowledge_file):
    """
    Test que verifica que RAGService detecta archivos nuevos después de reload.

    Flujo:
    1. Verificar que archivo temporal NO está en memoria
    2. Llamar a reload_knowledge_base()
    3. Verificar que archivo AHORA SÍ está en memoria
    """
    normalized_path = temp_knowledge_file.replace("\\", "/")

    # Estado inicial: archivo no debe estar en memoria (fue creado después del __init__)
    assert normalized_path not in rag_service.knowledge_map

    # Ejecutar recarga
    result = rag_service.reload_knowledge_base()
    assert result["status"] == "success"

    # Verificar que ahora SÍ está en memoria
    assert normalized_path in rag_service.knowledge_map

    # Verificar contenido
    content = rag_service.knowledge_map[normalized_path]
    assert "FIXTURE_HOTRELOAD_2024" in content


def test_ragservice_reload_removes_deleted_files(temp_knowledge_file):
    """
    Test que verifica que RAGService elimina archivos borrados después de reload.

    Flujo:
    1. Cargar archivo temporal con reload
    2. Eliminar archivo del disco
    3. Reload de nuevo
    4. Verificar que ya NO está en memoria
    """
    normalized_path = temp_knowledge_file.replace("\\", "/")

    # Cargar archivo
    rag_service.reload_knowledge_base()
    assert normalized_path in rag_service.knowledge_map

    # Eliminar archivo del disco
    os.remove(temp_knowledge_file)

    # Recargar de nuevo
    result = rag_service.reload_knowledge_base()
    assert result["status"] == "success"

    # Verificar que ya NO está en memoria
    assert normalized_path not in rag_service.knowledge_map


def test_ragservice_search_works_after_reload(temp_knowledge_file):
    """
    Test que verifica que search_knowledge() funciona correctamente después de reload.
    """
    normalized_path = temp_knowledge_file.replace("\\", "/")

    # Recargar para incluir archivo temporal
    rag_service.reload_knowledge_base()

    # Buscar contenido
    result = rag_service.search_knowledge(normalized_path, "FIXTURE_HOTRELOAD_2024")

    # Verificar que encontró el contenido
    assert "FIXTURE_HOTRELOAD_2024" in result
    assert not result.startswith("[ERROR]")


# ===== TESTS UNITARIOS: InfoAgent =====

def test_infoagent_reload_method_exists():
    """Verificar que InfoAgent tiene método reload_knowledge_base()."""
    assert hasattr(agent, 'reload_knowledge_base')
    assert callable(agent.reload_knowledge_base)


def test_infoagent_reload_delegates_to_rag():
    """Verificar que InfoAgent.reload_knowledge_base() delega correctamente a RAGService."""
    result = agent.reload_knowledge_base()

    # Verificar estructura de retorno
    assert isinstance(result, dict)
    assert "status" in result
    assert "files_loaded" in result
    assert result["status"] == "success"
    assert result["files_loaded"] >= 0


# ===== TESTS DE INTEGRACIÓN: API Endpoint =====

def test_api_endpoint_reload_success(client):
    """
    Test de integración que verifica endpoint /admin/reload-kb responde correctamente.
    """
    response = client.post("/admin/reload-kb")

    # Verificar respuesta HTTP
    assert response.status_code == 200

    # Verificar estructura JSON
    data = response.json()
    assert data["status"] == "success"
    assert "files_loaded" in data
    assert data["files_loaded"] >= 0
    assert "message" in data


def test_api_endpoint_reload_integrates_with_rag(client, temp_knowledge_file):
    """
    Test end-to-end que verifica el flujo completo:
    HTTP POST → InfoAgent → RAGService → Detección de archivo nuevo

    Este es el test más importante: valida toda la cadena de hot-reload.
    """
    normalized_path = temp_knowledge_file.replace("\\", "/")

    # Estado inicial: archivo no está en memoria
    initial_count = len(rag_service.knowledge_map)
    assert normalized_path not in rag_service.knowledge_map

    # Llamar al endpoint HTTP
    response = client.post("/admin/reload-kb")

    # Verificar respuesta exitosa
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"

    # Verificar que el contador de archivos aumentó
    assert data["files_loaded"] == initial_count + 1

    # Verificar que el archivo nuevo AHORA SÍ está en memoria
    assert normalized_path in rag_service.knowledge_map

    # Verificar contenido
    content = rag_service.knowledge_map[normalized_path]
    assert "FIXTURE_HOTRELOAD_2024" in content


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


# ===== TEST DE CONSISTENCIA =====

def test_reload_count_consistency():
    """
    Test que verifica consistencia entre el contador reportado y archivos reales.
    """
    result = rag_service.reload_knowledge_base()

    reported_count = result["files_loaded"]
    actual_count = len(rag_service.knowledge_map)

    assert reported_count == actual_count, \
        f"Inconsistencia: reportado={reported_count}, real={actual_count}"


# ===== TEST DE IDEMPOTENCIA =====

def test_reload_is_idempotent():
    """
    Test que verifica que múltiples recargas consecutivas producen el mismo resultado.
    """
    # Primera recarga
    result1 = rag_service.reload_knowledge_base()
    count1 = result1["files_loaded"]

    # Segunda recarga (sin cambios en disco)
    result2 = rag_service.reload_knowledge_base()
    count2 = result2["files_loaded"]

    # Tercera recarga
    result3 = rag_service.reload_knowledge_base()
    count3 = result3["files_loaded"]

    # Verificar que el contador es idéntico
    assert count1 == count2 == count3
    assert result1["status"] == result2["status"] == result3["status"] == "success"
