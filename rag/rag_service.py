"""
Servicio de RAG (Retrieval-Augmented Generation) con búsqueda semántica.
Usa pgvector para almacenamiento de embeddings y búsqueda por similitud.
"""

import time
from typing import Dict, List, Any

import psycopg
from langchain_core.documents import Document
from logging_config import logger
from rag.data_loader import load_and_chunk_documents, load_placeholder_documents
from rag.vector_store import pg_vector_store

# ===== LISTA DE NÚMEROS OBSOLETOS =====
# Estos números fueron reemplazados y NO deben aparecer en respuestas
OBSOLETE_PHONE_NUMBERS = [
    "322 502 1493",  # Número viejo del área de contratos
    "3225021493",    # Sin espacios
    "+573225021493", # Con código país
]


class RAGService:
    """
    Servicio central de RAG. Orquesta la búsqueda y gestión de la Base de Conocimiento (KB).

    NOTA CRÍTICA: Se eliminó la inicialización perezosa (lazy) para asegurar que
    la carga e indexación pesada ocurra en el startup del servidor, evitando timeouts.
    """
    KNOWLEDGE_BASE_DIR = "knowledge_base"
    CHUNK_SIZE = 500
    CHUNK_OVERLAP = 100

    def __init__(self):
        """
        La inicialización pesada de la DB y la carga de KB
        se debe hacer explícitamente a través de reload_knowledge_base()
        en el evento de startup del servidor (ver app.py).
        """
        logger.info("[RAG] RAGService inicializado. La Base de Conocimiento debe ser cargada al inicio.")

    def reload_knowledge_base(self) -> Dict[str, Any]:
        """
        Función pública para forzar la carga e indexación de la Base de Conocimiento.
        Debe ser llamada durante el inicio del servidor (startup).
        """
        start_time = time.time()
        logger.info(f"[RAG] Iniciando recarga e indexación completa de la KB desde {self.KNOWLEDGE_BASE_DIR}...")

        try:
            # 1. Asegurar que la DB esté conectada (inicializar PgVectorStore)
            pg_vector_store.initialize_db()

            # Si PGVector no está inicializado (p.ej. DATABASE_URL ausente),
            # saltamos la carga/indexación de la KB para no bloquear el startup.
            if not getattr(pg_vector_store, "_is_initialized", False):
                logger.warning("[RAG] PGVector no inicializado - se omite carga de KB.")
                return {
                    "status": "skipped",
                    "chunks_indexed": 0,
                    "duration": 0,
                    "message": "PGVector no configurado - carga de KB omitida."
                }

            # 2. Cargar y dividir documentos
            chunks = load_and_chunk_documents(
                base_dir=self.KNOWLEDGE_BASE_DIR,
                chunk_size=self.CHUNK_SIZE,
                chunk_overlap=self.CHUNK_OVERLAP
            )

            # 3. Fallback a placeholder si KB vacía
            if not chunks:
                logger.warning("[RAG] Base de conocimiento vacía. Cargando documentos placeholder.")
                chunks = load_placeholder_documents()

            # 4. LIMPIAR ÍNDICE ANTERIOR (DELETE total)
            logger.info(f"[RAG] Limpiando {pg_vector_store.collection_name} para re-indexación...")
            self._clear_vector_store()

            # 5. Indexar chunks nuevos
            logger.info(f"[RAG] Indexando {len(chunks)} chunks en pgvector. ¡Esto puede ser lento!")
            ids = pg_vector_store.add_documents(chunks)

            end_time = time.time()
            duration = end_time - start_time
            logger.info(f"[RAG] ✅ Indexación completa en {duration:.2f} segundos. Chunks indexados: {len(ids)}")

            return {
                "status": "success",
                "chunks_indexed": len(ids),
                "duration": duration,
                "message": f"Base de conocimiento actualizada. {len(ids)} chunks indexados en {duration:.2f}s."
            }

        except Exception as e:
            logger.error(f"[RAG] ❌ Error CRÍTICO durante la recarga de la Base de Conocimiento: {e}", exc_info=True)
            return {
                "status": "error",
                "chunks_indexed": 0,
                "message": str(e)
            }

    def _clear_vector_store(self) -> None:
        """
        Limpia el vector store eliminando documentos antiguos.
        Ejecuta DELETE directo en PostgreSQL para evitar duplicados.
        """
        try:
            logger.info("[RAG] Limpiando índice vectorial anterior...")

            # Obtener connection string (pg_vector_store ya está importado en el scope global)
            connection_string = pg_vector_store.connection_string
            collection_name = pg_vector_store.collection_name

            # Conectar y ejecutar DELETE
            if connection_string is None:
                raise ValueError("DATABASE_URL no está configurada")
            
            # psycopg v3: NO usar context manager para conexión (no hace commit auto)
            # Se debe hacer commit() explícito antes de cerrar
            conn = psycopg.connect(connection_string)
            try:
                with conn.cursor() as cursor:
                    # Eliminar todos los documentos de la colección
                    # LangChain PGVector usa collection_id (UUID) en langchain_pg_embedding
                    # que referencia a langchain_pg_collection.uuid por nombre
                    cursor.execute(
                        """
                        DELETE FROM langchain_pg_embedding
                        WHERE collection_id IN (
                            SELECT uuid FROM langchain_pg_collection WHERE name = %s
                        )
                        """,
                        (collection_name,)
                    )
                    deleted_count = cursor.rowcount
                conn.commit()
                logger.info("[RAG] Vector store limpiado: %d documentos eliminados", deleted_count)
            finally:
                conn.close()
                
        except Exception as e:
            # No es fatal si falla la limpieza, solo logueamos warning
            logger.warning("[RAG] No se pudo limpiar vector store: %s", e)

    def _validate_response_no_obsolete_numbers(self, response: str) -> str:
        """
        🛡️ VALIDACIÓN DE SEGURIDAD: Verifica que la respuesta no contenga números telefónicos obsoletos.
        
        Esta es una medida preventiva para evitar servir información desactualizada.
        Si se detecta un número obsoleto, se lanza una excepción.
        
        Args:
            response: Texto de respuesta del RAG
            
        Returns:
            str: La respuesta validada (o excepción si contiene números obsoletos)
            
        Raises:
            RuntimeError: Si se detecta un número obsoleto en la respuesta
        """
        for obsolete_num in OBSOLETE_PHONE_NUMBERS:
            if obsolete_num in response:
                error_msg = (
                    f"🚨 NÚMERO OBSOLETO DETECTADO EN RESPUESTA: {obsolete_num} | "
                    f"Este número fue reemplazado y NO debe servirse a usuarios."
                )
                logger.critical(error_msg)
                raise RuntimeError(error_msg)
        
        return response

    def search_knowledge(self, document_path: str, query: str, k: int = 5) -> str:
        """
        Realiza la búsqueda de similitud en el vector store, filtrando por el documento de origen.

        Args:
            document_path: La ruta del documento a filtrar (usado como metadata 'source').
            query: La pregunta del usuario.

        Returns:
            str: El contexto relevante concatenado o un mensaje de error.
        """
        # Ya no necesitamos _ensure_db_initialized, ya que se hizo en el startup.

        try:
            logger.debug(f"[RAG] Búsqueda en '{document_path}' con query: '{query}'")

            # Normalizar ruta para comparación
            normalized_doc_path = document_path.replace("\\", "/")

            # Usar filtrado nativo de PGVector (más eficiente que filtrar en Python)
            filtered_results = pg_vector_store.similarity_search(
                query,
                k=k,
                filter={"source": normalized_doc_path}
            )

            if not filtered_results:
                logger.warning(f"[RAG] No se encontraron resultados para '{document_path}'")
                return f"[ERROR] Documento '{document_path}' no disponible en el índice actual."

            # Formatear resultados
            context_parts = [doc.page_content.strip() for doc in filtered_results]
            formatted_context = "\n".join(context_parts)

            # 🛡️ Validar que no contenga números obsoletos
            self._validate_response_no_obsolete_numbers(formatted_context)

            logger.debug("[RAG] Encontrados %d chunks relevantes", len(filtered_results))
            return formatted_context

        except (ValueError, RuntimeError) as e:
            logger.error("[RAG] Error en búsqueda: %s", e, exc_info=True)
            return f"[ERROR] Error al buscar en '{document_path}': {str(e)}"

    def semantic_search(self, query: str, k: int = 5) -> List[Document]:
        """
        Búsqueda semántica pura (sin filtrado por documento).
        """
        # Ya no se requiere _ensure_db_initialized (se hizo en startup)

        try:
            logger.debug(f"[RAG] Búsqueda semántica: '{query}' (top-{k})")
            results = pg_vector_store.similarity_search(query, k=k)
            logger.debug(f"[RAG] Encontrados {len(results)} resultados")
            return results

        except Exception as e:
            logger.error(f"[RAG] Error en búsqueda: {e}", exc_info=True)
            return []

    def get_context_for_query(self, query: str, k: int = 3) -> str:
        """
        Obtiene contexto relevante formateado para el LLM (búsqueda global).
        """
        documents = self.semantic_search(query, k=k)

        if not documents:
            logger.warning("[RAG] No se encontró contexto relevante")
            return "[Sin contexto disponible]"

        # Formatear documentos para el LLM
        context_parts = []
        for i, doc in enumerate(documents, 1):
            source = doc.metadata.get("source", "desconocido")
            content = doc.page_content.strip()
            context_parts.append(f"[Fuente {i}: {source}]\n{content}")

        formatted_context = "\n\n".join(context_parts)
        logger.debug(f"[RAG] Contexto generado: {len(formatted_context)} caracteres")

        return formatted_context


# Instancia global
rag_service = RAGService()

# Función main para testing/validación manual
def main():
    """
    Función principal para validar la carga e indexación manualmente.
    Ejecutar: python rag.py
    """
    print("=" * 60)
    print("VALIDACIÓN DE INDEXACIÓN RAG")
    print("=" * 60)

    print("\n[1/3] Forzando recarga de base de conocimiento...")
    result = rag_service.reload_knowledge_base()
    print(f"Resultado: {result}")

    print("\n[2/3] Realizando búsqueda de prueba (semántica global)...")
    query = "¿Cuál es la misión de la empresa?"
    results = rag_service.semantic_search(query, k=3)
    
    print(f"\nResultados para '{query}':")
    for i, doc in enumerate(results, 1):
        print(f"\n--- Resultado {i} ---")
        print(f"Fuente: {doc.metadata.get('source', 'N/A')}")
        print(f"Contenido: {doc.page_content[:200]}...")

    print("\n[3/3] Obteniendo contexto formateado...")
    context = rag_service.get_context_for_query(query, k=2)
    print("\nContexto generado:")
    print(context[:500] + "..." if len(context) > 500 else context)

    print("\n" + "=" * 60)
    print("✅ VALIDACIÓN COMPLETADA")
    print("=" * 60)


if __name__ == "__main__":
    main()