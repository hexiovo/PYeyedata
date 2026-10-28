"""Run fixed product diagnostics in fresh processes, without driving the desktop."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ("synthetic-pipeline", "reliability-smoke", "usability-smoke")


def run_checks(output: Path, executable: Path | None = None) -> dict:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    results = []
    for scenario in SCENARIOS:
        workspace = output / scenario
        evidence = workspace / "result.json"
        config = output / f"{scenario}.json"
        config.write_text(json.dumps({
            "schema": "pyeyedata.diagnostic-config/v1",
            "scenario": scenario, "workspace": str(workspace),
            "evidence_path": str(evidence), "display": "offscreen",
        }), encoding="utf-8")
        command = ([str(executable.resolve())] if executable else
                   [sys.executable, "-X", "utf8", "-B", str(ROOT / "pyeyedata_launcher.py")])
        print(f"Checking {scenario} ({'frozen' if executable else 'source'})...", flush=True)
        with (output / f"{scenario}.log").open("w", encoding="utf-8") as log:
            process = subprocess.run(
                command + ["--diagnostic-config", str(config)], cwd=output,
                stdout=log, stderr=subprocess.STDOUT, timeout=300,
            )
        if process.returncode or not evidence.is_file():
            raise RuntimeError(f"{scenario} failed (exit {process.returncode}); see {output}")
        record = json.loads(evidence.read_text(encoding="utf-8"))
        if record.get("status") != "pass" or record.get("cleanup_error"):
            raise RuntimeError(f"{scenario} diagnostic failed: {evidence}")
        if record["process"]["frozen"] != (executable is not None):
            raise RuntimeError(f"{scenario} ran in the wrong source/frozen mode")
        origins = record["process"].get("module_origins", {})
        required = {"pyeyedata", "pyeyedata.pipeline", "pyeyedata.gui.main_window", "pyeyedata_bootstrap"}
        if not required.issubset(origins):
            raise RuntimeError(f"{scenario} lacks actual entry-point module origins")
        expected_root = (Path(record["process"]["bundle_root"]) if executable else ROOT).resolve()
        for name, origin in origins.items():
            if not Path(origin).resolve().is_relative_to(expected_root):
                raise RuntimeError(f"{scenario} loaded {name} outside {expected_root}: {origin}")
        results.append({"scenario": scenario, "status": "pass",
                        "application": record["application"], "module_origins": origins})
    summary = {"status": "pass", "mode": "frozen" if executable else "source",
               "display": "offscreen", "scenarios": results}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--exe", type=Path)
    args = parser.parse_args()
    output = args.output or Path(tempfile.gettempdir()) / f"PYeyedata-check-{uuid.uuid4().hex}"
    summary = run_checks(output, args.exe)
    print(json.dumps({
        **{key: value for key, value in summary.items() if key != "scenarios"},
        "scenarios": [{key: value for key, value in item.items() if key != "module_origins"}
                      for item in summary["scenarios"]],
    }, indent=2))
    print(f"Evidence: {output.resolve()}")


if __name__ == "__main__":
    main()
