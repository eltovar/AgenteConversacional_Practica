# rag.py
"""
Servicio de RAG (Retrieval-Augmented Generation) con búsqueda semántica.
Usa pgvector para almacenamiento de embeddings y búsqueda por similitud.
"""

from typing import Dict, List, Any
from langchain_core.documents import Document
from logging_config import logger
from rag.data_loader import load_and_chunk_documents, load_placeholder_documents
from rag.vector_store import pg_vector_store


class RAGService:
    """
    Servicio de RAG con búsqueda semántica usando pgvector.
    Soporta Hot-Reload de la base de conocimiento sin reiniciar el servidor.
    """
    KNOWLEDGE_BASE_DIR = "knowledge_base"
    CHUNK_SIZE = 500
    CHUNK_OVERLAP = 100

    def __init__(self):
        """
        Inicializa el servicio RAG y carga la base de conocimiento.
        """
        # Inicializar vector store
        logger.info("[RAG] Inicializando vector store...")
        try:
            pg_vector_store.initialize_db()
            logger.info("[RAG] Vector store inicializado exitosamente")
        except Exception as e:
            logger.error(f"[RAG] Error al inicializar vector store: {e}", exc_info=True)
            raise

        # Cargar documentos inmediatamente
        result = self.reload_knowledge_base()
        logger.info(f"[RAG] Servicio inicializado: {result['message']}")

    def reload_knowledge_base(self) -> Dict[str, Any]:
        """
        Recarga la base de conocimiento:
        1. Carga documentos del disco
        2. Los divide en chunks
        3. Elimina embeddings existentes (limpieza para hot-reload)
        4. Indexa los nuevos chunks en pgvector

        Returns:
            Dict con status, número de chunks y mensaje
        """
        logger.info("[RAG] Iniciando recarga de base de conocimiento...")

        try:
            # 1. Cargar y dividir documentos en chunks
            chunks = load_and_chunk_documents(
                base_dir=self.KNOWLEDGE_BASE_DIR,
                chunk_size=self.CHUNK_SIZE,
                chunk_overlap=self.CHUNK_OVERLAP
            )

            # Si no hay documentos, usar placeholder
            if not chunks:
                logger.warning("[RAG] No se encontraron documentos, usando placeholder")
                chunks = load_placeholder_documents()

            # 2. Limpiar embeddings existentes (para hot-reload)
            # NOTA: PGVector no tiene un método directo para limpiar la colección
            # En producción, podrías implementar una estrategia de versionado o timestamps
            logger.info("[RAG] Limpiando índice anterior...")
            self._clear_vector_store()

            # 3. Agregar nuevos chunks al vector store
            logger.info(f"[RAG] Indexando {len(chunks)} chunks en pgvector...")
            ids = pg_vector_store.add_documents(chunks)

            logger.info(f"[RAG] Indexación completada: {len(ids)} chunks indexados")

            return {
                "status": "success",
                "chunks_indexed": len(ids),
                "message": f"Base de conocimiento actualizada. {len(ids)} chunks indexados."
            }

        except Exception as e:
            logger.error(f"[RAG] Error crítico durante la recarga: {e}", exc_info=True)
            return {
                "status": "error",
                "chunks_indexed": 0,
                "message": f"Fallo al recargar: {str(e)}"
            }

    def _clear_vector_store(self) -> None:
        """
        Limpia el vector store eliminando documentos antiguos.
        NOTA: Implementación simplificada. En producción, usar estrategia más robusta.
        """
        try:
            # PGVector/LangChain no proporciona un método directo de limpieza
            # Por ahora, simplemente logueamos la acción
            # En una implementación completa, ejecutarías SQL: DELETE FROM collection_name
            logger.info("[RAG] Limpieza de vector store (pendiente implementación SQL directa)")

            # TODO: Implementar limpieza real usando SQL directo si es necesario
            # from sqlalchemy import text
            # engine = pg_vector_store.get_vector_store()._engine
            # with engine.connect() as conn:
            #     conn.execute(text("DELETE FROM knowledge_base"))
            #     conn.commit()

        except Exception as e:
            logger.warning(f"[RAG] No se pudo limpiar vector store: {e}")

    def search_knowledge(self, document_path: str, query: str, k: int = 5) -> str:
        """
        Busca contexto relevante en un documento específico (compatibilidad con InfoAgent).

        IMPORTANTE: Esta es la interfaz de compatibilidad con el código legacy.
        Usa búsqueda semántica pero filtra por source en metadata.

        Args:
            document_path: Ruta del documento (usado para filtrar por source en metadata)
            query: Consulta del usuario
            k: Número máximo de chunks a recuperar

        Returns:
            String con contexto formateado
        """
        try:
            logger.debug(f"[RAG] Búsqueda en '{document_path}' con query: '{query}'")

            # Realizar búsqueda semántica (recupera más resultados para filtrar)
            all_results = pg_vector_store.similarity_search(query, k=k * 3)

            # Filtrar resultados por source que coincida con document_path
            # Normalizar rutas para comparación
            normalized_doc_path = document_path.replace("\\", "/")

            filtered_results = [
                doc for doc in all_results
                if doc.metadata.get("source", "").replace("\\", "/") == normalized_doc_path
            ]

            # Limitar a k resultados después del filtrado
            filtered_results = filtered_results[:k]

            if not filtered_results:
                logger.warning(f"[RAG] No se encontraron resultados para '{document_path}'")
                return f"[ERROR] Documento '{document_path}' no disponible en el índice actual."

            # Formatear resultados
            context_parts = [doc.page_content.strip() for doc in filtered_results]
            formatted_context = "\n".join(context_parts)

            logger.debug(f"[RAG] Encontrados {len(filtered_results)} chunks relevantes")
            return formatted_context

        except Exception as e:
            logger.error(f"[RAG] Error en búsqueda: {e}", exc_info=True)
            return f"[ERROR] Error al buscar en '{document_path}': {str(e)}"

    def semantic_search(self, query: str, k: int = 5) -> List[Document]:
        """
        Búsqueda semántica pura (sin filtrado por documento).

        Args:
            query: Consulta del usuario
            k: Número de documentos a retornar

        Returns:
            Lista de documentos más relevantes
        """
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

        Args:
            query: Consulta del usuario
            k: Número de chunks a recuperar

        Returns:
            String con contexto formateado
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

    print("\n[2/3] Realizando búsqueda de prueba...")
    query = "¿Cuál es la misión de la empresa?"
    results = rag_service.search_knowledge(query, k=3)

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
