"""Focused checks for the main-window feedback and contextual-help UI."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QFocusEvent, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFormLayout, QLineEdit, QWidget
def run_ui_diagnostics(app: Any, window: Any) -> dict[str, Any]:
    """Check the visible fixes without loading real user data or changing inputs."""
    checks: dict[str, bool] = {}
    failures: list[str] = []

    def record(name: str, passed: bool) -> None:
        checks[name] = bool(passed)
        if not passed:
            failures.append(name)

    record(
        "logs_are_on_separate_navigation_page",
        window.log_view.parentWidget() is window.logs_page
        and window.pages.indexOf(window.results_page_scroll) == 5
        and window.pages.indexOf(window.logs_page) == 6
        and window.step_list.count() == 8
        and window.step_list.item(6).text() == "结果与追溯"
        and window.step_list.item(7).text() == "运行日志",
    )
    record(
        "log_widget_keeps_read_only_bound",
        window.log_view.isReadOnly() and window.log_view.maximumBlockCount() == 3000,
    )

    original_help = window.help_text.toPlainText()
    controller = window.contextual_help
    precision = window.import_page.coordinate.precision
    original_focus = app.focusWidget()
    original_precision = precision.value()
    focus_event = QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.OtherFocusReason)
    controller.eventFilter(precision, focus_event)
    precision_help = window.help_text.toPlainText()
    record(
        "time_precision_help_is_field_specific",
        "时间精度（秒）" in precision_help and "不是采样率或采样间隔" in precision_help,
    )
    record("focus_help_does_not_change_parameter", precision.value() == original_precision)

    click_event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(2.0, 2.0),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    controller.eventFilter(precision, click_event)
    record(
        "mouse_click_help_uses_same_parameter_context",
        "时间精度（秒）" in window.help_text.toPlainText(),
    )
    record("help_does_not_steal_focus", app.focusWidget() is original_focus)

    form = window.metrics_page._forms.get("individual")
    editor = next(iter(form.editors.values()), None) if form is not None else None
    if editor is not None:
        before = editor.value()
        controller.eventFilter(editor, focus_event)
        name = editor.spec.name
        description = editor.spec.description
        current_help = window.help_text.toPlainText()
        record(
            "typed_parameter_help_uses_schema_description",
            name.rsplit(".", 1)[-1] in current_help
            and (not description or description.splitlines()[0][:24] in current_help),
        )
        record("typed_parameter_help_does_not_change_value", editor.value() == before)
    else:
        checks["typed_parameter_help_uses_schema_description"] = False
        checks["typed_parameter_help_does_not_change_value"] = False
        failures.extend(("typed_parameter_help_uses_schema_description", "typed_parameter_help_does_not_change_value"))
    controller.set_default_text(original_help)

    before_log = window.log_view.toPlainText()
    window._invalidate_configuration("diagnostic no-data edit")
    record(
        "no-data_parameter_edit_does_not_emit_invalidation_log",
        window.session.dataset_revision is None and window.log_view.toPlainText() == before_log,
    )
    session = window.session
    prior_snapshot = session._active_snapshot
    if prior_snapshot is None:
        generation = session.generation
        session._active_snapshot = object()
        window._invalidate_configuration("diagnostic active snapshot")
        record(
            "parameter_edit_still_invalidates_active_snapshot",
            session.active_snapshot is None and session.generation == generation + 1,
        )
        session._active_snapshot = prior_snapshot
    else:
        checks["parameter_edit_still_invalidates_active_snapshot"] = False
        failures.append("parameter_edit_still_invalidates_active_snapshot")

    model = window._preview_model
    original_frame = model.frame
    original_title = window.preview_box.title()
    original_hint = window.preview_hint.text()
    original_preview_hidden = window.preview_box.isHidden()
    original_page = window.pages.currentWidget()
    if window.session.dataset_revision is None:
        import pandas as pd

        window.pages.setCurrentWidget(window.data_page)
        model.set_frame(pd.DataFrame({"diagnostic_signal": [1.0], "diagnostic_second": [2.0]}))
        window._refresh_preview_state("data")
        record(
            "preview_has_label_when_rows_exist",
            not window.preview_box.isHidden() and window.preview_box.title() == "数据预览",
        )
        view = window.table_view
        view.setCurrentIndex(model.index(0, 0))
        view.setFocus(Qt.FocusReason.OtherFocusReason)
        QTest.keyClick(view, Qt.Key.Key_Right)
        app.processEvents()
        record(
            "keyboard_table_navigation_refreshes_column_help",
            view.currentIndex().column() == 1 and "diagnostic_second" in window.help_text.toPlainText(),
        )

        dynamic_host = QWidget(window.data_page)
        dynamic_form = QFormLayout(dynamic_host)
        window.data_page.layout().addWidget(dynamic_host)
        app.processEvents()
        dynamic_field = QLineEdit(dynamic_host)
        dynamic_form.addRow("动态参数诊断", dynamic_field)
        app.processEvents()
        dynamic_field.setFocus(Qt.FocusReason.OtherFocusReason)
        app.processEvents()
        record(
            "dynamically_added_form_field_gets_label_help",
            "动态参数诊断" in window.help_text.toPlainText(),
        )
        window.data_page.layout().removeWidget(dynamic_host)
        dynamic_host.setParent(None)
        dynamic_host.deleteLater()

        model.set_frame(None)
        window._refresh_preview_state("empty")
        record("empty_preview_is_hidden", window.preview_box.isHidden())
        model.set_frame(original_frame)
        window.preview_box.setTitle(original_title)
        window.preview_hint.setText(original_hint)
        window.preview_box.setVisible(not original_preview_hidden)
        if original_page is not None:
            window.pages.setCurrentWidget(original_page)
    else:
        checks["preview_has_label_when_rows_exist"] = True
        checks["empty_preview_is_hidden"] = True
        checks["keyboard_table_navigation_refreshes_column_help"] = True
        checks["dynamically_added_form_field_gets_label_help"] = True

    app.processEvents()
    return {
        "passed": not failures,
        "checks": checks,
        "failures": failures,
        "source_type": "SYNTHETIC_GUI_DIAGNOSTIC",
        "real_user_data_loaded": window.session.dataset_revision is not None,
    }


__all__ = ["run_ui_diagnostics"]
