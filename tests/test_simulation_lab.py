"""Simulation Lab validation and process-control tests; process launches are mocked."""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

import src.sim.simulation_lab as simulation_lab
from src.sim.simulation_lab import (
    SAMPLE_INPUT,
    ROOT,
    SimulationSettings,
    build_runner_command,
    get_run_status,
    launch_simulation,
    list_lab_runs,
    resolve_hotspot_configuration,
    validate_input_bytes,
)


def _settings(**overrides):
    values = dict(
        start_month_index=4, month_count=2, first_calendar_month=11,
        resource="RO_unit", resource_pool=2, round_number=1,
        proposal_timeout_s=1.5, confirmation_timeout_s=2.5, stage_timeout_s=15.0,
    )
    values.update(overrides)
    return SimulationSettings(**values)


class FakeProcess:
    def __init__(self, code=None):
        self.pid = 987654
        self.code = code

    def poll(self):
        return self.code


class SimulationLabTests(unittest.TestCase):
    def setUp(self):
        self.previous_processes = set(simulation_lab._PROCESSES)

    def tearDown(self):
        for run_id in set(simulation_lab._PROCESSES) - self.previous_processes:
            simulation_lab._PROCESSES.pop(run_id, None)

    def test_existing_sample_schema_and_provenance_validate(self):
        validated = validate_input_bytes(SAMPLE_INPUT.read_bytes())
        self.assertGreater(validated.row_count, 0)
        self.assertTrue(validated.preview)
        self.assertEqual(validated.preview[0]["source"], "synthetic")

    def test_upload_rejects_schema_and_real_source_mismatch(self):
        contents = SAMPLE_INPUT.read_bytes()
        with self.assertRaisesRegex(ValueError, "columns must exactly match"):
            validate_input_bytes(contents.replace(b"well_id,", b"extra,well_id,", 1))
        with self.assertRaisesRegex(ValueError, "expected only source='synthetic'"):
            validate_input_bytes(contents.replace(b",synthetic,", b",real,"))

    def test_settings_explicitly_map_indices_to_calendar_month_labels(self):
        config = _settings()
        self.assertEqual(config.end_month_index, 5)
        self.assertEqual(config.calendar_months, (11, 12))
        with self.assertRaisesRegex(ValueError, "Unknown cannot"):
            _settings(hotspot_trigger_tiers=("Unknown",), hotspot_consecutive_months=2)
        with self.assertRaisesRegex(ValueError, "at least 2"):
            _settings(hotspot_trigger_tiers=("Poor",), hotspot_consecutive_months=1)

    def test_hotspot_checkbox_toggle_preserves_selection_but_disabled_state_passes_no_rule(self):
        selected = ("Poor", "Unsuitable")
        self.assertEqual(resolve_hotspot_configuration(True, selected, 2), (selected, 2))
        # Session-state selections may remain after unchecking, but no rule reaches the runner.
        self.assertEqual(resolve_hotspot_configuration(False, selected, 2), ((), None))
        self.assertEqual(resolve_hotspot_configuration(True, selected, 2), (selected, 2))

    def test_hotspot_configuration_requires_explicit_tiers_and_two_months(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            resolve_hotspot_configuration(True, (), 2)
        with self.assertRaisesRegex(ValueError, "at least 2"):
            resolve_hotspot_configuration(True, ("Poor",), 1)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            resolve_hotspot_configuration(True, ("Unknown",), 2)

    def test_selected_hotspot_configuration_reaches_runner_cli_as_repeatable_flags(self):
        tiers, window = resolve_hotspot_configuration(True, ("Poor", "Very Poor", "Unsuitable"), 2)
        settings = _settings(hotspot_trigger_tiers=tiers, hotspot_consecutive_months=window)
        command = build_runner_command(settings, input_path=SAMPLE_INPUT,
                                       output_path=Path("/tmp/isolated-run"), run_id="hotspot-test")
        self.assertEqual(command.count("--hotspot-trigger-tier"), 3)
        self.assertEqual(command[command.index("--hotspot-trigger-tier") + 1], "Poor")
        self.assertIn("Very Poor", command)
        self.assertIn("Unsuitable", command)
        self.assertEqual(command[command.index("--hotspot-consecutive-months") + 1], "2")

    def test_disabled_hotspot_configuration_adds_no_runner_rule_arguments(self):
        tiers, window = resolve_hotspot_configuration(False, ("Poor",), 2)
        settings = _settings(hotspot_trigger_tiers=tiers, hotspot_consecutive_months=window)
        command = build_runner_command(settings, input_path=SAMPLE_INPUT,
                                       output_path=Path("/tmp/isolated-run"), run_id="no-hotspot")
        self.assertNotIn("--hotspot-trigger-tier", command)
        self.assertNotIn("--hotspot-consecutive-months", command)

    def test_upload_launch_uses_selected_file_and_fresh_isolated_run_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "lab"
            process = FakeProcess()

            def popen(command, **_kwargs):
                self.assertIn(str(Path(temp) / "lab" / "inputs"), " ".join(command))
                self.assertIn("--input", command)
                self.assertIn("--output-dir", command)
                self.assertIn("--embedded-server", command)
                return process

            uploaded = SAMPLE_INPUT.read_bytes()
            launched = launch_simulation(root, _settings(), uploaded_csv=uploaded, popen_factory=popen)
            self.assertEqual(launched["status"], "queued")
            run_dir = Path(launched["run_dir"])
            input_path = Path(launched["input_path"])
            self.assertTrue(run_dir.is_relative_to(root / "runs"))
            self.assertEqual(input_path.read_bytes(), uploaded)
            self.assertFalse((root / "logs").exists())
            self.assertEqual(get_run_status(launched["run_id"], root)["status"], "queued")

            run_dir.mkdir(parents=True)
            manifest_path = run_dir / "run_manifest.json"
            manifest_path.write_text(json.dumps({"run_id": launched["run_id"], "status": "running"}),
                                     encoding="utf-8")
            self.assertEqual(get_run_status(launched["run_id"], root)["status"], "running")
            manifest_path.write_text(json.dumps({"run_id": launched["run_id"], "status": "completed_with_unavailable_stages"}),
                                     encoding="utf-8")
            self.assertEqual(get_run_status(launched["run_id"], root)["status"],
                             "completed_with_unavailable_stages")

    def test_only_one_simulation_can_run_and_previous_paths_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "lab"
            existing = root / "runs" / "previous"
            existing.mkdir(parents=True)
            marker = existing / "keep.txt"
            marker.write_text("preserve", encoding="utf-8")
            launched = launch_simulation(root, _settings(), input_path=SAMPLE_INPUT,
                                         popen_factory=lambda *_a, **_k: FakeProcess())
            with self.assertRaisesRegex(RuntimeError, "already queued"):
                launch_simulation(root, _settings(), input_path=SAMPLE_INPUT,
                                  popen_factory=lambda *_a, **_k: FakeProcess())
            self.assertTrue(marker.exists())
            self.assertTrue(Path(launched["run_dir"]).exists() is False)
            self.assertEqual(len(list_lab_runs(root)), 1)

    def test_runner_process_failure_is_persisted_with_clear_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "lab"

            def fail_to_start(*_args, **_kwargs):
                raise OSError("illustrative process launch failure")

            with self.assertRaisesRegex(RuntimeError, "Could not start runner"):
                launch_simulation(root, _settings(), input_path=SAMPLE_INPUT,
                                  popen_factory=fail_to_start)
            rows = list_lab_runs(root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "failed")
            self.assertIn("illustrative process launch failure", rows[0]["error"])

    def test_production_log_root_is_rejected_before_creating_lab_directories(self):
        protected_root = ROOT / "logs" / f"simulation-lab-must-not-create-{uuid.uuid4().hex}"
        self.assertFalse(protected_root.exists())
        with self.assertRaisesRegex(ValueError, "protected path"):
            launch_simulation(protected_root, _settings(), input_path=SAMPLE_INPUT,
                              popen_factory=lambda *_a, **_k: FakeProcess())
        self.assertFalse(protected_root.exists())

    def test_exited_nonzero_process_is_reported_failed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "lab"
            launched = launch_simulation(root, _settings(), input_path=SAMPLE_INPUT,
                                         popen_factory=lambda *_a, **_k: FakeProcess(code=2))
            status = get_run_status(launched["run_id"], root)
            self.assertEqual(status["status"], "failed")
            self.assertIn("code 2", status["error"])

    def test_clean_process_exit_without_completed_manifest_is_not_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "lab"
            launched = launch_simulation(root, _settings(), input_path=SAMPLE_INPUT,
                                         popen_factory=lambda *_a, **_k: FakeProcess(code=0))
            status = get_run_status(launched["run_id"], root)
            self.assertEqual(status["status"], "failed")
            self.assertIn("without a completed run manifest", status["error"])


if __name__ == "__main__":
    unittest.main()
