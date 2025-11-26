# rag/vector_store.py
"""
Módulo de gestión del Vector Store con pgvector.
Proporciona la capa de abstracción para interactuar con PostgreSQL + pgvector.
"""

import os
from typing import List, Optional
from langchain_postgres import PGVector
from langchain_core.documents import Document
from llm_client import embeddings
from logging_config import logger


class PgVectorStore:
    """
    Clase para gestionar la conexión y operaciones con pgvector.
    """

    def __init__(self, collection_name: str = "knowledge_base"):
        """
        Inicializa el vector store.

        Args:
            collection_name: Nombre de la colección/tabla en PostgreSQL
        """
        self.collection_name = collection_name
        self.connection_string = os.getenv("DATABASE_URL")

        if not self.connection_string:
            raise ValueError("DATABASE_URL no encontrada en variables de entorno")

        self.vector_store: Optional[PGVector] = None
        logger.info(f"[PgVectorStore] Inicializado con collection_name='{collection_name}'")

    def initialize_db(self) -> bool:
        """
        Inicializa la base de datos y crea la tabla de vectores si no existe.

        Returns:
            bool: True si la inicialización fue exitosa
        """
        try:
            logger.info("[PgVectorStore] Inicializando conexión a PostgreSQL...")

            # Crear instancia de PGVector (esto crea la tabla automáticamente)
            self.vector_store = PGVector(
                embeddings=embeddings,
                collection_name=self.collection_name,
                connection=self.connection_string,
                use_jsonb=True,
            )

            logger.info(f"[PgVectorStore] Tabla '{self.collection_name}' verificada/creada exitosamente")
            return True

        except Exception as e:
            logger.error(f"[PgVectorStore] Error al inicializar base de datos: {e}", exc_info=True)
            raise

    def get_vector_store(self) -> PGVector:
        """
        Retorna la instancia del vector store.

        Returns:
            PGVector: Instancia configurada del vector store

        Raises:
            RuntimeError: Si el vector store no ha sido inicializado
        """
        if self.vector_store is None:
            raise RuntimeError(
                "Vector store no inicializado. Llama a initialize_db() primero."
            )

        return self.vector_store

    def add_documents(self, documents: List[Document]) -> List[str]:
        """
        Agrega documentos al vector store.

        Args:
            documents: Lista de documentos de LangChain

        Returns:
            List[str]: IDs de los documentos agregados
        """
        if self.vector_store is None:
            raise RuntimeError("Vector store no inicializado")

        logger.info(f"[PgVectorStore] Agregando {len(documents)} documentos...")
        ids = self.vector_store.add_documents(documents)
        logger.info(f"[PgVectorStore] {len(ids)} documentos agregados exitosamente")
        return ids

    def similarity_search(self, query: str, k: int = 5, filter: Optional[dict] = None) -> List[Document]:
        """
        Realiza búsqueda por similitud semántica.

        Args:
            query: Texto de búsqueda
            k: Número de resultados a retornar
            filter: Filtro opcional para metadata (ej: {"source": "path/to/doc.txt"})

        Returns:
            List[Document]: Documentos más similares
        """
        if self.vector_store is None:
            raise RuntimeError("Vector store no inicializado")

        logger.debug(f"[PgVectorStore] Búsqueda de similitud: '{query}' (top-{k}, filter={filter})")

        # PGVector soporta filtrado nativo por metadata
        if filter:
            results = self.vector_store.similarity_search(query, k=k, filter=filter)
        else:
            results = self.vector_store.similarity_search(query, k=k)

        logger.debug(f"[PgVectorStore] {len(results)} resultados encontrados")
        return results


# Instancia global (Singleton)
pg_vector_store = PgVectorStore()
