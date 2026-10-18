"""Readable offline help and a reproducible, opt-in practice pack."""
from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
                              QMessageBox, QPushButton, QTabWidget, QTextBrowser, QVBoxLayout)

from pyeyedata.resource_paths import read_resource_bytes, read_resource_text

INPUT_ROLES = ("signals", "segments", "aois", "performance", "external_events",
               "target_movement_intervals", "eyelid_intervals", "reference_fixations",
               "reference_trajectory", "reading_annotations")
GUIDES = (("第一次使用", "help/quick-start-zh.md"),
          ("文件怎么填", "help/input-files-zh.md"),
          ("方案、档案与退出", "help/plans-and-archives-zh.md"))


def input_instructions(role: str) -> str:
    if role not in INPUT_ROLES:
        raise ValueError("未知的文件类型。")
    text = read_resource_text("help/input-files-zh.md")
    match = re.search(r"(?m)^## " + re.escape(role) + r" · .+$", text)
    if match is None:
        raise ValueError("未找到该文件的填写示例。")
    following = re.search(r"(?m)^## ", text[match.end():])
    end = match.end() + following.start() if following else len(text)
    return text[:text.index("## ")] + text[match.start():end]


def learning_pack_contents() -> dict[str, bytes]:
    resources = {
        "01_先读我.txt": "help/quick-start-zh.md",
        "02_文件填写说明.txt": "help/input-files-zh.md",
        "03_方案和档案.txt": "help/plans-and-archives-zh.md",
        "PYeyedata_使用手册.pdf": "help/PYeyedata-user-guide-zh.pdf",
        "signals-practice.csv": "help/examples/signals-practice.csv",
        "pupil-mean-practice.pyeplan": "help/examples/pupil-mean-practice.pyeplan",
    }
    for role in INPUT_ROLES:
        if role == "signals":
            continue
        resources[f"{role}-example.csv"] = f"help/examples/{role}-example.csv"
    for role in INPUT_ROLES[:4]:
        resources[f"{role}-blank.csv"] = f"templates/{role}-contract-1.3.0.csv"
        resources[f"{role}-template.xlsx"] = f"templates/{role}-contract-1.3.0.xlsx"
    return {name: read_resource_bytes(resource) for name, resource in resources.items()}


def export_learning_pack(parent_directory: str | os.PathLike[str]) -> Path:
    """Create a fresh pack without overwriting any existing user files."""
    contents = learning_pack_contents()  # Fail before creating anything if incomplete.
    parent = Path(parent_directory)
    with tempfile.TemporaryDirectory(prefix=".pyeyedata-practice-", dir=parent) as temporary:
        staged = Path(temporary) / "pack"
        staged.mkdir()
        for name, content in contents.items():
            (staged / name).write_bytes(content)
        number = 1
        while True:
            suffix = "" if number == 1 else f"_{number}"
            destination = parent / ("PYeyedata_新手练习" + suffix)
            if destination.exists():
                number += 1
                continue
            try:
                staged.rename(destination)
                return destination
            except FileExistsError:
                number += 1


def export_learning_pack_dialog(parent) -> None:
    directory = QFileDialog.getExistingDirectory(parent, "选择练习资料的保存位置（会新建文件夹）")
    if not directory:
        return
    try:
        destination = export_learning_pack(directory)
        QMessageBox.information(parent, "练习资料已准备好", f"资料已保存到：\n{destination}\n\n先打开“01_先读我.txt”，再按步骤导入 signals-practice.csv。所有示例均为合成数据。")
    except Exception as exc:
        QMessageBox.warning(parent, "练习资料未导出", str(exc))


def make_help_dialog(parent=None, *, role: str | None = None) -> QDialog:
    dialog = QDialog(parent)
    dialog.setWindowTitle("新手入门与文件填写示例")
    dialog.resize(920, 720)
    layout = QVBoxLayout(dialog)
    tabs = QTabWidget(dialog)
    dialog.help_tabs = tabs
    if role is None:
        pages = [(title, read_resource_text(resource)) for title, resource in GUIDES]
    else:
        pages = [("这份文件怎么填", input_instructions(role)),
                 ("第一次使用", read_resource_text(GUIDES[0][1]))]
    for title, text in pages:
        viewer = QTextBrowser(tabs)
        viewer.setOpenExternalLinks(False)
        viewer.setOpenLinks(False)
        viewer.setMarkdown(text)
        viewer.document().setDocumentMargin(16)
        tabs.addTab(viewer, title)
    layout.addWidget(tabs, 1)
    row = QHBoxLayout()
    export = QPushButton("导出练习资料与空白模板…", dialog)
    export.clicked.connect(lambda: export_learning_pack_dialog(dialog))
    row.addWidget(export)
    row.addStretch(1)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=dialog)
    buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
    buttons.rejected.connect(dialog.reject)
    row.addWidget(buttons)
    layout.addLayout(row)
    return dialog


def show_help(parent=None, *, role: str | None = None) -> None:
    try:
        make_help_dialog(parent, role=role).exec()
    except Exception as exc:
        QMessageBox.warning(parent, "帮助暂时不可用", str(exc))
