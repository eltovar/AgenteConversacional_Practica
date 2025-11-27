# rag/vector_store.py
"""
Módulo de gestión del Vector Store con pgvector.
Proporciona la capa de abstracción para interactuar con PostgreSQL + pgvector.
"""

import os
from typing import Any, List, Optional, Dict
from langchain_postgres import PGVector
from langchain_core.documents import Document
from llm_client import embeddings
from logging_config import logger

class SimpleDocument:
    def __init__(self, page_content: str, metadata: Dict[str, Any]):
        self.page_content = page_content
        self.metadata = metadata

class PgVectorStore:
    def __init__(self, connection_string: str, collection_name: str, embedding_function: Any):
        self.connection_string = connection_string
        self.collection_name = collection_name
        self.embedding_function = embedding_function
        
        # === CRÍTICO: Bandera de estado interna para Inicialización Anticipada ===
        self._is_initialized = False 
        logger.info(f"[VectorStore] Inicializando {collection_name}. Estado: NO LISTO.")

    def initialize_db(self) -> None:
        """
        [CRÍTICO] Inicializa la conexión a la base de datos.
        Debe ser llamada al inicio del servidor.
        """
        try:
            # Lógica real de conexión y configuración de la tabla
            # (e.g., usando LangChain PGVector o lógica directa de psycopg)
            
            # --- SIMULACIÓN DE LA CONEXIÓN EXITOSA ---
            if self.connection_string: 
                # Conexión exitosa. Establecer el flag.
                self._is_initialized = True 
                logger.info("[VectorStore] Conexión a DB exitosa. Estado: LISTO.")
            else:
                raise ValueError("La cadena de conexión es inválida.")
            # ----------------------------------------

        except Exception as e:
            self._is_initialized = False
            logger.error(f"[VectorStore] Fallo al conectar o inicializar la DB: {e}")
            raise e


    def similarity_search(self, query: str, k: int = 5, filter: Dict[str, str] = None) -> List[SimpleDocument]:
        """
        Realiza la búsqueda de similitud.
        """
        # === CRÍTICO: Verificación de estado de inicialización ===
        if not self._is_initialized:
            logger.error("[VectorStore] Intento de búsqueda en un Vector store no inicializado.")
            # Este es el error que ve en los logs:
            raise RuntimeError("Vector store no inicializado") 
        # =========================================================

        # Lógica real de búsqueda de PgVectorStore (omitiendo detalles de LangChain/psycopg)
        logger.debug(f"[VectorStore] Ejecutando búsqueda por similitud (k={k}, filtro={filter})")

        # --- SIMULACIÓN DE BÚSQUEDA EXITOSA ---
        return [
            SimpleDocument("Contenido de prueba 1.", {"source": "doc/general.pdf"}),
            SimpleDocument("Contenido de prueba 2.", {"source": "doc/general.pdf"}),
        ]
        # ----------------------------------------
        
    def add_documents(self, documents: List[Document]) -> List[str]:
        """Añade documentos al vector store."""
        if not self._is_initialized:
            raise RuntimeError("Vector store no inicializado. No se pueden añadir documentos.")
        
        # Lógica real de indexación...
        logger.info(f"[VectorStore] Añadiendo {len(documents)} documentos...")
        return [f"id-{i}" for i in range(len(documents))]

    def clear_db(self) -> None:
        """Limpia todos los documentos del store."""
        # Se requiere la conexión a DB, pero la limpieza de PgVectorStore en rag_service.py
        # usa un método directo de psycopg, por lo que esta implementación es opcional aquí.
        pass

# Instancia global (Asegúrese de que el constructor reciba los parámetros reales)
pg_vector_store = PgVectorStore(
    connection_string="postgresql://...", # Sustituir por su valor real
    collection_name="rag_kb_collection",
    embedding_function="Su función de embeddings" # Sustituir por su función real
)