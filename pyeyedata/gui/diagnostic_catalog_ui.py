"""Fixed checks for the metric catalogue layout and preset review flow."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtGui import QFocusEvent, QTextOption
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QStackedWidget, QTreeWidgetItem

from .contextual_help import ContextualHelpController
from .metrics_page import MetricCatalogueWidget, MetricsPage, _PresetDiffDialog
from .presets import (
    PresetDefinition,
    _display_category,
    builtin_presets,
    catalogue_for_module,
    schema_version,
)


def _find_item(tree: Any, metric_id: str) -> QTreeWidgetItem | None:
    def find(item: QTreeWidgetItem) -> QTreeWidgetItem | None:
        if item.data(0, Qt.ItemDataRole.UserRole) == metric_id:
            return item
        for index in range(item.childCount()):
            result = find(item.child(index))
            if result is not None:
                return result
        return None

    for index in range(tree.topLevelItemCount()):
        result = find(tree.topLevelItem(index))
        if result is not None:
            return result
    return None


def _visible_rows(widget: MetricCatalogueWidget) -> int:
    tree = widget.tree
    viewport = tree.viewport().rect()
    rows: list[QTreeWidgetItem] = []

    def collect(item: QTreeWidgetItem) -> None:
        rows.append(item)
        for index in range(item.childCount()):
            collect(item.child(index))

    for index in range(tree.topLevelItemCount()):
        collect(tree.topLevelItem(index))
    return sum(
        1 for item in rows
        if not item.isHidden() and tree.visualItemRect(item).intersects(viewport)
    )


def _click_active_dialog(app: Any, button: QDialogButtonBox.StandardButton) -> None:
    dialog = app.activeModalWidget()
    if isinstance(dialog, QDialog) and hasattr(dialog, "buttons"):
        dialog.buttons.button(button).click()


def run_catalogue_ui_diagnostics(app: Any, window: Any, workspace: Path) -> dict[str, Any]:
    """Check catalogue visibility, row help, filter persistence and preset commits."""
    checks: dict[str, Any] = {}
    failures: list[str] = []

    def record(name: str, passed: bool) -> None:
        checks[name] = bool(passed)
        if not passed:
            failures.append(name)

    metrics: MetricsPage = window.metrics_page
    schema = metrics._schema
    individual = [
        entry for entry in catalogue_for_module(schema, "individual")
        if (entry.get("implemented") is not False and not entry.get("experimental")
            and entry.get("category") not in (None, ""))
    ]
    if len(individual) < 2:
        raise RuntimeError("Catalogue UI diagnostic needs two selectable individual metrics")
    selected_id = str(individual[0]["metric_id"])
    target_id = str(individual[1]["metric_id"])
    selected_entry = individual[0]

    catalogue = MetricCatalogueWidget(schema=schema, module="individual")
    selected_item = _find_item(catalogue.tree, selected_id)
    category = str(selected_entry.get("category") or "")
    record(
        "choice_column_is_left_of_label_and_filters_start_collapsed",
        catalogue.tree.columnCount() == 2
        and catalogue.tree.treePosition() == 1
        and catalogue.tree.columnWidth(0) >= 44
        and not catalogue.filter_toggle.isChecked()
        and catalogue.filter_panel.isHidden(),
    )
    record(
        "category_labels_are_localized_without_changing_filter_keys",
        bool(category)
        and catalogue.category_filter.findData(category) >= 0
        and catalogue.category_filter.itemData(catalogue.category_filter.findData(category)) == category
        and catalogue.category_filter.itemText(catalogue.category_filter.findData(category))
        == _display_category(category),
    )
    catalogue.set_selection_ids("individual", (selected_id,))
    catalogue.search.setText(selected_id)
    catalogue.filter_toggle.setChecked(True)
    category_index = catalogue.category_filter.findData(category)
    catalogue.category_filter.setCurrentIndex(category_index)
    catalogue.state_filter.setCurrentIndex(catalogue.state_filter.findData("selected"))
    catalogue.filter_toggle.setChecked(False)
    catalogue.set_module("group")
    catalogue.set_module("individual")
    selected_item = _find_item(catalogue.tree, selected_id)
    record(
        "selection_survives_search_category_state_collapse_and_module_switch",
        catalogue.selected_ids("individual") == (selected_id,)
        and selected_item is not None
        and selected_item.checkState(0) == Qt.CheckState.Checked,
    )
    catalogue.clear_filters_button.click()
    selected_item = _find_item(catalogue.tree, selected_id)
    record(
        "clear_filters_restores_list_without_clearing_selection",
        not catalogue.search.text()
        and catalogue.category_filter.currentData() == "all"
        and catalogue.state_filter.currentData() == "all"
        and selected_item is not None
        and selected_item.checkState(0) == Qt.CheckState.Checked,
    )

    unavailable = MetricCatalogueWidget(schema=schema, module="individual")
    reason = "诊断条件未满足：缺少合成眼动信号。"
    unavailable.set_availability(pd.DataFrame([{
        "module": "individual",
        "metric_id": selected_id,
        "available": False,
        "status": "unavailable",
        "reason_code": "diagnostic_missing_signal",
        "reason": reason,
    }]))
    unavailable_item = _find_item(unavailable.tree, selected_id)
    unavailable_flags = unavailable_item.flags() if unavailable_item is not None else Qt.ItemFlag.NoItemFlags
    unavailable_context = (
        unavailable_item.data(0, Qt.ItemDataRole.UserRole + 4)
        if unavailable_item is not None else ""
    )
    foreground = unavailable_item.foreground(1).color().name() if unavailable_item is not None else ""
    record(
        "unavailable_row_is_gray_inspectable_and_not_checkable",
        unavailable_item is not None
        and bool(unavailable_flags & Qt.ItemFlag.ItemIsEnabled)
        and bool(unavailable_flags & Qt.ItemFlag.ItemIsSelectable)
        and not bool(unavailable_flags & Qt.ItemFlag.ItemIsUserCheckable)
        and foreground == "#626b78"
        and unavailable.tree.itemWidget(unavailable_item, 0) is None
        and unavailable.tree.itemWidget(unavailable_item, 1) is None
        and reason in str(unavailable_context)
        and selected_id in str(unavailable_context)
        and selected_id in unavailable_item.toolTip(1),
    )
    unavailable_help = QLabel()
    unavailable_stack = QStackedWidget()
    unavailable_stack.addWidget(unavailable)
    unavailable_help_controller = ContextualHelpController(unavailable_help, unavailable_stack)
    if unavailable_item is not None:
        unavailable.tree.setCurrentItem(unavailable_item, 0)
        app.processEvents()
        help_after_checkbox_column = unavailable_help.text()
        unavailable.tree.setCurrentItem(unavailable_item, 1)
        app.processEvents()
        help_after_label_column = unavailable_help.text()
    else:
        help_after_checkbox_column = ""
        help_after_label_column = ""
    record(
        "unavailable_row_reason_survives_tree_and_header_current_changes",
        all(
            reason in context and selected_id in context and "选择" not in context.splitlines()[0]
            for context in (help_after_checkbox_column, help_after_label_column)
        ),
    )

    tiny_schema = {
        "pipeline_schema_version": "catalog-ui-diagnostic",
        "individual": {"catalogue": [{
            "metric_id": "individual.demo.saccade",
            "label": "合成眼跳指标",
            "category": "saccade",
            "implemented": True,
        }]},
        "group": {"catalogue": [{
            "metric_id": "group.demo.aoi",
            "label": "合成 AOI 指标",
            "category": "aoi",
            "implemented": True,
        }]},
        "advanced": {"methods": [{
            "metric_id": "advanced.demo.microsaccade",
            "label": "合成微眼跳指标",
            "category": "microsaccade",
            "implemented": True,
        }]},
    }
    theme_presets = {
        (preset.module, preset.category): preset
        for preset in builtin_presets(tiny_schema)
        if preset.kind == "theme"
    }
    expected_themes = {
        ("individual", "saccade"): ("个体分析", "眼跳"),
        ("group", "aoi"): ("群体分析", "AOI 区域"),
        ("advanced", "microsaccade"): ("高级方法", "微眼跳"),
    }
    localization_ok = True
    for (module, category_key), (module_label, category_label) in expected_themes.items():
        preset = theme_presets.get((module, category_key))
        localization_ok = localization_ok and preset is not None and (
            preset.preset_id == f"theme:{module}:{category_key}"
            and preset.category == category_key
            and module_label in preset.label
            and category_label in preset.label
            and module_label in preset.description
            and category_label in preset.description
        )
    record("theme_presets_localize_labels_and_descriptions_preserve_ids_and_category_keys", localization_ok)

    page = MetricsPage(schema=schema)
    page.set_selection({
        "individual_metric_ids": (selected_id,),
        "group_metric_ids": (),
        "advanced_metric_ids": (),
        "run_individual": True,
        "run_group": False,
        "run_advanced": False,
    })
    preset = PresetDefinition(
        "catalog-ui-apply-diagnostic",
        "界面诊断预设",
        kind="fixed",
        schema_version=schema_version(schema),
        target_modules=("individual",),
        selections={"individual": (target_id,)},
    )
    before_selections = page._current_selections()
    before_parameters = deepcopy(page.all_parameter_values())
    before_enabled = dict(page._run_enabled)
    QTimer.singleShot(0, lambda: _click_active_dialog(app, QDialogButtonBox.StandardButton.Cancel))
    canceled = not page._apply_preset(preset)
    record(
        "preset_cancel_rejects_without_mutating_selection_or_parameters",
        canceled
        and page._current_selections() == before_selections
        and page.all_parameter_values() == before_parameters
        and page._run_enabled == before_enabled,
    )
    QTimer.singleShot(0, lambda: _click_active_dialog(app, QDialogButtonBox.StandardButton.Apply))
    applied = page._apply_preset(preset)
    record(
        "preset_apply_button_accepts_and_commits_only_after_confirmation",
        applied and page._selected["individual"] == {target_id},
    )
    page.close()
    page.deleteLater()

    original_step = window.step_list.currentRow()
    original_module = metrics.module
    original_tab = metrics.tabs.currentWidget()
    original_help = window.help_text.toPlainText()
    original_help_default = window.contextual_help._default_text
    original_current = metrics.catalogue.tree.currentItem()
    original_current_id = (
        original_current.data(0, Qt.ItemDataRole.UserRole) if original_current is not None else None
    )
    original_filters = (
        metrics.catalogue.search.text(),
        metrics.catalogue.category_filter.currentData(),
        metrics.catalogue.state_filter.currentData(),
        metrics.catalogue.filter_toggle.isChecked(),
    )
    original_focus = app.focusWidget()
    try:
        window.step_list.setCurrentRow(2)
        metrics.set_module("individual")
        metrics.tabs.setCurrentWidget(metrics.catalogue)
        metrics.catalogue.filter_toggle.setChecked(False)
        metrics.catalogue.search.clear()
        metrics.catalogue.category_filter.setCurrentIndex(
            metrics.catalogue.category_filter.findData("all")
        )
        metrics.catalogue.state_filter.setCurrentIndex(
            metrics.catalogue.state_filter.findData("all")
        )
        focused = metrics.focus_metric(selected_id, "individual")
        app.processEvents()
        metric_help = window.help_text.toPlainText()
        main_item = _find_item(metrics.catalogue.tree, selected_id)
        viewport_rows = _visible_rows(metrics.catalogue)
        long_source_key = (
            "analysis_stream_id=individual.pupil_left.validity_stream_for_capture_quality"
            ";reason_code=synthetic_missing_pupil"
        )
        long_help = "\n".join([long_source_key, *(["目录帮助滚动诊断"] * 80)])
        window.help_text.setPlainText(long_help)
        app.processEvents()
        scrolls = window.help_text.verticalScrollBar().maximum() > 0
        wraps_source_key = (
            window.help_text.toPlainText() == long_help
            and window.help_text.horizontalScrollBar().maximum() == 0
            and window.help_text.wordWrapMode() == QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
        )
        record("right_help_wraps_long_source_keys_and_preserves_copyable_text", wraps_source_key)
        window.help_text.setPlainText(metric_help)
        app.processEvents()
        if main_item is not None:
            metrics.catalogue.tree.setCurrentItem(main_item, 1)
            app.processEvents()
        help_text = window.help_text.toPlainText()
        record(
            "module_combo_and_parameter_help_layout_are_readable_and_scrollable",
            metrics.module_combo.minimumWidth()
            >= max(metrics.module_combo.fontMetrics().horizontalAdvance(metrics.module_combo.itemText(i))
                   for i in range(metrics.module_combo.count())) + 40
            and window.help_text.isReadOnly()
            and window.help_text.wordWrapMode() == QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
            and scrolls
            and not hasattr(metrics.catalogue, "detail")
            and metrics.parameter_error.isHidden(),
        )
        record(
            "main_window_keyboard_row_selection_shows_full_metric_help",
            focused
            and main_item is not None
            and selected_id in help_text
            and _display_label(selected_entry) in help_text,
        )
        record("catalogue_keeps_at_least_ten_visible_rows", viewport_rows >= 10)
        checks["catalogue_visible_rows"] = viewport_rows
        checks["catalogue_viewport_height"] = metrics.catalogue.tree.viewport().height()
        help_before_action = window.help_text.toPlainText()
        help_dialog = _PresetDiffDialog(
            preset,
            SimpleNamespace(incompatible_schema=False, parameters={}, blocked={}, unknown={}),
            {"individual": {"added": (target_id,)}},
            metrics,
            schema=schema,
        )
        apply_button = help_dialog.buttons.button(QDialogButtonBox.StandardButton.Apply)
        window.contextual_help.eventFilter(
            apply_button,
            QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.OtherFocusReason),
        )
        record(
            "preset_action_buttons_preserve_selected_metric_help",
            window.help_text.toPlainText() == help_before_action
            and bool(apply_button.property("preserveContextHelp")),
        )
        help_dialog.close()
    finally:
        metrics.set_module(original_module)
        metrics.tabs.setCurrentWidget(original_tab)
        search, category_data, state_data, expanded = original_filters
        metrics.catalogue.search.setText(search)
        metrics.catalogue.category_filter.setCurrentIndex(
            metrics.catalogue.category_filter.findData(category_data)
        )
        metrics.catalogue.state_filter.setCurrentIndex(
            metrics.catalogue.state_filter.findData(state_data)
        )
        metrics.catalogue.filter_toggle.setChecked(expanded)
        restored_item = _find_item(metrics.catalogue.tree, str(original_current_id)) if original_current_id else None
        if restored_item is not None:
            metrics.catalogue.tree.setCurrentItem(restored_item)
        window.step_list.setCurrentRow(original_step)
        app.processEvents()
        window.contextual_help.set_default_text(original_help_default)
        window.help_text.setPlainText(original_help)
        if original_focus is not None:
            original_focus.setFocus()
        catalogue.deleteLater()
        unavailable.deleteLater()
        unavailable_stack.deleteLater()
        unavailable_help_controller.deleteLater()
        unavailable_help.deleteLater()
        app.processEvents()

    evidence: dict[str, Any] = {
        "status": "passed" if not failures else "failed",
        "checks": checks,
        "failures": failures,
        "source_type": "SYNTHETIC_GUI_DIAGNOSTIC",
        "dataset_revision_present": window.session.dataset_revision is not None,
    }
    artifact = Path(workspace) / "catalogue-ui-diagnostics.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    evidence["evidence_path"] = str(artifact)
    return evidence


def _display_label(entry: dict[str, Any]) -> str:
    value = entry.get("label")
    return str(value) if value is not None else ""


__all__ = ["run_catalogue_ui_diagnostics"]
