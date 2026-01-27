"""
Módulo de detección y clasificación de links de portales inmobiliarios.
Proyecto Sofía - Inmobiliaria Proteger
"""

import re
from typing import Optional, Dict, Any
from dataclasses import dataclass
from enum import Enum


class PortalOrigen(str, Enum):
    """Portales inmobiliarios soportados"""
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    FINCA_RAIZ = "finca_raiz"
    METRO_CUADRADO = "metrocuadrado"
    MERCADO_LIBRE = "mercado_libre"
    CIENCUADRAS = "ciencuadras"
    PAGINA_WEB = "pagina_web"
    WHATSAPP_DIRECT = "whatsapp_directo"
    DESCONOCIDO = "desconocido"


@dataclass
class LinkDetectionResult:
    """Resultado del análisis de links"""
    tiene_link: bool
    portal: PortalOrigen
    url_original: Optional[str]
    es_inmueble: bool  # True si parece ser un link a propiedad específica
    metadata_extra: Dict[str, Any]


class LinkDetector:
    """
    Detecta y clasifica links de portales inmobiliarios en mensajes de WhatsApp.

    Uso:
        detector = LinkDetector()
        resultado = detector.analizar_mensaje("Hola, vi este apartamento https://www.fincaraiz.com.co/apartamento/123")

        if resultado.tiene_link:
            print(f"Link de: {resultado.portal}")
    """

    # Patrones de URL por portal (orden importa: más específicos primero)
    PATRONES_PORTALES = {
        PortalOrigen.FINCA_RAIZ: [
            r'https?://(?:www\.)?fincaraiz\.com\.co/[^\s]+',
            r'fincaraiz\.com\.co/[^\s]+',
        ],
        PortalOrigen.METRO_CUADRADO: [
            r'https?://(?:www\.)?metrocuadrado\.com/[^\s]+',
            r'metrocuadrado\.com/[^\s]+',
        ],
        PortalOrigen.MERCADO_LIBRE: [
            r'https?://(?:www\.)?(?:inmuebles\.)?mercadolibre\.com\.co/[^\s]+',
            r'mercadolibre\.com\.co/[^\s]+',
        ],
        PortalOrigen.CIENCUADRAS: [
            r'https?://(?:www\.)?ciencuadras\.com/[^\s]+',
            r'ciencuadras\.com/[^\s]+',
        ],
        PortalOrigen.INSTAGRAM: [
            r'https?://(?:www\.)?instagram\.com/[^\s]+',
            r'instagram\.com/[^\s]+',
            r'https?://instagr\.am/[^\s]+',
        ],
        PortalOrigen.FACEBOOK: [
            r'https?://(?:www\.)?facebook\.com/[^\s]+',
            r'https?://(?:www\.)?fb\.com/[^\s]+',
            r'https?://m\.facebook\.com/[^\s]+',
            r'facebook\.com/marketplace/[^\s]+',
        ],
        PortalOrigen.PAGINA_WEB: [
            # Dominio de Inmobiliaria Proteger (ajustar según dominio real)
            r'https?://(?:www\.)?inmobiliariaproteger\.com[^\s]*',
            r'https?://(?:www\.)?proteger\.com\.co[^\s]*',
        ],
    }

    # Palabras clave que indican que es un link a inmueble específico
    KEYWORDS_INMUEBLE = [
        'apartamento', 'casa', 'local', 'oficina', 'bodega', 'lote',
        'arriendo', 'venta', 'alquiler', 'inmueble', 'propiedad',
        '/p/', '/listing/', '/detail/', '/inmueble/', '/property/'
    ]

    def __init__(self):
        # Compilar patrones para eficiencia
        self._patrones_compilados = {
            portal: [re.compile(p, re.IGNORECASE) for p in patterns]
            for portal, patterns in self.PATRONES_PORTALES.items()
        }

        # Patrón genérico para cualquier URL
        self._patron_url_generica = re.compile(
            r'https?://[^\s]+|www\.[^\s]+',
            re.IGNORECASE
        )

    def analizar_mensaje(self, mensaje: str) -> LinkDetectionResult:
        """
        Analiza un mensaje buscando links de portales inmobiliarios.

        Args:
            mensaje: Texto del mensaje del usuario

        Returns:
            LinkDetectionResult con información del link detectado
        """
        if not mensaje:
            return self._resultado_sin_link()

        mensaje_lower = mensaje.lower()

        # Buscar en cada portal
        for portal, patrones in self._patrones_compilados.items():
            for patron in patrones:
                match = patron.search(mensaje)
                if match:
                    url = match.group(0)
                    return LinkDetectionResult(
                        tiene_link=True,
                        portal=portal,
                        url_original=url,
                        es_inmueble=self._es_link_inmueble(url, mensaje_lower),
                        metadata_extra={
                            "mensaje_completo": mensaje,
                            "posicion_link": match.span(),
                        }
                    )

        # Verificar si hay URL genérica (portal desconocido)
        match_generico = self._patron_url_generica.search(mensaje)
        if match_generico:
            return LinkDetectionResult(
                tiene_link=True,
                portal=PortalOrigen.DESCONOCIDO,
                url_original=match_generico.group(0),
                es_inmueble=self._es_link_inmueble(match_generico.group(0), mensaje_lower),
                metadata_extra={"mensaje_completo": mensaje}
            )

        return self._resultado_sin_link()

    def _es_link_inmueble(self, url: str, mensaje: str) -> bool:
        """Determina si el link parece ser de un inmueble específico"""
        texto_completo = f"{url} {mensaje}".lower()
        return any(kw in texto_completo for kw in self.KEYWORDS_INMUEBLE)

    def _resultado_sin_link(self) -> LinkDetectionResult:
        """Retorna resultado vacío cuando no hay links"""
        return LinkDetectionResult(
            tiene_link=False,
            portal=PortalOrigen.WHATSAPP_DIRECT,
            url_original=None,
            es_inmueble=False,
            metadata_extra={}
        )

    def obtener_nombre_portal(self, portal: PortalOrigen) -> str:
        """Retorna nombre amigable del portal para usar en respuestas"""
        nombres = {
            PortalOrigen.INSTAGRAM: "Instagram",
            PortalOrigen.FACEBOOK: "Facebook",
            PortalOrigen.FINCA_RAIZ: "Finca Raíz",
            PortalOrigen.METRO_CUADRADO: "Metrocuadrado",
            PortalOrigen.MERCADO_LIBRE: "Mercado Libre",
            PortalOrigen.CIENCUADRAS: "Ciencuadras",
            PortalOrigen.PAGINA_WEB: "nuestra página web",
            PortalOrigen.WHATSAPP_DIRECT: "WhatsApp",
            PortalOrigen.DESCONOCIDO: "internet",
        }
        return nombres.get(portal, "internet")