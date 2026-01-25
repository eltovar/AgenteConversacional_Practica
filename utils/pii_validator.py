# utils/pii_validator.py (OPTIMIZADO - Sin spaCy)
from typing import Optional
import re

# Palabras comunes que NO son nombres (en minúscula para comparación case-insensitive)
COMMON_WORDS = {
    # Artículos y preposiciones
    'me', 'mi', 'es', 'soy', 'el', 'la', 'los', 'las', 'un', 'una',
    'de', 'del', 'al', 'con', 'sin', 'por', 'para', 'y', 'o',
    # Saludos y expresiones
    'hola', 'como', 'estas', 'que', 'cual', 'donde', 'cuando',
    'si', 'no', 'bien', 'gracias', 'buenas', 'buenos',
    # Verbos comunes (evitar falsos positivos)
    'quiero', 'quisiera', 'necesito', 'busco', 'tengo', 'puedo',
    'pedir', 'ver', 'saber', 'hacer', 'hablar', 'comunicar',
    'agendar', 'solicitar', 'contactar',
    # Palabras inmobiliarias (evitar "pedir una cita" como nombre)
    'cita', 'casa', 'apartamento', 'apartaestudio', 'local', 'oficina',
    'propiedad', 'inmueble', 'arriendo', 'arrendar', 'compra', 'comprar',
    'venta', 'vender', 'asesor', 'asesora', 'informacion', 'ayuda',
    'contacto', 'zona', 'barrio', 'sector',
    # Números y cantidades
    'millones', 'millon', 'mil', 'pesos'
}


def _is_common_word(word: str) -> bool:
    """Verifica si una palabra es común (case-insensitive)."""
    return word.lower() in COMMON_WORDS


def robust_extract_name(text: str) -> Optional[str]:
    """
    Extrae nombres de personas usando patrones simples.
    Solo detecta nombres cuando hay patrones explícitos como "Me llamo X" o "Soy X".
    """
    text = text.strip()

    # Limpiar puntuación básica para análisis
    text_clean = re.sub(r'[¿?¡!,;.]', '', text)
    words = text_clean.split()

    if not words:
        return None

    # Caso 1: Patrón explícito "Me llamo X" / "Mi nombre es X" / "Soy X"
    explicit_patterns = [
        r'(?:me llamo|mi nombre es)\s+([A-Z][a-záéíóúñ]+(?:\s+[A-Z][a-záéíóúñ]+)*)',
    ]

    for pattern in explicit_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            name = match.group(1).strip()
            name_words = name.split()
            # Validar que el nombre tenga entre 1 y 3 palabras y no sean comunes
            if 1 <= len(name_words) <= 3:
                if not any(_is_common_word(w) for w in name_words):
                    # Capitalizar correctamente
                    return ' '.join(w.capitalize() for w in name_words)

    # Caso 2: Texto directo corto (1-3 palabras) que parece ser solo un nombre
    # Solo si NO contiene verbos ni palabras inmobiliarias
    if 1 <= len(words) <= 3:
        # Verificar que todas las palabras empiecen con mayúscula y no sean comunes
        all_capitalized = all(w[0].isupper() for w in words if w)
        none_common = not any(_is_common_word(w) for w in words)

        if all_capitalized and none_common:
            # Verificación adicional: no debe contener dígitos
            if not any(char.isdigit() for char in text_clean):
                return ' '.join(words)

    # Caso 3: Buscar secuencias de 2-3 palabras capitalizadas consecutivas
    # que NO sean palabras comunes
    capitalized_sequence = []

    for word in words:
        if word and word[0].isupper() and not _is_common_word(word):
            capitalized_sequence.append(word)
        elif capitalized_sequence:
            # Terminó la secuencia
            break

    # Solo aceptar si hay 2-3 palabras (más probable que sea nombre completo)
    if 2 <= len(capitalized_sequence) <= 3:
        return ' '.join(capitalized_sequence)

    return None
