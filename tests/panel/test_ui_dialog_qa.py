"""
QA Fase 6 — Modales de decision del panel (ui-dialog.js).

Sustituyen a confirm()/prompt() nativos en los 13 puntos de decision. Esta QA
valida el contrato completo: que no queden nativos, que cada await este en
contexto async, que los tonos existan en CSS y que no haya clases Tailwind
dinamicas (que el build purgaria dejando el modal sin estilo).

Ejecutar:
    python -m pytest tests/panel/test_ui_dialog_qa.py -v
"""
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PANEL = os.path.join(ROOT, "middleware", "PanelAsesores")
INDEX_JS = os.path.join(PANEL, "index.js")
DIALOG_JS = os.path.join(PANEL, "ui-dialog.js")
STYLE_CSS = os.path.join(PANEL, "style.css")
INDEX_HTML = os.path.join(PANEL, "index.html")
TW_CONFIG = os.path.join(PANEL, "tailwind.config.js")

TONES = ["danger", "warning", "info", "success"]


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ═══════════════════════════════════════════════════════════════
# Migracion: no deben quedar dialogos nativos de decision
# ═══════════════════════════════════════════════════════════════

def test_01_sin_confirm_ni_prompt_nativos():
    """Ningun confirm()/prompt() nativo debe sobrevivir en el panel."""
    js = _read(INDEX_JS)
    restos = []
    for i, line in enumerate(js.split("\n"), 1):
        code = line.split("//")[0]
        if re.search(r"(?<![\w.])(confirm|prompt)\s*\(", code):
            restos.append(f"L{i}: {line.strip()[:70]}")

    assert not restos, "quedan dialogos nativos:\n  " + "\n  ".join(restos)


def test_02_los_13_sitios_migrados():
    """Los 13 puntos de decision usan las primitivas nuevas."""
    js = _read(INDEX_JS)
    total = (
        len(re.findall(r"await confirmDialog\(", js))
        + len(re.findall(r"await choiceDialog\(", js))
        + len(re.findall(r"await promptDialog\(", js))
    )
    print(f"\n  [METRIC] Sitios migrados: {total}")
    assert total == 13, f"esperados 13 sitios migrados, hay {total}"


def test_03_todo_await_en_contexto_async():
    """
    Un await fuera de funcion async es SyntaxError y romperia el panel entero.
    Es el riesgo principal de cambiar confirm() sincrono por una Promise.
    """
    lines = _read(INDEX_JS).split("\n")
    malos = []
    for i, line in enumerate(lines):
        if not re.search(r"await (confirmDialog|choiceDialog|promptDialog)\(", line):
            continue
        for j in range(i, -1, -1):
            m = re.match(r"^\s*(async\s+)?function\s+(\w+)", lines[j])
            if m:
                if not m.group(1):
                    malos.append(f"L{i+1} dentro de {m.group(2)}() que no es async")
                break

    assert not malos, "awaits fuera de contexto async:\n  " + "\n  ".join(malos)


# ═══════════════════════════════════════════════════════════════
# Riesgo Fase 3: purga de Tailwind
# ═══════════════════════════════════════════════════════════════

def test_04_sin_clases_tailwind_dinamicas():
    """
    El escaner de Tailwind solo detecta literales: una clase armada por
    fragmentos desaparece del build y el modal sale sin estilo. Las clases
    del modal deben venir de style.css, no de Tailwind interpolado.
    """
    js = _read(DIALOG_JS)
    dinamicas = re.findall(r"['\"`](?:bg|text|border|ring)-\$\{", js)
    assert not dinamicas, f"clases Tailwind dinamicas: {dinamicas}"


def test_05_tonos_definidos_en_css():
    """Cada tono usado por el JS debe tener acento, icono y boton en CSS."""
    css = _read(STYLE_CSS)
    faltantes = []
    for tone in TONES:
        for prefijo in ("dlg-accent-", "dlg-icon-", "dlg-btn-"):
            clase = f".{prefijo}{tone}"
            if clase not in css:
                faltantes.append(clase)
    # 'cancel' es variante de boton, no un tono completo
    if ".dlg-btn-cancel" not in css:
        faltantes.append(".dlg-btn-cancel")

    assert not faltantes, f"clases CSS faltantes: {faltantes}"


def test_06_variantes_de_boton_usadas_existen_en_css():
    """Toda variante pasada desde index.js debe existir como .dlg-btn-*."""
    js = _read(INDEX_JS)
    css = _read(STYLE_CSS)

    usadas = set(re.findall(r"tone:\s*'(\w+)'", js))
    faltantes = [t for t in usadas if f".dlg-btn-{t}" not in css]
    print(f"\n  [METRIC] Tonos usados en index.js: {sorted(usadas)}")

    assert not faltantes, f"tonos sin clase de boton: {faltantes}"


# ═══════════════════════════════════════════════════════════════
# Carga y contrato del modulo
# ═══════════════════════════════════════════════════════════════

def test_07_script_cargado_antes_de_index_js():
    """Si ui-dialog.js cargara despues, las primitivas serian undefined."""
    html = _read(INDEX_HTML)
    pos_dialog = html.find("ui-dialog.js")
    pos_index = html.find('static/index.js"')

    assert pos_dialog != -1, "ui-dialog.js no esta enlazado en index.html"
    assert pos_index != -1, "no se encontro el script de index.js"
    assert pos_dialog < pos_index, "ui-dialog.js debe cargar ANTES que index.js"


def test_08_primitivas_expuestas_en_window():
    """index.js las invoca como globales."""
    js = _read(DIALOG_JS)
    for fn in ("confirmDialog", "choiceDialog", "promptDialog"):
        assert f"window.{fn} = {fn}" in js, f"{fn} no se expone en window"


def test_09_accesibilidad_del_modal():
    """El modal nativo era accesible por defecto; el custom debe declararlo."""
    js = _read(DIALOG_JS)
    assert "role', 'dialog'" in js or 'role", "dialog"' in js, "falta role=dialog"
    assert "aria-modal" in js, "falta aria-modal"
    assert "aria-labelledby" in js, "falta aria-labelledby"


def test_10_esc_y_foco_gestionados():
    """
    ESC debe cancelar y el foco quedar atrapado. El handler global de ESC de
    index.js ignora los modales, asi que el modulo debe capturarlo el mismo.
    """
    js = _read(DIALOG_JS)
    assert "'Escape'" in js, "ESC no gestionado"
    assert "keydown" in js and "true" in js, "el listener debe ir en captura"
    assert "'Tab'" in js, "foco no atrapado (Tab)"
    assert "previousFocus" in js, "no se restaura el foco al cerrar"


def test_11_ui_dialog_en_content_de_tailwind():
    """Preventivo: si alguien agrega una clase Tailwind ahi, no debe purgarse."""
    cfg = _read(TW_CONFIG)
    assert "ui-dialog.js" in cfg, "ui-dialog.js no esta en content de tailwind.config.js"


# ═══════════════════════════════════════════════════════════════
# Calidad de UX
# ═══════════════════════════════════════════════════════════════

def test_12_audio_usa_dos_acciones_con_nombre():
    """
    El confirm() del audio era no-binario: 'Cancelar' agregaba texto en vez de
    cancelar. Debe ser choiceDialog con dos acciones nombradas.
    """
    js = _read(INDEX_JS)
    # Buscar la DEFINICION, no la primera mencion (el llamador aparece antes).
    m = re.search(r"async function confirmAndSendAudio", js)
    assert m, "confirmAndSendAudio no encontrada o no es async"
    cuerpo = js[m.start():m.start() + 3000]

    assert "choiceDialog" in cuerpo, "el audio sigue usando una confirmacion binaria"
    assert "Enviar ahora" in cuerpo and "Agregar texto antes" in cuerpo, \
        "las acciones deben decir lo que hacen"
    assert re.search(r"async function confirmAndSendAudio", js), \
        "confirmAndSendAudio debe ser async"


def test_13_destructivos_en_tono_danger():
    """Eliminar/cancelar envio deben ir en rojo, no en el tono por defecto."""
    js = _read(INDEX_JS)
    destructivos = [
        "¿Eliminar este template?",
        "¿Eliminar este mensaje?",
        "¿Eliminar esta nota?",
        "¿Eliminar esta cita?",
        "¿Cancelar este mensaje programado?",
    ]
    sin_danger = []
    for titulo in destructivos:
        i = js.find(titulo)
        if i == -1:
            sin_danger.append(f"{titulo} (no encontrado)")
            continue
        bloque = js[i:i + 400]
        if "tone: 'danger'" not in bloque:
            sin_danger.append(titulo)

    print(f"\n  [METRIC] Destructivos en rojo: {len(destructivos) - len(sin_danger)}/{len(destructivos)}")
    assert not sin_danger, f"destructivos sin tono danger: {sin_danger}"


def test_14_confirmaciones_tienen_titulo_y_accion_explicita():
    """
    Metrica de calidad: cada dialogo debe decir en el boton lo que hace
    ('Eliminar', 'Transferir') en vez de un 'Aceptar' generico.
    """
    js = _read(INDEX_JS)
    bloques = re.findall(r"await confirmDialog\(\{(.*?)\}\);", js, re.DOTALL)
    assert bloques, "no se encontraron llamadas a confirmDialog"

    sin_titulo = [b for b in bloques if "title:" not in b]
    sin_label = [b for b in bloques if "confirmLabel:" not in b]

    print(f"\n  [METRIC] confirmDialog con titulo: {len(bloques) - len(sin_titulo)}/{len(bloques)}")
    print(f"  [METRIC] confirmDialog con accion explicita: {len(bloques) - len(sin_label)}/{len(bloques)}")

    assert not sin_titulo, f"{len(sin_titulo)} dialogos sin title"
    assert not sin_label, f"{len(sin_label)} dialogos sin confirmLabel"
