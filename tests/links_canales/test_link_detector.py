# tests/utils/test_link_detector.py
import pytest
from utils.link_detector import LinkDetector, LinkDetectionResult, PortalOrigen


@pytest.fixture
def detector():
    """Fixture que proporciona una instancia de LinkDetector"""
    return LinkDetector()


# ═══════════════════════════════════════════════════════════════════════════════
# TESTS DE DETECCIÓN DE PORTALES ESPECÍFICOS
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeteccionFincaRaiz:
    """Tests para links de Finca Raíz"""

    def test_detecta_link_fincaraiz_completo(self, detector):
        mensaje = "Hola, vi este apartamento https://www.fincaraiz.com.co/apartamento-en-arriendo/medellin/el-poblado/123456"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.portal == PortalOrigen.FINCA_RAIZ
        assert "fincaraiz.com.co" in resultado.url_original
        assert resultado.es_inmueble is True

    def test_detecta_link_fincaraiz_sin_www(self, detector):
        mensaje = "Me interesa https://fincaraiz.com.co/casa/123"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.portal == PortalOrigen.FINCA_RAIZ

    def test_detecta_link_fincaraiz_sin_https(self, detector):
        mensaje = "Mira fincaraiz.com.co/apartamento/456"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.portal == PortalOrigen.FINCA_RAIZ


class TestDeteccionMetrocuadrado:
    """Tests para links de Metrocuadrado"""

    def test_detecta_link_metrocuadrado(self, detector):
        mensaje = "Vi esta casa https://www.metrocuadrado.com/inmueble/venta-casa-medellin"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.portal == PortalOrigen.METRO_CUADRADO
        assert resultado.es_inmueble is True

    def test_detecta_metrocuadrado_sin_www(self, detector):
        mensaje = "https://metrocuadrado.com/propiedad/123"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.portal == PortalOrigen.METRO_CUADRADO


class TestDeteccionMercadoLibre:
    """Tests para links de Mercado Libre"""

    def test_detecta_link_mercadolibre(self, detector):
        mensaje = "https://inmuebles.mercadolibre.com.co/apartamento-venta"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.portal == PortalOrigen.MERCADO_LIBRE

    def test_detecta_mercadolibre_dominio_principal(self, detector):
        mensaje = "Mira esto https://www.mercadolibre.com.co/inmueble/casa"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.portal == PortalOrigen.MERCADO_LIBRE


class TestDeteccionCiencuadras:
    """Tests para links de Ciencuadras"""

    def test_detecta_link_ciencuadras(self, detector):
        mensaje = "https://www.ciencuadras.com/arriendo/apartamento/medellin"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.portal == PortalOrigen.CIENCUADRAS


class TestDeteccionRedesSociales:
    """Tests para links de Instagram y Facebook"""

    def test_detecta_link_instagram(self, detector):
        mensaje = "Vi esta publicación https://www.instagram.com/p/ABC123xyz/"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.portal == PortalOrigen.INSTAGRAM

    def test_detecta_link_instagram_corto(self, detector):
        mensaje = "Mira https://instagr.am/p/XYZ789"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.portal == PortalOrigen.INSTAGRAM

    def test_detecta_link_facebook(self, detector):
        mensaje = "Lo vi en https://www.facebook.com/marketplace/item/123456"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.portal == PortalOrigen.FACEBOOK

    def test_detecta_link_facebook_mobile(self, detector):
        mensaje = "https://m.facebook.com/post/789"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.portal == PortalOrigen.FACEBOOK

    def test_detecta_link_fb_corto(self, detector):
        mensaje = "https://fb.com/inmueble/456"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.portal == PortalOrigen.FACEBOOK


class TestDeteccionPaginaWeb:
    """Tests para links de la página web de la inmobiliaria"""

    def test_detecta_link_inmobiliaria_proteger(self, detector):
        mensaje = "https://www.inmobiliariaproteger.com/propiedad/123"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.portal == PortalOrigen.PAGINA_WEB


# ═══════════════════════════════════════════════════════════════════════════════
# TESTS DE MENSAJES SIN LINKS
# ═══════════════════════════════════════════════════════════════════════════════

class TestMensajesSinLinks:
    """Tests para mensajes sin links"""

    def test_mensaje_sin_link(self, detector):
        mensaje = "Hola, busco un apartamento en Medellín"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is False
        assert resultado.portal == PortalOrigen.WHATSAPP_DIRECT
        assert resultado.url_original is None
        assert resultado.es_inmueble is False

    def test_mensaje_vacio(self, detector):
        resultado = detector.analizar_mensaje("")

        assert resultado.tiene_link is False
        assert resultado.portal == PortalOrigen.WHATSAPP_DIRECT

    def test_mensaje_none(self, detector):
        resultado = detector.analizar_mensaje(None)

        assert resultado.tiene_link is False

    def test_mensaje_solo_texto_con_palabras_similares(self, detector):
        mensaje = "Busco en fincaraiz pero no tengo link"
        resultado = detector.analizar_mensaje(mensaje)

        # No debe detectar porque no tiene URL válida
        assert resultado.tiene_link is False


# ═══════════════════════════════════════════════════════════════════════════════
# TESTS DE DETECCIÓN DE INMUEBLE
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeteccionInmueble:
    """Tests para verificar si un link es de inmueble específico"""

    def test_link_con_apartamento_en_url(self, detector):
        mensaje = "https://fincaraiz.com.co/apartamento-arriendo/123"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.es_inmueble is True

    def test_link_con_casa_en_mensaje(self, detector):
        mensaje = "Vi esta casa https://fincaraiz.com.co/inmueble/456"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.es_inmueble is True

    def test_link_con_arriendo_en_url(self, detector):
        mensaje = "https://metrocuadrado.com/arriendo-apartamento"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.es_inmueble is True

    def test_link_con_venta_en_url(self, detector):
        mensaje = "https://ciencuadras.com/venta/casa/medellin"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.es_inmueble is True

    def test_link_generico_sin_keywords(self, detector):
        mensaje = "Mira https://www.example.com/pagina/random"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.portal == PortalOrigen.DESCONOCIDO
        # Sin keywords de inmueble
        assert resultado.es_inmueble is False

    def test_link_generico_con_keyword_en_mensaje(self, detector):
        mensaje = "Vi este apartamento https://www.example.com/item/123"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.es_inmueble is True  # keyword "apartamento" en el mensaje


# ═══════════════════════════════════════════════════════════════════════════════
# TESTS DE URLS GENÉRICAS (PORTAL DESCONOCIDO)
# ═══════════════════════════════════════════════════════════════════════════════

class TestUrlsGenericas:
    """Tests para URLs de portales no conocidos"""

    def test_url_generica_https(self, detector):
        mensaje = "Mira esto https://www.otroportal.com/inmueble/123"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.portal == PortalOrigen.DESCONOCIDO

    def test_url_generica_www(self, detector):
        mensaje = "www.sitioaleatorio.com/propiedad"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.portal == PortalOrigen.DESCONOCIDO


# ═══════════════════════════════════════════════════════════════════════════════
# TESTS DE METADATA
# ═══════════════════════════════════════════════════════════════════════════════

class TestMetadata:
    """Tests para verificar metadata_extra"""

    def test_metadata_contiene_mensaje_completo(self, detector):
        mensaje = "Hola, vi este apartamento https://fincaraiz.com.co/apt/123 me interesa"
        resultado = detector.analizar_mensaje(mensaje)

        assert "mensaje_completo" in resultado.metadata_extra
        assert resultado.metadata_extra["mensaje_completo"] == mensaje

    def test_metadata_contiene_posicion_link(self, detector):
        mensaje = "Hola https://fincaraiz.com.co/apt/123"
        resultado = detector.analizar_mensaje(mensaje)

        assert "posicion_link" in resultado.metadata_extra
        # La posición debe ser una tupla (inicio, fin)
        assert isinstance(resultado.metadata_extra["posicion_link"], tuple)


# ═══════════════════════════════════════════════════════════════════════════════
# TESTS DE NOMBRE AMIGABLE DE PORTAL
# ═══════════════════════════════════════════════════════════════════════════════

class TestNombrePortal:
    """Tests para obtener_nombre_portal"""

    def test_nombre_finca_raiz(self, detector):
        nombre = detector.obtener_nombre_portal(PortalOrigen.FINCA_RAIZ)
        assert nombre == "Finca Raíz"

    def test_nombre_metrocuadrado(self, detector):
        nombre = detector.obtener_nombre_portal(PortalOrigen.METRO_CUADRADO)
        assert nombre == "Metrocuadrado"

    def test_nombre_instagram(self, detector):
        nombre = detector.obtener_nombre_portal(PortalOrigen.INSTAGRAM)
        assert nombre == "Instagram"

    def test_nombre_facebook(self, detector):
        nombre = detector.obtener_nombre_portal(PortalOrigen.FACEBOOK)
        assert nombre == "Facebook"

    def test_nombre_mercado_libre(self, detector):
        nombre = detector.obtener_nombre_portal(PortalOrigen.MERCADO_LIBRE)
        assert nombre == "Mercado Libre"

    def test_nombre_desconocido(self, detector):
        nombre = detector.obtener_nombre_portal(PortalOrigen.DESCONOCIDO)
        assert nombre == "internet"

    def test_nombre_whatsapp_direct(self, detector):
        nombre = detector.obtener_nombre_portal(PortalOrigen.WHATSAPP_DIRECT)
        assert nombre == "WhatsApp"


# ═══════════════════════════════════════════════════════════════════════════════
# TESTS DE CASOS EDGE
# ═══════════════════════════════════════════════════════════════════════════════

class TestCasosEdge:
    """Tests para casos especiales y edge cases"""

    def test_multiples_links_detecta_primero(self, detector):
        mensaje = "https://fincaraiz.com.co/apt/1 y también https://metrocuadrado.com/casa/2"
        resultado = detector.analizar_mensaje(mensaje)

        # Debe detectar el primero que encuentre según el orden de PATRONES_PORTALES
        assert resultado.tiene_link is True
        # El orden en PATRONES_PORTALES tiene FINCA_RAIZ primero
        assert resultado.portal == PortalOrigen.FINCA_RAIZ

    def test_link_con_espacios_alrededor(self, detector):
        mensaje = "   https://fincaraiz.com.co/inmueble/123   "
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.portal == PortalOrigen.FINCA_RAIZ

    def test_link_mayusculas(self, detector):
        mensaje = "HTTPS://WWW.FINCARAIZ.COM.CO/APARTAMENTO/123"
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.portal == PortalOrigen.FINCA_RAIZ

    def test_mensaje_largo_con_link_al_final(self, detector):
        mensaje = (
            "Buenos días, estoy buscando un apartamento en el sector del Poblado, "
            "vi una publicación muy interesante que me gustaría que me ayudaran con más información: "
            "https://www.fincaraiz.com.co/apartamento-en-arriendo/medellin/el-poblado/codigo-123456"
        )
        resultado = detector.analizar_mensaje(mensaje)

        assert resultado.tiene_link is True
        assert resultado.portal == PortalOrigen.FINCA_RAIZ
        assert resultado.es_inmueble is True