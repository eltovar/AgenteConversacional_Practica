"""
Módulo de detección y clasificación de links de portales inmobiliarios.
Proyecto Sofía - Inmobiliaria Proteger
"""

import re
from typing import Optional, Dict, Any
from dataclasses import dataclass
from enum import Enum


class PortalOrigen(str, Enum):
    """Portales inmobiliarios y redes sociales soportados"""
    # Redes Sociales
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    LINKEDIN = "linkedin"
    YOUTUBE = "youtube"
    TIKTOK = "tiktok"
    # Portales Inmobiliarios
    FINCA_RAIZ = "finca_raiz"
    METRO_CUADRADO = "metrocuadrado"
    MERCADO_LIBRE = "mercado_libre"
    CIENCUADRAS = "ciencuadras"
    PAGINA_WEB = "pagina_web"
    # Otros
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
    Detecta y clasifica links de portales inmobiliarios en mensajes de WhatsApp
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
        PortalOrigen.LINKEDIN: [
            r'https?://(?:www\.)?linkedin\.com/[^\s]+',
            r'linkedin\.com/[^\s]+',
            r'https?://(?:www\.)?lnkd\.in/[^\s]+',  # Short links
        ],
        PortalOrigen.YOUTUBE: [
            r'https?://(?:www\.)?youtube\.com/[^\s]+',
            r'https?://youtu\.be/[^\s]+',
            r'youtube\.com/[^\s]+',
            r'https?://(?:www\.)?youtube\.com/shorts/[^\s]+',
        ],
        PortalOrigen.TIKTOK: [
            r'https?://(?:www\.)?tiktok\.com/[^\s]+',
            r'https?://vm\.tiktok\.com/[^\s]+',  # Short links
            r'tiktok\.com/@[^\s]+',
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
        '/p/', '/listing/', '/detail/', '/inmueble/', '/property/',
        # Instagram content patterns
        '/reel/', '/reels/', '/stories/',
        # Facebook content patterns
        '/posts/', '/videos/', '/watch/', '/groups/', '/marketplace/',
        # Palabras clave de interés
        'interesa', 'interesado', 'interesada', 'info', 'información',
        'precio', 'disponible', 'disponibilidad', 'más detalles'
    ]

    # Patrones de URL de contenido específico en redes sociales
    # Estos se consideran links de inmueble por defecto (la inmobiliaria publica propiedades)
    SOCIAL_MEDIA_CONTENT_PATTERNS = [
        # Instagram
        r'/p/[A-Za-z0-9_-]+',      # Instagram posts: /p/ABC123
        r'/reel/[A-Za-z0-9_-]+',   # Instagram reels: /reel/ABC123
        r'/reels/[A-Za-z0-9_-]+',  # Instagram reels alternate
        r'/stories/',              # Instagram stories
        # Facebook
        r'/posts/',                # Facebook posts
        r'/videos/',               # Facebook videos
        r'/watch/',                # Facebook watch
        r'/marketplace/',          # Facebook marketplace
        r'/groups/\d+/posts/',     # Facebook group posts
        r'/photo',                 # Facebook photos
        r'\?fbid=',                # Facebook photo IDs
        r'\?v=',                   # Facebook video IDs
        # LinkedIn
        r'/posts/[^\s]+',          # LinkedIn posts
        r'/pulse/',                # LinkedIn articles
        r'/feed/update/',          # LinkedIn feed updates
        # YouTube
        r'/watch\?v=',             # YouTube videos
        r'/shorts/',               # YouTube shorts
        r'youtu\.be/',             # YouTube short links
        # TikTok
        r'/@[^/]+/video/',         # TikTok videos
        r'/video/\d+',             # TikTok video IDs
        r'vm\.tiktok\.com/',       # TikTok short links
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
                        es_inmueble=self._es_link_inmueble(url, mensaje_lower, portal),
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

    def _es_link_inmueble(self, url: str, mensaje: str, portal: PortalOrigen = None) -> bool:
        """
        Determina si el link parece ser de un inmueble específico.

        Para redes sociales (Instagram/Facebook), los links de contenido
        (posts, reels, videos, etc.) se consideran inmuebles por defecto
        ya que la inmobiliaria los usa para promocionar propiedades.
        """
        url_lower = url.lower() if url else ""
        mensaje_lower = mensaje.lower() if mensaje else ""
        texto_completo = f"{url_lower} {mensaje_lower}"

        # Verificar keywords generales primero
        if any(kw in texto_completo for kw in self.KEYWORDS_INMUEBLE):
            return True

        # Para redes sociales, verificar si es un link de contenido
        # (posts, reels, videos, etc.) - estos se asumen como inmuebles
        redes_sociales = [
            PortalOrigen.INSTAGRAM,
            PortalOrigen.FACEBOOK,
            PortalOrigen.LINKEDIN,
            PortalOrigen.YOUTUBE,
            PortalOrigen.TIKTOK,
        ]

        if portal in redes_sociales:
            for pattern in self.SOCIAL_MEDIA_CONTENT_PATTERNS:
                if re.search(pattern, url_lower):
                    return True

            # URLs con parámetros de sharing son contenido específico
            if '?' in url_lower:
                # Instagram sharing params
                if 'igsh=' in url_lower or 'utm_' in url_lower:
                    return True
                # YouTube video params
                if 'v=' in url_lower:
                    return True
                # TikTok params
                if 'is_from_webapp' in url_lower:
                    return True

        return False

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
            # Redes Sociales
            PortalOrigen.INSTAGRAM: "Instagram",
            PortalOrigen.FACEBOOK: "Facebook",
            PortalOrigen.LINKEDIN: "LinkedIn",
            PortalOrigen.YOUTUBE: "YouTube",
            PortalOrigen.TIKTOK: "TikTok",
            # Portales Inmobiliarios
            PortalOrigen.FINCA_RAIZ: "Finca Raíz",
            PortalOrigen.METRO_CUADRADO: "Metrocuadrado",
            PortalOrigen.MERCADO_LIBRE: "Mercado Libre",
            PortalOrigen.CIENCUADRAS: "Ciencuadras",
            PortalOrigen.PAGINA_WEB: "nuestra página web",
            # Otros
            PortalOrigen.WHATSAPP_DIRECT: "WhatsApp",
            PortalOrigen.DESCONOCIDO: "internet",
        }
        return nombres.get(portal, "internet")