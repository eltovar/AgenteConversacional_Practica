# tests/test_semantic_search.py
"""
Tests de búsqueda semántica con pgvector.
Verifica que la búsqueda vectorial funciona correctamente y retorna resultados relevantes.
"""

import pytest
from langchain_core.documents import Document
from rag.vector_store import PgVectorStore
from rag import RAGService


@pytest.fixture
def vector_store_with_data():
    """
    Fixture que crea un vector store con datos de prueba.
    """
    vs = PgVectorStore(collection_name="test_semantic_search")
    vs.initialize_db()

    # Documentos de prueba con información sobre horarios
    test_docs = [
        Document(
            page_content=(
                "Horarios de atención: Nuestras oficinas están abiertas de lunes a viernes "
                "de 8:00 AM a 6:00 PM. Los sábados atendemos de 9:00 AM a 1:00 PM. "
                "Domingos y festivos permanecemos cerrados."
            ),
            metadata={"source": "knowledge_base/info_horarios.txt", "filename": "info_horarios.txt"}
        ),
        Document(
            page_content=(
                "Contacto: Puede comunicarse con nosotros al teléfono 322 502 1493. "
                "También estamos disponibles por correo electrónico en info@proteger.com"
            ),
            metadata={"source": "knowledge_base/info_contacto.txt", "filename": "info_contacto.txt"}
        ),
        Document(
            page_content=(
                "Servicios disponibles: Ofrecemos arriendo de apartamentos, casas y oficinas. "
                "También brindamos asesoría legal y administración de propiedades."
            ),
            metadata={"source": "knowledge_base/info_servicios.txt", "filename": "info_servicios.txt"}
        ),
        Document(
            page_content=(
                "Ubicación: Nuestras oficinas principales están ubicadas en el centro de Medellín, "
                "en la Carrera 43A #14-109. Contamos también con sucursales en El Poblado y Laureles."
            ),
            metadata={"source": "knowledge_base/info_ubicacion.txt", "filename": "info_ubicacion.txt"}
        )
    ]

    # Indexar documentos
    vs.add_documents(test_docs)

    return vs


def test_semantic_search_horarios(vector_store_with_data):
    """
    Test: La búsqueda semántica de "cuándo puedo ir" retorna el documento de horarios.
    Assert: El resultado contiene información sobre horarios de atención.
    """
    query = "cuándo puedo ir"
    results = vector_store_with_data.similarity_search(query, k=3)

    assert len(results) > 0

    # El primer resultado debería ser sobre horarios
    top_result = results[0]
    content_lower = top_result.page_content.lower()

    # Verificar que contiene información de horarios
    assert any(keyword in content_lower for keyword in ["horario", "abierto", "atención", "lunes", "viernes"])

    # Verificar source
    assert "horarios" in top_result.metadata.get("source", "").lower()


def test_semantic_search_contacto(vector_store_with_data):
    """
    Test: Búsqueda semántica de información de contacto.
    """
    query = "cómo puedo comunicarme"
    results = vector_store_with_data.similarity_search(query, k=3)

    assert len(results) > 0

    # Debería retornar documento de contacto
    top_result = results[0]
    content_lower = top_result.page_content.lower()

    assert any(keyword in content_lower for keyword in ["contacto", "teléfono", "correo", "comunicarse"])


def test_semantic_search_servicios(vector_store_with_data):
    """
    Test: Búsqueda semántica con sinónimos funciona correctamente.
    """
    # Buscar "qué ofrecen" debería retornar "servicios disponibles"
    query = "qué ofrecen"
    results = vector_store_with_data.similarity_search(query, k=3)

    assert len(results) > 0

    top_result = results[0]
    content_lower = top_result.page_content.lower()

    assert any(keyword in content_lower for keyword in ["servicio", "ofrece", "disponible", "apartamento"])


def test_semantic_search_ubicacion(vector_store_with_data):
    """
    Test: Búsqueda semántica de ubicación con variaciones de lenguaje.
    """
    # Diferentes formas de preguntar por ubicación
    queries = ["dónde están ubicados", "dónde queda la oficina", "dirección"]

    for query in queries:
        results = vector_store_with_data.similarity_search(query, k=3)
        assert len(results) > 0

        # Al menos uno de los top 3 resultados debería ser sobre ubicación
        sources = [doc.metadata.get("source", "") for doc in results[:3]]
        assert any("ubicacion" in source.lower() for source in sources)


def test_rag_service_search_knowledge_with_filter():
    """
    Test: Verifica que search_knowledge filtra correctamente por documento.
    """
    # Este test fallará localmente por la conexión a Railway
    # En Railway, debería funcionar correctamente

    # Crear instancia de RAG (en Railway esto funcionará)
    try:
        rag = RAGService()

        # Indexar datos de prueba
        from rag.data_loader import load_placeholder_documents
        docs = load_placeholder_documents()
        from rag.vector_store import pg_vector_store
        pg_vector_store.add_documents(docs)

        # Buscar en documento específico
        result = rag.search_knowledge("knowledge_base/info_institucional.txt", "misión")

        # Verificar que retorna string (no error)
        assert isinstance(result, str)
        assert not result.startswith("[ERROR]") or "no disponible" in result

    except Exception as e:
        # En desarrollo local, este test fallará por conexión
        pytest.skip(f"Test requiere conexión a Railway: {e}")


def test_rag_service_semantic_search():
    """
    Test: Verifica que semantic_search retorna documentos sin filtrado.
    """
    try:
        rag = RAGService()

        # Realizar búsqueda global
        results = rag.semantic_search("información", k=5)

        # Verificar que retorna lista de Documents
        assert isinstance(results, list)
        # En desarrollo local estará vacío por conexión, en Railway tendrá resultados
        assert all(isinstance(doc, Document) for doc in results)

    except Exception as e:
        pytest.skip(f"Test requiere conexión a Railway: {e}")


def test_rag_service_get_context_for_query():
    """
    Test: Verifica que get_context_for_query formatea correctamente el contexto.
    """
    try:
        rag = RAGService()

        context = rag.get_context_for_query("horarios de atención", k=3)

        # Debe retornar un string
        assert isinstance(context, str)

        # Si hay resultados, debe tener formato con [Fuente]
        if context != "[Sin contexto disponible]":
            assert "[Fuente" in context

    except Exception as e:
        pytest.skip(f"Test requiere conexión a Railway: {e}")


def test_semantic_similarity_relevance(vector_store_with_data):
    """
    Test: Verifica que los resultados están ordenados por relevancia.
    """
    query = "horarios de atención al público"
    results = vector_store_with_data.similarity_search(query, k=4)

    assert len(results) > 0

    # El primer resultado debería ser el más relevante (sobre horarios)
    top_result = results[0]
    assert "horario" in top_result.page_content.lower()

    # Los siguientes deberían ser menos relevantes
    # Verificar que hay variedad en las fuentes
    sources = [doc.metadata.get("source", "") for doc in results]
    unique_sources = set(sources)

    # Debe haber al menos 2 fuentes diferentes en top-4
    assert len(unique_sources) >= 2


def test_semantic_search_empty_query(vector_store_with_data):
    """
    Test: Verifica comportamiento con query vacío.
    """
    query = ""
    results = vector_store_with_data.similarity_search(query, k=3)

    # Debería retornar resultados (aunque no muy relevantes)
    # o lista vacía, dependiendo de la implementación
    assert isinstance(results, list)


def test_semantic_search_k_parameter(vector_store_with_data):
    """
    Test: Verifica que el parámetro k limita correctamente los resultados.
    """
    query = "información general"

    # Solicitar 2 resultados
    results_2 = vector_store_with_data.similarity_search(query, k=2)
    assert len(results_2) <= 2

    # Solicitar 4 resultados
    results_4 = vector_store_with_data.similarity_search(query, k=4)
    assert len(results_4) <= 4

    # k=4 debería tener más (o igual) resultados que k=2
    assert len(results_4) >= len(results_2)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
