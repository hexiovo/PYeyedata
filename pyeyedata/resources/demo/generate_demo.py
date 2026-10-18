"""Build deterministic, visibly synthetic A-D input fixtures for software-path checks."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import random
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pyeyedata.contracts import CONTRACT_VERSION


ROOT = Path(__file__).resolve().parent
RESOURCE_VERSION = "c03-resources/v3"
DEMO_RECIPE_VERSION = "synthetic-eye-fixtures/v1"
AOI_DOMAIN_SCHEMA_VERSION = "2.0.0"
AOI_GEOMETRY_METHOD_VERSION = "static-aoi-geometry-2.0.0"
AOI_BOUNDARY_POLICY = "closed"
SEED = 20261014
INTERACTION = "SYNTHETIC_ONLY_B_INTERACTION_01"
SHARED_CLOCK = "SYNTHETIC_ONLY_B_GENERATED_CLOCK"
STIMULUS = "SYNTHETIC_ONLY_STIMULUS_01"
AOI_FIELDS = [
    "aoi_id", "stimulus_id", "shape", "aoi_name", "xmin", "ymin", "xmax", "ymax",
    "vertices", "unit", "coordinate_frame", "overlap_policy", "priority",
]
AOI_ROWS_A = [
    {"aoi_id": "SYNTHETIC_ONLY_A_ROI_LEFT", "stimulus_id": STIMULUS, "shape": "rectangle",
     "aoi_name": "左侧注视区", "xmin": 400, "ymin": 200, "xmax": 500, "ymax": 350,
     "vertices": "", "unit": "px", "coordinate_frame": "synthetic_top_left",
     "overlap_policy": "exclusive", "priority": 1},
    {"aoi_id": "SYNTHETIC_ONLY_A_ROI_RIGHT", "stimulus_id": STIMULUS, "shape": "rectangle",
     "aoi_name": "右侧注视区", "xmin": 500, "ymin": 200, "xmax": 600, "ymax": 350,
     "vertices": "", "unit": "px", "coordinate_frame": "synthetic_top_left",
     "overlap_policy": "exclusive", "priority": 2},
]
AOI_ROWS_B = [
    {"aoi_id": "SYNTHETIC_ONLY_B_ROI_LEFT", "stimulus_id": STIMULUS, "shape": "rectangle",
     "aoi_name": "共享互动左区", "xmin": 0, "ymin": 0, "xmax": 50, "ymax": 100,
     "vertices": "", "unit": "px", "coordinate_frame": "synthetic_top_left",
     "overlap_policy": "exclusive", "priority": 1},
    {"aoi_id": "SYNTHETIC_ONLY_B_ROI_RIGHT", "stimulus_id": STIMULUS, "shape": "rectangle",
     "aoi_name": "共享互动右区", "xmin": 50, "ymin": 0, "xmax": 100, "ymax": 100,
     "vertices": "", "unit": "px", "coordinate_frame": "synthetic_top_left",
     "overlap_policy": "exclusive", "priority": 2},
]
AOI_CONTRACT = {
    "domain_schema_version": AOI_DOMAIN_SCHEMA_VERSION,
    "geometry_method_version": AOI_GEOMETRY_METHOD_VERSION,
    "boundary_policy": AOI_BOUNDARY_POLICY,
    "validation_status": "Resource-owner checks passed for public AOI normalization and static geometry audit on bundled examples; independent C05 acceptance is not claimed.",
}


def f(value: float, digits: int = 6) -> str:
    return f"{value:.{digits}f}"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_csv(filename: str, fields: list[str], rows: list[dict[str, object]]) -> dict[str, object]:
    path = ROOT / filename
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="raise", lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)
    return {
        "path": filename,
        "format": "csv",
        "sha256": sha256(path),
        "byte_size": path.stat().st_size,
        "data_rows": len(rows),
    }


def write_aoi_demo_files() -> list[dict[str, object]]:
    """Refresh only the two AOI inputs without regenerating unrelated demos."""
    return [
        write_csv("SYNTHETIC_ONLY_A_aois.csv", AOI_FIELDS, AOI_ROWS_A),
        write_csv("SYNTHETIC_ONLY_B_shared_interaction_aois.csv", AOI_FIELDS, AOI_ROWS_B),
    ]


def refresh_aoi_manifest_only(file_records: list[dict[str, object]]) -> None:
    path = ROOT / "demo-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["resource_version"] = RESOURCE_VERSION
    manifest["generator_sha256"] = sha256(Path(__file__).resolve())
    manifest["aoi_contract"] = AOI_CONTRACT
    for scenario in ("A", "B"):
        manifest["scenarios"][scenario]["aoi_domain_schema_version"] = AOI_DOMAIN_SCHEMA_VERSION
        manifest["scenarios"][scenario]["aoi_geometry_method_version"] = AOI_GEOMETRY_METHOD_VERSION
        manifest["scenarios"][scenario]["aoi_boundary_policy"] = AOI_BOUNDARY_POLICY
    replacements = {str(item["path"]): item for item in file_records}
    manifest["files"] = [replacements.get(str(item["path"]), item) for item in manifest["files"]]
    missing = set(replacements).difference(str(item["path"]) for item in manifest["files"])
    if missing:
        raise ValueError(f"AOI manifest entries are missing: {sorted(missing)}")
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def base_signal(
    subject: str,
    session: str,
    t: float,
    x: float,
    y: float,
    *,
    condition: str = "SYNTHETIC_ONLY_condition",
    trial: int = 1,
    stimulus: str = STIMULUS,
    interaction: str | None = None,
    timebase: str | None = None,
    pupil_left: float | None = None,
    pupil_right: float | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "subject_id": subject,
        "session_id": session,
        "time_s": f(t),
        "gaze_x": f(x),
        "gaze_y": f(y),
        "gaze_valid": "1",
        "stimulus_id": stimulus,
        "condition": condition,
        "trial": trial,
    }
    if interaction is not None:
        row["interaction_id"] = interaction
    if timebase is not None:
        row["timebase_id"] = timebase
    if pupil_left is not None:
        row["pupil_left"] = f(pupil_left, 4)
        row["pupil_left_valid"] = "1"
    if pupil_right is not None:
        row["pupil_right"] = f(pupil_right, 4)
        row["pupil_right_valid"] = "1"
    return row


def main() -> None:
    if sys.argv[1:] == ["--aois-only"]:
        refresh_aoi_manifest_only(write_aoi_demo_files())
        return

    rng = random.Random(SEED)
    files: list[dict[str, object]] = []

    signal_base_fields = [
        "subject_id", "session_id", "time_s", "gaze_x", "gaze_y", "gaze_valid",
        "stimulus_id", "condition", "trial",
    ]

    # A: low-rate XY stream with explicit identity/time, no manufactured events.
    a_subject = "SYNTHETIC_ONLY_A_P01"
    a_session = "SYNTHETIC_ONLY_A_S01"
    a_signals = []
    for index in range(21):
        t = index / 10.0
        a_signals.append(base_signal(
            a_subject, a_session, t,
            480 + 35 * math.sin(2 * math.pi * t / 2.0),
            270 + 22 * math.cos(2 * math.pi * t / 2.0),
            condition="SYNTHETIC_ONLY_A_baseline",
        ))
    a_segment = [{
        "subject_id": a_subject, "session_id": a_session,
        "segment_id": "SYNTHETIC_ONLY_A_SEG01", "segment_name": "合成演示区间",
        "start_s": "0.000000", "end_s": "2.100000",
        "condition": "SYNTHETIC_ONLY_A_baseline", "trial": 1, "stimulus_id": STIMULUS,
    }]
    a_aois = AOI_ROWS_A
    files.append(write_csv("SYNTHETIC_ONLY_A_low_rate_signals.csv", signal_base_fields, a_signals))
    files.append(write_csv("SYNTHETIC_ONLY_A_segments.csv", [
        "subject_id", "session_id", "segment_id", "segment_name", "start_s", "end_s",
        "condition", "trial", "stimulus_id",
    ], a_segment))
    files.append(write_csv("SYNTHETIC_ONLY_A_aois.csv", AOI_FIELDS, a_aois))

    # B: a generated common clock and shifted low-rate trajectories for four members.
    member_ids = [f"SYNTHETIC_ONLY_B_P{number:02d}" for number in range(1, 5)]
    session_ids = [f"SYNTHETIC_ONLY_B_S{number:02d}" for number in range(1, 5)]
    shifts = [0.0, 0.25, -0.25, 0.50]
    b_signals = []
    for subject, session, shift in zip(member_ids, session_ids, shifts):
        for index in range(49):
            t = index / 4.0
            shifted = t - shift
            jitter = rng.uniform(-0.015, 0.015)
            x = 50 + 24 * math.sin(2 * math.pi * shifted / 4.0) + jitter
            y = 42 + 18 * math.cos(2 * math.pi * shifted / 5.0) - jitter
            b_signals.append(base_signal(
                subject, session, t, x, y,
                condition="SYNTHETIC_ONLY_B_shared_task", trial=1,
                interaction=INTERACTION, timebase=SHARED_CLOCK,
            ))
    b_segments = [{
        "subject_id": "", "session_id": "", "interaction_id": INTERACTION,
        "segment_id": "SYNTHETIC_ONLY_B_SHARED_SEG01", "segment_name": "合成共享互动窗口",
        "start_s": "0.000000", "end_s": "12.000000",
        "condition": "SYNTHETIC_ONLY_B_shared_task", "trial": 1, "stimulus_id": STIMULUS,
        "timebase_id": SHARED_CLOCK,
    }]
    b_aois = AOI_ROWS_B
    b_shift_reference = [
        {"pair_id": "SYNTHETIC_ONLY_B_P01__P02", "member_a": member_ids[0], "member_b": member_ids[1],
         "construction_shift_s": "0.250000", "construction_rule": "x_B(t)=base_x(t-0.25s)",
         "meaning": "member_b is generated as a delayed copy; this is not an app-estimated lag"},
        {"pair_id": "SYNTHETIC_ONLY_B_P01__P03", "member_a": member_ids[0], "member_b": member_ids[2],
         "construction_shift_s": "-0.250000", "construction_rule": "x_B(t)=base_x(t+0.25s)",
         "meaning": "member_b is generated as an advanced copy; this is not an app-estimated lag"},
    ]
    b_signal_fields = signal_base_fields + ["interaction_id", "timebase_id"]
    files.append(write_csv("SYNTHETIC_ONLY_B_shared_interaction_signals.csv", b_signal_fields, b_signals))
    files.append(write_csv("SYNTHETIC_ONLY_B_shared_interaction_segments.csv", [
        "subject_id", "session_id", "interaction_id", "segment_id", "segment_name", "start_s", "end_s",
        "condition", "trial", "stimulus_id", "timebase_id",
    ], b_segments))
    files.append(write_csv("SYNTHETIC_ONLY_B_shared_interaction_aois.csv", AOI_FIELDS, b_aois))
    files.append(write_csv("SYNTHETIC_ONLY_B_pair_shift_reference.csv", [
        "pair_id", "member_a", "member_b", "construction_shift_s", "construction_rule", "meaning",
    ], b_shift_reference))

    # C: repeated within-member conditions with one binary and one continuous performance target.
    c_signals = []
    c_segments = []
    c_performance = []
    c_members = [f"SYNTHETIC_ONLY_C_P{number:02d}" for number in range(1, 5)]
    for member_number, subject in enumerate(c_members, start=1):
        session = f"SYNTHETIC_ONLY_C_S{member_number:02d}"
        for condition_index, condition in enumerate(("SYNTHETIC_ONLY_C_condition_A", "SYNTHETIC_ONLY_C_condition_B")):
            for trial in range(1, 4):
                offset = (condition_index * 3 + trial - 1) * 2.1
                segment_id = f"SYNTHETIC_ONLY_C_SEG_P{member_number:02d}_{condition_index + 1}_{trial}"
                c_segments.append({
                    "subject_id": subject, "session_id": session, "segment_id": segment_id,
                    "segment_name": f"合成条件{condition_index + 1}试次{trial}",
                    "start_s": f(offset), "end_s": f(offset + 2.0),
                    "condition": condition, "trial": trial, "stimulus_id": STIMULUS,
                })
                for index in range(21):
                    local_t = index / 10.0
                    t = offset + local_t
                    jitter = rng.uniform(-0.02, 0.02)
                    c_signals.append(base_signal(
                        subject, session, t,
                        50 + 14 * math.sin(2 * math.pi * local_t / 2.0) + member_number + jitter,
                        45 + 10 * math.cos(2 * math.pi * local_t / 2.0) - jitter,
                        condition=condition, trial=trial,
                    ))
                accuracy = int((member_number + condition_index + trial) % 3 != 0)
                rt = 0.42 + member_number * 0.025 + condition_index * 0.035 + trial * 0.01 + rng.uniform(-0.004, 0.004)
                c_performance.append({
                    "subject_id": subject, "session_id": session, "condition": condition,
                    "trial": trial, "accuracy": accuracy, "reaction_time_s": f(rt, 4),
                })
    files.append(write_csv("SYNTHETIC_ONLY_C_repeated_conditions_signals.csv", signal_base_fields, c_signals))
    files.append(write_csv("SYNTHETIC_ONLY_C_repeated_conditions_segments.csv", [
        "subject_id", "session_id", "segment_id", "segment_name", "start_s", "end_s",
        "condition", "trial", "stimulus_id",
    ], c_segments))
    files.append(write_csv("SYNTHETIC_ONLY_C_performance.csv", [
        "subject_id", "session_id", "condition", "trial", "accuracy", "reaction_time_s",
    ], c_performance))
    files.append(write_csv("SYNTHETIC_ONLY_C_performance_type_reference.csv", [
        "column", "declared_demo_type", "unit", "interpretation_limit",
    ], [
        {"column": "accuracy", "declared_demo_type": "binary", "unit": "0/1", "interpretation_limit": "do not force continuous correlation; method support must be explicit"},
        {"column": "reaction_time_s", "declared_demo_type": "continuous", "unit": "s", "interpretation_limit": "use only with explicit one-to-one join keys and an eligible method"},
    ]))

    # D: synthetic angular XY with separate raw pupil channels; workflow outcomes are not precomputed.
    d_subject = "SYNTHETIC_ONLY_D_P01"
    d_session = "SYNTHETIC_ONLY_D_S01"
    d_signals = []
    for index in range(121):
        t = index / 20.0
        # Holds with two one-sample synthetic jumps make threshold changes visible in a demo input.
        position = 0.0
        if t >= 2.0:
            position += 4.0
        if t >= 4.0:
            position += 4.0
        pupil_base = 3.0 + 0.04 * math.sin(2 * math.pi * t / 5.0)
        d_signals.append(base_signal(
            d_subject, d_session, t, position, 0.2 * math.sin(2 * math.pi * t / 6.0),
            condition="SYNTHETIC_ONLY_D_threshold_case", trial=1,
            pupil_left=pupil_base,
            pupil_right=pupil_base + 0.03 * math.cos(2 * math.pi * t / 4.0),
        ))
    d_segments = [{
        "subject_id": d_subject, "session_id": d_session,
        "segment_id": "SYNTHETIC_ONLY_D_FULL_WINDOW", "segment_name": "合成阈值演示窗口",
        "start_s": "0.000000", "end_s": "6.000000",
        "condition": "SYNTHETIC_ONLY_D_threshold_case", "trial": 1, "stimulus_id": STIMULUS,
    }]
    d_reference = [
        {"setting": "fixation_method", "candidate_value_a": "ivt", "candidate_value_b": "ivt", "unit": "enum", "note": "candidate method names only; actual UI/config support must be checked"},
        {"setting": "velocity_threshold_deg_s", "candidate_value_a": 30, "candidate_value_b": 100, "unit": "deg/s", "note": "synthetic angular coordinates; this parameter change is a workflow recipe, not a measured outcome"},
        {"setting": "high_velocity_deg_s", "candidate_value_a": 150, "candidate_value_b": 150, "unit": "deg/s", "note": "kept above both candidate fixation thresholds; no event result is claimed"},
        {"setting": "raw_pupil_inputs", "candidate_value_a": "same fixture bytes", "candidate_value_b": "same fixture bytes", "unit": "source data", "note": "input-layer identity only; downstream recomputation/reuse remains NOT_RUN"},
    ]
    d_signal_fields = signal_base_fields + ["pupil_left", "pupil_left_valid", "pupil_right", "pupil_right_valid"]
    files.append(write_csv("SYNTHETIC_ONLY_D_threshold_pupil_signals.csv", d_signal_fields, d_signals))
    files.append(write_csv("SYNTHETIC_ONLY_D_segments.csv", [
        "subject_id", "session_id", "segment_id", "segment_name", "start_s", "end_s",
        "condition", "trial", "stimulus_id",
    ], d_segments))
    files.append(write_csv("SYNTHETIC_ONLY_D_threshold_change_reference.csv", [
        "setting", "candidate_value_a", "candidate_value_b", "unit", "note",
    ], d_reference))

    manifest = {
        "schema": "pyeyedata.synthetic-demo-manifest/v1",
        "label": "合成演示数据 / SYNTHETIC_ONLY",
        "resource_version": RESOURCE_VERSION,
        "recipe_version": DEMO_RECIPE_VERSION,
        "seed": SEED,
        "contract_version": CONTRACT_VERSION,
        "aoi_contract": AOI_CONTRACT,
        "generator": "generate_demo.py",
        "generator_sha256": sha256(Path(__file__).resolve()),
        "determinism": "Python random.Random(seed), analytic trajectories, fixed field order, UTF-8-SIG CSV, CRLF line endings",
        "scenarios": {
            "A": {
                "title": "低采样率 XY 与矩形 AOI / Low-rate XY and rectangle AOIs",
                "sampling_hz": 10,
                "rows": len(a_signals),
                "identity": {"subject_id": a_subject, "session_id": a_session},
                "input_roles": ["signals", "segments", "aois"],
                "limits": ["Synthetic software fixture only; no event, participant, device, or physiology claim."],
            },
            "B": {
                "title": "四成员共享互动、六对参考与 AOI / Four-member shared interaction and AOIs",
                "sampling_hz": 4,
                "members": [{"subject_id": s, "session_id": r} for s, r in zip(member_ids, session_ids)],
                "possible_pairs": 6,
                "interaction_id": INTERACTION,
                "generated_common_timebase": SHARED_CLOCK,
                "member_shifts_s": dict(zip(member_ids, shifts)),
                "pair_shift_reference": "SYNTHETIC_ONLY_B_pair_shift_reference.csv",
                "segment_scope_setup": {
                    "mode": "shared_interaction",
                    "synchronized": True,
                    "timebase_id": SHARED_CLOCK,
                    "member_session_map": [{"subject_id": s, "session_id": r, "interaction_id": INTERACTION, "timebase_id": SHARED_CLOCK} for s, r in zip(member_ids, session_ids)],
                    "meaning": "generated simulation clock only; not hardware synchronization or device verification",
                },
                "limits": ["Construction shifts are known inputs, not app-estimated lags or scientific validation."],
            },
            "C": {
                "title": "重复条件及二元准确率/连续反应时 / Repeated conditions with binary accuracy and continuous RT",
                "subjects": c_members,
                "conditions": ["SYNTHETIC_ONLY_C_condition_A", "SYNTHETIC_ONLY_C_condition_B"],
                "trials_per_subject_condition": 3,
                "performance_columns": {"accuracy": {"type": "binary", "unit": "0/1"}, "reaction_time_s": {"type": "continuous", "unit": "s"}},
                "candidate_join_keys": ["subject_id", "session_id", "condition", "trial"],
                "performance_reference": "SYNTHETIC_ONLY_C_performance_type_reference.csv",
                "limits": ["No statistical result is precomputed; method eligibility, sample support, and inference remain for the real app to determine."],
            },
            "D": {
                "title": "阈值更改、原始瞳孔分支与取消/旧历史操作参考 / Threshold workflow, raw pupil channels, and cancel/history reference",
                "sampling_hz": 20,
                "identity": {"subject_id": d_subject, "session_id": d_session},
                "candidate_event_settings": {"fixation_method": "ivt", "velocity_threshold_deg_s": [30, 100], "high_velocity_deg_s": 150},
                "reference_table": "SYNTHETIC_ONLY_D_threshold_change_reference.csv",
                "workflow_cases": ["change threshold and inspect actual dependency recomputation", "cancel an in-progress run", "retain prior run/history", "check raw pupil input identity across threshold-only change"],
                "limits": ["Fixture contains synthetic angular coordinates and independent raw pupil columns; workflow, event output, cache reuse, cancellation, histories, device validation, and physiology are NOT_RUN."],
            },
        },
        "files": files,
        "device_verified": False,
        "device_validation_status": "NOT_RUN",
        "physiology_validation_status": "NOT_RUN",
        "guide_screenshots": "Three C01 themed-UI captures are referenced in the PDF guide as control-layout illustrations only; visible test rows are not analysis evidence or full-workflow acceptance.",
    }
    (ROOT / "demo-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
