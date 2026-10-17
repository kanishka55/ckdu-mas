"""Typed, non-diagnostic village risk-indicator alerts.

Hotspot status is supplied by an integration adapter; missing status is
unavailable, never interpreted as ``False``. ``Unknown`` and tiers without an
approved recipient policy are likewise not deliverable. Suppression decisions
remain internal and are not written as sent or received messages.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from src.core.msg_log import log_message
from src.core.ontology import INFORM, encode, validate

DEFAULT_RECEIVE_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class AlertDecision:
    """Result of handling one village/simulated-month risk record."""

    status: str  # "emitted", "suppressed" or "unavailable"
    ds_division: str
    month_index: int
    tier: str
    message: str | None
    hotspot: bool | None
    reason: str | None = None

    def alert_payload(self) -> dict[str, Any]:
        """Return the ontology-validated body for an issued alert only."""
        if self.status != "emitted" or self.message is None or self.hotspot is None:
            raise ValueError("Only an emitted alert has a recipient payload")
        body = {
            "type": "alert",
            "month_index": self.month_index,
            "ds_division": self.ds_division,
            "tier": self.tier,
            "hotspot": self.hotspot,
            "message": self.message,
        }
        validate(INFORM, body)
        return body


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
    raise ValueError(f"No approved recipient alert policy for risk tier: {tier!r}")


class AlertAgent:
    """Format and deduplicate alerts from ontology-shaped Member B records.

    The supported recipient wording is currently limited to Good, Excellent
    and Poor. ``Very Poor``, ``Unsuitable`` and ``Unknown`` return an
    unavailable decision pending team policy. No periodic reminders are sent:
    identical village/month records and unchanged tier/hotspot states are
    suppressed. Only issued alerts are logged.
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
        """Return a copy of suppression state for tests and deterministic replay."""
        return {
            "last_state_by_village": dict(self.last_state_by_village),
            "state_by_village_month": dict(self.state_by_village_month),
        }

    def process_risk_record(
        self,
        risk_record: Mapping[str, Any],
        *,
        hotspot: bool | None = None,
    ) -> AlertDecision:
        """Process a typed risk record; do not infer absent hotspot status."""
        record = dict(risk_record)
        validate(INFORM, record)
        village = record["ds_division"]
        month_index = record["month_index"]
        tier = record["tier"]

        if hotspot is None:
            return AlertDecision(
                "unavailable", village, month_index, tier, None, None,
                "hotspot_status_unavailable",
            )
        if not isinstance(hotspot, bool):
            return AlertDecision(
                "unavailable", village, month_index, tier, None, None,
                "hotspot_status_must_be_bool",
            )
        if tier not in {"Excellent", "Good", "Poor"}:
            reason = "unknown_tier_policy_unapproved" if tier == "Unknown" else "tier_policy_unapproved"
            return AlertDecision("unavailable", village, month_index, tier, None, hotspot, reason)

        state = (tier, hotspot)
        month_key = (village, month_index)
        current_month_state = self.state_by_village_month.get(month_key)
        previous_state = self.last_state_by_village.get(village)
        if current_month_state == state:
            return AlertDecision(
                "suppressed", village, month_index, tier, None, hotspot,
                "duplicate_same_state_same_month",
            )
        if current_month_state is None and previous_state == state:
            self.state_by_village_month[month_key] = state
            return AlertDecision("suppressed", village, month_index, tier, None, hotspot, "unchanged_state")

        decision = AlertDecision(
            "emitted", village, month_index, tier, _alert_text(tier, hotspot), hotspot,
        )
        payload = decision.alert_payload()
        encoded = encode(INFORM, payload)
        self._logger(
            self.agent_id,
            village,
            INFORM,
            f"alert:{village}:{month_index}",
            encoded,
            "sent",
        )
        self.state_by_village_month[month_key] = state
        self.last_state_by_village[village] = state
        return decision

    async def receive_once(
        self,
        receive: Callable[[], Awaitable[Any]],
        *,
        timeout_s: float = DEFAULT_RECEIVE_TIMEOUT_S,
    ) -> Any:
        """Receive one transport message with an explicit positive timeout."""
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        return await asyncio.wait_for(receive(), timeout=timeout_s)
