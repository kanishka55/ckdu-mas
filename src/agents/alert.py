"""Risk-indicator alerts with deterministic, replaceable integration seams.

``AlertAgent.process_risk_record`` consumes the risk-record contract published
by Member B and a provisional ``hotspot: bool`` flag. It returns a decision
object and records both emission and suppression decisions through the shared
message logger. The ontology now has explicit issued-alert and suppression
schemas; the ``alert`` to ``inform`` performative mapping and hotspot adapter
remain provisional pending team confirmation. It is not a diagnosis and makes
no causal claim.

No SPADE dependency is required here. A transport can inject a receive callable;
``receive_once`` wraps it in an explicit timeout.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from src.core.msg_log import log_message
from src.core.ontology import MESSAGE_PERFORMATIVES, validate, validate_message


# The ontology maps the alert message family to the existing ``inform``
# performative provisionally. Confirm that mapping before SPADE integration.
ALERT_PERFORMATIVE = MESSAGE_PERFORMATIVES["alert"]
# Suppression is an internal audit row, not an agent message, so its
# performative cell is intentionally empty (no new FIPA performative is added).
AUDIT_EVENT_PERFORMATIVE = ""
DEFAULT_RECEIVE_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class AlertDecision:
    """Result of handling one village/month risk record."""

    status: str  # "emitted" or "suppressed"
    ds_division: str
    month: int
    tier: str
    text: str | None
    source: str
    hotspot: bool
    reason: str | None = None

    def alert_payload(self) -> dict[str, Any]:
        """Return the strict issued-alert message body."""
        if self.status != "emitted" or self.text is None:
            raise ValueError("A suppressed decision is not an issued alert")
        return {
            "event_type": "alert_issued",
            "ds_division": self.ds_division,
            "month": self.month,
            "tier": self.tier,
            "text": self.text,
            "source": self.source,
            "hotspot": self.hotspot,
        }

    def suppression_payload(self) -> dict[str, Any]:
        """Return a separate internal audit body, never a recipient alert."""
        if self.status != "suppressed" or self.reason is None:
            raise ValueError("Only a suppressed decision has a suppression audit body")
        return {
            "event_type": "alert_suppressed",
            "ds_division": self.ds_division,
            "month": self.month,
            "tier": self.tier,
            "source": self.source,
            "hotspot": self.hotspot,
            "reason": self.reason,
        }


def _alert_text(tier: str, hotspot: bool) -> str:
    disclaimer = "Risk indicator derived from published water-quality thresholds."
    if tier in {"Excellent", "Good"}:
        return (
            f"{disclaimer} This month's water-quality indicators for your well are "
            "within published guideline ranges."
        )
    if tier == "Poor":
        text = (
            f"{disclaimer} Water-quality indicators for this well are outside published "
            "guideline ranges for drinking water. Where possible, use an alternate source "
            "(RO-treated or piped supply) for drinking and cooking."
        )
        if hotspot:
            text += " Routine kidney-health screening is available at your nearest clinic."
        return text
    raise ValueError(f"Unsupported risk tier: {tier!r}")


class AlertAgent:
    """Format and suppress village alerts using B's risk-record fields.

    Args:
        agent_id: Stable sender label for message logs.
        logger: Injectable logger with the same signature as
            :func:`src.core.msg_log.log_message`.

    ``hotspot`` is deliberately a boolean adapter seam, not a claim about
    Member B's eventual HotspotAgent output format. Repeat policy is fixed to
    the documented rule: no periodic reminders; alert when either tier or
    hotspot state changes, and suppress duplicate/unchanged states. Suppression
    records are self-addressed audit events with a distinct ``event_type``;
    they are never sent to the village health officer.
    """

    def __init__(
        self,
        agent_id: str = "AlertAgent",
        logger: Callable[..., None] = log_message,
    ) -> None:
        self.agent_id = agent_id
        self._logger = logger
        self.last_state_by_village: dict[str, tuple[str, bool]] = {}
        self.state_by_village_month: dict[tuple[str, int], tuple[str, bool]] = {}

    @property
    def suppression_state(self) -> dict[str, Any]:
        """Read-only copy of state useful for tests and deterministic replay."""
        return {
            "last_state_by_village": dict(self.last_state_by_village),
            "state_by_village_month": dict(self.state_by_village_month),
        }

    def process_risk_record(
        self,
        risk_record: Mapping[str, Any],
        *,
        hotspot: bool = False,
    ) -> AlertDecision:
        """Validate B's record, create a decision, and log it under its schema.

        ``risk_record`` uses the current provisional schema in
        ``src.core.ontology.SCHEMAS['risk_record']``. The alert payload schema
        is explicit but provisional, as is its mapping to ``inform``.
        """
        validate("risk_record", risk_record)
        if not isinstance(hotspot, bool):
            raise TypeError("hotspot must be a bool until Member B publishes its output shape")

        village = risk_record["ds_division"]
        month = risk_record["month"]
        tier = risk_record["tier"]
        if tier not in {"Excellent", "Good", "Poor"}:
            raise ValueError(f"Unsupported risk tier: {tier!r}")

        state = (tier, hotspot)
        month_key = (village, month)
        current_month_state = self.state_by_village_month.get(month_key)
        previous_state = self.last_state_by_village.get(village)
        if current_month_state == state:
            decision = AlertDecision(
                "suppressed", village, month, tier, None, risk_record["source"], hotspot,
                "duplicate_same_state_same_month",
            )
        elif current_month_state is None and previous_state == state:
            decision = AlertDecision(
                "suppressed", village, month, tier, None, risk_record["source"], hotspot,
                "unchanged_state",
            )
        else:
            decision = AlertDecision(
                "emitted", village, month, tier, _alert_text(tier, hotspot),
                risk_record["source"], hotspot,
            )

        self.state_by_village_month[month_key] = state
        if decision.status == "emitted":
            self.last_state_by_village[village] = state
            payload = decision.alert_payload()
            validate_message("alert", payload)
            self._logger(self.agent_id, village, ALERT_PERFORMATIVE, f"alert:{village}:{month}", payload)
        else:
            payload = decision.suppression_payload()
            validate_message("alert_suppression", payload)
            # The fixed CSV has no event-type column. Keep this audit record
            # self-addressed, leave the non-message performative cell empty,
            # and mark payload.event_type so it cannot be read as an alert
            # delivered to the officer. Consumers should classify by event_type.
            self._logger(
                self.agent_id,
                self.agent_id,
                AUDIT_EVENT_PERFORMATIVE,
                f"alert:{village}:{month}:suppressed",
                payload,
            )
        return decision

    async def receive_once(
        self,
        receive: Callable[[], Awaitable[Any]],
        *,
        timeout_s: float = DEFAULT_RECEIVE_TIMEOUT_S,
    ) -> Any:
        """Receive one transport message, always bounded by ``timeout_s``.

        This adapter is intentionally transport-neutral while SPADE is absent
        from the current environment. Its return value can be passed to
        ``process_risk_record`` after the integration layer extracts the record
        and hotspot flag.
        """
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        return await asyncio.wait_for(receive(), timeout=timeout_s)
