# rag/data_loader.py
"""
Módulo de carga y procesamiento de documentos para RAG.
Maneja la lectura de archivos y el chunking (división en fragmentos).
"""

import os
import glob
from typing import List
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from logging_config import logger


def load_and_chunk_documents(
    base_dir: str = "knowledge_base",
    chunk_size: int = 500,
    chunk_overlap: int = 100
) -> List[Document]:
    """
    Carga documentos desde el directorio especificado y los divide en chunks.

    Args:
        base_dir: Directorio donde se encuentran los archivos .txt
        chunk_size: Tamaño máximo de cada chunk en caracteres
        chunk_overlap: Solapamiento entre chunks consecutivos

    Returns:
        List[Document]: Lista de documentos chunkeados con metadata
    """
    logger.info(f"[DataLoader] Cargando documentos desde '{base_dir}'...")

    # 1. Encontrar archivos .txt
    pattern = os.path.join(base_dir, "*.txt")
    file_paths = glob.glob(pattern, recursive=False)

    if not file_paths:
        logger.warning(f"[DataLoader] No se encontraron archivos .txt en '{base_dir}'")
        return []

    # 2. Leer contenido de los archivos
    documents: List[Document] = []
    encodings = ['utf-8', 'utf-16', 'latin-1', 'cp1252']

    for file_path in file_paths:
        rel_path = file_path.replace("\\", "/")  # Normalizar ruta
        content = None

        # Intentar con diferentes encodings
        for encoding in encodings:
            try:
                with open(file_path, 'r', encoding=encoding) as f:
                    content = f.read()
                    break
            except (UnicodeDecodeError, UnicodeError):
                continue
            except Exception as e:
                logger.error(f"[DataLoader] Error leyendo '{rel_path}': {e}")
                break

        if content:
            # Crear documento con metadata
            doc = Document(
                page_content=content,
                metadata={
                    "source": rel_path,
                    "filename": os.path.basename(file_path)
                }
            )
            documents.append(doc)
            logger.info(f"[DataLoader] Cargado: {rel_path} ({len(content)} caracteres)")
        else:
            logger.error(f"[DataLoader] FALLO: No se pudo cargar '{rel_path}'")

    if not documents:
        logger.warning("[DataLoader] No se cargaron documentos")
        return []

    logger.info(f"[DataLoader] {len(documents)} documentos cargados. Iniciando chunking...")

    # 3. Dividir en chunks usando RecursiveCharacterTextSplitter
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=len,
        separators=["\n\n", "\n", ". ", " ", ""]
    )

    chunks = text_splitter.split_documents(documents)

    logger.info(f"[DataLoader] Chunking completado: {len(chunks)} chunks generados")
    logger.info(f"[DataLoader] Configuración: chunk_size={chunk_size}, overlap={chunk_overlap}")

    return chunks


def load_placeholder_documents() -> List[Document]:
    """
    Carga documentos de respaldo en caso de que no existan archivos.

    Returns:
        List[Document]: Lista con documento placeholder
    """
    logger.warning("[DataLoader] Usando contenido placeholder")

    return [
        Document(
            page_content="Misión: Conectar personas con su espacio ideal. Contacto: 604 444 6364.",
            metadata={"source": "placeholder", "filename": "info_institucional.txt"}
        )
    ]
