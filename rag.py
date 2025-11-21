import os
import glob
from typing import Dict, List, Tuple, Any
from logging_config import logger  # ✅ Opción A: Uso del logger centralizado

class RAGService:
    """
    Servicio de RAG refactorizado que permite la recarga en caliente (Hot-Reload)
    de la base de conocimiento sin reiniciar el servidor.
    """
    KNOWLEDGE_BASE_DIR = "knowledge_base"

    # Mapa en memoria: { "ruta/archivo.txt": "contenido..." }
    knowledge_map: Dict[str, str]

    def __init__(self):
        # Inicializar diccionario vacío
        self.knowledge_map = {}
        # Cargar documentos inmediatamente usando el mecanismo de recarga
        result = self.reload_knowledge_base()
        logger.info(f"[RAG] Servicio inicializado: {result['message']}")

    def _load_documents_from_disk(self, base_dir: str) -> Dict[str, str]:
        """
        MÉTODO PRIVADO: Carga los archivos del disco y retorna un nuevo diccionario.
        NO modifica el estado interno de la clase, lo que lo hace seguro y limpio.
        """
        new_knowledge_map: Dict[str, str] = {}

        # Normalizar ruta para compatibilidad
        pattern = os.path.join(base_dir, "*.txt")
        file_paths = glob.glob(pattern, recursive=False)

        if not file_paths:
            logger.warning(f"[RAG] No se encontraron archivos .txt en '{base_dir}'.")
            return self._placeholder_content_map()

        # Lista de encodings para probar (robustez)
        encodings = ['utf-8', 'utf-16', 'latin-1', 'cp1252']

        for file_path in file_paths:
            content = None
            rel_path = file_path.replace("\\", "/") # Normalizar a slash de Linux

            for encoding in encodings:
                try:
                    with open(file_path, 'r', encoding=encoding) as f:
                        content = f.read()
                        break # Éxito, salir del loop de encodings
                except (UnicodeDecodeError, UnicodeError):
                    continue # Probar siguiente encoding
                except Exception as e:
                    logger.error(f"[RAG] Error de lectura en '{rel_path}': {e}")
                    break

            if content:
                new_knowledge_map[rel_path] = content
                # Log nivel debug para no saturar consola en producción
                # logger.debug(f"[RAG] Cargado: {rel_path}")
            else:
                logger.error(f"[RAG] FALLO: No se pudo cargar '{rel_path}'")

        return new_knowledge_map

    def reload_knowledge_base(self) -> Dict[str, Any]:
        """
        MÉTODO PÚBLICO: Orquesta la recarga de la base de conocimiento.
        1. Llama a la carga desde disco.
        2. Actualiza el estado interno (self.knowledge_map) de forma atómica.
        """
        logger.info("[RAG] Iniciando recarga de base de conocimiento...")

        try:
            # 1. Cargar datos nuevos en variable temporal
            new_map = self._load_documents_from_disk(self.KNOWLEDGE_BASE_DIR)

            # 2. Actualizar estado (Reemplazo atómico del diccionario)
            self.knowledge_map = new_map

            count = len(self.knowledge_map)
            logger.info(f"[RAG] Recarga completada exitosamente. {count} documentos disponibles.")

            return {
                "status": "success",
                "files_loaded": count,
                "message": f"Base de conocimiento actualizada. {count} archivos cargados."
            }

        except Exception as e:
            logger.error(f"[RAG] Error crítico durante la recarga: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"Fallo al recargar: {str(e)}"
            }

    def _placeholder_content_map(self) -> Dict[str, str]:
        """Contenido simulado por si falla la carga de archivos."""
        return {
            f"{self.KNOWLEDGE_BASE_DIR}/info_institucional.txt":
                "Misión: Conectar personas con su espacio ideal. Contacto: 322 502 1493."
        }

    def search_knowledge(self, document_path: str, query: str) -> str:
        """
        Busca contexto relevante en un documento específico.
        """
        # Validación de existencia del documento
        document_path = document_path.replace("\\", "/")

        if document_path not in self.knowledge_map:
            logger.warning(f"[RAG] Documento solicitado no encontrado: '{document_path}'")
            return f"[ERROR] Documento '{document_path}' no disponible en el índice actual."

        full_content = self.knowledge_map[document_path]

        # Algoritmo de búsqueda simple (Keyword Matching)
        # NOTA: Aquí se mantiene la lógica de scoring que definimos previamente
        # Se simplifica en este ejemplo para enfocar en la recarga,
        # pero deberías mantener tu lógica de 'scoring multifactorial' aquí.

        lines = [line.strip() for line in full_content.split('\n') if line.strip()]
        query_keywords = set(query.lower().split())

        if not query_keywords:
            return "\n".join(lines[:5]) # Fallback simple

        # Scoring rápido (ejemplo simplificado)
        scored_lines = []
        for i, line in enumerate(lines):
            count = sum(1 for k in query_keywords if k in line.lower())
            if count > 0:
                scored_lines.append((count, line))

        scored_lines.sort(key=lambda x: x[0], reverse=True)

        if not scored_lines:
             return "\n".join(lines[:5]) + "\n\n[Sin coincidencias exactas]"

        top_lines = [line for _, line in scored_lines[:5]]
        return f"--- Fuente: {document_path} ---\n" + "\n".join(top_lines)

# Instancia global
rag_service = RAGService()