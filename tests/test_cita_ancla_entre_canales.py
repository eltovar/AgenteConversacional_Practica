"""El ancla de una cita sobrevive a que el hilo esté partido por canal.

Why: el canal se decide POR MENSAJE, según el link que trae el texto, así que
un mismo hilo de WhatsApp queda repartido entre varias etiquetas — 1.144 de
3.382 teléfonos están partidos. Y el backend es asimétrico: la resolución de
la cita busca sin filtro de canal y acierta (la burbuja se pinta), mientras el
historial filtra por `{phone, channel}` y el ancla nunca llega al DOM.

Resultado medido sobre 1.257 citas: 106 (8,4%) apuntan a un mensaje de otro
canal. Paginar hacia atrás no puede encontrarlas nunca, y el panel acababa
diciendo "ya no está disponible" sobre mensajes que sí existen. Las 106 están
a mediana 2 mensajes de distancia y máximo 17: una sola carga sin filtro las
cubre todas.

Se analiza el fuente porque el panel no tiene arnés de JS en este repo, mismo
criterio que `test_citas_render_y_anclas.py`. La sintaxis se valida con
`node --check`.
"""

import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
INDEX_JS = (RAIZ / "middleware/PanelAsesores/index.js").read_text(encoding="utf-8")


def _cuerpo(nombre: str) -> str:
    """Cuerpo de una función JS por conteo de llaves.

    Se salta primero la lista de parámetros balanceando paréntesis: con
    `loadChatHistory(contactId, opciones = {})` la primera llave del fuente es
    la del valor por defecto, no la del cuerpo, y buscarla a ciegas devolvía
    `{}` — un cuerpo vacío contra el que cualquier aserción de contenido falla
    sin que el código tenga nada malo.
    """
    m = re.search(rf"(?:async\s+)?function\s+{re.escape(nombre)}\s*\(", INDEX_JS)
    assert m, f"no se encontró la función {nombre}"
    prof = 0
    for j in range(m.end() - 1, len(INDEX_JS)):
        if INDEX_JS[j] == "(":
            prof += 1
        elif INDEX_JS[j] == ")":
            prof -= 1
            if prof == 0:
                break
    i = INDEX_JS.index("{", j)
    prof = 0
    for j in range(i, len(INDEX_JS)):
        if INDEX_JS[j] == "{":
            prof += 1
        elif INDEX_JS[j] == "}":
            prof -= 1
            if prof == 0:
                return INDEX_JS[i:j + 1]
    pytest.fail(f"llaves desbalanceadas en {nombre}")


# ── El arreglo existe ───────────────────────────────────────────────────────

def test_scroll_amplia_a_todos_los_canales_antes_de_rendirse():
    cuerpo = _cuerpo("scrollToMessage")
    assert "todosLosCanales" in cuerpo, (
        "scrollToMessage no intenta el hilo completo: las 106 citas cruzadas "
        "seguirían sin ancla por mucho que pagine"
    )


def test_la_ampliacion_ocurre_antes_del_aviso_de_no_disponible():
    """Si avisara primero, el intento no serviría de nada."""
    cuerpo = _cuerpo("scrollToMessage")
    amplia = cuerpo.index("todosLosCanales")
    aviso = cuerpo.index("ya no está disponible")
    assert amplia < aviso


def test_solo_amplia_si_habia_filtro_de_canal():
    """Sin filtro activo no hay nada que ampliar; evita trabajo inútil y bucles."""
    cuerpo = _cuerpo("scrollToMessage")
    bloque = cuerpo[:cuerpo.index("todosLosCanales")]
    assert "chatHistoryState.canal" in bloque


def test_load_chat_history_acepta_la_opcion():
    cuerpo = _cuerpo("loadChatHistory")
    assert re.search(r"opciones\.todosLosCanales\s*\?\s*null\s*:\s*currentCanal", cuerpo), (
        "loadChatHistory debe omitir el canal cuando se le pide el hilo completo"
    )


def test_el_cambio_de_vista_se_avisa():
    """Aparecen mensajes que no estaban: callarlo es un cambio invisible."""
    cuerpo = _cuerpo("scrollToMessage")
    assert "otros canales" in cuerpo and "showToast" in cuerpo


# ── La regresión que casi se cuela ──────────────────────────────────────────

def test_los_reintentos_conservan_las_opciones():
    """Un reintento sin opciones deshace la ampliación a los 3-8 segundos.

    `loadChatHistory` se re-llama sola en tres ramas: 502/503/504, timeout de
    HubSpot y error de red. La de HubSpot está en la ruta de éxito, así que la
    vista ya se había ampliado y la asesora ya había leído el aviso: 8 segundos
    después los mensajes desaparecerían delante de ella.
    """
    cuerpo = _cuerpo("loadChatHistory")
    reintentos = re.findall(r"loadChatHistory\((.*?)\)\s*;", cuerpo)
    assert reintentos, "no se encontró ninguna re-llamada dentro de loadChatHistory"
    sin_opciones = [r for r in reintentos if "opciones" not in r]
    assert not sin_opciones, (
        f"reintentos que pierden las opciones: {sin_opciones}. "
        "Revertirían al filtro de canal y desharían la ampliación."
    )


def test_los_llamadores_externos_no_necesitan_la_opcion():
    """El parámetro es opcional: nadie más tuvo que cambiar."""
    externos = [
        m for m in re.findall(r"loadChatHistory\(([^)]*)\)", INDEX_JS)
        if "todosLosCanales" not in m
    ]
    assert externos, "se esperaban llamadas sin la opción"
    assert re.search(r"function\s+loadChatHistory\s*\([^)]*opciones\s*=\s*\{\}", INDEX_JS), (
        "sin valor por defecto en la firma, los 17 llamadores existentes romperían"
    )
