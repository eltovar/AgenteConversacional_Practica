# rag/__init__.py
"""
Módulo RAG (Retrieval-Augmented Generation) con búsqueda semántica.
"""

from rag.rag_service import rag_service, RAGService
from rag.vector_store import pg_vector_store, PgVectorStore
from rag.data_loader import load_and_chunk_documents, load_placeholder_documents

__all__ = [
    "rag_service",
    "RAGService",
    "pg_vector_store",
    "PgVectorStore",
    "load_and_chunk_documents",
    "load_placeholder_documents",
]