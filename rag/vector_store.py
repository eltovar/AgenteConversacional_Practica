# rag/vector_store.py
"""
Módulo de gestión del Vector Store con pgvector.
Proporciona la capa de abstracción para interactuar con PostgreSQL + pgvector usando LangChain.
"""

import os
from typing import Any, List, Optional, Dict
from langchain_postgres import PGVector
from langchain_core.documents import Document
from llm_client import embeddings
from logging_config import logger


class PgVectorStore:
    """
    Wrapper para LangChain PGVector con gestión de estado de inicialización.
    """

    def __init__(self, connection_string: str, collection_name: str, embedding_function: Any):
        self.connection_string = connection_string
        self.collection_name = collection_name
        self.embedding_function = embedding_function

        # Instancia real de PGVector (se inicializará en initialize_db)
        self.vector_db: Optional[PGVector] = None

        # Bandera de estado interna para Inicialización Anticipada
        self._is_initialized = False
        logger.info(f"[VectorStore] PgVectorStore creado para colección '{collection_name}'. Estado: NO LISTO.")

    def initialize_db(self) -> None:
        """
        [CRÍTICO] Inicializa la conexión REAL a PostgreSQL usando LangChain PGVector.
        Debe ser llamada al inicio del servidor (startup event).
        """
        try:
            logger.info("[VectorStore] Inicializando conexión REAL a PostgreSQL + pgvector...")

            # Validar connection string
            if not self.connection_string:
                raise ValueError("DATABASE_URL (connection_string) no está configurada.")

            # Normalizar connection string: postgres:// → postgresql://
            # SQLAlchemy requiere 'postgresql' como dialecto, no 'postgres'
            normalized_connection = self.connection_string.replace("postgres://", "postgresql://")

            if normalized_connection != self.connection_string:
                logger.info("[VectorStore] Connection string normalizada: postgres:// → postgresql://")

            # Crear instancia REAL de PGVector de LangChain
            self.vector_db = PGVector(
                connection=normalized_connection,
                collection_name=self.collection_name,
                embeddings=self.embedding_function,
                use_jsonb=True  # Usar JSONB para metadata (más eficiente)
            )

            # Verificar conexión ejecutando una operación simple
            # (PGVector crea las tablas automáticamente si no existen)
            logger.info("[VectorStore] PGVector inicializado. Verificando tablas...")

            # Marcar como inicializado
            self._is_initialized = True
            logger.info("[VectorStore] ✅ Conexión a DB exitosa. Estado: LISTO.")

        except Exception as e:
            self._is_initialized = False
            logger.error(f"[VectorStore] ❌ Fallo al conectar o inicializar la DB: {e}", exc_info=True)
            raise ConnectionError(f"No se pudo inicializar PGVector: {e}") from e

    def similarity_search(
        self,
        query: str,
        k: int = 5,
        filter: Optional[Dict[str, str]] = None
    ) -> List[Document]:
        """
        Realiza la búsqueda de similitud REAL usando PGVector.

        Args:
            query: Texto de consulta
            k: Número de resultados a retornar
            filter: Filtro de metadata (ej: {"source": "knowledge_base/doc.txt"})

        Returns:
            Lista de documentos LangChain (Document objects)
        """
        # Verificación de estado de inicialización
        if not self._is_initialized or self.vector_db is None:
            logger.error("[VectorStore] Intento de búsqueda en un Vector store no inicializado.")
            raise RuntimeError("Vector store no inicializado. Llame a initialize_db() primero.")

        try:
            logger.debug(f"[VectorStore] Búsqueda de similitud: query='{query[:50]}...', k={k}, filter={filter}")

            # Ejecutar búsqueda REAL con PGVector
            results = self.vector_db.similarity_search(
                query=query,
                k=k,
                filter=filter
            )

            logger.debug(f"[VectorStore] Encontrados {len(results)} documentos")
            return results

        except Exception as e:
            logger.error(f"[VectorStore] Error durante búsqueda de similitud: {e}", exc_info=True)
            raise

    def add_documents(self, documents: List[Document]) -> List[str]:
        """
        Añade documentos REALES al vector store usando PGVector.

        Args:
            documents: Lista de documentos LangChain (Document objects)

        Returns:
            Lista de IDs de los documentos añadidos
        """
        if not self._is_initialized or self.vector_db is None:
            raise RuntimeError("Vector store no inicializado. No se pueden añadir documentos.")

        try:
            logger.info(f"[VectorStore] Añadiendo {len(documents)} documentos al vector store...")

            # Indexar documentos REALES con PGVector
            ids = self.vector_db.add_documents(documents)

            logger.info(f"[VectorStore] ✅ {len(ids)} documentos indexados correctamente")
            return ids

        except Exception as e:
            logger.error(f"[VectorStore] Error al añadir documentos: {e}", exc_info=True)
            raise

    def delete_collection(self) -> None:
        """
        Elimina completamente la colección (DROP TABLE).
        Útil para limpieza completa antes de re-indexación.
        """
        if not self._is_initialized or self.vector_db is None:
            logger.warning("[VectorStore] No se puede eliminar colección - vector store no inicializado")
            return

        try:
            logger.info(f"[VectorStore] Eliminando colección '{self.collection_name}'...")
            self.vector_db.delete_collection()
            logger.info("[VectorStore] Colección eliminada exitosamente")
        except Exception as e:
            logger.warning(f"[VectorStore] Error al eliminar colección: {e}")


# ===== INSTANCIA GLOBAL =====

# Obtener configuración desde variables de entorno
DATABASE_URL = os.getenv("DATABASE_URL")
COLLECTION_NAME = os.getenv("VECTOR_COLLECTION_NAME", "rag_knowledge_base")

# Crear instancia global del vector store
pg_vector_store = PgVectorStore(
    connection_string=DATABASE_URL,
    collection_name=COLLECTION_NAME,
    embedding_function=embeddings  # Importado desde llm_client
)