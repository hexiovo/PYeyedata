"""Focused checks for the statistics design presets and plan round-trip."""
from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from PySide6.QtWidgets import QApplication

from pyeyedata import get_analysis_schema

from .analysis_plan import make_plan, read_plan, validate_plan, write_plan
from .design_page import DesignPage, _STATISTICS_PRESETS
from .group_page import GroupDesignPage
from .metrics_page import MetricsPage
from .models import SelectionSpec
from .quality_page import QualityPage


def _same(left: Any, right: Any) -> bool:
    return json.dumps(left, ensure_ascii=False, sort_keys=True, allow_nan=False) == json.dumps(
        right, ensure_ascii=False, sort_keys=True, allow_nan=False
    )


def run_design_presets_diagnostic(
    app: QApplication | None = None,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Check presets and .pyeplan persistence on isolated page instances.

    ``workspace`` is an optional parent for a temporary directory. It is never
    used as an input-data location, and the generated plan is removed on return.
    """
    if QApplication.instance() is None and app is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication([])
    elif app is None:
        app = QApplication.instance()

    checks: dict[str, bool] = {}

    def check(name: str, passed: bool) -> None:
        if not passed:
            raise AssertionError("statistics preset diagnostic failed: " + name)
        checks[name] = True

    page = DesignPage(schema=get_analysis_schema())
    try:
        configurations: dict[str, dict[str, Any]] = {}
        for preset_id, _label, design_type, outcome_type, analysis_unit in _STATISTICS_PRESETS:
            page.apply_preset(preset_id)
            values = page.configuration_values()
            check(
                f"preset_{preset_id}_uses_public_design_spec",
                values["design_type"] == design_type
                and values["outcome_type"] == outcome_type
                and values["analysis_unit"] == analysis_unit,
            )
            check(
                f"preset_{preset_id}_never_confirms_independence",
                values["independence_confirmed"] is False and values["independence_note"] is None,
            )
            check(
                f"preset_{preset_id}_does_not_invent_performance",
                values["performance_columns"] == () and values["performance_join_keys"] == (),
            )
            check(
                f"preset_{preset_id}_clears_irrelevant_factors",
                values["group_column"] == ("between_group" if design_type in {"independent", "team_independent"} else None)
                and values["condition_column"] == ("condition" if design_type in {"paired", "repeated", "lmm"} else None),
            )
            configurations[preset_id] = values
        check(
            "independent_two_and_multi_share_level_count_routing",
            configurations["independent_two"]["design_type"]
            == configurations["independent_multi"]["design_type"] == "independent",
        )
        check(
            "pearson_preset_declares_method",
            configurations["pearson"]["options"].get("association_method") == "pearson",
        )
        check(
            "spearman_preset_declares_method",
            configurations["spearman"]["options"].get("association_method") == "spearman",
        )
        check(
            "lmm_preset_uses_existing_subject_intercept_template",
            configurations["lmm_subject_intercept"]["options"].get("lmm_template") == "subject_intercept",
        )
        check(
            "team_preset_does_not_guess_team_id_from_interaction_id",
            configurations["team_independent"]["team_column"] is None
            and configurations["team_independent"]["interaction_column"] is None,
        )

        stale = {
            **configurations["lmm_subject_intercept"],
            "design_type": "lmm",
            "subject_column": "participant_key",
            "session_column": "visit_key",
            "trial_column": "trial_key",
            "group_column": "arm",
            "condition_column": "phase",
            "performance_columns": ["score"],
            "performance_join_keys": ["participant_key"],
            "independence_confirmed": True,
            "independence_note": "Declared in a prior custom study design.",
            "options": {
                "selected_levels": {"condition": ["pre", "post"]},
                "covariates": ["age"],
                "lmm_template": "subject_condition_slope",
                "performance_types": {"score": "continuous"},
                "aggregate_pairs": True,
            },
        }
        page.set_design_spec(stale)
        page.apply_preset("independent_two")
        switched = page.configuration_values()
        switched_options = switched["options"]
        check(
            "preset_switch_clears_stale_factor_model_and_performance_settings",
            switched["group_column"] == "between_group"
            and switched["condition_column"] is None
            and switched["performance_columns"] == ()
            and switched["performance_join_keys"] == ()
            and "selected_levels" not in switched_options
            and "covariates" not in switched_options
            and "lmm_template" not in switched_options
            and switched_options.get("aggregate_pairs") is not True,
        )
        check(
            "preset_switch_preserves_only_meaningful_subject_ids",
            switched["subject_column"] == "participant_key"
            and switched["session_column"] == "visit_key"
            and switched["trial_column"] == "trial_key",
        )

        custom = {
            **switched,
            "design_type": "lmm",
            "analysis_unit": "subject",
            "group_column": "arm",
            "condition_column": "visit_type",
            "performance_columns": ["score"],
            "performance_join_keys": ["participant_key", "visit_key"],
            "independence_confirmed": True,
            "independence_note": "Participant IDs map to distinct enrolled people; repeated visits are nested within participant.",
            "options": {
                **switched_options,
                "selected_levels": {"condition": ["pre", "post"]},
                "covariates": ["age"],
                "lmm_template": "subject_condition_slope",
                "performance_types": {"score": "continuous"},
                "performance_units": {"score": "points"},
            },
        }
        page.set_design_spec(custom)
        restored_in_page = page.configuration_values()
        check("custom_design_does_not_show_active_preset", page._active_preset_id is None)
        check("custom_design_fields_and_hidden_options_are_preserved", _same(restored_in_page, custom))
        check(
            "collapsed_custom_configuration_surfaces_nondefault_options",
            "组别与条件水平选择" in page.settings_summary_label.text()
            and "自定义设置" in page.settings_summary_label.text(),
        )

        schema = get_analysis_schema()
        selection = SelectionSpec()
        quality = QualityPage()
        metrics = MetricsPage(schema=schema)
        group = GroupDesignPage()
        plan_design = DesignPage(schema=schema)
        helper_pages = [quality, metrics, group, plan_design]
        try:
            plan_design.set_design_spec(custom)
            configuration = {
                "selection": selection.to_dict(),
                "experimental": {"individual": False, "group": False, "advanced": False},
                "processing": quality.processing_config(),
                "metrics": metrics.all_parameter_values(),
                "group": group.analysis_plan_parameters(selection.group_metric_ids),
                "statistics_design": plan_design.configuration_values(),
            }
            payload = make_plan(
                configuration,
                pipeline_schema=str(schema["pipeline_schema_version"]),
                name="statistics-presets-diagnostic",
            )
            workspace_path = Path(workspace) if workspace is not None else None
            if workspace_path is not None:
                workspace_path.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(prefix="pyeyedata-statistics-presets-", dir=workspace_path) as temp:
                plan_path = Path(temp) / "custom.pyeplan"
                write_plan(plan_path, payload)
                loaded = read_plan(plan_path)
                validated = validate_plan(loaded, schema)
                check("pyeplan_api_roundtrip_preserves_custom_statistics_design",
                      _same(validated["statistics_design"], configuration["statistics_design"]))
                check("pyeplan_loaded_design_is_not_a_named_preset",
                      plan_design._active_preset_id is None)
        finally:
            for helper_page in helper_pages:
                helper_page.deleteLater()
        if app is not None:
            app.processEvents()
    finally:
        page.deleteLater()
        if app is not None:
            app.processEvents()

    return {
        "status": "passed",
        "checks": checks,
        "preset_ids": [item[0] for item in _STATISTICS_PRESETS],
        "check_count": len(checks),
        "plan_roundtrip": "make_plan -> write_plan -> read_plan -> validate_plan",
    }
