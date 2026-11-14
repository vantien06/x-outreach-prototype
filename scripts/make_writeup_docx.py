"""Build WRITEUP.docx (one A4 page) from WRITEUP.md.  Usage: uv run --with python-docx python scripts/make_writeup_docx.py"""
import re
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parent.parent
FONT, SIZE = "Calibri", 9.5


def add_runs(par, text):
    """Markdown **bold** to runs."""
    for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if not part:
            continue
        run = par.add_run(part)
        run.bold = i % 2 == 1
        run.font.name, run.font.size = FONT, Pt(SIZE)


def tight(par, before=0, after=2):
    f = par.paragraph_format
    f.space_before, f.space_after, f.line_spacing = Pt(before), Pt(after), 1.0


doc = Document()
sec = doc.sections[0]
sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
sec.left_margin = sec.right_margin = Cm(1.7)
sec.top_margin = sec.bottom_margin = Cm(1.4)
doc.styles["Normal"].font.name = FONT
doc.styles["Normal"].font.size = Pt(SIZE)

for raw in (ROOT / "WRITEUP.md").read_text(encoding="utf-8").splitlines():
    line = raw.rstrip()
    if not line:
        continue
    if line.startswith("# "):
        p = doc.add_paragraph()
        r = p.add_run(line[2:])
        r.bold, r.font.size, r.font.name = True, Pt(15), FONT
        tight(p, 0, 1)
    elif line.startswith("## "):
        p = doc.add_paragraph()
        r = p.add_run(line[3:])
        r.bold, r.font.size, r.font.name = True, Pt(11), FONT
        r.font.color.rgb = RGBColor(0x43, 0x38, 0xCA)
        tight(p, 5, 1)
    elif line.startswith("- "):
        p = doc.add_paragraph(style="List Bullet")
        add_runs(p, line[2:])
        tight(p, 0, 1)
        p.paragraph_format.left_indent = Cm(0.5)
    else:
        p = doc.add_paragraph()
        add_runs(p, line)
        tight(p, 0, 2)
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        if "·" in line and "Hosted:" in line:
            for r in p.runs:
                r.font.size = Pt(8.5)
                r.font.color.rgb = RGBColor(0x55, 0x55, 0x4E)

doc.save(ROOT / "WRITEUP.docx")
print("saved", ROOT / "WRITEUP.docx")
