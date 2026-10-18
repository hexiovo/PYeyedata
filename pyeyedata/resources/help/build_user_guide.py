"""Build the PYeyedata 0.9.0 offline guide from its retained C03 PDF source.

This is a release-authoring tool. It is not imported by the application and its
PDF dependencies are not part of the application runtime.
"""

from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

import pypdf
import reportlab
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parent
SOURCE_DIR = ROOT / "source"
BASE_PDF = SOURCE_DIR / "PYeyedata-user-guide-zh-base.pdf"
OUTPUT_PDF = ROOT / "PYeyedata-user-guide-zh.pdf"
FONT_FILE = ROOT.parent / "fonts" / "PYeyedataCJK-Regular.ttf"
BUILD_MANIFEST = SOURCE_DIR / "guide-build-manifest.json"

APP_VERSION = "0.9.0"
GUIDE_VERSION = "0.9.0"
BASE_SHA256 = "d40f3b57d15a451a8f4333a9e549c11fa69b748ce4a62788f0ce9716390a686f"
FONT_SHA256 = "3880e685b37744c7713cd5657d74cc7d570754c802916af9496f36c42127bd22"
EXPECTED_PYPDF = "6.10.0"
EXPECTED_REPORTLAB = "4.4.9"
FONT_NAME = "PYeyedataGuideCJK"
PAGE_COUNT = 8

INK = HexColor("#19364f")
TEAL = HexColor("#087f7c")
MUTED = HexColor("#60778c")
RULE = HexColor("#cbd7e1")
CARD = HexColor("#f1f6f8")
WHITE = HexColor("#ffffff")

GUIDE_TEXT = (
    "PYeyedata 0.9.0 版本补充",
    "应用版本与数据契约版本分别记录",
    "当前发行版本为 PYeyedata 0.9.0，产品需求基线为 PRD 0.9。",
    "数据契约 1.3.0、分析流程 schema 1.0.0 是独立版本；具体方法版本记录在运行结果和档案中。",
    "查看与许可资料",
    "帮助 / 关于 PYeyedata",
    "显示应用版本、PRD 基线、数据契约与分析流程 schema。",
    "具体方法算法版本随运行结果和档案一并记录。",
    "帮助 / 许可与第三方声明",
    "离线查看第三方声明、LGPLv3/GPLv3、PyInstaller、Apache 2.0 与字体 OFL 文本。",
    "同时提供 Qt/PySide 源码归档信息及替换、重建说明。",
    "源码归档地址和校验值详见 Qt 源码与重建说明；随附组件以本版发行清单为准。",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _path_for_manifest(path: Path, relative_root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(relative_root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _validate_inputs(base_path: Path, font_path: Path) -> None:
    if pypdf.__version__ != EXPECTED_PYPDF:
        raise RuntimeError(
            f"Expected pypdf {EXPECTED_PYPDF}, found {pypdf.__version__}"
        )
    if reportlab.Version != EXPECTED_REPORTLAB:
        raise RuntimeError(
            f"Expected ReportLab {EXPECTED_REPORTLAB}, found {reportlab.Version}"
        )
    if not base_path.is_file():
        raise FileNotFoundError(f"Guide base PDF is missing: {base_path}")
    if not font_path.is_file():
        raise FileNotFoundError(f"Bundled CJK font is missing: {font_path}")
    if _sha256(base_path) != BASE_SHA256:
        raise RuntimeError("Guide base PDF hash does not match the retained source")
    if _sha256(font_path) != FONT_SHA256:
        raise RuntimeError("CJK font hash does not match the application resource")
    reader = PdfReader(str(base_path), strict=True)
    if len(reader.pages) != PAGE_COUNT:
        raise RuntimeError(
            f"Expected the retained {PAGE_COUNT}-page guide, found {len(reader.pages)}"
        )

    pdfmetrics.registerFont(TTFont(FONT_NAME, str(font_path)))
    cmap = pdfmetrics.getFont(FONT_NAME).face.charToGlyph
    missing = sorted({char for line in GUIDE_TEXT for char in line if ord(char) not in cmap})
    if missing:
        rendered = "".join(missing)
        raise RuntimeError(f"Bundled CJK font does not cover guide text: {rendered}")


def _draw_footer(page_canvas: canvas.Canvas, width: float, index: int) -> None:
    footer_height = 38
    page_canvas.saveState()
    page_canvas.setFillColor(WHITE)
    page_canvas.rect(0, 0, width, footer_height, fill=1, stroke=0)
    page_canvas.setStrokeColor(RULE)
    page_canvas.setLineWidth(0.45)
    page_canvas.line(42, 31, width - 42, 31)

    label = (
        "PYeyedata 0.9.0  •  离线指南 0.9.0  •  "
        "数据契约 1.3.0  •  Pipeline schema 1.0.0"
    )
    available = width - 115
    size = 6.4
    while pdfmetrics.stringWidth(label, FONT_NAME, size) > available and size > 5.0:
        size -= 0.2
    page_canvas.setFont(FONT_NAME, size)
    page_canvas.setFillColor(MUTED)
    page_canvas.drawString(42, 17, label)
    page_canvas.drawRightString(width - 42, 17, f"{index} / {PAGE_COUNT}")
    page_canvas.restoreState()


def _draw_release_note(page_canvas: canvas.Canvas, width: float) -> None:
    page_canvas.saveState()
    panel_x = 48
    panel_y = 94
    panel_width = width - 96
    panel_height = 225
    page_canvas.setFillColor(CARD)
    page_canvas.setStrokeColor(RULE)
    page_canvas.setLineWidth(0.6)
    page_canvas.roundRect(
        panel_x,
        panel_y,
        panel_width,
        panel_height,
        5,
        fill=1,
        stroke=1,
    )

    page_canvas.setFont(FONT_NAME, 13)
    page_canvas.setFillColor(TEAL)
    page_canvas.drawString(panel_x + 14, panel_y + panel_height - 23, GUIDE_TEXT[0])

    page_canvas.setFont(FONT_NAME, 8.4)
    page_canvas.setFillColor(INK)
    page_canvas.drawString(panel_x + 14, panel_y + panel_height - 40, GUIDE_TEXT[1])
    page_canvas.setFont(FONT_NAME, 7.4)
    page_canvas.setFillColor(MUTED)
    page_canvas.drawString(panel_x + 14, panel_y + panel_height - 56, GUIDE_TEXT[2])
    page_canvas.drawString(panel_x + 14, panel_y + panel_height - 69, GUIDE_TEXT[3])

    page_canvas.setFont(FONT_NAME, 9)
    page_canvas.setFillColor(TEAL)
    page_canvas.drawString(panel_x + 14, panel_y + panel_height - 94, GUIDE_TEXT[4])

    gap = 10
    card_y = panel_y + 49
    card_height = 69
    card_width = (panel_width - 28 - gap) / 2
    card_left_x = panel_x + 14
    card_right_x = card_left_x + card_width + gap
    for card_x in (card_left_x, card_right_x):
        page_canvas.setFillColor(WHITE)
        page_canvas.setStrokeColor(RULE)
        page_canvas.setLineWidth(0.5)
        page_canvas.roundRect(
            card_x,
            card_y,
            card_width,
            card_height,
            3,
            fill=1,
            stroke=1,
        )

    page_canvas.setFont(FONT_NAME, 8)
    page_canvas.setFillColor(INK)
    page_canvas.drawString(card_left_x + 8, card_y + card_height - 15, GUIDE_TEXT[5])
    page_canvas.setFont(FONT_NAME, 7)
    page_canvas.setFillColor(MUTED)
    page_canvas.drawString(card_left_x + 8, card_y + card_height - 31, GUIDE_TEXT[6])
    page_canvas.drawString(card_left_x + 8, card_y + card_height - 44, GUIDE_TEXT[7])

    page_canvas.setFont(FONT_NAME, 8)
    page_canvas.setFillColor(INK)
    page_canvas.drawString(card_right_x + 8, card_y + card_height - 15, GUIDE_TEXT[8])
    page_canvas.setFont(FONT_NAME, 6.8)
    page_canvas.setFillColor(MUTED)
    page_canvas.drawString(card_right_x + 8, card_y + card_height - 31, GUIDE_TEXT[9])
    page_canvas.drawString(card_right_x + 8, card_y + card_height - 44, GUIDE_TEXT[10])

    page_canvas.setFont(FONT_NAME, 6.8)
    page_canvas.setFillColor(MUTED)
    page_canvas.drawString(panel_x + 14, panel_y + 29, GUIDE_TEXT[11])
    page_canvas.restoreState()


def _make_overlay(width: float, height: float, page_index: int, final_page: bool) -> bytes:
    buffer = tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024)
    page_canvas = canvas.Canvas(
        buffer,
        pagesize=(width, height),
        pageCompression=1,
        invariant=1,
    )
    _draw_footer(page_canvas, width, page_index)
    if final_page:
        _draw_release_note(page_canvas, width)
    page_canvas.save()
    buffer.seek(0)
    data = buffer.read()
    buffer.close()
    return data


def build_guide(base_path: Path, output_path: Path, font_path: Path) -> dict[str, object]:
    _validate_inputs(base_path, font_path)
    reader = PdfReader(str(base_path), strict=True)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)

    for index, page in enumerate(writer.pages, start=1):
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        overlay_reader = PdfReader(
            BytesIO(_make_overlay(width, height, index, final_page=index == PAGE_COUNT))
        )
        page.merge_page(overlay_reader.pages[0], over=True, expand=False)

    writer.add_metadata(
        {
            "/Title": "PYeyedata 0.9.0 离线用户指南",
            "/Subject": "PYeyedata 0.9.0 用户工作流、版本信息与许可材料入口",
            "/Author": "PYeyedata resources",
            "/Creator": "PYeyedata offline guide builder",
            "/Keywords": "PYeyedata, 0.9.0, offline guide, licenses, Qt source",
        }
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.stem}-",
        suffix=".tmp.pdf",
        dir=str(output_path.parent),
    )
    os.close(fd)
    temporary_path = Path(temporary_name)
    try:
        with temporary_path.open("wb") as stream:
            writer.write(stream)
        verified = PdfReader(str(temporary_path), strict=True)
        if len(verified.pages) != PAGE_COUNT:
            raise RuntimeError("Generated guide page count changed")
        metadata = verified.metadata or {}
        if metadata.get("/Title") != "PYeyedata 0.9.0 离线用户指南":
            raise RuntimeError("Generated guide metadata did not retain version 0.9.0")
        temporary_path.replace(output_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    return {
        "application_version": APP_VERSION,
        "guide_version": GUIDE_VERSION,
        "base_pdf": {
            "path": _path_for_manifest(base_path, ROOT),
            "sha256": _sha256(base_path),
        },
        "builder": {
            "path": str(Path(__file__).name),
            "sha256": _sha256(Path(__file__).resolve()),
        },
        "font": {
            "path": _path_for_manifest(font_path, ROOT.parent),
            "sha256": _sha256(font_path),
            "glyph_check": "all added strings covered by the bundled font cmap",
        },
        "output_pdf": {
            "path": _path_for_manifest(output_path, ROOT),
            "sha256": _sha256(output_path),
            "pages": PAGE_COUNT,
            "layout": "pypdf vector/text overlays; all original pages retained",
        },
        "qa_tooling": {
            "pypdf": pypdf.__version__,
            "reportlab": reportlab.Version,
            "scope": "release-authoring QA only; excluded from application runtime",
        },
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=BASE_PDF)
    parser.add_argument("--output", type=Path, default=OUTPUT_PDF)
    parser.add_argument("--font", type=Path, default=FONT_FILE)
    parser.add_argument("--manifest", type=Path, default=BUILD_MANIFEST)
    args = parser.parse_args(argv)
    result = build_guide(args.base.resolve(), args.output.resolve(), args.font.resolve())
    manifest_path = args.manifest.resolve()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
