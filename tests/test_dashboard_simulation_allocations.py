"""Allocation rows from an attested simulation run are shown as synthetic run results."""
import json
from pathlib import Path

from dashboard.app import SIMULATION_ALLOCATION_STATUS, _load_team_allocations

HEADER = "month,resource,ds_division,round,winning_bid,method\n"
ROWS = ("10,RO_unit,Talawa,1,6738663.8,contract_net\n"
        "10,RO_unit,Galnewa,1,3291373.2,equal_split\n"
        "10,RO_unit,Talawa,1,6738663.8,first_come\n")
RUNNER_EVIDENCE = {
    "path": "logs/allocations.csv",
    "baselines_supplied": ["equal_split", "first_come"],
    "team_output_attestation": True,
    "attestation_scope": "rows were produced by this run from synthetic input; "
                         "not real-world or scientific validation",
}


def _run_dir(tmp_path: Path, evidence=None) -> Path:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "allocations.csv").write_text(HEADER + ROWS, encoding="utf-8")
    if evidence is not None:
        (tmp_path / "run_manifest.json").write_text(
            json.dumps({"allocation_evidence": evidence}), encoding="utf-8")
    return logs / "allocations.csv"


def test_attested_simulation_run_rows_are_available_as_synthetic_results(tmp_path):
    rows, status = _load_team_allocations(_run_dir(tmp_path, RUNNER_EVIDENCE))
    assert len(rows) == 3
    assert status == SIMULATION_ALLOCATION_STATUS
    assert "synthetic" in status and "not real-world" in status


def test_file_without_a_run_manifest_stays_unverified(tmp_path):
    _rows, status = _load_team_allocations(_run_dir(tmp_path))
    assert status.startswith("Present but unverified")


def test_manifest_without_attestation_or_baselines_stays_unverified(tmp_path):
    evidence = {**RUNNER_EVIDENCE, "team_output_attestation": False}
    _rows, status = _load_team_allocations(_run_dir(tmp_path, evidence))
    assert status.startswith("Present but unverified")


def test_manifest_missing_baselines_stays_unverified(tmp_path):
    evidence = {**RUNNER_EVIDENCE, "baselines_supplied": []}
    _rows, status = _load_team_allocations(_run_dir(tmp_path, evidence))
    assert status.startswith("Present but unverified")


def test_windows_style_manifest_path_is_accepted(tmp_path):
    evidence = {**RUNNER_EVIDENCE, "path": r"logs\allocations.csv"}
    _rows, status = _load_team_allocations(_run_dir(tmp_path, evidence))
    assert status == SIMULATION_ALLOCATION_STATUS
