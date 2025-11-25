# tests/test_vector_store.py
"""
Tests de conexión y funcionalidad básica del Vector Store con pgvector.
"""

import pytest
from langchain_core.documents import Document
from rag.vector_store import PgVectorStore


def test_vector_store_initialization():
    """
    Test: Verifica que el vector store se inicializa correctamente.
    """
    vs = PgVectorStore(collection_name="test_collection")
    assert vs.collection_name == "test_collection"
    assert vs.connection_string is not None
    assert vs.vector_store is None  # No inicializado aún


def test_database_connection():
    """
    Test: Verifica que la conexión a PostgreSQL es exitosa.
    Assert: La conexión se establece sin errores.

    NOTA: Este test requiere conexión a PostgreSQL (Railway).
          Se skipea automáticamente si se ejecuta localmente.
    """
    try:
        vs = PgVectorStore(collection_name="test_knowledge_base")
        result = vs.initialize_db()

        assert result is True
        assert vs.vector_store is not None
    except Exception as e:
        # Skip si no hay conexión a PostgreSQL (ejecución local)
        if "could not translate host name" in str(e) or "Connection refused" in str(e):
            pytest.skip(f"Test requiere PostgreSQL (Railway). Error: {e}")
        else:
            raise  # Re-raise si es otro tipo de error


def test_table_creation():
    """
    Test: Verifica que la tabla de vectores se crea correctamente.
    Assert: La tabla existe después de initialize_db().

    NOTA: Este test requiere conexión a PostgreSQL (Railway).
          Se skipea automáticamente si se ejecuta localmente.
    """
    try:
        vs = PgVectorStore(collection_name="test_vectors")
        vs.initialize_db()

        # Si llegamos aquí sin excepciones, la tabla fue creada
        vector_store = vs.get_vector_store()
        assert vector_store is not None
    except Exception as e:
        # Skip si no hay conexión a PostgreSQL (ejecución local)
        if "could not translate host name" in str(e) or "Connection refused" in str(e):
            pytest.skip(f"Test requiere PostgreSQL (Railway). Error: {e}")
        else:
            raise  # Re-raise si es otro tipo de error


def test_get_vector_store_before_init():
    """
    Test: Verifica que get_vector_store() lanza error si no está inicializado.
    """
    vs = PgVectorStore()

    with pytest.raises(RuntimeError, match="Vector store no inicializado"):
        vs.get_vector_store()


def test_add_documents():
    """
    Test: Verifica que se pueden agregar documentos al vector store.

    NOTA: Este test requiere conexión a PostgreSQL (Railway).
          Se skipea automáticamente si se ejecuta localmente.
    """
    try:
        vs = PgVectorStore(collection_name="test_add_docs")
        vs.initialize_db()

        # Crear documentos de prueba
        test_docs = [
            Document(
                page_content="Inmobiliaria Proteger ofrece apartamentos en Medellín.",
                metadata={"source": "test", "category": "info"}
            ),
            Document(
                page_content="Tenemos casas disponibles en el poblado.",
                metadata={"source": "test", "category": "properties"}
            )
        ]

        ids = vs.add_documents(test_docs)

        assert len(ids) == 2
        assert all(isinstance(doc_id, str) for doc_id in ids)
    except Exception as e:
        # Skip si no hay conexión a PostgreSQL (ejecución local)
        if "could not translate host name" in str(e) or "Connection refused" in str(e):
            pytest.skip(f"Test requiere PostgreSQL (Railway). Error: {e}")
        else:
            raise  # Re-raise si es otro tipo de error


def test_similarity_search():
    """
    Test: Verifica que la búsqueda por similitud funciona correctamente.

    NOTA: Este test requiere conexión a PostgreSQL (Railway).
          Se skipea automáticamente si se ejecuta localmente.
    """
    try:
        vs = PgVectorStore(collection_name="test_similarity")
        vs.initialize_db()

        # Agregar documentos
        test_docs = [
            Document(page_content="Apartamentos en Medellín zona norte"),
            Document(page_content="Casas campestres en Rionegro"),
            Document(page_content="Oficinas en el centro de la ciudad")
        ]
        vs.add_documents(test_docs)

        # Realizar búsqueda semántica
        query = "Quiero un apartamento en Medellín"
        results = vs.similarity_search(query, k=2)

        assert len(results) <= 2
        assert all(isinstance(doc, Document) for doc in results)
        # El resultado más relevante debería contener "Apartamentos" o "Medellín"
        assert any("Medellín" in doc.page_content or "Apartamentos" in doc.page_content
                   for doc in results)
    except Exception as e:
        # Skip si no hay conexión a PostgreSQL (ejecución local)
        if "could not translate host name" in str(e) or "Connection refused" in str(e):
            pytest.skip(f"Test requiere PostgreSQL (Railway). Error: {e}")
        else:
            raise  # Re-raise si es otro tipo de error


def test_similarity_search_before_init():
    """
    Test: Verifica que similarity_search() falla si no está inicializado.
    """
    vs = PgVectorStore()

    with pytest.raises(RuntimeError, match="Vector store no inicializado"):
        vs.similarity_search("test query")


def test_add_documents_before_init():
    """
    Test: Verifica que add_documents() falla si no está inicializado.
    """
    vs = PgVectorStore()
    test_doc = [Document(page_content="Test content")]

    with pytest.raises(RuntimeError, match="Vector store no inicializado"):
        vs.add_documents(test_doc)


if __name__ == "__main__":
    # Permite ejecutar los tests directamente
    pytest.main([__file__, "-v"])
