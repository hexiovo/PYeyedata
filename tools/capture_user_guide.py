"""Authoring tool: capture actual Qt widgets with SYNTHETIC_ONLY practice data.

Run with the pinned application interpreter from the repository root:
python -B tools/capture_user_guide.py --work <new external directory>
No mouse takeover, synthetic UI painting, or external device claims.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=False)
    from PySide6.QtCore import QPoint, QRect, QTimer
    from PySide6.QtWidgets import QApplication, QToolBar, QPushButton, QDialog, QMessageBox, QComboBox
    from pyeyedata.gui.app import create_application
    from pyeyedata.gui.main_window import MainWindow
    from pyeyedata.gui.help_center import export_learning_pack, make_help_dialog
    from pyeyedata.gui.analysis_plan import read_plan
    from pyeyedata.gui.diagnostic import _wait_for_condition, _run_gui_task
    from pyeyedata.app_info import APP_VERSION

    app = create_application(["PYeyedata guide capture"])
    window = MainWindow()
    window.show()
    window.resize(1400, 1000)
    app.processEvents()
    assets = ROOT / "pyeyedata/resources/help/source/screenshots"
    assets.mkdir(parents=True, exist_ok=True)
    manifest = {"application_version": APP_VERSION, "synthetic_only": True,
                "capture_method": "actual Qt QWidget.grab; no OS mouse control", "images": {}}

    def shot(name, widget, targets=(), crop=None):
        app.processEvents()
        rect = crop or widget.rect()
        pix = widget.grab(rect)
        path = assets / (name + ".png")
        if not pix.save(str(path)):
            raise RuntimeError("Cannot save " + name)
        marks = []
        for number, target in enumerate(targets, 1):
            if isinstance(target, QRect):
                box = target
            else:
                box = QRect(target.mapTo(widget, QPoint(0, 0)), target.size())
            box = box.intersected(rect)
            if box.isEmpty():
                raise RuntimeError(f"Callout outside image: {name} #{number}")
            box.translate(-rect.x(), -rect.y())
            ratio = pix.devicePixelRatio()
            marks.append([round(v * ratio) for v in (box.x(), box.y(), box.width(), box.height())])
        manifest["images"][name] = {"file": "screenshots/" + path.name,
            "width": pix.width(), "height": pix.height(), "marks": marks,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        print(name, pix.width(), pix.height(), marks, flush=True)

    def modal_shot(name, operation, choose_targets):
        failures = []
        def capture():
            try:
                dialog = QApplication.activeModalWidget()
                if not isinstance(dialog, QDialog):
                    raise RuntimeError("Expected actual modal dialog for " + name)
                dialog.resize(max(dialog.width(), 700), dialog.height())
                app.processEvents()
                shot(name, dialog, choose_targets(dialog))
                dialog.reject()
            except Exception as exc:
                failures.append(str(exc))
                if QApplication.activeModalWidget():
                    QApplication.activeModalWidget().reject()
        QTimer.singleShot(180, capture)
        operation()
        if failures:
            raise RuntimeError(str(failures))

    try:
        toolbar = next(bar for bar in window.findChildren(QToolBar) if window.pdf_guide_action in bar.actions())
        shot("01-toolbar", toolbar, [toolbar.widgetForAction(window.pdf_guide_action),
             toolbar.widgetForAction(window.help_action), toolbar.widgetForAction(window.preflight_action),
             toolbar.widgetForAction(window.run_action)], QRect(0, 0, 790, toolbar.height()))
        shot("01-navigation", window.step_list, [window.step_list.visualItemRect(window.step_list.item(0)),
              window.step_list.visualItemRect(window.step_list.item(6))])
        help_dialog = make_help_dialog(window)
        help_dialog.show(); app.processEvents()
        export_button = next(b for b in help_dialog.findChildren(QPushButton) if b.text().startswith("导出练习"))
        shot("02-help", help_dialog, [export_button, help_dialog.findChild(QPushButton, "pdfGuideButton")])
        help_dialog.close()
        pack = export_learning_pack(args.work)
        panel = window.import_page.sources["signals"]
        panel.path_edit.setText(str(pack/"signals-practice.csv"))
        previewed, failed = [], []
        panel.preview_loaded.connect(lambda *values: previewed.append(values))
        panel.preview_failed.connect(lambda *values: failed.append(values))
        panel.request_preview()
        _wait_for_condition(app, lambda: bool(previewed or failed), "guide preview")
        if failed:
            raise RuntimeError(str(failed))
        for name in ("subject_id", "session_id", "time_s", "pupil_left", "pupil_left_valid"):
            row = next(i for i, spec in enumerate(panel.mapping.fields) if spec.name == name)
            combo = panel.mapping._table.cellWidget(row, 2)
            combo.setCurrentIndex(combo.findData(name))
            if name == "pupil_left":
                pupil_row = row
                panel.mapping._table.cellWidget(row, 5).setCurrentText("mm")
        shot("03-file", panel, [panel.path_edit, panel.preview_button, panel.help_button],
             QRect(0, 0, panel.width(), panel.mapping.y()))
        table = panel.mapping._table
        for i, width in enumerate((100, 90, 175, 80, 100, 100)):
            table.setColumnWidth(i, width)
        table.scrollToTop(); app.processEvents()
        shot("04-mapping", table, [table.cellWidget(0, 2), table.cellWidget(pupil_row, 2),
                                   table.cellWidget(pupil_row, 5)])
        valid_row = next(i for i, spec in enumerate(panel.mapping.fields) if spec.name == "pupil_left_valid")
        table.scrollToItem(table.item(valid_row, 0)); app.processEvents()
        shot("04-validity", table, [table.cellWidget(valid_row, 2)])
        coordinate = window.import_page.coordinate
        coordinate.time_unit.setCurrentIndex(coordinate.time_unit.findData("s"))
        coordinate.time_confirmed.setChecked(True)
        coordinate.pupil_left_unit.setText("mm")
        shot("05-time", coordinate, [coordinate.time_unit, coordinate.time_confirmed],
             QRect(0, 0, coordinate.width(), 175))
        pupil_top = coordinate.pupil_left_unit.mapTo(coordinate, QPoint(0, 0)).y() - 115
        shot("05-pupil-unit", coordinate, [coordinate.pupil_left_unit],
             QRect(0, pupil_top, coordinate.width(), 180))
        shot("06-import", window.import_page, [window.import_page.import_button],
             QRect(0, window.import_page.height()-90, window.import_page.width(), 90))
        window.import_page.start_import()
        _wait_for_condition(app, lambda: not window.import_page.import_active, "guide import")
        if window.session.dataset_revision is None:
            raise RuntimeError(window.import_page.status.text())
        window.apply_analysis_plan(read_plan(pack/"pupil-mean-practice.pyeplan"))
        window.step_list.setCurrentRow(1); app.processEvents()
        q = window.quality_page
        shot("07-processing", q.controls_box, [q.interpolate_pupil, q.use_lowpass, q.use_baseline])
        window.step_list.setCurrentRow(2); app.processEvents()
        m = window.metrics_page
        m.catalogue.search.setText("individual.pupil_left.mean")
        app.processEvents()
        menu = window.file_menu
        menu.popup(window.mapToGlobal(QPoint(10, 45))); app.processEvents()
        shot("09-plan-menu", menu, [menu.actionGeometry(window.save_plan_action), menu.actionGeometry(window.load_plan_action)])
        menu.close()
        outcome, failed = _run_gui_task(app, window, phase="preflight")
        if outcome is None or failed:
            raise RuntimeError(str(failed))
        window.step_list.setCurrentRow(2); app.processEvents()
        for i, width in enumerate((370, 145, 90, 80)):
            m.catalogue.tree.setColumnWidth(i, width)
        shot("08-metric", m, [m.run_module, m.catalogue.search, m.catalogue.tree])
        window.step_list.setCurrentRow(1); app.processEvents()
        q.tabs.setCurrentIndex(0)
        shot("10-quality", q.tabs, [q.tabs.tabBar()])
        outcome, failed = _run_gui_task(app, window, phase="run")
        if outcome is None or failed:
            raise RuntimeError(str(failed))
        record = window.session.history[0]
        row = record.tables()["metrics"].query("metric_id == 'individual.pupil_left.mean'").iloc[0]
        if row["status"] != "complete" or abs(float(row["value"]) - 3.3) > 1e-10:
            raise RuntimeError("Practice metric mismatch")
        window.step_list.setCurrentRow(6); app.processEvents()
        r = window.results_page
        shot("11-results", r, [r.history_choice, r.archive_button, r.full_export_button, r.reopen_button],
             QRect(0, 0, r.width(), r.tabs.y()))
        r.tabs.setCurrentWidget(r.individual_tab); app.processEvents()
        t = r.individual_tab
        frame = t._source_model._frame
        value_index = list(frame.columns).index("value")
        t.table_view.scrollTo(t._proxy_model.index(0, value_index)); app.processEvents()
        shot("12-table", t, [t.table_choice, t.table_view, t.filtered_export_button, t.save_chart_button],
             QRect(0, 0, t.width(), 300))
        shot("12-chart", t.chart, [t.chart.x_column, t.chart.y_column, t.chart.draw_button])
        modal_shot("13-export", window._export_dialog, lambda d: d.findChildren(QComboBox))
        modal_shot("14-archive", lambda: window._save_archive_dialog(record), lambda d: d.findChildren(QComboBox))
        modal_shot("15-exit", lambda: window._confirm_unarchived_close(window.unarchived_records()),
                   lambda d: [next(b for b in d.buttons() if b.text() == text)
                              for text in ("返回继续使用", "逐个归档后退出", "不归档，直接退出")])
        window.step_list.setCurrentRow(3); app.processEvents()
        g = window.group_page
        g.tabs.setCurrentIndex(0)
        shot("16-group", g, [g.mode_combo, g.member_table, g.tabs.tabBar()])
        g.tabs.setCurrentIndex(2); app.processEvents()
        shot("17-clock", g.time_confirmed.parentWidget(), [g.time_confirmed, g.sync_confirmed])
        shot("17-space", g.spatial_comparable.parentWidget(), [g.spatial_comparable, g.pupil_comparable, g.aoi_confirmed])
        window.step_list.setCurrentRow(4); app.processEvents()
        d = window.design_page
        design_box = d._design_editors["design_type"].parentWidget()
        shot("18-design", design_box, [d._design_editors[key] for key in ("design_type", "analysis_unit")],
             QRect(0, 0, design_box.width(), 265))
        independence = d._design_editors["independence_confirmed"]
        iy = independence.mapTo(design_box, QPoint(0, 0)).y()
        shot("18-independence", design_box, [independence], QRect(0, iy-10, design_box.width(), design_box.height()-iy+10))
        window.step_list.setCurrentRow(5); app.processEvents()
        m.catalogue.search.clear()
        shot("19-advanced", m, [m.run_module, m.catalogue.experimental_toggle, m.catalogue.tree])
        window.step_list.setCurrentRow(0)
        window.import_page.source_tabs.setCurrentWidget(window.import_page.context_editor)
        app.processEvents()
        c = window.import_page.context_editor
        shot("20-context", c, [c.tabs.tabBar(), c.panels["external_events"].preview_button,
                               c.panels["external_events"].load_button], QRect(0, 0, c.width(), 430))
        manifest["practice_result"] = {"rows": 201, "metric_id": str(row["metric_id"]),
                                      "value": float(row["value"]), "status": str(row["status"]),
                                      "overall_status": record.status}
        (assets.parent / "screenshots.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    finally:
        window._confirm_unarchived_close = lambda _: "discard"
        window.close(); app.processEvents()


if __name__ == "__main__":
    main()
