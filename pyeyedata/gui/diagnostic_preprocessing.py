"""Fixed numerical and persistence checks for optional pupil preprocessing."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import numpy as np
import pandas as pd
from scipy.interpolate import PchipInterpolator

from pyeyedata.gui.dependencies import DependencyTracker
from pyeyedata.processing import (
    ProcessingConfig,
    _interpolate_column,
    _process_base,
    _pupil_preprocessing_audit,
)


def _fixture(times: list[float], pupil_left: list[float], *, blink_at: int | None = None) -> pd.DataFrame:
    count = len(times)
    values: dict[str, Any] = {
        "subject_id": ["preprocessing-diagnostic"] * count,
        "session_id": ["session-1"] * count,
        "time_s": times,
        "original_index": list(range(count)),
        "gaze_x": [10.0 + index for index in range(count)],
        "gaze_y": [20.0 + index for index in range(count)],
        "gaze_valid": [True] * count,
        "pupil_left": pupil_left,
        "pupil_left_valid": [True] * count,
        "pupil_right": [3.0] * count,
        "pupil_right_valid": [True] * count,
    }
    if blink_at is not None:
        markers = [False] * count
        markers[blink_at] = True
        values["blink_left_event"] = markers
    return pd.DataFrame(values)


def _require(checks: dict[str, bool], name: str, passed: bool) -> None:
    if not passed:
        raise AssertionError("pupil preprocessing diagnostic failed: " + name)
    checks[name] = True


def _run_plan_checks(app: Any, workspace: str | Path | None) -> dict[str, bool]:
    from PySide6.QtWidgets import QApplication

    from pyeyedata import get_analysis_schema
    from .analysis_plan import make_plan, read_plan, validate_plan, write_plan
    from .design_page import DesignPage
    from .group_page import GroupDesignPage
    from .metrics_page import MetricsPage
    from .models import SelectionSpec
    from .quality_page import QualityPage

    if QApplication.instance() is None and app is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication([])
    elif app is None:
        app = QApplication.instance()

    schema = get_analysis_schema()
    selection = SelectionSpec()
    quality = QualityPage()
    metrics = MetricsPage(schema=schema)
    group = GroupDesignPage()
    design = DesignPage(schema=schema)
    pages = (quality, metrics, group, design)
    checks: dict[str, bool] = {}
    try:
        configuration = {
            "selection": selection.to_dict(),
            "experimental": {"individual": False, "group": False, "advanced": False},
            "processing": quality.processing_config(),
            "metrics": metrics.all_parameter_values(),
            "group": group.analysis_plan_parameters(selection.group_metric_ids),
            "statistics_design": design.configuration_values(),
        }
        old_configuration = deepcopy(configuration)
        for key in (
            "pupil_interpolation_method", "pupil_speed_mad_enabled", "pupil_speed_mad_n",
            "pupil_blink_padding_enabled", "pupil_blink_padding_before_s", "pupil_blink_padding_after_s",
        ):
            old_configuration["processing"].pop(key)

        new_configuration = deepcopy(configuration)
        new_configuration["processing"].update({
            "interpolate_pupil": True,
            "pupil_interpolation_method": "pchip",
            "pupil_speed_mad_enabled": True,
            "pupil_speed_mad_n": 18,
            "pupil_blink_padding_enabled": True,
            "pupil_blink_padding_before_s": 0.04,
            "pupil_blink_padding_after_s": 0.12,
        })
        temp_parent = Path(workspace) if workspace is not None else None
        if temp_parent is not None:
            temp_parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="pyeyedata-preprocessing-plan-", dir=temp_parent) as temp:
            directory = Path(temp)
            old_path = directory / "old-plan.pyeplan"
            write_plan(old_path, make_plan(
                old_configuration, pipeline_schema=str(schema["pipeline_schema_version"]), name="old-processing-plan",
            ))
            old_restored = validate_plan(read_plan(old_path), schema)
            defaults = ProcessingConfig().to_dict()
            _require(checks, "old_plan_migrates_only_new_fields_to_defaults", all(
                old_restored["processing"][key] == defaults[key]
                for key in (
                    "pupil_interpolation_method", "pupil_speed_mad_enabled", "pupil_speed_mad_n",
                    "pupil_blink_padding_enabled", "pupil_blink_padding_before_s", "pupil_blink_padding_after_s",
                )
            ))

            new_path = directory / "new-plan.pyeplan"
            write_plan(new_path, make_plan(
                new_configuration, pipeline_schema=str(schema["pipeline_schema_version"]), name="new-processing-plan",
            ))
            new_restored = validate_plan(read_plan(new_path), schema)
            _require(checks, "new_plan_roundtrip_preserves_optional_preprocessing", all(
                new_restored["processing"][key] == new_configuration["processing"][key]
                for key in (
                    "interpolate_pupil", "pupil_interpolation_method", "pupil_speed_mad_enabled",
                    "pupil_speed_mad_n", "pupil_blink_padding_enabled",
                    "pupil_blink_padding_before_s", "pupil_blink_padding_after_s",
                )
            ))
    finally:
        for page in pages:
            page.deleteLater()
        if app is not None:
            app.processEvents()
    return checks


def _run_archive_check(audit: dict[str, Any], flags: pd.DataFrame,
                       workspace: str | Path | None) -> dict[str, Any]:
    from pyeyedata.archive_session import load_run_archive, save_run_archive
    from pyeyedata.gui.models import RunRecord

    parent = Path(workspace) if workspace is not None else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="pyeyedata-preprocessing-archive-", dir=parent) as temp:
        archive_path = Path(temp) / "pupil-preprocessing.pyea"
        record = RunRecord.from_archive(
            run_id="diagnostic-preprocessing-archive",
            generation=1,
            revision_id="diagnostic-revision",
            config_fingerprint="diagnostic-config",
            phase="completed",
            status="complete",
            created_at="2026-10-30T00:00:00+00:00",
            metadata={"execution_status": "complete", "pupil_preprocessing": audit},
            tables={"pupil_preprocessing_flags": flags},
        )
        save_run_archive(archive_path, record, mode="results_only")
        restored = load_run_archive(archive_path).record
        restored_audit = restored.metadata().get("pupil_preprocessing")
        restored_flags = restored.table_copy("pupil_preprocessing_flags")
        return {
            # RunRecord freezes JSON arrays as tuples in read-only metadata. Compare their
            # canonical JSON values so the check tests archived audit content, not container
            # mutability representation.
            "roundtrip_preserved_audit": json.dumps(
                restored_audit, sort_keys=True, ensure_ascii=False, allow_nan=False,
            ) == json.dumps(audit, sort_keys=True, ensure_ascii=False, allow_nan=False),
            "roundtrip_preserved_flags": restored_flags.equals(flags),
            "archive_mode": "results_only",
        }


def run_preprocessing_diagnostic(
    app: Any = None,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Check the fixed pupil algorithms, plan migration, dependency scope and archive trace."""
    checks: dict[str, bool] = {}

    # Default configuration keeps source values, validity and gaze behavior intact.
    default_frame = _fixture(
        [0.00, 0.01, 0.02, 0.03, 0.04], [3.0, 3.1, np.nan, 3.3, 3.4],
    )
    default_input = default_frame.copy(deep=True)
    default_config = ProcessingConfig()
    default_result = _process_base(default_frame, default_config)
    default_output = default_result.frame
    _require(checks, "optional_methods_disabled_and_linear_default", (
        default_config.pupil_interpolation_method == "linear"
        and not default_config.pupil_speed_mad_enabled
        and not default_config.pupil_blink_padding_enabled
        and not default_output["pupil_left_interpolated"].any()
        and np.allclose(
            pd.to_numeric(default_output["pupil_left"], errors="coerce").to_numpy(dtype=np.float64, na_value=np.nan),
            pd.to_numeric(default_input["pupil_left"], errors="coerce").to_numpy(dtype=np.float64, na_value=np.nan),
            equal_nan=True,
        )
        and np.array_equal(
            default_output["source_pupil_left_valid"].to_numpy(dtype=object),
            default_input["pupil_left_valid"].astype("boolean").to_numpy(dtype=object),
        )
        and np.allclose(default_output["gaze_x"].to_numpy(dtype=np.float64, na_value=np.nan), default_input["gaze_x"].to_numpy())
        and np.allclose(default_output["gaze_y"].to_numpy(dtype=np.float64, na_value=np.nan), default_input["gaze_y"].to_numpy())
        and "raw__pupil_left" not in default_output
    ))

    # Three regular missing samples use four local anchors and match SciPy's direct PCHIP oracle.
    times = [index * 0.01 for index in range(7)]
    pupil = [0.0, 1.0, np.nan, np.nan, np.nan, 2.0, 3.0]
    pchip_frame = _fixture(times, pupil)
    pchip_input = pchip_frame.copy(deep=True)
    pchip_settings = ProcessingConfig(
        support_gap_max_s=0.03, interpolate_pupil=True, pupil_interpolation_method="pchip",
        pupil_max_gap_s=0.20, max_missing_samples=3,
    )
    pchip_result = _process_base(pchip_frame, pchip_settings)
    pchip_output = pchip_result.frame
    oracle = PchipInterpolator(
        [times[0], times[1], times[5], times[6]], [pupil[0], pupil[1], pupil[5], pupil[6]],
        extrapolate=False,
    )(times[2:5])
    actual = pd.to_numeric(pchip_output.loc[2:4, "pupil_left"], errors="coerce").to_numpy(
        dtype=np.float64, na_value=np.nan,
    )
    _require(checks, "three_sample_pchip_matches_scipy_oracle", (
        np.allclose(actual, oracle, rtol=1e-12, atol=1e-12)
        and pchip_output.loc[2:4, "pupil_left_interpolated"].all()
        and pchip_output.loc[2:4, "pupil_left_interpolation_status"].eq("interpolated_pchip").all()
    ))
    _require(checks, "pchip_raw_values_and_source_validity_are_retained", (
        np.allclose(
            pd.to_numeric(pchip_output["raw__pupil_left"], errors="coerce").to_numpy(dtype=np.float64, na_value=np.nan),
            pd.to_numeric(pchip_input["pupil_left"], errors="coerce").to_numpy(dtype=np.float64, na_value=np.nan),
            equal_nan=True,
        )
        and pchip_output["source_pupil_left_valid"].eq(True).all()
        and not pchip_output["gaze_x_interpolated"].any()
    ))
    interpolation_record = next(item for item in pchip_result.pupil_preprocessing_audit
                                if item.get("eye") == "left" and item.get("stage") == "interpolation")
    inserted_gap = interpolation_record["insertion_provenance"][0]
    _require(checks, "pchip_audit_names_inserted_rows_and_actual_anchors", (
        inserted_gap["method"] == "pchip"
        and len(inserted_gap["target_observation_ids"]) == 3
        and inserted_gap["anchor_observation_ids"] == [
            pchip_output.loc[index, "observation_id"] for index in (0, 1, 5, 6)
        ]
        and inserted_gap["replacement_source"].startswith("derived_interpolation")
    ))

    # One source gap with only one left/right anchor is left missing and explained in status.
    short_anchor_frame = _fixture([0.0, 0.01, 0.02], [1.0, np.nan, 2.0])
    short_anchor = _process_base(short_anchor_frame, pchip_settings).frame
    _require(checks, "pchip_insufficient_anchors_stays_missing_with_status", (
        pd.isna(short_anchor.loc[1, "pupil_left"])
        and short_anchor.loc[1, "pupil_left_interpolation_status"] == "pchip_insufficient_anchors"
        and short_anchor.loc[1, "analysis_pupil_left_status"] == "pchip_insufficient_anchors"
    ))

    # Directly exercise the local-support guard for a .10 s step under a .03 s support limit.
    discontinuous = pd.DataFrame({
        "time_s": [0.00, 0.01, 0.02, 0.12, 0.13, 0.14, 0.15],
        "pupil_left": [0.0, 1.0, 2.0, np.nan, 3.0, 4.0, 5.0],
        "analysis_pupil_left_valid": [True, True, True, False, True, True, True],
        "source_pupil_left_valid": pd.Series([True] * 7, dtype="boolean"),
        "pupil_left_interpolated": [False] * 7,
        "pupil_left_interpolation_status": pd.Series(["not_missing"] * 7, dtype="string"),
    })
    support_stats = _interpolate_column(
        discontinuous, np.arange(7), "pupil_left", "analysis_pupil_left_valid",
        "pupil_left_interpolated", 0.20, ProcessingConfig(support_gap_factor=3.0),
        "source_pupil_left_valid", method="pchip", status_column="pupil_left_interpolation_status",
    )
    _require(checks, "pchip_skips_an_internal_timestamp_jump", (
        support_stats["skipped_pchip_discontinuous_support"] == 1
        and pd.isna(discontinuous.loc[3, "pupil_left"])
        and discontinuous.loc[3, "pupil_left_interpolation_status"] == "pchip_discontinuous_support"
    ))

    # A single spike gives median speed=0 and MAD=0; the documented strict threshold masks samples above 0.
    spike_values = [3.0, 3.0, 3.0, 3.0, 4.0, 3.0, 3.0, 3.0, 3.0]
    mad_frame = _fixture([index * 0.01 for index in range(len(spike_values))], spike_values)
    mad_input = mad_frame.copy(deep=True)
    mad_settings = ProcessingConfig(
        support_gap_max_s=0.03, interpolate_pupil=True, pupil_speed_mad_enabled=True,
        pupil_speed_mad_n=20,
    )
    mad_artifact = _process_base(mad_frame, mad_settings)
    mad_output = mad_artifact.frame
    rejected = np.flatnonzero(mad_output["pupil_left_speed_mad_rejected"].to_numpy(dtype=bool))
    mad_audit = next(item for item in mad_artifact.pupil_preprocessing_audit
                     if item.get("eye") == "left" and "speed_mad_status" in item)
    _require(checks, "mad_zero_uses_literal_threshold_and_retains_mask_provenance_after_interpolation", (
        np.array_equal(rejected, np.asarray([3, 4, 5]))
        and mad_audit["median_speed"] == 0.0 and mad_audit["mad_speed"] == 0.0
        and mad_audit["speed_threshold"] == 0.0 and mad_audit["speed_mad_status"] == "computed_zero_mad"
        and mad_audit["mad_zero_manual_review_recommended"] is True
        and mad_output.loc[rejected, "pupil_left_interpolated"].all()
        and mad_output.loc[rejected, "analysis_pupil_left_valid"].all()
        and mad_output.loc[rejected, "analysis_pupil_left_status"].eq("interpolated").all()
        and mad_output.loc[rejected, "pupil_left_speed_mad_rejected"].all()
        and int(mad_artifact.quality.loc[mad_artifact.quality["signal"].eq("pupil_left"), "n_observed"].iloc[0]) == 6
        and int(mad_artifact.quality.loc[mad_artifact.quality["signal"].eq("pupil_left"), "n_interpolated"].iloc[0]) == 3
        and np.allclose(
            mad_output["raw__pupil_left"].to_numpy(dtype=np.float64, na_value=np.nan),
            mad_input["pupil_left"].to_numpy(dtype=np.float64, na_value=np.nan), equal_nan=True,
        )
        and mad_output["source_pupil_left_valid"].eq(True).all()
    ))

    # Unknown source validity cannot anchor speed rejection of neighboring observed samples.
    unknown_frame = _fixture([index * 0.01 for index in range(9)], spike_values)
    unknown_frame["pupil_left_valid"] = unknown_frame["pupil_left_valid"].astype("boolean")
    unknown_frame.loc[4, "pupil_left_valid"] = pd.NA
    unknown_output = _process_base(unknown_frame, ProcessingConfig(
        support_gap_max_s=0.03, pupil_speed_mad_enabled=True,
    )).frame
    _require(checks, "mad_unknown_validity_is_not_an_anchor", (
        not unknown_output["pupil_left_speed_mad_rejected"].any()
        and pd.isna(unknown_output.loc[4, "source_pupil_left_valid"])
        and unknown_output.loc[4, "pupil_left"] == 4.0
    ))

    # Identical subject/session clocks in distinct sources must not share masks or MAD statistics.
    source_a = _fixture([index * 0.01 for index in range(9)], [3.0] * 9, blink_at=8)
    source_b = _fixture([index * 0.01 for index in range(9)], spike_values)
    source_a["source_id"], source_b["source_id"] = "source-A", "source-B"
    sources = pd.concat([source_a, source_b], ignore_index=True)
    sources_output = _process_base(sources, ProcessingConfig(
        support_gap_max_s=0.03, pupil_speed_mad_enabled=True,
        pupil_blink_padding_enabled=True, pupil_blink_padding_before_s=0.02,
        pupil_blink_padding_after_s=0.02,
    )).frame
    first = sources_output.loc[sources_output["source_id"].eq("source-A")]
    second = sources_output.loc[sources_output["source_id"].eq("source-B")]
    _require(checks, "pupil_masks_and_mad_are_isolated_by_source_partition", (
        first["processing_partition_id"].nunique() == 1
        and second["processing_partition_id"].nunique() == 1
        and first["processing_partition_id"].iloc[0] != second["processing_partition_id"].iloc[0]
        and first["pupil_left_blink_loss_padded"].sum() == 3
        and not first["pupil_left_speed_mad_rejected"].any()
        and not second["pupil_left_blink_loss_padded"].any()
        and second["pupil_left_speed_mad_rejected"].sum() == 3
        and not sources_output["pupil_right_blink_loss_padded"].any()
        and not sources_output["pupil_right_speed_mad_rejected"].any()
    ))

    # A deliberately oversized padding window must stop at the timestamp support barrier.
    support_frame = _fixture([0.0, 0.01, 0.02, 0.10, 0.11, 0.12], [3.0] * 6, blink_at=2)
    support_output = _process_base(support_frame, ProcessingConfig(
        support_gap_max_s=0.03, pupil_blink_padding_enabled=True,
        pupil_blink_padding_before_s=0.02, pupil_blink_padding_after_s=0.20,
    )).frame
    _require(checks, "padding_stops_at_timestamp_support_gap", (
        support_output.loc[support_output["time_s"].le(0.02), "pupil_left_blink_loss_padded"].all()
        and not support_output.loc[support_output["time_s"].ge(0.10), "pupil_left_blink_loss_padded"].any()
        and support_output.loc[support_output["time_s"].ge(0.10), "pupil_left"].eq(3.0).all()
    ))

    # A long padded gap exceeds the existing sample bound and remains missing with separate flags.
    pad_values = [3.0] * 11
    pad_frame = _fixture([index * 0.01 for index in range(11)], pad_values, blink_at=5)
    pad_input = pad_frame.copy(deep=True)
    pad_settings = ProcessingConfig(
        support_gap_max_s=0.03, interpolate_pupil=True, pupil_blink_padding_enabled=True,
        pupil_blink_padding_before_s=0.02, pupil_blink_padding_after_s=0.02,
    )
    pad_artifact = _process_base(pad_frame, pad_settings)
    pad_output = pad_artifact.frame
    padded_positions = np.flatnonzero(pad_output["pupil_left_blink_loss_padded"].to_numpy(dtype=bool))
    _require(checks, "padding_is_audited_and_masked_samples_stay_invalid", (
        np.array_equal(padded_positions, np.asarray([3, 4, 5, 6, 7]))
        and pad_output["pupil_left_blink_loss_seed"].sum() == 1
        and pad_output.loc[padded_positions, "pupil_left"].isna().all()
        and not pad_output.loc[padded_positions, "pupil_left_interpolated"].any()
        and not pad_output.loc[padded_positions, "analysis_pupil_left_valid"].any()
        and pad_output.loc[padded_positions, "analysis_pupil_left_status"].eq("blink_loss_padded").all()
        and np.allclose(
            pad_output["raw__pupil_left"].to_numpy(dtype=np.float64, na_value=np.nan),
            pad_input["pupil_left"].to_numpy(dtype=np.float64, na_value=np.nan), equal_nan=True,
        )
    ))

    # A single padded sample is allowed to be reconstructed when PCHIP has four local anchors;
    # both padding and interpolation flags remain, and audit provenance identifies every row.
    short_pad_input = _fixture([index * 0.01 for index in range(11)], [3.0] * 11, blink_at=5)
    short_pad_raw = short_pad_input.copy(deep=True)
    short_pad_settings = ProcessingConfig(
        support_gap_max_s=0.03, interpolate_pupil=True, pupil_interpolation_method="pchip",
        pupil_blink_padding_enabled=True, pupil_blink_padding_before_s=0.0,
        pupil_blink_padding_after_s=0.0,
    )
    short_pad_artifact = _process_base(short_pad_input, short_pad_settings)
    short_pad_output = short_pad_artifact.frame
    short_pad_record = next(item for item in short_pad_artifact.pupil_preprocessing_audit
                            if item.get("eye") == "left" and item.get("stage") == "interpolation")
    short_pad_gap = short_pad_record["insertion_provenance"][0]
    _require(checks, "short_padded_gap_can_be_reconstructed_with_both_flags", (
        bool(short_pad_output.loc[5, "pupil_left_blink_loss_padded"])
        and bool(short_pad_output.loc[5, "pupil_left_interpolated"])
        and bool(short_pad_output.loc[5, "analysis_pupil_left_valid"])
        and short_pad_output.loc[5, "analysis_pupil_left_status"] == "interpolated"
        and short_pad_output.loc[5, "pupil_left_interpolation_status"] == "interpolated_pchip"
        and len(short_pad_gap["target_observation_ids"]) == 1
        and short_pad_gap["anchor_observation_ids"] == [
            short_pad_output.loc[index, "observation_id"] for index in (3, 4, 6, 7)
        ]
        and short_pad_output.loc[5, "raw__pupil_left"] == short_pad_raw.loc[5, "pupil_left"]
    ))

    audit = _pupil_preprocessing_audit(short_pad_settings, short_pad_artifact.pupil_preprocessing_audit)
    archive_evidence = _run_archive_check(
        audit,
        short_pad_output[["pupil_left", "pupil_left_blink_loss_seed", "pupil_left_blink_loss_padded",
                          "pupil_left_speed_mad_rejected", "pupil_left_interpolated",
                          "pupil_left_interpolation_status"]].copy(deep=True),
        workspace,
    )
    _require(checks, "archive_roundtrip_preserves_audit_and_mask_flags", (
        archive_evidence["roundtrip_preserved_audit"] and archive_evidence["roundtrip_preserved_flags"]
    ))

    # Changing pupil controls stales pupil processing only; gaze keeps its dependency identity.
    tracker = DependencyTracker()
    inputs = {"gaze": {"x": [1.0, 2.0]}, "pupil": {"left": {"values": [3.0, 3.1]}, "right": {"values": [3.0, 3.2]}}}
    parameters = {"processing": ProcessingConfig().to_dict()}
    tracker.update(inputs, parameters)
    before = tracker.node_fingerprints
    changed_parameters = deepcopy(parameters)
    changed_parameters["processing"]["pupil_speed_mad_enabled"] = True
    tracker.update(inputs, changed_parameters)
    after = tracker.node_fingerprints
    _require(checks, "optional_pupil_settings_invalidate_pupil_not_gaze", (
        before["processing.gaze"] == after["processing.gaze"]
        and before["processing.pupil.left"] != after["processing.pupil.left"]
        and before["processing.pupil.right"] != after["processing.pupil.right"]
    ))

    checks.update(_run_plan_checks(app, workspace))
    return {
        "status": "passed",
        "checks": checks,
        "check_count": len(checks),
        "archive": archive_evidence,
        "scientific_scope": "Optional pupil preprocessing only; gaze source and gaze processing remain unchanged.",
    }
