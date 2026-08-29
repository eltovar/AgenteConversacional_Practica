"""Renderizado y anclas de las citaciones en el panel.

Why: la investigación de agosto de 2026 encontró tres implementaciones
distintas de la misma burbuja de cita en `index.js`, y un ancla que falla en
silencio. Ninguna de las dos cosas causa el síntoma que reportó la asesora —la
cita que no aparece—, pero una de ellas es un sumidero de XSS latente y la otra
deja el 16% de los clics sin hacer nada.

Los tests marcados `xfail` describen el estado deseado y hoy fallan a
propósito: pasan a verde cuando se corrija cada defecto, sin tocarlos. Es el
mismo patrón que `test_content_sids_coherencia.py`.

Se analiza el fuente porque el panel no tiene arnés de JS en este repo, mismo
criterio que `test_memory_leak_diagnosis.py`.
"""

import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
INDEX_JS = (RAIZ / "middleware/PanelAsesores/index.js").read_text(encoding="utf-8")
WEBHOOK_PY = (RAIZ / "middleware/webhook_handler.py").read_text(encoding="utf-8")

# Cada bloque que construye el HTML de una burbuja de cita, con su contexto.
# La ventana toma texto ANTES y DESPUES del `<div>`: las variables (color,
# texto citado) se asignan arriba y `sender_name` se usa dentro del template.
# Con una ventana solo hacia atras, el bloque optimista quedaba sin
# `sender_name` y su test no habria podido pasar nunca.
_BLOQUES = re.compile(
    r'.{900}<div class="reply-quote.{600}', re.DOTALL
)


def _bloques_de_cita() -> list:
    """Los fragmentos de `index.js` que arman una burbuja de cita."""
    return _BLOQUES.findall(INDEX_JS)


# ── Lo que ya es cierto: guardas de regresión ───────────────────────────────

def test_hay_exactamente_tres_renderizadores_de_cita():
    """Documenta el estado de partida. Si aparece un cuarto, o si la
    unificación reduce el número, este test obliga a revisar los de abajo."""
    assert len(_bloques_de_cita()) == 3, (
        f"se esperaban 3 bloques `reply-quote`, hay {len(_bloques_de_cita())}"
    )


def test_solo_hay_un_publicador_de_message_updated():
    """El renderizador divergente (index.js:~6003) sólo se alcanza por este
    evento, y su único emisor está detrás de `conversation_sid`, que en
    producción está al 0%. Si alguien añade otro emisor, ese renderizador se
    vuelve alcanzable — y con él su fallo de escapado."""
    assert WEBHOOK_PY.count('"type":       "message_updated"') + \
           WEBHOOK_PY.count('"type": "message_updated"') == 1


def test_la_edicion_del_cliente_no_usa_el_renderizador_divergente():
    """`onMessageUpdated` publica `message_edited`, un tipo distinto. Confundir
    ambos haría alcanzable el renderizador inseguro."""
    assert '"type": "message_edited"' in WEBHOOK_PY


def test_el_renderizador_canonico_distingue_los_tres_remitentes():
    """Cliente gris, bot ámbar, asesora azul. Es el comportamiento correcto que
    los otros dos deben respetar."""
    canonico = _bloques_de_cita()[0]
    assert "'#6B7280'" in canonico or "#6B7280" in canonico
    assert "#D97706" in canonico, "falta el caso del bot"
    assert "#2563EB" in canonico


def test_la_burbuja_optimista_escapa_el_contenido():
    """El eco local de un envío propio ya escapa. Guarda de regresión."""
    optimista = _bloques_de_cita()[1]
    assert "escapeHtml" in optimista


# ── Lo que aún no es cierto: se ponen en verde al corregir ─────────────────

@pytest.mark.xfail(
    reason="index.js:~6003 inyecta rp.content sin escapar via insertAdjacentHTML",
    strict=False,
)
def test_todos_los_renderizadores_escapan_el_contenido_citado():
    """El texto citado viene de un mensaje de WhatsApp del cliente: es entrada
    controlada por terceros. Los otros dos bloques lo pasan por `escapeHtml`;
    el del WebSocket no, y lo inyecta con `insertAdjacentHTML`."""
    sin_escapar = [i for i, b in enumerate(_bloques_de_cita()) if "escapeHtml" not in b]
    assert not sin_escapar, (
        f"bloques que no escapan el contenido citado: {sin_escapar}"
    )


@pytest.mark.xfail(
    reason="index.js:~6003 usa #3b82f6/#6b7280 en vez de la paleta canonica",
    strict=False,
)
def test_todos_los_renderizadores_usan_la_misma_paleta():
    """La misma cita no puede cambiar de color según por dónde llegó."""
    for i, bloque in enumerate(_bloques_de_cita()):
        assert "#2563EB" in bloque, f"bloque {i}: azul de asesora fuera de paleta"
        assert "#D97706" in bloque, f"bloque {i}: falta el ámbar del bot"


@pytest.mark.xfail(
    reason="index.js:~6003 rotula 'Tu'/'Asesor' fijo en vez de usar sender_name",
    strict=False,
)
def test_todos_los_renderizadores_usan_el_nombre_del_remitente():
    for i, bloque in enumerate(_bloques_de_cita()):
        assert "sender_name" in bloque, f"bloque {i}: rotulo fijo en vez de sender_name"


@pytest.mark.xfail(
    reason="index.js:~6003 pinta [image] en vez del icono; divergencia visual",
    strict=False,
)
def test_todos_los_renderizadores_usan_los_mismos_iconos_de_media():
    for i, bloque in enumerate(_bloques_de_cita()):
        assert "📷 Imagen" in bloque, f"bloque {i}: iconos de media distintos"


@pytest.mark.xfail(
    reason="scrollToMessage hace `if (!el) return;` — el 16.4% de los clics no hace nada",
    strict=False,
)
def test_el_clic_en_una_cita_avisa_cuando_no_puede_saltar():
    """Medido sobre 1.203 citas: 163 apuntan fuera de la página inicial de 50
    mensajes y 34 a un mensaje inexistente. En los 197 casos el clic es un
    no-op mudo, que la asesora lee como que el panel se colgó."""
    cuerpo = re.search(
        r"function scrollToMessage\([^)]*\)\s*\{(.*?)\n\}", INDEX_JS, re.DOTALL
    )
    assert cuerpo, "no se encontro scrollToMessage"
    texto = cuerpo.group(1)
    assert "if (!el) return;" not in texto, (
        "el destino ausente se ignora en silencio: hay que cargar mas historial "
        "o avisar de que el mensaje citado no esta a la vista"
    )
