# utils/pii_validator.py (NUEVO)
import spacy
from typing import Optional

# Ejecutar: python -m spacy download es_core_news_sm
try:
    nlp = spacy.load("es_core_news_sm")
except OSError:
    # Si el modelo no está instalado, mostrar advertencia
    print("⚠️ Modelo spaCy 'es_core_news_sm' no encontrado.")
    print("   Ejecuta: python -m spacy download es_core_news_sm")
    nlp = None

def robust_extract_name(text: str) -> Optional[str]:
    """
    Extrae nombres de personas usando NER (Named Entity Recognition).

    Args:
        text: Texto del usuario que potencialmente contiene un nombre.

    Returns:
        El nombre extraído si se detecta, None en caso contrario.
    """
    if not nlp:
        # Fallback si spaCy no está disponible
        return _simple_fallback(text)

    doc = nlp(text)

    # Buscar entidades de tipo PERSON (PER en español)
    for ent in doc.ents:
        if ent.label_ == "PER":
            return ent.text

    # Fallback simple si NER no detecta nada
    return _simple_fallback(text)

def _simple_fallback(text: str) -> Optional[str]:
    """
    Fallback simple: asumir que el input es un nombre si tiene 1-3 palabras.
    """
    words = text.strip().split()

    # Si tiene entre 1 y 3 palabras, probablemente sea un nombre
    if 1 < len(words) <= 4:
        return text.strip()

    # Si es una sola palabra y empieza con mayúscula, probablemente sea un nombre
    if len(words) == 1 and text[0].isupper():
        return text.strip()

    return None
