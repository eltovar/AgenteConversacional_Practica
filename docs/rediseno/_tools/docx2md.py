#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
docx2md.py — Recuperacion de emergencia: Word → Markdown.

Reconstruye los .md de docs/rediseno/ a partir del espejo en Word cuando
la fuente Markdown se pierde. Invierte el mapeo de formato de md2docx.py:

    23pt bold accent  → portada (se descarta)
    19/15pt bold rojo → # / ##
    12.5pt bold tinta → ###
    11pt bold tinta   → ####
    Consolas + sombra → bloque de codigo cercado
    sombra crema      → cita (>)
    List Bullet/Number→ - / 1.
    tabla             → tabla GFM

Perdida conocida: las URL de los enlaces (solo se guardo el texto visible).
Se reparan por patron al final.

Uso:  python docs/rediseno/_tools/docx2md.py
"""
import os
import re

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

SRC_ROOT = r"C:\Users\Salo\Desktop\Documentacion Re-estructura SofIA\Documentacion Faltante"
DST_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# docx relativo a SRC_ROOT  →  md relativo a docs/rediseno
LAYOUT = {
    "00 - LEEME (Como esta organizada esta carpeta).docx":          "_LEEME.md",
    "00 - Indice Maestro.docx":                                     "00-INDICE.md",
    "Fase 0 - Fundamentos/01 - Glosario y Lenguaje Ubicuo.docx":    "01-glosario.md",
    "Fase 0 - Fundamentos/02 - Actores y Roles.docx":               "02-actores-y-roles.md",
    "Fase 0 - Fundamentos/03 - Vision, Objetivos y No-Objetivos.docx": "03-vision.md",
    "Fase 3 - Transicion/16 - Antes y Despues.docx":                "16-antes-despues.md",
    "Fase 3 - Transicion/17 - Benchmark de CRMs de Referencia.docx": "17-benchmark-crms.md",
    "Wireframes/Catalogo de Wireframes.docx":                       "wireframes/README.md",
}

ACCENT = "C0392B"
INK = "1A1A1A"
MUTED = "6B6B6B"
CODE_BG = "F2F2F2"
QUOTE_BG = "FBF6E9"

HEAD_BY_SIZE = {23.0: None, 19.0: 1, 15.0: 2, 12.5: 3, 11.0: 4}


def shading_fill(par):
    pPr = par._p.find(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pPr")
    if pPr is None:
        return None
    shd = pPr.find(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}shd")
    if shd is None:
        return None
    return (shd.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}fill")
            or "").upper()


def run_color(run):
    try:
        if run.font.color is not None and run.font.color.type is not None:
            return str(run.font.color.rgb).upper()
    except Exception:
        pass
    return None


def run_size(run):
    return run.font.size.pt if run.font.size else None


def inline(par):
    """Reconstruye el Markdown inline a partir de los runs."""
    parts = []
    for r in par.runs:
        t = r.text
        if not t:
            continue
        name = r.font.name or ""
        col = run_color(r)
        if name == "Consolas" and col == ACCENT:
            parts.append(f"`{t}`")
        elif r.bold and r.italic:
            parts.append(f"***{t}***")
        elif r.bold:
            parts.append(f"**{t}**")
        elif r.italic:
            parts.append(f"*{t}*")
        else:
            parts.append(t)
    s = "".join(parts)
    # Fusiona marcadores adyacentes: **a****b** → **ab**
    for mark in ("***", "**", "*", "`"):
        s = s.replace(mark + mark, "")
    return s.strip()


def par_to_md(par):
    """Devuelve (tipo, texto). tipo ∈ head1..4, code, quote, ul, ol, hr, p, skip"""
    txt = par.text.strip()
    runs = [r for r in par.runs if r.text.strip()]

    if not txt:
        # regla horizontal: parrafo vacio con borde inferior
        pPr = par._p.find(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pPr")
        if pPr is not None and pPr.find(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pBdr") is not None:
            return ("hr", "")
        return ("skip", "")

    fill = shading_fill(par)
    first = runs[0] if runs else None

    # Bloque de codigo
    if fill == CODE_BG or (first and first.font.name == "Consolas"
                           and (run_size(first) or 99) <= 9):
        return ("code", par.text)

    # Cita
    if fill == QUOTE_BG:
        return ("quote", inline(par))

    # Listas
    if par.style.name == "List Bullet":
        return ("ul", inline(par))
    if par.style.name == "List Number":
        return ("ol", inline(par))

    # Encabezados
    if first and first.bold:
        sz = run_size(first)
        col = run_color(first)
        if sz in HEAD_BY_SIZE and (col in (ACCENT, INK) or sz == 23.0):
            lvl = HEAD_BY_SIZE[sz]
            if lvl is None:
                return ("skip", "")  # portada
            return (f"head{lvl}", par.text.strip())

    if first and run_size(first) == 8.5 and run_color(first) == MUTED:
        return ("skip", "")  # linea de procedencia de la portada

    return ("p", inline(par))


def cell_md(cell):
    return " / ".join(inline(p) for p in cell.paragraphs if p.text.strip()) or ""


def table_to_md(tbl):
    rows = []
    for row in tbl.rows:
        rows.append([cell_md(c).replace("|", "\\|") for c in row.cells])
    if not rows:
        return []
    ncol = max(len(r) for r in rows)
    out = ["| " + " | ".join((rows[0] + [""] * ncol)[:ncol]) + " |",
           "|" + "---|" * ncol]
    for r in rows[1:]:
        out.append("| " + " | ".join((r + [""] * ncol)[:ncol]) + " |")
    return out


LANG_TAG = re.compile(r"^\s*(?:\[(\w+)\]|DIAGRAMA\b.*)\s*$")


def convert(docx_path, md_path):
    doc = Document(docx_path)
    lines = []
    ol_n = 0

    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}tbl"):
            lines.append("")
            lines.extend(table_to_md(Table(child, doc)))
            lines.append("")
            ol_n = 0
            continue
        if not child.tag.endswith("}p"):
            continue

        kind, text = par_to_md(Paragraph(child, doc))
        if kind == "skip":
            continue
        if kind != "ol":
            ol_n = 0

        if kind == "hr":
            lines += ["", "---", ""]
        elif kind.startswith("head"):
            lvl = int(kind[-1])
            lines += ["", "#" * lvl + " " + text, ""]
        elif kind == "code":
            body = text.split("\n")
            lang = ""
            if body and LANG_TAG.match(body[0]):
                m = LANG_TAG.match(body[0])
                lang = (m.group(1) or "") if m else ""
                body = body[1:]
            if not lang:
                head = "\n".join(body[:2])
                if re.search(r"\b(flowchart|graph|erDiagram|sequenceDiagram)\b", head):
                    lang = "mermaid"
            lines += ["", "```" + lang] + body + ["```", ""]
        elif kind == "quote":
            lines.append("> " + text)
        elif kind == "ul":
            lines.append("- " + text)
        elif kind == "ol":
            ol_n += 1
            lines.append(f"{ol_n}. " + text)
        else:
            lines += ["", text]

    md = "\n".join(lines)
    md = re.sub(r"\n{4,}", "\n\n\n", md).strip() + "\n"
    md = repair_links(md)

    os.makedirs(os.path.dirname(md_path), exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    return md.count("\n")


# Enlaces internos que se perdieron al pasar a Word (solo quedo el texto)
LINK_REPAIRS = [
    (r"\[?(D-01 Glosario)\]?(?!\()", r"[\1](01-glosario.md)"),
    (r"\[?(D-02 §\d+[a-z-]*)\]?(?!\()", r"[\1](02-actores-y-roles.md)"),
    (r"\[?(D-03 Visión)\]?(?!\()", r"[\1](03-vision.md)"),
    (r"\[?(D-16 §\d+)\]?(?!\()", r"[\1](16-antes-despues.md)"),
    (r"\[?(D-17 Benchmark)\]?(?!\()", r"[\1](17-benchmark-crms.md)"),
]


def repair_links(md):
    for pat, rep in LINK_REPAIRS:
        md = re.sub(pat, rep, md)
    return md


def main():
    total = 0
    for rel_docx, rel_md in LAYOUT.items():
        src = os.path.join(SRC_ROOT, rel_docx.replace("/", os.sep))
        if not os.path.exists(src):
            print(f"  FALTA  {rel_docx}")
            continue
        dst = os.path.join(DST_ROOT, rel_md.replace("/", os.sep))
        n = convert(src, dst)
        total += 1
        print(f"  OK  {rel_md}  ({n} lineas)")
    print(f"\n{total} documentos recuperados en {DST_ROOT}")


if __name__ == "__main__":
    main()
