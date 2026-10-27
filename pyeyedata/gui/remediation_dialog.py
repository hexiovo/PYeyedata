"""Read-only presentation of a recorded run's remediation guide."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from .remediation import NavigationTarget, RemediationGuide


def _display(value: Any, fallback: str = "未记录") -> str:
    if value is None or (isinstance(value, str) and value == ""):
        return fallback
    return str(value)


def _has_display_value(value: Any) -> bool:
    return value is not None and not (isinstance(value, str) and value == "")


class RemediationDialog(QDialog):
    """Show the original diagnostic and emit an exact page-navigation request."""

    navigateRequested = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("原因与处理指引")
        self.resize(720, 540)
        self._guide: RemediationGuide | None = None

        layout = QVBoxLayout(self)
        self.run_label = QLabel("运行：未选择", self)
        self.run_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.run_label)
        self.status_label = QLabel("状态：未记录", self)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status_label)
        self.reason_label = QLabel("原因：未记录", self)
        self.reason_label.setWordWrap(True)
        self.reason_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.reason_label)

        self.diagnostic_view = QTextBrowser(self)
        self.diagnostic_view.setObjectName("remediationOriginalDiagnostic")
        self.diagnostic_view.setOpenExternalLinks(False)
        self.diagnostic_view.setMaximumHeight(180)
        layout.addWidget(self.diagnostic_view)

        layout.addWidget(QLabel("建议核对", self))
        self.actions = QListWidget(self)
        self.actions.setObjectName("remediationActions")
        layout.addWidget(self.actions, 1)

        self.limitations = QLabel(self)
        self.limitations.setWordWrap(True)
        self.limitations.setObjectName("remediationLimitations")
        layout.addWidget(self.limitations)

        footer = QHBoxLayout()
        self.navigate_button = QPushButton("打开选中位置", self)
        self.navigate_button.setObjectName("remediationNavigate")
        self.navigate_button.setEnabled(False)
        footer.addWidget(self.navigate_button)
        footer.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        footer.addWidget(buttons)
        layout.addLayout(footer)

        self.actions.currentItemChanged.connect(self._update_navigation)
        self.navigate_button.clicked.connect(self._navigate_selected)

    @property
    def guide(self) -> RemediationGuide | None:
        return self._guide

    def set_guide(self, guide: RemediationGuide | None) -> None:
        self._guide = guide
        self.actions.clear()
        if guide is None:
            self.run_label.setText("运行：未选择")
            self.status_label.setText("状态：未记录")
            self.reason_label.setText("原因：未记录")
            self.diagnostic_view.clear()
            self.limitations.clear()
            self.navigate_button.setEnabled(False)
            return

        diagnostic = guide.original_diagnostic
        availability = diagnostic.get("availability")
        availability = availability if isinstance(availability, dict) else {}
        self.run_label.setText(f"运行：{guide.run_id}")
        status = diagnostic.get("status")
        if not _has_display_value(status):
            status = availability.get("status")
        reason_code_value = diagnostic.get("reason_code")
        if not _has_display_value(reason_code_value):
            reason_code_value = availability.get("reason_code")
        reason_value = diagnostic.get("reason")
        if not _has_display_value(reason_value):
            reason_value = availability.get("reason")
        self.status_label.setText(f"原状态：{_display(status)}")
        reason_code = _display(reason_code_value)
        reason = _display(reason_value)
        self.reason_label.setText(f"原原因：{reason_code} · {reason}")
        fields = ("metric_id", "scope", "analysis_stream_id", "aoi_id", "source_role", "required_fields", "required_metadata")
        facts = [f"{name}：{diagnostic[name]}" for name in fields if name in diagnostic and _has_display_value(diagnostic[name])]
        for name in ("status", "reason_code", "reason", "required_fields", "required_metadata"):
            if name in availability and _has_display_value(availability[name]):
                facts.append(f"availability.{name}：{availability[name]}")
        issue = availability.get("aoi_geometry_issue")
        if isinstance(issue, dict):
            for name in ("code", "message", "aoi_id", "canonical_field", "source_file", "sheet", "source_row"):
                if name in issue and _has_display_value(issue[name]):
                    facts.append(f"availability.aoi_geometry_issue.{name}：{issue[name]}")
        self.diagnostic_view.setPlainText("\n".join(facts) or "未附加其他结构化诊断字段。")

        for action in guide.actions:
            item = QListWidgetItem(f"{action.title}\n{action.detail}")
            item.setData(Qt.ItemDataRole.UserRole, action.target)
            item.setToolTip(action.detail)
            self.actions.addItem(item)
        if self.actions.count():
            self.actions.setCurrentRow(0)
        self.limitations.setText("\n".join(guide.limitations))
        self._update_navigation()

    def _update_navigation(self, *_: Any) -> None:
        item = self.actions.currentItem()
        self.navigate_button.setEnabled(item is not None and isinstance(
            item.data(Qt.ItemDataRole.UserRole), NavigationTarget
        ))

    def _navigate_selected(self) -> None:
        item = self.actions.currentItem()
        target = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if isinstance(target, NavigationTarget):
            self.navigateRequested.emit(target)


__all__ = ["RemediationDialog"]
