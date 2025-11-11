# utils/pii_validator.py (OPTIMIZADO - Sin spaCy)
from typing import Optional
import re

def robust_extract_name(text: str) -> Optional[str]:
    """
    Extrae nombres de personas usando patrones simples.
    """
    return _simple_fallback(text)

def _simple_fallback(text: str) -> Optional[str]:
    """
    Fallback simple: extrae nombres usando patrones heurísticos.
    """
    text = text.strip()

    # Limpiar puntuacion basica para analisis
    text_clean = re.sub(r'[¿?¡!,;.]', '', text)
    words = text_clean.split()

    # Palabras comunes que no son nombres
    common_words = {
        'Me', 'Mi', 'Es', 'Soy', 'El', 'La', 'Los', 'Las', 'Un', 'Una',
        'Hola', 'Como', 'Estas', 'Que', 'Cual', 'Donde', 'Cuando', 'Por',
        'Para', 'Con', 'Sin', 'Si', 'No', 'Y', 'O', 'De', 'Del', 'Al'
    }

    # Caso 1: Texto directo (1-3 palabras capitalizadas, sin puntuacion)
    if 1 <= len(words) <= 3:
        # Todas las palabras deben empezar con mayuscula y no ser comunes
        if all(w[0].isupper() and w not in common_words for w in words if w):
            return ' '.join(words)

    # Caso 2: Patron "Me llamo X" o similar
    patterns = [
        r'(?:me llamo|mi nombre es|soy)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)',
        r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})$',  # Nombre al final
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            name = match.group(1).strip()
            name_words = name.split()
            # Validar que el nombre tenga entre 1 y 3 palabras y no sean comunes
            if 1 <= len(name_words) <= 3:
                if not any(w in common_words for w in name_words):
                    return name

    # Caso 3: Buscar secuencias de palabras capitalizadas
    capitalized_words = []

    for word in words:
        # Palabra capitalizada y no es palabra comun
        if word and word[0].isupper() and word not in common_words:
            capitalized_words.append(word)
        elif capitalized_words:
            # Si ya tenemos palabras acumuladas y encontramos una no capitalizada, terminamos
            break

    # Si encontramos 2-3 palabras capitalizadas consecutivas, probablemente sea un nombre
    # (evitamos 1 sola palabra para reducir falsos positivos como "Hola")
    if 2 <= len(capitalized_words) <= 3:
        return ' '.join(capitalized_words)

    # Caso especial: 1 palabra capitalizada SOLO si tiene 2+ silabas y no es comun
    if len(capitalized_words) == 1:
        word = capitalized_words[0]
        # Verificar que tenga al menos 3 caracteres y no sea saludo comun
        if len(word) >= 3 and word not in common_words:
            # Verificar que no sea inicio de pregunta
            if text_clean.lower().startswith(word.lower()) and len(words) > 1:
                # Es el inicio de una oracion, probablemente no sea nombre
                return None
            return word

    return None
