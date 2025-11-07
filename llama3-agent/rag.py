# rag.py
from typing import List

class RAGService:
    """
    Servicio de RAG que simula la búsqueda de contexto en la base de conocimiento 
    de la empresa, ahora apuntando a la nueva ruta: 'data_base/info_empresa.txt'.
    """
    # 🚨 RUTA ACTUALIZADA 🚨
    def __init__(self, knowledge_base_path: str = "data_base/info_empresa.txt"):
        self.knowledge_base = self._load_knowledge_base(knowledge_base_path)

    def _load_knowledge_base(self, path: str) -> str:
        """Carga el contenido del documento de la empresa desde el archivo."""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
                print(f"[RAG] Archivo '{path}' cargado correctamente ({len(content)} caracteres)")
                return content
        except FileNotFoundError:
            print(f"[RAG] Advertencia: Archivo no encontrado en '{path}'. Usando placeholder.")
            return self._placeholder_content()
        except Exception as e:
            print(f"[RAG] Error al cargar archivo: {e}. Usando placeholder.")
            return self._placeholder_content()
    
    def _placeholder_content(self):
        """Contenido simulado de 'data_base/info_empresa.txt'."""
        return """
        # Base de Conocimiento - Inmobiliaria GlobalHome
        
        Misión: Conectar a las personas con su espacio ideal, ofreciendo una experiencia transparente y personalizada.
        Servicios: Venta (Comisión 3% con exclusividad, 4% sin exclusividad), Alquiler (50% de un mes de alquiler), Administración (8% mensual).
        Contacto: Teléfono +57 601 555 1234. Horario: 8:00 AM - 6:00 PM Lunes a Viernes.
        Exclusividad: Mínimo 6 meses, reduce la comisión de venta de 4% a 3%.
        """

    def search_knowledge(self, query: str) -> str:
        """
        Busca información relevante en la base de conocimiento usando keywords.
        Retorna secciones del documento que coincidan con la consulta.
        """
        query_lower = query.lower()

        # Dividir el documento en líneas para búsqueda
        lines = self.knowledge_base.split('\n')
        relevant_lines: List[str] = []

        # Búsqueda por palabras clave en el contenido real del archivo
        keywords = query_lower.split()

        for i, line in enumerate(lines):
            line_lower = line.lower()
            # Si alguna keyword coincide, tomar contexto (línea + 2 siguientes)
            if any(keyword in line_lower for keyword in keywords):
                context_start = max(0, i)
                context_end = min(len(lines), i + 3)
                relevant_lines.extend(lines[context_start:context_end])

        if relevant_lines:
            # Eliminar duplicados manteniendo orden
            seen = set()
            unique_lines = []
            for line in relevant_lines:
                if line.strip() and line not in seen:
                    seen.add(line)
                    unique_lines.append(line)
            return "\n".join(unique_lines)

        # Si no hay coincidencias, retornar las primeras líneas del documento
        return "\n".join(lines[:10]) + "\n\n[Información general de la empresa]"

# Instancia global
rag_service = RAGService()