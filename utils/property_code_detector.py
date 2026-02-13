# utils/property_code_detector.py
"""
Detector de códigos de inmuebles en mensajes de WhatsApp.
Los códigos son ÚNICAMENTE numéricos (4-6 dígitos).
Proyecto Sofía - Inmobiliaria Proteger
"""

import re
from typing import Optional
from dataclasses import dataclass
from logging_config import logger


@dataclass
class PropertyCodeResult:
    """Resultado de la detección de código de inmueble"""
    has_code: bool
    code: Optional[str]
    has_intent: bool  # Si el mensaje tiene intención de preguntar por código
    context: Optional[str]  # Contexto donde se encontró el código


class PropertyCodeDetector:
    """
    Detecta códigos de inmuebles en mensajes de clientes.

    Los códigos son ÚNICAMENTE numéricos de 4-6 dígitos.
    Se buscan en contexto de intención inmobiliaria.

    Ejemplos detectados:
    - "Me interesa el código 12345"
    - "Quiero info del inmueble 78901"
    - "Vi el #54321 en Instagram"
    - "Referencia 123456"
    """

    # Patrones de códigos NUMÉRICOS (4-6 dígitos) con contexto
    CODE_PATTERNS = [
        # Con palabra clave explícita
        (r'\bc[oó]digo\s*[:#]?\s*(\d{4,6})\b', 'código'),
        (r'\bref(?:erencia)?\s*[:#]?\s*(\d{4,6})\b', 'referencia'),
        (r'\binmueble\s*[:#]?\s*(\d{4,6})\b', 'inmueble'),
        (r'\bpropiedad\s*[:#]?\s*(\d{4,6})\b', 'propiedad'),
        (r'\bn[uú]mero\s*[:#]?\s*(\d{4,6})\b', 'número'),
        (r'\bid\s*[:#]?\s*(\d{4,6})\b', 'id'),
        # Con hashtag (sin \b inicial porque # no es alfanumérico)
        (r'(?:^|\s)#(\d{4,6})(?:\s|$)', 'hashtag'),
        # Con "el" + número (en contexto de conversación inmobiliaria)
        (r'\bel\s+(\d{4,6})\b', 'referencia directa'),
        # Número aislado después de palabras de interés
        (r'(?:interesa|info|información|detalles|precio|disponible|ver)\s+(?:el\s+)?(\d{4,6})\b', 'interés'),
    ]

    # Keywords que indican intención de preguntar por código de inmueble
    CODE_INTENT_KEYWORDS = [
        # Preguntas sobre código
        "código", "codigo", "referencia", "ref",
        "número de", "numero de", "número del", "numero del",
        # Interés en inmueble específico
        "el inmueble", "la propiedad", "el apartamento", "la casa",
        "vi este", "vi ese", "vi el", "vi un",
        "me interesa el", "me interesa ese", "me interesa este",
        "información del", "info del", "detalles del", "precio del",
        "disponible el", "disponibilidad del",
        # Contexto de redes sociales
        "que publicaron", "que vi en", "del post", "de la publicación",
        "en instagram", "en facebook", "en tiktok", "en youtube",
    ]

    # Palabras que invalidan un número como código de inmueble
    # (evitar falsos positivos)
    EXCLUDE_CONTEXTS = [
        # Teléfonos (números más largos o con formato específico)
        r'\+\d{10,}',
        r'\d{3}[\s-]?\d{3}[\s-]?\d{4}',
        # Precios (con símbolo de moneda o palabras)
        r'\$\s*\d+',
        r'\d+\s*(?:millones?|mill?|pesos|cop)',
        # Fechas
        r'\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}',
        # Horas
        r'\d{1,2}:\d{2}',
        # Direcciones (números de calle/carrera)
        r'(?:calle|carrera|cra|cl|av|avenida|diagonal|transversal)\s*#?\s*\d+',
        # Metros cuadrados
        r'\d+\s*(?:m2|mt2|metros?)',
    ]

    def __init__(self):
        # Compilar patrones para eficiencia
        self._code_patterns = [
            (re.compile(p, re.IGNORECASE), ctx) for p, ctx in self.CODE_PATTERNS
        ]
        self._exclude_patterns = [
            re.compile(p, re.IGNORECASE) for p in self.EXCLUDE_CONTEXTS
        ]

    def detect(self, message: str) -> PropertyCodeResult:
        """
        Detecta códigos de inmueble en el mensaje.

        Args:
            message: Texto del mensaje del cliente

        Returns:
            PropertyCodeResult con información del código detectado
        """
        if not message:
            return PropertyCodeResult(False, None, False, None)

        message_clean = message.strip()
        message_lower = message_clean.lower()

        # Verificar si el mensaje tiene intención de código
        has_intent = self._has_code_intent(message_lower)

        # Buscar códigos en el mensaje
        for pattern, context in self._code_patterns:
            match = pattern.search(message_clean)
            if match:
                code = match.group(1)

                # Verificar que el código no esté en un contexto excluido
                if self._is_excluded_context(message_clean, match.start(), match.end()):
                    continue

                logger.info(f"[PropertyCodeDetector] Código detectado: {code} (contexto: {context})")
                return PropertyCodeResult(
                    has_code=True,
                    code=code,
                    has_intent=True,
                    context=context
                )

        # No se encontró código, pero puede haber intención
        return PropertyCodeResult(
            has_code=False,
            code=None,
            has_intent=has_intent,
            context=None
        )

    def _has_code_intent(self, message_lower: str) -> bool:
        """Verifica si el mensaje tiene intención de preguntar por código."""
        return any(kw in message_lower for kw in self.CODE_INTENT_KEYWORDS)

    def _is_excluded_context(self, message: str, start: int, end: int) -> bool:
        """
        Verifica si el número encontrado está en un contexto que debe excluirse
        (teléfonos, precios, fechas, etc.)
        """
        # Tomar contexto alrededor del match (30 caracteres antes y después)
        context_start = max(0, start - 30)
        context_end = min(len(message), end + 30)
        context = message[context_start:context_end]

        for pattern in self._exclude_patterns:
            if pattern.search(context):
                return True

        return False

    def extract_all_codes(self, message: str) -> list[str]:
        """
        Extrae todos los posibles códigos de un mensaje.
        Útil cuando un cliente menciona múltiples propiedades.

        Args:
            message: Texto del mensaje

        Returns:
            Lista de códigos encontrados (sin duplicados)
        """
        if not message:
            return []

        codes = set()
        message_clean = message.strip()

        for pattern, _ in self._code_patterns:
            for match in pattern.finditer(message_clean):
                code = match.group(1)
                if not self._is_excluded_context(message_clean, match.start(), match.end()):
                    codes.add(code)

        return list(codes)


# Instancia singleton para uso directo
_detector = None


def get_detector() -> PropertyCodeDetector:
    """Retorna instancia singleton del detector."""
    global _detector
    if _detector is None:
        _detector = PropertyCodeDetector()
    return _detector


def detect_property_code(message: str) -> PropertyCodeResult:
    """
    Función de conveniencia para detectar código de inmueble.

    Args:
        message: Texto del mensaje

    Returns:
        PropertyCodeResult
    """
    return get_detector().detect(message)