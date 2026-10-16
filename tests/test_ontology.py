"""Focused ontology and message-log tests; stdlib unittest keeps setup light."""

from __future__ import annotations

import csv
import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from src.core.msg_log import CSV_COLUMNS, log_message, log_received_message
from src.core.ontology import PERFORMATIVES, SCHEMAS, validate


def _write_batch(path: str, worker: int, count: int) -> None:
    for index in range(count):
        log_message(
            f"sender-{worker}",
            "receiver",
            "inform",
            f"thread-{worker}",
            {"index": index},
            path,
        )


class TestOntology(unittest.TestCase):
    def test_declared_performatives_and_message_families_exist(self) -> None:
        self.assertEqual(
            PERFORMATIVES,
            {"cfp", "propose", "refuse", "accept-proposal", "reject-proposal", "inform", "failure"},
        )
        self.assertTrue({"month_tick", "well_reading", "risk_record", "alert"}.issubset(SCHEMAS))

    def test_cfp_accepts_documented_example_fields(self) -> None:
        validate(
            "cfp",
            {
                "thread": "month-1-ro",
                "month": 1,
                "resource": "ro_unit",
                "n_available": 3,
                "round": 1,
                "deadline_s": 2.0,
            },
        )

    def test_missing_and_unexpected_fields_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing fields"):
            validate("cfp", {"thread": "t"})
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            validate(
                "risk_record",
                {
                    "ds_division": "A",
                    "month": 1,
                    "wqi": 90.0,
                    "tier": "Good",
                    "drivers": {},
                    "n_wells": 2,
                    "source": "synthetic",
                    "diagnosis": "not allowed",
                },
            )

    def test_wrong_types_and_invalid_cfp_values_are_rejected(self) -> None:
        body = {
            "thread": "t",
            "month": True,
            "resource": "ro_unit",
            "n_available": 3,
            "round": 1,
            "deadline_s": 1.0,
        }
        with self.assertRaisesRegex(ValueError, "must be int"):
            validate("cfp", body)
        body["month"] = 1
        body["resource"] = "invented_resource"
        with self.assertRaisesRegex(ValueError, "resource must"):
            validate("cfp", body)

    def test_sent_and_received_records_keep_exact_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "messages.csv"
            log_message("a", "b", "cfp", "thread-1", {"month": 1}, path)
            log_received_message("a", "b", "cfp", "thread-1", {"month": 1}, path)
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
                self.assertEqual(tuple(reader.fieldnames or ()), CSV_COLUMNS)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["payload"], '{"month":1}')
            self.assertTrue(rows[0]["timestamp"].endswith("+00:00"))

    def test_multiprocess_appends_are_complete_and_keep_one_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "messages.csv")
            worker_count, rows_per_worker = 4, 15
            processes = [
                multiprocessing.Process(target=_write_batch, args=(path, worker, rows_per_worker))
                for worker in range(worker_count)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
            with open(path, newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
                self.assertEqual(tuple(reader.fieldnames or ()), CSV_COLUMNS)
            self.assertEqual(len(rows), worker_count * rows_per_worker)
            self.assertEqual({row["sender"] for row in rows}, {f"sender-{i}" for i in range(worker_count)})
            self.assertTrue(all(isinstance(json.loads(row["payload"]), dict) for row in rows))


if __name__ == "__main__":
    unittest.main()
