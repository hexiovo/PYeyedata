"""Environment inspection and pinned venv repair with visible process output."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from PySide6.QtCore import QProcess, QRunnable, QObject, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QHeaderView,
)

from pyeyedata.environment import inspect_environment, install_commands


class _Signals(QObject):
    done = Signal(object)


class _BundledProbe(QRunnable):
    def __init__(self):
        super().__init__()
        self.signals = _Signals()

    def run(self):
        try:
            from pyeyedata.build_identity import load_build_identity
            from pyeyedata.app_info import APP_NAME, APP_VERSION, PRD_VERSION
            from pyeyedata.contracts import CONTRACT_VERSION
            from pyeyedata.pipeline import PIPELINE_SCHEMA_VERSION
            identity = load_build_identity(
                application={"name": APP_NAME, "version": APP_VERSION, "prd_version": PRD_VERSION},
                contracts={"data_contract_version": CONTRACT_VERSION,
                           "pipeline_schema_version": PIPELINE_SCHEMA_VERSION},
            )
            pins = identity.archive_metadata()["manifest"]["build_environment"]["distributions"]
            result = inspect_environment(pins, frozen=True)
        except Exception as exc:
            result = {"error": str(exc), "passed": False}
        self.signals.done.emit(result)


class EnvironmentDialog(QDialog):
    def __init__(self, parent=None, *, allow_install: bool = True):
        super().__init__(parent)
        self.setWindowTitle("环境检查与依赖安装")
        self.resize(880, 660)
        self.root = Path(__file__).resolve().parents[2]
        self.frozen = bool(getattr(sys, "frozen", False))
        self.allow_install = allow_install
        self.report = None
        self.busy = False
        self.installing = False
        self.restart_required = False
        self.cancelled = False
        self.commands = []
        self.output = ""
        self._worker = None
        layout = QVBoxLayout(self)
        intro = QLabel(
            "检查 Python、所需依赖的版本与导入状态。源码版按 requirements.txt 修复当前项目环境；"
            "安装完成后请关闭并重新启动程序。EXE 版检查内置依赖，无需额外安装 Python 包。", self)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.status = QLabel("准备检查环境…", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(["依赖包", "用途", "要求版本", "当前版本", "检查结果"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 2)
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 0)
        layout.addWidget(self.progress)
        self.log = QPlainTextEdit(self)
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        layout.addWidget(self.log, 1)
        buttons = QHBoxLayout()
        self.check_button = QPushButton("重新检查", self)
        self.install_button = QPushButton("一键安装 / 修复依赖", self)
        self.cancel_button = QPushButton("停止当前操作", self)
        self.close_button = QPushButton("关闭", self)
        for button in (self.check_button, self.install_button, self.cancel_button, self.close_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.check_button.clicked.connect(self.check_environment)
        self.install_button.clicked.connect(self.install)
        self.cancel_button.clicked.connect(self.cancel)
        self.close_button.clicked.connect(self.reject)
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._timeout)
        self._set_busy(False)
        QTimer.singleShot(0, self.check_environment)

    def _set_busy(self, busy):
        self.busy = busy
        self.progress.setVisible(busy)
        self.check_button.setEnabled(not busy)
        self.close_button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy and not self.frozen)
        self.install_button.setEnabled(False)
        if not busy and self.report and not self.report.get("passed") and not self.report.get("error"):
            try:
                install_commands(self.root, frozen=self.frozen, report=self.report)
                self.install_button.setEnabled(self.allow_install and not self.restart_required)
            except ValueError as exc:
                self.install_button.setToolTip(str(exc))
        if self.frozen:
            self.install_button.setText("EXE 内置依赖，无需安装")
        elif not self.allow_install:
            self.install_button.setToolTip("请等待当前导入、分析或导出结束后再打开本窗口安装依赖。")

    def check_environment(self):
        if self.busy:
            return
        self.installing = False
        self.cancelled = False
        self.report = None
        self.table.setRowCount(0)
        self._set_busy(True)
        self.status.setText("正在检查依赖版本、实际导入及依赖关系…")
        if self.frozen:
            self._worker = _BundledProbe()
            self._worker.signals.done.connect(self._show_report)
            QThreadPool.globalInstance().start(self._worker)
        else:
            self._start(["-I", "-X", "utf8", str(self.root / "pyeyedata" / "environment.py"),
                         str(self.root / "requirements.txt")], timeout_ms=180000)

    def _start(self, arguments, *, timeout_ms=1200000):
        self.output = ""
        self.log.appendPlainText("执行：" + sys.executable + " " + " ".join(arguments))
        self.process.setWorkingDirectory(str(self.root))
        self.process.start(sys.executable, ["-X", "utf8", *arguments])
        self.timer.start(timeout_ms)

    def _read_output(self):
        text = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self.output = (self.output + text)[-2 * 1024 * 1024:]
        # Keep structured results in the table; display installer output live.
        if self.installing:
            self.log.moveCursor(self.log.textCursor().MoveOperation.End)
            self.log.insertPlainText(text)

    def _finished(self, code, exit_status):
        self.timer.stop()
        self._read_output()
        if self.cancelled:
            self._set_busy(False)
            return
        if code != 0 or exit_status != QProcess.ExitStatus.NormalExit:
            self.status.setText("安装失败，环境可能仅完成部分修改；请查看日志，重新检查并重启程序。" if self.installing else "环境检查进程失败，请查看日志。")
            self.log.appendPlainText(self.output)
            self._set_busy(False)
            return
        if self.installing:
            if self.commands:
                self._start(self.commands.pop(0))
            else:
                self.restart_required = True
                self._set_busy(False)
                self.check_environment()
            return
        try:
            line = next(line for line in reversed(self.output.splitlines()) if line.startswith("PYEYEDATA_ENV="))
            self._show_report(json.loads(line.split("=", 1)[1]))
        except (StopIteration, ValueError) as exc:
            self._show_report({"error": f"未取得有效检查报告：{exc}\n{self.output}", "passed": False})

    def _show_report(self, report):
        self.report = report
        rows = report.get("rows", ())
        self.table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            values = [item["name"], "运行" if item["runtime"] else "开发 / 打包",
                      item["expected"], item["actual"] or "—", item["detail"]]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(value)
                self.table.setItem(row, column, cell)
        if report.get("error"):
            self.status.setText("环境检查失败：" + report["error"])
        else:
            bad = sum(item["state"] != "ok" for item in rows)
            self.status.setText(
                ("环境检查通过。" if report["passed"] else f"环境需要处理：{bad} 项依赖异常。")
                + f" Python {report['python']} · {report['executable']}"
                + ("" if report["supported"] else "；要求 Windows x64 CPython 3.12.1+（仅 3.12 系列）。")
                + (" 安装已结束，请关闭并重新启动程序。" if self.restart_required else "")
                + (" 内置依赖异常时请重新获取完整发布包。" if self.frozen and not report["passed"] else ""))
            self.log.appendPlainText(report["pip_detail"])
        self._set_busy(False)

    def install(self):
        if self.busy or not self.allow_install or self.restart_required or not self.report:
            return
        try:
            self.commands = install_commands(self.root, frozen=self.frozen, report=self.report)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.installing = True
        self.restart_required = True
        self.cancelled = False
        self._set_busy(True)
        self.status.setText("正在安装项目锁定版本，请保持网络连接；完成后将自动重新检查。")
        self._start(self.commands.pop(0))

    def cancel(self):
        if self.busy and not self.frozen:
            self.cancelled = True
            if self.installing:
                self.restart_required = True
            self.status.setText("已停止操作；安装可能仅完成一部分，请重新检查后重启程序。" if self.installing
                                else "环境检查已停止，尚未确认环境完整。")
            self.process.kill()

    def _timeout(self):
        self.cancel()
        self.status.setText("操作超时，已停止。请检查网络或日志后重试。")

    def _process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.timer.stop()
            self.status.setText("无法启动环境检查/安装进程：" + self.process.errorString())
            self._set_busy(False)

    def reject(self):
        if not self.busy:
            super().reject()

    def closeEvent(self, event):
        if self.busy:
            event.ignore()
        else:
            super().closeEvent(event)
