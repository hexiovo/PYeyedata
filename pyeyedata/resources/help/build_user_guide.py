"""Rebuild the readable offline PDF from the same text shown by F1 help.

Authoring only: the GUI never imports this module. Uses the existing ReportLab
dependency and bundled OFL CJK font; no external download or PDF editor is needed.
"""
from __future__ import annotations

import csv
import hashlib
from html import escape
import json
from pathlib import Path
import re

import reportlab
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, PageBreak,
                               Table, TableStyle)

ROOT = Path(__file__).resolve().parent
SOURCES = ("quick-start-zh.md", "input-files-zh.md", "plans-and-archives-zh.md")
FONT_FILE = ROOT.parent / "fonts" / "PYeyedataCJK-Regular.ttf"
OUTPUT_PDF = ROOT / "PYeyedata-user-guide-zh.pdf"
APP_VERSION = "0.9.2"
GUIDE_VERSION = "0.9.2"
FONT_NAME = "PYeyedataGuideCJK"


def _inline(text: str) -> str:
    value = escape(text)
    value = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value)
    return re.sub(r"`(.+?)`", r'<font color="#087f7c">\1</font>', value)


def build() -> dict:
    pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_FILE)))
    body = ParagraphStyle("body", fontName=FONT_NAME, fontSize=10, leading=16,
                          textColor=colors.HexColor("#20384e"), spaceAfter=9,
                          wordWrap="CJK", splitLongWords=True)
    title = ParagraphStyle("title", parent=body, fontSize=20, leading=29, spaceAfter=18,
                           textColor=colors.HexColor("#087f7c"), keepWithNext=True)
    heading = ParagraphStyle("heading", parent=body, fontSize=13, leading=21,
                             spaceBefore=14, spaceAfter=8, keepWithNext=True)
    cell = ParagraphStyle("cell", parent=body, fontSize=8, leading=12, spaceAfter=0)
    width = A4[0] - 92
    story = []
    table_count = 0

    def append_csv(lines):
        nonlocal table_count
        rows = list(csv.reader(lines))
        if len(rows) < 2:
            return
        headers, examples = rows[0], rows[1:]
        count = len(examples)
        data = [[Paragraph("列名", cell)] + [Paragraph(f"示例 {i + 1}", cell) for i in range(count)]]
        for i, name in enumerate(headers):
            data.append([Paragraph(escape(name), cell)] +
                        [Paragraph(escape(row[i] if i < len(row) else ""), cell) for row in examples])
        first_width = 119
        table = Table(data, colWidths=[first_width] + [(width-first_width)/count]*count,
                      repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#e6f3f2")),
            ("GRID", (0,0), (-1,-1), .4, colors.HexColor("#cbd7e1")),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING", (0,0), (-1,-1), 8), ("RIGHTPADDING", (0,0), (-1,-1), 8),
            ("TOPPADDING", (0,0), (-1,-1), 6), ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ]))
        story.extend([table, Spacer(1, 11)])
        table_count += 1

    for index, source in enumerate(SOURCES):
        if index:
            story.append(PageBreak())
        in_code = False
        code = []
        for line in (ROOT/source).read_text(encoding="utf-8").splitlines():
            if line.startswith("```"):
                if in_code:
                    append_csv(code)
                    code = []
                in_code = not in_code
                continue
            if in_code:
                code.append(line)
            elif line.startswith("# "):
                story.append(Paragraph(_inline(line[2:]), title))
            elif line.startswith("## "):
                if source == "plans-and-archives-zh.md" and line.startswith("## 退出提醒"):
                    story.append(PageBreak())
                # Internal table role keys remain in the reference chapter so
                # users can match the CSV files without guessing translations.
                story.append(Paragraph(_inline(line[3:]), heading))
            elif line.strip():
                if line.startswith("- "):
                    story.append(Paragraph(_inline(line[2:]), body, bulletText="•"))
                else:
                    story.append(Paragraph(_inline(line), body))

    page_numbers = []
    def decorate(canvas, doc):
        canvas.saveState()
        canvas.setFont(FONT_NAME, 8)
        canvas.setFillColor(colors.HexColor("#60778c"))
        canvas.drawString(46, A4[1]-29, f"PYeyedata {APP_VERSION}  |  新手操作与文件示例")
        canvas.drawString(46, 25, "练习数据均为合成数据，请勿混入真实研究。")
        canvas.drawRightString(A4[0]-46, 25, f"第 {doc.page} 页")
        canvas.restoreState()
        page_numbers.append(doc.page)

    doc = SimpleDocTemplate(str(OUTPUT_PDF), pagesize=A4, leftMargin=46, rightMargin=46,
                            topMargin=53, bottomMargin=48, title=f"PYeyedata {GUIDE_VERSION} 使用手册",
                            author="PYeyedata", pageCompression=1)
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {"schema": "pyeyedata.guide-build/v2", "application_version": APP_VERSION,
                "guide_version": GUIDE_VERSION, "page_count": len(page_numbers),
                "example_tables": table_count, "reportlab": reportlab.Version,
                "input_sha256": {name: digest(ROOT/name) for name in SOURCES},
                "font_sha256": digest(FONT_FILE), "output_sha256": digest(OUTPUT_PDF)}
    (ROOT/"source"/"guide-build-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
