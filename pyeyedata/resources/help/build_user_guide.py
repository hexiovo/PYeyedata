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
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, PageBreak,
                               Table, TableStyle, Flowable)
from reportlab.lib.utils import ImageReader

ROOT = Path(__file__).resolve().parent
SOURCES = ("quick-start-zh.md", "input-files-zh.md", "plans-and-archives-zh.md")
FONT_FILE = ROOT.parent / "fonts" / "PYeyedataCJK-Regular.ttf"
OUTPUT_PDF = ROOT / "PYeyedata-user-guide-zh.pdf"
APP_VERSION = "0.9.3"
GUIDE_VERSION = "0.9.3"
FONT_NAME = "PYeyedataGuideCJK"
PAGE = landscape(A4)


class AnnotatedScreenshot(Flowable):
    """Draw callouts as PDF vectors over unmodified, actual Qt screenshots."""
    def __init__(self, entry, max_width=528, max_height=384):
        super().__init__()
        self.entry = entry
        self.path = ROOT / "source" / entry["file"]
        if hashlib.sha256(self.path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("Screenshot differs from capture manifest: " + str(self.path))
        self.scale = min(max_width / entry["width"], max_height / entry["height"], 1.0)
        self.width = entry["width"] * self.scale
        self.height = entry["height"] * self.scale

    def draw(self):
        c = self.canv
        c.drawImage(ImageReader(str(self.path)), 0, 0, self.width, self.height)
        c.setStrokeColor(colors.HexColor("#c33832"))
        c.setLineWidth(1.2)
        for number, (x, y, w, h) in enumerate(self.entry["marks"], 1):
            x, y, w, h = x*self.scale, self.height-(y+h)*self.scale, w*self.scale, h*self.scale
            c.roundRect(x+.5, y+.5, max(1,w-1), max(1,h-1), 2, stroke=1, fill=0)
            # Number sits at the left edge, leaving the field's text legible.
            cx, cy = max(7, x-4), min(self.height-7, y+h-2)
            c.setFillColor(colors.HexColor("#c33832"))
            c.circle(cx, cy, 7, fill=1, stroke=0)
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 8)
            c.drawCentredString(cx, cy-2.8, str(number))


def illustrated_chapters(story, body, title, heading, width):
    content = json.loads((ROOT/"source/illustrated-workflow-zh.json").read_text(encoding="utf-8"))
    captures = json.loads((ROOT/"source/screenshots.json").read_text(encoding="utf-8"))
    if captures["application_version"] != APP_VERSION or not captures["synthetic_only"]:
        raise ValueError("Guide captures must match this application and declare synthetic data")
    steps_style = ParagraphStyle("steps", parent=body, fontSize=10, leading=16, spaceAfter=10)
    caption = ParagraphStyle("caption", parent=body, fontSize=9, leading=14,
                             textColor=colors.HexColor("#60778c"))
    story.extend([Spacer(1, 32), Paragraph(content["title"], title),
                  Paragraph(content["subtitle"], heading), Spacer(1, 12),
                  Paragraph(f"适用版本 {APP_VERSION} · 离线操作说明 · 截图标注版", body),
                  Paragraph("本手册面向第一次接触软件的使用者。先用合成练习跑通导入、分析、导出和归档，再替换为自己的设备数据。操作不需要编程。", body),
                  Paragraph("阅读方法", heading),
                  Paragraph("每张截图的红色编号框，对应右侧同号步骤。截图取自本版本真实 Qt 界面；部分页面为局部截图，按钮位置可通过章节所写路径找到。字体和窗口大小随 Windows 显示设置略有变化。", body),
                  Paragraph("练习核对值", heading),
                  Paragraph("201 行 SYNTHETIC_ONLY 合成瞳孔数据，原值由 3.0 mm 等间隔增加到 3.6 mm。左瞳孔平均值应约为 3.3 mm，指标行状态 complete；整体 partial 是因为练习没有配置统计推断。", body),
                  Paragraph("这些截图和数值用于学习操作，不代表真实设备验证或研究结论。十类输入文件实例、字段解释和常见错误见附录。", body), PageBreak(),
                  Paragraph("按任务找步骤", title)])
    index_style = ParagraphStyle("index", parent=body, fontSize=10, leading=17, spaceAfter=7)
    entries = [Paragraph(f'<a href="#step-{i:02d}" color="#087f7c">{i:02d}　{escape(ch["title"])}</a>', index_style)
               for i, ch in enumerate(content["chapters"], 1)]
    middle = (len(entries)+1)//2
    toc = Table([[entries[:middle], entries[middle:]]], colWidths=[width/2]*2)
    toc.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP"),
                            ("LEFTPADDING", (0,0), (-1,-1), 0)]))
    story.extend([toc, Spacer(1, 12), Paragraph('<a href="#appendix" color="#087f7c">附录：文字速查、十类文件填写实例、方案与归档说明</a>', body)])
    for i, chapter in enumerate(content["chapters"], 1):
        story.extend([PageBreak(), Paragraph(f'<a name="step-{i:02d}"/>{i:02d}　{escape(chapter["title"])}', title),
                      Paragraph(escape(chapter["where"]), body), Spacer(1, 7)])
        entry = captures["images"][chapter["image"]]
        if len(entry["marks"]) != len(chapter["steps"]):
            raise ValueError("Every screenshot mark needs a matching instruction: " + chapter["image"])
        right = [Paragraph("照着做", heading)]
        right.extend(Paragraph(escape(step), steps_style) for step in chapter["steps"])
        right.extend([Paragraph("例子与核对", heading), Paragraph(escape(chapter["example"]), steps_style),
                      Paragraph("遇到问题", heading), Paragraph(escape(chapter["tip"]), steps_style)])
        left = [AnnotatedScreenshot(entry), Spacer(1, 10),
                Paragraph("真实界面局部截图 · 红框数字对应右侧步骤 · 合成练习", caption)]
        table = Table([[left, right]], colWidths=[546, width-546])
        table.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP"),
                                  ("LEFTPADDING", (0,0), (-1,-1), 0),
                                  ("RIGHTPADDING", (0,0), (0,0), 16),
                                  ("TOPPADDING", (0,0), (-1,-1), 0)]))
        story.append(table)
    story.extend([PageBreak(), Paragraph('<a name="appendix"/>附录：文字速查与文件实例', title),
                  Paragraph("后续各页与 F1 帮助对应，保留可直接填写的列名、示例和常见错误。它们不是每次都要导入的必填清单，按所选方法准备即可。", body)])
    return content, captures


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
    width = PAGE[0] - 72
    story = []
    table_count = 0
    content, captures = illustrated_chapters(story, body, title, heading, width)

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

    for index, source in enumerate(("input-files-zh.md", "plans-and-archives-zh.md")):
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
                if source == "input-files-zh.md":
                    story.append(PageBreak())
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
        canvas.drawString(36, PAGE[1]-24, f"PYeyedata {APP_VERSION}  |  全流程图解手册")
        canvas.drawString(36, 22, "练习数据均为合成数据，请勿混入真实研究。")
        canvas.drawRightString(PAGE[0]-36, 22, f"第 {doc.page} 页")
        canvas.restoreState()
        page_numbers.append(doc.page)

    doc = SimpleDocTemplate(str(OUTPUT_PDF), pagesize=PAGE, leftMargin=36, rightMargin=36,
                            topMargin=42, bottomMargin=39, title=f"PYeyedata {GUIDE_VERSION} 全流程图解手册",
                            author="PYeyedata", pageCompression=1)
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {"schema": "pyeyedata.guide-build/v3", "application_version": APP_VERSION,
                "guide_version": GUIDE_VERSION, "page_count": len(page_numbers),
                "illustrated_chapters": len(content["chapters"]), "screenshots": len(captures["images"]),
                "example_tables": table_count, "reportlab": reportlab.Version,
                "input_sha256": {name: digest(ROOT/name) for name in SOURCES},
                "font_sha256": digest(FONT_FILE), "output_sha256": digest(OUTPUT_PDF)}
    manifest["input_sha256"].update({name: digest(ROOT/name) for name in
                                    ("source/illustrated-workflow-zh.json", "source/screenshots.json")})
    (ROOT/"source"/"guide-build-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
