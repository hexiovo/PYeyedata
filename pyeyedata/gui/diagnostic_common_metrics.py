"""Reusable synthetic/oracle checks for common eye-tracking metrics (no device claims)."""
from __future__ import annotations

import json
import math
from pathlib import Path
import random
import statistics

import numpy as np
import pandas as pd


def _fixture():
    times = np.round(np.arange(0, 8.01, .1), 8)
    signals = pd.DataFrame({"subject_id": "S1", "session_id": "session", "trial": "T1",
        "stimulus_id": "text", "time_s": times, "time_raw": times,
        "original_index": np.arange(len(times)), "gaze_x": 5.0, "gaze_y": 5.0,
        "gaze_valid": True, "time_valid": True})
    events = []
    fixations = [("A", .2, .8), ("B", 1., 1.4), ("B", 1.6, 2.),
                 ("A", 2.2, 2.5), ("B", 2.8, 3.), ("C", 3.2, 3.6), ("B", 5., 5.2)]
    centers = {"A": 5., "B": 15., "C": 25., "D": 35., "E": 45.}
    for index, (aid, start, end) in enumerate(fixations):
        signals.loc[signals["time_s"].between(start, end), "gaze_x"] = centers[aid]
        events.append({"event_id": f"F{index}", "event_type": "fixation", "start_s": start, "end_s": end})
    for index, (start, end, amp) in enumerate([(.8, 1., 2.), (1.4, 1.6, 4.), (2., 2.2, 8.)]):
        events.append({"event_id": f"S{index}", "event_type": "saccade", "start_s": start, "end_s": end,
                       "start_x": 0., "start_y": 0., "end_x": amp, "end_y": 0.})
    for index, (start, end) in enumerate([(4., 4.1), (6., 6.3), (7., 7.2)]):
        events.append({"event_id": f"B{index}", "event_type": "blink", "start_s": start, "end_s": end})
    for event in events:
        event.update(subject_id="S1", session_id="session", trial="T1", stimulus_id="text",
                     status="source_confirmed", reason_code=None, n_observed=3, n_interpolated=0,
                     event_evidence="vendor_observed", source_refs=("synthetic:events",),
                     algorithm_version="synthetic-events-v1", parameter_ref="synthetic")
    aois = pd.DataFrame([{"aoi_id": aid, "stimulus_id": "text", "shape": "rectangle", "aoi_name": aid,
        "xmin": value-4, "xmax": value+4, "ymin": 0., "ymax": 10., "unit": "px",
        "coordinate_frame": "synthetic", "overlap_policy": "exclusive", "priority": index}
        for index, (aid, value) in enumerate(centers.items())])
    annotations = pd.DataFrame([{"reading_scope_id": "sentence", "stimulus_id": "text", "aoi_id": aid,
        "reading_order": order} for aid, order in [("A", 1), ("D", 2), ("B", 3), ("C", 4), ("E", 5)]])
    config = {"run_id": "synthetic-common", "time_unit": "s", "time_confirmed": True,
        "coordinate_unit": "px", "coordinate_frame": "synthetic", "max_gap_s": .15,
        "aoi_mapping_confirmed": True, "reference_event_s": 0., "fixation_observation_confirmed": True,
        "reading_order_confirmed": True, "reading_scope_id": "sentence", "reading_direction": "ltr",
        "event_source_confirmed": True, "event_source": "synthetic", "event_algorithm_version": "synthetic-v1",
        "event_source_types": ["fixation", "saccade", "blink"], "blink_source_confirmed": True,
        "events": pd.DataFrame(events), "reading_annotations": annotations,
        "provenance": {"source_manifest": [{"source_id": "synthetic", "sha256": "a"*64}]}}
    return signals, aois, config


def run_common_metric_checks(workspace: Path | None = None, *, include_gui: bool = True) -> dict:
    from ..common_metrics import NEW_IDS, distribution, reading_measures, _reading_table
    from ..individual import analyze_individual
    checks = {}
    def check(name, condition):
        if not condition:
            raise AssertionError("common metric check failed: "+name)
        checks[name] = True
    signals, aois, config = _fixture()
    def run(frame=None, overrides=None, selected=None, segments=None):
        cfg = {**config, "metric_ids": list(NEW_IDS if selected is None else selected), **(overrides or {})}
        return analyze_individual(signals if frame is None else frame, cfg, segments=segments, aois=aois)
    result = run()
    metrics = result.tables["metrics"]
    def row(mid, aid=None, table=metrics):
        found = table.loc[table["metric_id"].eq(mid)]
        if aid is not None:
            found = found.loc[found["parameter_ref"].map(lambda p: str(json.loads(p).get("aoi_id")) == aid)]
        if len(found) != 1:
            raise AssertionError(f"expected exactly one {mid}/{aid}: {found.to_dict('records')}")
        return found.iloc[0]
    def value(mid, aid=None, table=metrics):
        return float(row(mid, aid, table)["value"])
    check("catalogue_selection_has_all_52_ids", set(metrics["metric_id"]) == NEW_IDS and len(NEW_IDS) == 52)
    for suffix, expected in {"fixation_ttff": 1., "fixation_first_duration": .4,
                             "fixation_total_duration": 1.2, "fixation_mean_duration": .3,
                             "fixations_before_first": 1., "hit": 1.}.items():
        check("aoi_"+suffix, math.isclose(value("individual.aoi."+suffix, "B"), expected, abs_tol=1e-12))
    check("unhit_ttff_is_missing", pd.isna(row("individual.aoi.fixation_ttff", "D")["value"]))
    check("known_miss_is_zero", value("individual.aoi.hit", "D") == 0)
    for suffix, expected in {"first_fixation_duration": .4, "first_pass_duration": .8,
                             "go_past_duration": 1.3, "rereading_duration": .4,
                             "first_pass_regression": 1., "skipping": 0.}.items():
        check("reading_"+suffix, math.isclose(value("individual.aoi.reading."+suffix, "B"), expected, abs_tol=1e-12))
    check("multi_fixation_not_single", row("individual.aoi.reading.single_fixation_duration", "B")["reason_code"] == "multiple_target_fixations")
    check("skipping_requires_observed_progression", value("individual.aoi.reading.skipping", "D") == 1)
    check("not_reached_is_censored", pd.isna(row("individual.aoi.reading.skipping", "E")["value"]))
    check("go_past_right_censored", row("individual.aoi.reading.go_past_duration", "C")["reason_code"] == "go_past_right_censored")
    check("sample_sd", math.isclose(value("individual.saccade.amplitude.sd"), statistics.stdev([2., 4., 8.]), abs_tol=1e-12))
    check("linear_amplitude_iqr", value("individual.saccade.amplitude.iqr") == 3.)
    check("blink_onset_interval", math.isclose(value("individual.blink.interval.median"), 1.5))
    check("single_sd_unavailable", distribution([1.], "sd") == (None, "insufficient_events_for_sample_sd"))
    check("empty_distribution_unavailable", distribution([], "p10")[0] is None)
    check("audit_keeps_source_ids", set(result.tables["aoi_fixation_audit"]["event_id"]) == {f"F{i}" for i in range(7)})
    # Independent linear-quantile oracle and Python statistics sample SD.
    rng = random.Random(20261027)
    comparisons = 0
    for _ in range(120):
        values = [rng.uniform(0, 20) for _ in range(rng.randrange(2, 45))]
        ordered = sorted(values)
        def quantile(p):
            point = (len(ordered)-1)*p
            low = math.floor(point)
            return ordered[low]+(ordered[min(low+1, len(ordered)-1)]-ordered[low])*(point-low)
        expected = {"median": statistics.median(values), "sd": statistics.stdev(values),
                    "iqr": quantile(.75)-quantile(.25), **{f"p{p}": quantile(p/100) for p in (10, 25, 75, 90)}}
        for name, oracle in expected.items():
            check(f"distribution_oracle_{comparisons}", math.isclose(distribution(values, name)[0], oracle, rel_tol=1e-12, abs_tol=1e-12))
            comparisons += 1
    # Generated sequence oracle walks the first visit and first progressive exit independently.
    for index in range(100):
        orders = [rng.randrange(1, 6) for _ in range(rng.randrange(2, 20))]
        durations = [rng.randrange(1, 10)/10 for _ in orders]
        target = rng.randrange(1, 6)
        sequence = [{"order": order, "duration": dur} for order, dur in zip(orders, durations)]
        answer = reading_measures(sequence, target, True)
        first = next((i for i, order in enumerate(orders) if order >= target), None)
        if first is None:
            check(f"reading_oracle_censored_{index}", answer["skipping"][0] is None)
        elif orders[first] > target:
            check(f"reading_oracle_skip_{index}", answer["skipping"][0] == 1)
        else:
            progressive = next((i for i in range(first+1, len(orders)) if orders[i] > target), None)
            expected = sum(durations[first:progressive]) if progressive is not None else None
            check(f"reading_oracle_path_{index}", answer["go_past_duration"][0] is None if expected is None else math.isclose(answer["go_past_duration"][0], expected))
    rtl = run(overrides={"reading_direction": "rtl"})
    check("rtl_uses_explicit_order", math.isclose(value("individual.aoi.reading.go_past_duration", "B", rtl.tables["metrics"]), 1.3))
    blocked = run(overrides={"reading_order_confirmed": False}, selected=["individual.aoi.reading.skipping"])
    check("reading_confirmation_gate", blocked.tables["metrics"]["status"].eq("unavailable").all())
    duplicate = config["reading_annotations"].copy()
    duplicate.loc[1, "reading_order"] = 1
    check("duplicate_reading_order_gate", _reading_table({**config, "reading_annotations": duplicate})[1] is not None)
    boolean_order = config["reading_annotations"].copy().astype({"reading_order": object})
    boolean_order.loc[0, "reading_order"] = True
    check("boolean_not_reading_order", _reading_table({**config, "reading_annotations": boolean_order})[1] is not None)
    legacy_annotations = run(overrides={"reading_annotations": pd.DataFrame({"reading_scope_id": ["sentence"]})}, selected=["individual.aoi.reading.skipping"])
    check("legacy_annotations_remain_unavailable", legacy_annotations.tables["metrics"]["status"].eq("unavailable").all())
    unconfirmed = run(overrides={"fixation_observation_confirmed": False}, selected=["individual.aoi.hit"])
    check("unconfirmed_empty_is_unknown", pd.isna(row("individual.aoi.hit", "D", unconfirmed.tables["metrics"])["value"]))
    invalid = signals.copy()
    invalid.loc[invalid["time_s"].eq(1.2), "gaze_valid"] = False
    invalid_result = run(frame=invalid, selected=["individual.aoi.fixation_first_duration", "individual.aoi.hit"])
    check("invalid_event_not_complete", row("individual.aoi.fixation_first_duration", "B", invalid_result.tables["metrics"])["status"] != "complete")
    check("gap_not_counted_as_miss", pd.isna(row("individual.aoi.hit", "D", invalid_result.tables["metrics"])["value"]))
    source_invalid = signals.copy()
    source_invalid["source_gaze_valid"] = True
    source_invalid.loc[source_invalid["time_s"].eq(1.2), "source_gaze_valid"] = False
    source_result = run(frame=source_invalid, selected=["individual.aoi.fixation_first_duration"])
    check("source_invalid_not_repaired_by_analysis_flag", row("individual.aoi.fixation_first_duration", "B", source_result.tables["metrics"])["status"] == "partial")
    interpolated = signals.copy()
    interpolated["gaze_interpolated"] = interpolated["time_s"].eq(1.2)
    interp_result = run(frame=interpolated, selected=["individual.aoi.hit"])
    check("interpolation_not_counted_as_observed_miss", pd.isna(row("individual.aoi.hit", "D", interp_result.tables["metrics"])["value"]))
    gap_frame = signals.loc[~signals["time_s"].between(1.1, 1.3)].copy()
    gap_result = run(frame=gap_frame, selected=["individual.aoi.hit"])
    check("long_gap_not_bridged", pd.isna(row("individual.aoi.hit", "D", gap_result.tables["metrics"])["value"]))
    window = pd.DataFrame([{"subject_id": "S1", "session_id": "session", "segment_id": "window", "start_s": 1.2, "end_s": 3.5}])
    window_result = run(overrides={"reference_event_s": 1.2}, segments=window,
        selected=["individual.aoi.fixation_ttff", "individual.aoi.reading.skipping", "individual.fixation.duration.sd"])
    check("prewindow_onset_not_recounted", math.isclose(value("individual.aoi.fixation_ttff", "B", window_result.tables["metrics"]), .4, abs_tol=1e-12))
    check("reading_left_censoring_preserved", row("individual.aoi.reading.skipping", "B", window_result.tables["metrics"])["reason_code"] == "reading_window_left_censored")
    check("clipped_distribution_partial", row("individual.fixation.duration.sd", table=window_result.tables["metrics"])["status"] == "partial")
    second_trial = config["events"].copy(deep=True)
    second_trial["trial"] = "T2"
    second_trial["event_id"] = "OTHER-"+second_trial["event_id"]
    labeled_window = pd.DataFrame([{"subject_id": "S1", "session_id": "session", "segment_id": "trial-label", "trial": "T1", "start_s": 0., "end_s": 8.}])
    labeled_result = run(frame=signals.drop(columns="trial"), segments=labeled_window,
        overrides={"events": pd.concat([config["events"], second_trial], ignore_index=True)},
        selected=["individual.fixation.duration.sd"])
    check("segment_trial_scope_filters_event_sources", row("individual.fixation.duration.sd", table=labeled_result.tables["metrics"])["n_observed"] == 7)
    no_source = run(overrides={"events": config["events"].assign(event_evidence="unknown"), "event_source_confirmed": False}, selected=["individual.fixation.duration.sd"])
    check("unqualified_source_gate", no_source.tables["metrics"]["status"].eq("unavailable").all())
    mixed_events = config["events"].copy(deep=True)
    mixed_events.loc[mixed_events["event_id"].eq("F0"), "event_evidence"] = "unknown"
    mixed = run(overrides={"events": mixed_events}, selected=["individual.aoi.hit", "individual.aoi.reading.skipping", "individual.fixation.duration.sd"])
    check("unqualified_fixation_not_treated_as_miss", pd.isna(row("individual.aoi.hit", "D", mixed.tables["metrics"])["value"]))
    check("unknown_reading_event_not_treated_as_skipping", pd.isna(row("individual.aoi.reading.skipping", "D", mixed.tables["metrics"])["value"]))
    check("mixed_qualification_distribution_partial", row("individual.fixation.duration.sd", table=mixed.tables["metrics"])["status"] == "partial")
    unbound = run(frame=signals.assign(physical_partition_id="P1"), selected=["individual.aoi.hit"])
    check("unbound_events_not_confirmed_misses", pd.isna(row("individual.aoi.hit", "D", unbound.tables["metrics"])["value"]))
    invalid_interval = config["events"].copy(deep=True)
    invalid_interval.loc[invalid_interval["event_id"].eq("F0"), "start_s"] = np.nan
    invalid_interval_result = run(overrides={"events": invalid_interval}, selected=["individual.aoi.hit"])
    check("unplaced_event_not_confirmed_miss", pd.isna(row("individual.aoi.hit", "D", invalid_interval_result.tables["metrics"])["value"]))
    missing_provenance = run(overrides={"provenance": {}}, selected=["individual.aoi.hit"])
    check("audit_does_not_upgrade_missing_provenance", missing_provenance.tables["aoi_fixation_audit"]["status"].eq("partial").all())
    # Two observed trials for S1 (one hit), one for S2 (hit), unknown for S3.
    from ..common_metrics import append_hit_rates, RATE_IDS
    exposures = []
    for subject, trial, hit in [("S1", "t1", 1.), ("S1", "t2", 0.), ("S2", "t1", 1.), ("S3", "t1", None)]:
        exposures.append({"subject": subject, "session": "s", "segment": "seg", "condition": "c",
            "stimulus_id": "text", "aoi_id": "B", "analysis_stream_id": "gaze:left", "aoi_geometry_fingerprint": "g",
            "trial": trial, "hit": hit, "support_ref": subject+trial})
    rates = []
    append_hit_rates(exposures + [exposures[0]], config, True, RATE_IDS, rates)
    rate_frame = pd.DataFrame(rates)
    s1 = rate_frame.loc[rate_frame["subject_id"].eq("S1") & rate_frame["metric_id"].eq("individual.aoi.trial_hit_rate")].iloc[0]
    participant = rate_frame.loc[rate_frame["metric_id"].eq("individual.aoi.participant_hit_rate")].iloc[0]
    check("trial_units_deduplicated", s1["value"] == .5 and json.loads(s1["parameter_ref"])["denominator"] == 2)
    check("participant_units_not_trial_weighted", participant["value"] == 1. and participant["status"] == "partial" and json.loads(participant["parameter_ref"])["unknown_units"] == 1)
    check("skipping_rate_separate_from_binary", value("individual.aoi.reading.skipping_rate", "D") == 1.)
    check("first_pass_regression_rate", value("individual.aoi.reading.first_pass_regression_rate", "B") == 1.)
    missing_rates = []
    append_hit_rates([{**exposures[0], "trial": None}], config, True, {"individual.aoi.trial_hit_rate"}, missing_rates)
    check("trial_rate_requires_trial_identity", missing_rates[0]["status"] == "unavailable")
    if include_gui:
        from PySide6.QtWidgets import QApplication
        from .metrics_page import MetricsPage
        from .mapping import context_fields
        from ..pipeline import get_analysis_schema
        app = QApplication.instance() or QApplication([])
        page = MetricsPage(schema=get_analysis_schema())
        check("gui_has_confirmation_and_reference_fields", {"reading_order_confirmed", "reference_event_s", "fixation_observation_confirmed"} <= set(page._forms["individual"].editors))
        check("reading_mapping_has_order", {"aoi_id", "reading_order"} <= {s.name for s in context_fields("reading_annotations")})
        page.deleteLater()
        app.processEvents()
        from .main_window import MainWindow
        from .analysis_plan import validate_plan, write_plan, read_plan
        from .method_registry import COMMON_PLAN_DEFAULTS
        from copy import deepcopy
        host = MainWindow()
        payload = host.analysis_plan("Synthetic metric compatibility")
        old_payload = deepcopy(payload)
        for key in COMMON_PLAN_DEFAULTS:
            old_payload["configuration"]["metrics"]["individual"].pop(key, None)
        restored = validate_plan(old_payload, get_analysis_schema())
        check("old_v1_plan_additive_safe_defaults", restored["metrics"] == payload["configuration"]["metrics"])
        check("plan_migration_does_not_mutate_payload", not set(COMMON_PLAN_DEFAULTS) & set(old_payload["configuration"]["metrics"]["individual"]))
        if workspace is not None:
            plan_path = workspace / "common-legacy.pyeplan"
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            write_plan(plan_path, old_payload)
            check("old_plan_file_roundtrip", validate_plan(read_plan(plan_path), get_analysis_schema())["metrics"] == restored["metrics"])
        host.deleteLater()
        app.processEvents()
    if workspace is not None:
        workspace.mkdir(parents=True, exist_ok=True)
        metrics.to_csv(workspace / "common-metrics.csv", index=False, encoding="utf-8-sig")
        integration = _pipeline_archive_checks(workspace, check)
    else:
        import tempfile
        with tempfile.TemporaryDirectory(prefix="PYeyedata-common-metrics-") as temporary:
            integration = _pipeline_archive_checks(Path(temporary), check)
    return {"status": "pass", "synthetic_only": True, "seed": 20261027,
            "new_metric_ids": len(NEW_IDS), "checks": len(checks), "distribution_oracle_comparisons": comparisons,
            "reading_generated_cases": 100, "integration": integration,
            "coverage": [name for name in checks if not name.startswith(("distribution_oracle_", "reading_oracle_"))]}


def _pipeline_archive_checks(workspace, check):
    from ..contracts import NormalizedDataset
    from ..common_metrics import NEW_IDS
    from ..pipeline import (AnalysisConfig, EyeSelectionConfig, EventDetectionConfig,
                            run_analysis, save_analysis, load_analysis)
    from ..store import dataframe_to_csv_member, dataframe_from_csv_member
    signals, aois, config = _fixture()
    signals["source_id"] = "synthetic"
    signals["timebase_id"] = "clock"
    signals["clock_id"] = "clock"
    external = config["events"].assign(source_id="synthetic", analysis_stream_id="gaze:legacy_source",
        timebase_id="clock", clock_id="clock", time_unit="s", classification_method="synthetic-classifier",
        source_validity_status="valid")
    coordinate = {"coordinate_unit": "px", "coordinate_frame": "synthetic", "time_unit": "s",
        "time_confirmed": True, "coordinate_representation": "point", "source_time_precision_s": .1,
        "clock_source": "synthetic"}
    dataset = NormalizedDataset(signals, {"coordinate_metadata": coordinate, "time_confirmed": True},
        aois=aois, source_manifest=config["provenance"]["source_manifest"])
    individual = {key: value for key, value in config.items() if key not in {"events", "reading_annotations", "provenance", "run_id"}}
    individual["metric_ids"] = sorted(NEW_IDS)
    settings = AnalysisConfig(run_id="common-integration", eye_selection=EyeSelectionConfig(mode="legacy_source"),
        event_detection=EventDetectionConfig(fixation_method="external_only", saccade_method="external_only"),
        individual=individual, context={"external_events": external, "reading_annotations": config["reading_annotations"]},
        run_group=False, run_advanced=False)
    result = run_analysis(dataset, settings)
    if result.metadata.get("execution_status") == "failed":
        raise AssertionError("common metric pipeline failed: "+repr(result.logs))
    metrics = result.tables["metrics"]
    check("pipeline_dispatch_retains_all_new_ids", set(metrics["metric_id"]) == NEW_IDS)
    target = metrics.loc[metrics["metric_id"].eq("individual.aoi.fixation_ttff")]
    target = target.loc[target["parameter_ref"].map(lambda p: json.loads(p).get("aoi_id") == "B")]
    check("pipeline_ttff_matches_hand_oracle", len(target) == 1 and math.isclose(float(target.iloc[0]["value"]), 1.))
    check("pipeline_stream_provenance", metrics["analysis_stream_id"].eq("gaze:legacy_source").all())
    payload, schema = dataframe_to_csv_member(metrics)
    check("typed_csv_metrics_roundtrip", dataframe_from_csv_member(payload, schema)["parameter_ref"].equals(metrics["parameter_ref"]))
    for mode in ("full", "results_only"):
        archive = workspace / ("common-"+mode+".pyea")
        save_analysis(archive, result, dataset=dataset if mode == "full" else None, config=settings, mode=mode)
        loaded = load_analysis(archive)
        check(mode+"_archive_metric_count", len(loaded.result.tables["metrics"]) == len(metrics))
        check(mode+"_archive_provenance_roundtrip", loaded.result.tables["metrics"]["parameter_ref"].equals(metrics["parameter_ref"]))
        check(mode+"_archive_exposure_audit", len(loaded.result.tables["individual_aoi_exposures"]) == 5)
        check(mode+"_archive_mode_preserved", (loaded.dataset is not None) == (mode == "full"))
    return {"pipeline_status": result.metadata["execution_status"], "metric_rows": len(metrics),
            "typed_csv": True, "archive_modes": ["full", "results_only"], "device_verified": False}


if __name__ == "__main__":
    print(json.dumps(run_common_metric_checks(), ensure_ascii=False, indent=2))
