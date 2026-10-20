"""SPADE subscriber for project-defined screening of typed village risk records."""
from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from spade.agent import Agent
from spade.behaviour import CyclicBehaviour, OneShotBehaviour

from src.agents import common
from src.core import ontology
from src.core.hotspot_screening import ScreeningResult, ScreeningRule, screen_risk_history


class HotspotAgent(Agent):
    """Receive scorer fan-out independently of villages and screen per division/month.

    Duplicate records and gaps are passed to the existing pure helper, which returns an
    explicit unavailable result. Out-of-order records are rejected and likewise produce
    unavailable results. No missing or Unknown value is converted to an inactive screen.
    """

    def __init__(
        self,
        jid: str,
        password: str,
        *,
        divisions: Sequence[str],
        rule: ScreeningRule | None,
        output_path: str | Path,
        result_recipient_jid: str | None = None,
        receive_timeout_s: float = 1.0,
        **kwargs,
    ):
        super().__init__(jid, password, **kwargs)
        if not divisions or any(not isinstance(value, str) or not value.strip() for value in divisions):
            raise ValueError("HotspotAgent requires non-empty division names")
        if len(set(divisions)) != len(divisions):
            raise ValueError("HotspotAgent division names must be unique")
        if (isinstance(receive_timeout_s, bool) or not isinstance(receive_timeout_s, (int, float))
                or not math.isfinite(receive_timeout_s) or receive_timeout_s <= 0):
            raise ValueError("receive_timeout_s must be positive and finite")
        self.divisions = tuple(divisions)
        self.rule = rule
        self.output_path = Path(output_path)
        self.result_recipient_jid = result_recipient_jid
        self.receive_timeout_s = float(receive_timeout_s)
        self.risk_history: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.last_month_by_division: dict[str, int] = {}
        self.results_by_key: dict[tuple[str, int], ScreeningResult] = {}
        self.rejected_records: list[dict[str, Any]] = []
        self.finalized_month_indexes: set[int] = set()
        self.thread_by_key: dict[tuple[str, int], str | None] = {}
        self.published_keys: set[tuple[str, int]] = set()

    def process_risk_record(self, body: Mapping[str, Any], thread_id: str | None = None) -> ScreeningResult:
        """Validate and screen one decoded risk body; exposed for deterministic unit tests."""
        raw_division = body.get("ds_division") if isinstance(body, Mapping) else None
        division = raw_division if isinstance(raw_division, str) else ""
        raw_index = body.get("month_index") if isinstance(body, Mapping) else None
        month_index = (raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool)
                       and raw_index >= 0 else -1)
        try:
            if not isinstance(body, Mapping):
                raise ontology.SchemaError("risk record must be a mapping")
            record = dict(body)
            ontology.validate(ontology.INFORM, record)
            if record["type"] != "risk_record":
                raise ontology.SchemaError("expected risk_record message")
            if record["ds_division"] not in self.divisions:
                raise ontology.SchemaError(f"unexpected division {record['ds_division']!r}")
        except (ontology.SchemaError, TypeError, ValueError) as exc:
            result = ScreeningResult("unavailable", division, month_index,
                                     self.rule.rule_id if self.rule else None,
                                     reason=f"invalid_risk_record:{exc}")
            self.rejected_records.append({"ds_division": division or None,
                                          "month_index": month_index,
                                          "reason": result.reason})
            if division and month_index >= 0:
                self.results_by_key[(division, month_index)] = result
            return result

        division = record["ds_division"]
        month_index = record["month_index"]
        key = (division, month_index)
        if thread_id is not None:
            previous_thread = self.thread_by_key.get(key)
            if previous_thread is None:
                self.thread_by_key[key] = thread_id
            elif previous_thread != thread_id:
                result = ScreeningResult("unavailable", division, month_index,
                                         self.rule.rule_id if self.rule else None,
                                         reason="conflicting_risk_correlation_threads")
                self.rejected_records.append({"ds_division": division, "month_index": month_index,
                                              "reason": result.reason})
                self.results_by_key[key] = result
                return result
        if month_index in self.finalized_month_indexes:
            result = ScreeningResult("unavailable", division, month_index,
                                     self.rule.rule_id if self.rule else None,
                                     reason="risk_record_arrived_after_month_finalized")
            self.rejected_records.append({"ds_division": division, "month_index": month_index,
                                          "reason": result.reason})
            self.results_by_key[key] = result
            return result

        previous = self.last_month_by_division.get(division)
        existing = [row for row in self.risk_history[division]
                    if row["month_index"] == month_index]
        if existing:
            # Keep the duplicate in history so the established helper enforces its duplicate rule.
            self.risk_history[division].append(record)
            result = self._screen(division, month_index)
        elif previous is not None and month_index < previous:
            result = ScreeningResult("unavailable", division, month_index,
                                     self.rule.rule_id if self.rule else None,
                                     reason=f"out_of_order_month_index:{month_index}_after_{previous}")
            self.rejected_records.append({"ds_division": division, "month_index": month_index,
                                          "reason": result.reason})
        else:
            self.risk_history[division].append(record)
            self.last_month_by_division[division] = month_index
            result = self._screen(division, month_index)
        self.results_by_key[key] = result
        return result

    def _screen(self, division: str, month_index: int) -> ScreeningResult:
        # Scope history to the helper's consecutive window so an old duplicate does not poison
        # otherwise complete, later windows; duplicates in the current window remain visible.
        if self.rule is None:
            relevant = self.risk_history[division]
        else:
            first = month_index - self.rule.consecutive_months + 1
            relevant = [record for record in self.risk_history[division]
                        if first <= record["month_index"] <= month_index]
        return screen_risk_history(relevant, ds_division=division,
                                   month_index=month_index, rule=self.rule)

    def finalize_month(self, month_index: int) -> list[ScreeningResult]:
        """Seal a month, making absent division records explicit unavailable outputs."""
        if isinstance(month_index, bool) or not isinstance(month_index, int) or month_index < 0:
            raise ValueError("month_index must be a non-negative integer")
        self.finalized_month_indexes.add(month_index)
        finalized = []
        for division in self.divisions:
            key = (division, month_index)
            if key not in self.results_by_key:
                result = self._screen(division, month_index)
                if not any(row["month_index"] == month_index
                           for row in self.risk_history[division]):
                    result = ScreeningResult(
                        "unavailable", division, month_index,
                        self.rule.rule_id if self.rule else None,
                        reason="risk_record_missing_for_month",
                    )
                self.results_by_key[key] = result
            finalized.append(self.results_by_key[key])
        return finalized

    def write_output(self) -> None:
        """Persist results once to the configured run-local path; never overwrite."""
        payload = [
            {"provenance": "project_defined_screening_not_validated_model",
             "component": "SPADE HotspotAgent",
             "risk_record_received": (
                 any(row["month_index"] == key[1] for row in self.risk_history.get(key[0], []))
                 or any(row["ds_division"] == key[0] and row["month_index"] == key[1]
                        for row in self.rejected_records)
             ),
             **result.__dict__}
            for key, result in sorted(self.results_by_key.items(), key=lambda item: (item[0][1], item[0][0]))
        ]
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")

    async def publish_month_results(self, month_index: int) -> None:
        """Publish typed per-division results once to the configured alert processor."""
        if self.result_recipient_jid is None:
            return
        for result in self.finalize_month(month_index):
            key = (result.ds_division, result.month_index)
            if key in self.published_keys:
                continue
            body = {"type": "hotspot_result", "month_index": result.month_index,
                    "ds_division": result.ds_division, "status": result.status,
                    "rule_id": result.rule_id,
                    "evidence_month_indexes": list(result.evidence_month_indexes),
                    "reason": result.reason}
            ontology.validate(ontology.INFORM, body)
            thread = self.thread_by_key.get(key) or f"readings-{month_index}"
            agent = self
            class Publish(OneShotBehaviour):
                async def run(self):
                    await common.send(self, agent.result_recipient_jid, ontology.INFORM, body, thread)
            behaviour = Publish()
            self.add_behaviour(behaviour)
            await behaviour.join()
            self.published_keys.add(key)

    class ReceiveRiskRecords(CyclicBehaviour):
        async def run(self):
            agent: HotspotAgent = self.agent
            got = await common.receive(self, timeout=agent.receive_timeout_s)
            if got is None:
                return
            msg, performative, body = got
            if performative == ontology.INFORM and body.get("type") == "risk_record":
                agent.process_risk_record(body, msg.thread)

    async def setup(self):
        self.add_behaviour(self.ReceiveRiskRecords())
