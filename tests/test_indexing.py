# tests/test_indexing.py
"""
Tests de carga, chunking e indexación de documentos en pgvector.
"""

import pytest
import os
from langchain_core.documents import Document
from rag.data_loader import load_and_chunk_documents, load_placeholder_documents
from rag.vector_store import PgVectorStore


def test_load_placeholder_documents():
    """
    Test: Verifica que se pueden cargar documentos placeholder.
    """
    docs = load_placeholder_documents()

    assert len(docs) == 1
    assert isinstance(docs[0], Document)
    assert "Misión" in docs[0].page_content
    assert docs[0].metadata["source"] == "placeholder"


def test_load_and_chunk_documents_empty_dir(tmp_path):
    """
    Test: Verifica comportamiento cuando el directorio está vacío.
    """
    empty_dir = tmp_path / "empty_knowledge"
    empty_dir.mkdir()

    chunks = load_and_chunk_documents(base_dir=str(empty_dir))

    assert chunks == []


def test_load_and_chunk_documents_with_files(tmp_path):
    """
    Test: Verifica carga y chunking de documentos reales.
    Assert: El número de chunks es >= al número de archivos.
    """
    # Crear directorio temporal con documentos
    kb_dir = tmp_path / "test_knowledge"
    kb_dir.mkdir()

    # Crear archivo de prueba 1
    file1 = kb_dir / "doc1.txt"
    content1 = "Este es un documento de prueba. " * 50  # ~1500 caracteres
    file1.write_text(content1, encoding='utf-8')

    # Crear archivo de prueba 2
    file2 = kb_dir / "doc2.txt"
    content2 = "Información importante sobre la empresa. " * 30  # ~1200 caracteres
    file2.write_text(content2, encoding='utf-8')

    # Cargar y chunkear con tamaño pequeño para forzar múltiples chunks
    chunks = load_and_chunk_documents(
        base_dir=str(kb_dir),
        chunk_size=500,
        chunk_overlap=100
    )

    # Asserts
    assert len(chunks) > 0
    assert len(chunks) >= 2  # Al menos 2 archivos, probablemente más chunks
    assert all(isinstance(chunk, Document) for chunk in chunks)

    # Verificar metadata
    for chunk in chunks:
        assert "source" in chunk.metadata
        assert "filename" in chunk.metadata
        assert chunk.metadata["filename"] in ["doc1.txt", "doc2.txt"]

    # Verificar que los chunks no están vacíos
    for chunk in chunks:
        assert len(chunk.page_content) > 0
        assert len(chunk.page_content) <= 500 + 100  # chunk_size + margen


def test_chunk_overlap():
    """
    Test: Verifica que el overlap funciona correctamente.
    """
    # Crear documento largo
    long_content = "Palabra " * 200  # ~1400 caracteres
    doc = Document(page_content=long_content, metadata={"source": "test"})

    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=100,
        length_function=len
    )

    chunks = splitter.split_documents([doc])

    # Debería haber múltiples chunks debido al tamaño
    assert len(chunks) > 1

    # Verificar que hay overlap (contenido repetido entre chunks consecutivos)
    if len(chunks) >= 2:
        # Los últimos ~100 caracteres del primer chunk deberían aparecer
        # en los primeros caracteres del segundo chunk
        chunk1_end = chunks[0].page_content[-50:]
        chunk2_start = chunks[1].page_content[:200]

        # Verificar que hay alguna superposición
        assert any(word in chunk2_start for word in chunk1_end.split())


def test_indexing_to_vector_store(tmp_path):
    """
    Test: Verifica la indexación completa en vector store.
    Assert: El número de documentos insertados coincide con los chunks esperados.

    NOTA: Este test requiere conexión a PostgreSQL (Railway).
          Se skipea automáticamente si se ejecuta localmente.
    """
    # Crear archivos de prueba
    kb_dir = tmp_path / "test_kb"
    kb_dir.mkdir()

    file1 = kb_dir / "info.txt"
    file1.write_text("Inmobiliaria Proteger. Contacto: 322 502 1493.", encoding='utf-8')

    file2 = kb_dir / "servicios.txt"
    file2.write_text("Ofrecemos apartamentos y casas en Medellín.", encoding='utf-8')

    # Cargar y chunkear
    chunks = load_and_chunk_documents(base_dir=str(kb_dir), chunk_size=200, chunk_overlap=50)

    expected_chunks = len(chunks)
    assert expected_chunks > 0

    try:
        # Inicializar vector store temporal
        vs = PgVectorStore(collection_name="test_indexing")
        vs.initialize_db()

        # Indexar documentos
        ids = vs.add_documents(chunks)

        # Assert: Número de IDs retornados coincide con chunks indexados
        assert len(ids) == expected_chunks
        assert all(isinstance(doc_id, str) for doc_id in ids)

        # Verificar que se pueden recuperar mediante búsqueda
        results = vs.similarity_search("Inmobiliaria", k=2)
        assert len(results) > 0
        assert any("Proteger" in doc.page_content or "Inmobiliaria" in doc.page_content
                   for doc in results)
    except Exception as e:
        # Skip si no hay conexión a PostgreSQL (ejecución local)
        if "could not translate host name" in str(e) or "Connection refused" in str(e):
            pytest.skip(f"Test requiere PostgreSQL (Railway). Error: {e}")
        else:
            raise  # Re-raise si es otro tipo de error


def test_indexing_with_metadata_preservation():
    """
    Test: Verifica que los metadatos se preservan durante la indexación.

    NOTA: Este test requiere conexión a PostgreSQL (Railway).
          Se skipea automáticamente si se ejecuta localmente.
    """
    # Crear documentos con metadata específica
    docs = [
        Document(
            page_content="Contenido del documento 1",
            metadata={"source": "test1.txt", "category": "info", "priority": "high"}
        ),
        Document(
            page_content="Contenido del documento 2",
            metadata={"source": "test2.txt", "category": "services", "priority": "medium"}
        )
    ]

    try:
        # Indexar
        vs = PgVectorStore(collection_name="test_metadata")
        vs.initialize_db()
        ids = vs.add_documents(docs)

        assert len(ids) == 2

        # Recuperar y verificar metadata
        results = vs.similarity_search("documento", k=2)

        assert len(results) == 2
        for doc in results:
            assert "source" in doc.metadata
            assert "category" in doc.metadata
            assert "priority" in doc.metadata
            assert doc.metadata["source"] in ["test1.txt", "test2.txt"]
    except Exception as e:
        # Skip si no hay conexión a PostgreSQL (ejecución local)
        if "could not translate host name" in str(e) or "Connection refused" in str(e):
            pytest.skip(f"Test requiere PostgreSQL (Railway). Error: {e}")
        else:
            raise  # Re-raise si es otro tipo de error


def test_chunk_size_limits():
    """
    Test: Verifica que los chunks respetan el tamaño máximo configurado.
    """
    # Crear documento muy largo
    very_long_content = "A" * 5000

    doc = Document(page_content=very_long_content, metadata={"source": "long.txt"})

    from langchain_text_splitters import RecursiveCharacterTextSplitter

    chunk_size = 500
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=50,
        length_function=len
    )

    chunks = splitter.split_documents([doc])

    # Verificar que ningún chunk excede el tamaño máximo (con margen de tolerancia)
    for chunk in chunks:
        # Permitir un pequeño margen debido a la lógica de separadores
        assert len(chunk.page_content) <= chunk_size + 50


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
