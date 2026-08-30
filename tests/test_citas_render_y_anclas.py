"""Renderizado y anclas de las citaciones en el panel.

Why: había tres copias del HTML de la burbuja de cita —historial, eco
optimista y actualización por WebSocket— y habían divergido. La del WebSocket
inyectaba `rp.content` sin escapar mediante insertAdjacentHTML, y ese texto es
un mensaje de WhatsApp del cliente: entrada controlada por un tercero. Estaba
inalcanzable en producción (`conversation_sid` al 0%), que es justo lo que la
hacía peligrosa — código muerto que una migración reviviría con el fallo
dentro, igual que pasó con los Content SID.

Ahora hay una sola función. Estos tests fijan que siga habiendo una sola y que
cumpla las cuatro propiedades que las copias habían perdido.

Se analiza el fuente porque el panel no tiene arnés de JS en este repo, mismo
criterio que `test_memory_leak_diagnosis.py`. La sintaxis se valida aparte con
`node --check`.
"""

import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
INDEX_JS = (RAIZ / "middleware/PanelAsesores/index.js").read_text(encoding="utf-8")
WEBHOOK_PY = (RAIZ / "middleware/webhook_handler.py").read_text(encoding="utf-8")


def _funcion_renderizadora() -> str:
    """El cuerpo de `construirCitaHtml`, única fuente del HTML de una cita."""
    m = re.search(
        r"function construirCitaHtml\(.*?\n\}", INDEX_JS, re.DOTALL
    )
    assert m, "no se encontro construirCitaHtml"
    return m.group(0)


# ── Fuente única ────────────────────────────────────────────────────────────

def test_hay_un_unico_renderizador_de_cita():
    """Si vuelve a aparecer HTML de cita fuera de la función, la divergencia
    —y con ella el fallo de escapado— puede reaparecer sin que nadie lo note."""
    bloques = INDEX_JS.count('class="reply-quote')
    assert bloques == 1, (
        f"hay {bloques} sitios construyendo la burbuja de cita; debe haber 1"
    )


def test_los_tres_sitios_consumen_la_funcion_compartida():
    """Historial, eco optimista y WebSocket."""
    llamadas = len(re.findall(r"construirCitaHtml\(", INDEX_JS))
    assert llamadas == 4, f"se esperaban 3 llamadas + 1 definicion, hay {llamadas}"


# ── Las cuatro propiedades que las copias habían perdido ────────────────────

def test_escapa_el_contenido_citado():
    """El texto citado viene de WhatsApp. Es el fallo que motivó la
    unificación: la copia del WebSocket lo inyectaba en crudo."""
    fn = _funcion_renderizadora()
    assert "escapeHtml((rp.content" in fn, "el contenido citado no se escapa"
    assert "escapeHtml(remitente)" in fn, "el nombre del remitente no se escapa"


def test_distingue_los_tres_remitentes():
    """Cliente gris, bot ámbar, asesora azul. La copia del WebSocket sólo
    contemplaba dos y pintaba al bot como si fuera el cliente."""
    fn = _funcion_renderizadora()
    for color, quien in (("#6B7280", "cliente"), ("#D97706", "bot"), ("#2563EB", "asesora")):
        assert color in fn, f"falta el color de {quien}"


def test_usa_el_nombre_real_del_remitente():
    """La copia rotulaba 'Tú'/'Asesor' fijo."""
    assert "rp.sender_name" in _funcion_renderizadora()


def test_usa_los_iconos_de_media():
    """La copia pintaba `[image]` en vez del icono."""
    fn = _funcion_renderizadora()
    assert "CITA_ICONOS" in fn
    assert "📷 Imagen" in INDEX_JS


def test_el_id_del_salto_se_valida_antes_de_entrar_en_el_onclick():
    """El id se interpola dentro de un atributo onclick. escapeHtml no escapa
    la comilla simple, así que se valida con lista blanca en su lugar."""
    fn = _funcion_renderizadora()
    assert "/^[A-Za-z0-9_-]+$/.test" in fn, (
        "el id entra crudo en el onclick: un id con comilla rompe el atributo"
    )


# ── Guardas sobre la ruta que hacía peligrosa la copia ──────────────────────

def test_solo_hay_un_publicador_de_message_updated():
    """La actualización en vivo tiene un único emisor, el fetch diferido, tras
    un guard de `conversation_sid` que en producción está al 0%. Si aparece
    otro emisor esa ruta se vuelve caliente y conviene revisarla entera."""
    assert WEBHOOK_PY.count('"type":       "message_updated"') + \
           WEBHOOK_PY.count('"type": "message_updated"') == 1


def test_la_edicion_del_cliente_publica_un_tipo_distinto():
    """`onMessageUpdated` publica `message_edited`. Confundir ambos cambiaría
    qué código se ejecuta en vivo."""
    assert '"type": "message_edited"' in WEBHOOK_PY


# ── El salto de la cita ─────────────────────────────────────────────────────
#
# Medido sobre 1.203 citas: 1.006 apuntan a un mensaje ya cargado, 163 apuntan
# fuera de la primera página y 34 a un mensaje inexistente. Los 197 ultimos
# eran un `return` mudo que la asesora leía como que el panel se colgó.

def _cuerpo_scroll_to_message() -> str:
    m = re.search(
        r"async function scrollToMessage\([^)]*\)\s*\{(.*?)\n\}", INDEX_JS, re.DOTALL
    )
    assert m, "no se encontro scrollToMessage (¿dejo de ser async?)"
    return m.group(1)


def test_el_destino_ausente_ya_no_se_ignora_en_silencio():
    cuerpo = _cuerpo_scroll_to_message()
    assert "if (!el) return;" not in cuerpo
    assert "showToast" in cuerpo, "sin aviso, el clic sigue pareciendo un cuelgue"


def test_busca_el_mensaje_en_las_paginas_anteriores():
    """El 13,5% de las citas apuntan fuera de la primera página: hay que traer
    historial antes de rendirse."""
    assert "loadOlderMessages" in _cuerpo_scroll_to_message()


def test_la_busqueda_tiene_tope_de_paginas():
    """Sin tope, un solo clic podría encadenar decenas de peticiones."""
    cuerpo = _cuerpo_scroll_to_message()
    assert "CITA_MAX_PAGINAS" in cuerpo
    assert re.search(r"const CITA_MAX_PAGINAS = \d+;", INDEX_JS), "el tope no es constante"


def test_no_puede_quedarse_dando_vueltas():
    """`loadOlderMessages` no hace nada si otra carga tiene el guard tomado.
    Sin comprobar que el cursor avanzo, el bucle giraria sin fin."""
    cuerpo = _cuerpo_scroll_to_message()
    assert "cursorAntes" in cuerpo and "break" in cuerpo


def test_el_aviso_distingue_los_dos_casos():
    """No es lo mismo 'esta mas atras' que 'ya no existe': la primera invita a
    seguir subiendo, la segunda cierra el asunto."""
    cuerpo = _cuerpo_scroll_to_message()
    assert "más atrás en la conversación" in cuerpo
    assert "ya no está disponible" in cuerpo
