"""Dependency-light tests for isolated local-demo orchestration outputs."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
import contextlib
import io
from pathlib import Path

from src.sim.run_local_demo import (
    ALERT_COLUMNS,
    HOTSPOT_COLUMNS,
    RISK_COLUMNS,
    WELL_COLUMNS,
    run_demo,
    ROOT as PROJECT_ROOT,
)
from src.core.local_allocation import DECISION_COLUMNS
from src.core.msg_log import COLUMNS as MESSAGE_COLUMNS

ROOT = Path(__file__).resolve().parents[1]


class LocalDemoRunnerTests(unittest.TestCase):
    def test_missing_inputs_create_unavailable_stage_in_isolated_root(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "demo"
            manifest = run_demo(
                output_dir=output,
                input_path=Path(temp) / "missing-input.csv",
                method_path=Path(temp) / "missing-method.csv",
                divisions_path=ROOT / "data/raw/ckdu_03_sampling_design_ds_divisions.csv",
                months=1,
            )
            self.assertTrue(manifest["stages"]["risk_scoring"].startswith("unavailable"))
            self.assertTrue((output / "provenance.json").is_file())
            self.assertTrue((output / "division_points.csv").is_file())
            self.assertFalse((output / "logs/allocations.csv").exists())
            self.assertFalse((output / "logs/messages.csv").resolve().is_relative_to(
                Path(__file__).resolve().parents[1] / "logs"))
            with (output / "logs/risk_scores.csv").open(newline="", encoding="utf-8") as handle:
                self.assertEqual(tuple(next(csv.reader(handle))), RISK_COLUMNS)
            with (output / "logs/well_scores.csv").open(newline="", encoding="utf-8") as handle:
                self.assertEqual(tuple(next(csv.reader(handle))), WELL_COLUMNS)
            with (output / "logs/messages.csv").open(newline="", encoding="utf-8") as handle:
                self.assertEqual(tuple(next(csv.reader(handle))), tuple(MESSAGE_COLUMNS))
            with (output / "hotspot_screening.csv").open(newline="", encoding="utf-8") as handle:
                self.assertEqual(tuple(next(csv.reader(handle))), HOTSPOT_COLUMNS)
            with (output / "allocation_decisions.csv").open(newline="", encoding="utf-8") as handle:
                self.assertEqual(tuple(next(csv.reader(handle))), DECISION_COLUMNS)
            with (output / "alerts.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(tuple(rows[0].keys()), ALERT_COLUMNS)
            self.assertEqual(rows[0]["status"], "unavailable")

    def test_output_root_must_be_empty_and_cannot_be_production_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "not-empty"
            root.mkdir()
            (root / "keep.txt").write_text("preserve", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                run_demo(output_dir=root, months=1)
            self.assertEqual((root / "keep.txt").read_text(encoding="utf-8"), "preserve")
        with self.assertRaisesRegex(ValueError, "protected path"):
            run_demo(output_dir=PROJECT_ROOT / "logs", months=1)

    def test_manifest_marks_outputs_illustrative_and_metric_evidence_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "demo"
            run_demo(output_dir=output, input_path=Path(temp) / "missing.csv",
                     method_path=Path(temp) / "missing-method.csv",
                     divisions_path=ROOT / "data/raw/ckdu_03_sampling_design_ds_divisions.csv", months=1)
            payload = json.loads((output / "provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["provenance"], "illustrative_local_demo")
        self.assertIn("not directly comparable", payload["scoring"]["limitation"])
        self.assertIn("not Member C code", payload["policies"]["allocation"]["implementation"])
        self.assertIn("unavailable", payload["allocation_metric_results"])

    def test_repository_synthetic_input_uses_available_pipeline_or_reports_dependency_gap(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            output = Path(temp) / "demo"
            manifest = run_demo(output_dir=output, months=1)
            self.assertEqual(manifest["synthetic_input"]["rows"], 440)
            self.assertEqual(manifest["synthetic_input"]["preflight_status"], "available")
            scoring_status = manifest["stages"]["risk_scoring"]
            self.assertTrue(scoring_status.startswith(("available", "partial", "unavailable")))
            self.assertTrue((output / "division_points.csv").is_file())
            with (output / "allocation_decisions.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(rows)
            self.assertTrue(all(row["provenance"] == "illustrative_local_demo" for row in rows))
            if scoring_status.startswith("unavailable"):
                self.assertTrue(all(row["status"] == "unavailable" for row in rows))
            else:
                with (output / "logs/risk_scores.csv").open(newline="", encoding="utf-8") as handle:
                    risk_rows = list(csv.DictReader(handle))
                self.assertTrue(risk_rows)


if __name__ == "__main__":
    unittest.main()
