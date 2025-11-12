# rag.py
from typing import List

class RAGService:
    """
    Servicio de RAG que carga múltiples documentos categorizados desde knowledge_base/
    y permite búsqueda segmentada por documento específico.
    """

    def __init__(self):
        """Inicializa el servicio cargando todos los documentos en un mapa."""
        self.knowledge_map = self._load_all_documents()
        print(f"[RAG] Inicializado con {len(self.knowledge_map)} documentos")

    def _load_all_documents(self) -> dict:
        """
        Carga todos los archivos .txt de knowledge_base/ en un diccionario.
        Retorna: {ruta_relativa: contenido}
        """
        import os
        import glob

        knowledge_map = {}
        base_path = "knowledge_base"

        # Obtener todos los archivos .txt en knowledge_base/
        pattern = os.path.join(base_path, "*.txt")
        files = glob.glob(pattern)

        for file_path in files:
            # Intentar múltiples encodings
            encodings = ['utf-8', 'utf-16', 'latin-1', 'cp1252']
            content = None

            for encoding in encodings:
                try:
                    with open(file_path, 'r', encoding=encoding) as f:
                        content = f.read()
                        rel_path = file_path.replace("\\", "/")
                        knowledge_map[rel_path] = content
                        print(f"[RAG] ✓ Cargado: {rel_path} ({len(content)} caracteres, encoding={encoding})")
                        break  # Éxito, salir del loop de encodings
                except (UnicodeDecodeError, UnicodeError):
                    continue  # Intentar siguiente encoding
                except Exception as e:
                    print(f"[RAG] ✗ Error cargando {file_path}: {e}")
                    break

            if content is None:
                print(f"[RAG] ✗ No se pudo decodificar {file_path} con ningún encoding")

        return knowledge_map

    def search_knowledge(self, document_path: str, query: str) -> str:
        """
        Busca información relevante en un documento específico usando keywords.

        Args:
            document_path: Ruta relativa al documento (ej: 'knowledge_base/info_pagos_online.txt')
            query: Consulta del usuario

        Returns:
            Secciones del documento que coincidan con la consulta
        """
        # Normalizar path (Windows/Linux)
        document_path = document_path.replace("\\", "/")

        # Validar que el documento existe
        if document_path not in self.knowledge_map:
            available = ", ".join(self.knowledge_map.keys())
            return f"[ERROR] Documento '{document_path}' no encontrado. Disponibles: {available}"

        # Obtener contenido del documento específico
        content = self.knowledge_map[document_path]
        query_lower = query.lower()

        # Dividir el documento en líneas para búsqueda
        lines = content.split('\n')
        relevant_lines: List[str] = []

        # Búsqueda por palabras clave
        keywords = query_lower.split()

        # Configuración de ranking
        TOP_N_LINES = 10

        # Calcular score de relevancia para cada línea
        line_scores = []  # [(line_index, score)]

        for i, line in enumerate(lines):
            line_lower = line.lower()

            # Contar keywords en esta línea
            keyword_count = sum(1 for kw in keywords if kw in line_lower)

            if keyword_count > 0:
                # Calcular métricas de relevancia
                words_in_line = len(line_lower.split())
                keyword_density = keyword_count / max(words_in_line, 1)  # Densidad de keywords
                position_ratio = i / max(len(lines), 1)  # Posición relativa en documento

                # Score compuesto (mayor = más relevante)
                score = (
                    keyword_count * 2.0 +           # Peso por cantidad de keywords
                    keyword_density * 3.0 +         # Peso por densidad
                    (1.0 - position_ratio) * 0.5    # Preferir líneas al inicio
                )

                line_scores.append((i, score))

        if line_scores:
            # Ordenar por score descendente (más relevantes primero)
            line_scores.sort(key=lambda x: x[1], reverse=True)

            # Tomar top-N líneas más relevantes
            top_indices = [idx for idx, score in line_scores[:TOP_N_LINES]]
            top_indices.sort()  # Mantener orden original del documento

            # Extraer líneas con contexto (línea + 2 siguientes)
            relevant_lines = []
            for idx in top_indices:
                context_start = max(0, idx)
                context_end = min(len(lines), idx + 3)
                relevant_lines.extend(lines[context_start:context_end])

            # Eliminar duplicados manteniendo orden
            seen = set()
            unique_lines = []
            for line in relevant_lines:
                if line.strip() and line not in seen:
                    seen.add(line)
                    unique_lines.append(line)

            return "\n".join(unique_lines)

        # Si no hay coincidencias, retornar las primeras líneas del documento
        return "\n".join(lines[:10]) + f"\n\n[Información general del documento: {document_path}]"

# Instancia global
rag_service = RAGService()