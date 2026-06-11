# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Deterministic Claims Engine Agent.

This module analyzes a patient's claims history and prior referral status over the last
12 months to compute key routing signals. By establishing whether a patient has an active
relationship with the target specialty, or has a history of missed appointments (no-shows),
this engine creates auditable data signals used directly by the care-path router.

Data Flow:
- Input: Reads `patient_context` from the session state.
- Output: Writes `claims_signal` back into the session state.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from .policy import policy


def claim_window_start(today: _dt.date | None = None) -> str:
    """ISO date marking the start of the business-owned claim look-back
    window (policy windows.specialty_claim_months, default 12 months)."""
    today = today or _dt.date.today()
    months = policy().windows.specialty_claim_months
    y, m = divmod((today.year * 12 + today.month - 1) - months, 12)
    return _dt.date(y, m + 1, min(today.day, 28)).isoformat()


class ClaimsEngine(BaseAgent):
    """Computes a prior specialist-engagement signal from claims and prior referrals."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        # 1. Pull the patient chart context loaded by the extraction agent
        pc = ctx.session.state.get("patient_context") or {}
        referral = pc.get("referral_order") or {}
        specialty = referral.get("OrderTypeName", "")

        # 2. Extract 12-month claims history and prior referral tracking list
        claims = pc.get("claims_12mo", []) or []
        prior = pc.get("prior_referrals", []) or []

        # 3. Analyze claims: Find claims corresponding to the requested specialty
        # WITHIN the policy's look-back window. The bundle field is named
        # claims_12mo, but the window is enforced here against the policy so
        # an aged claim can never keep firing the In-person rule and the
        # business's window edit actually takes effect.
        # A target-specialty claim is the ONLY claims condition in the routing
        # rules (the customer corrected "procedure code" to "specialty").
        cutoff = claim_window_start()
        matched = [
            c
            for c in claims
            if c.get("Specialty") == specialty
            and (c.get("ServiceDateFrom") or "9999") >= cutoff
        ]

        # 4. Analyze referrals: prior no-shows to the TARGET specialty (a no-show
        # generates no claim, so these patients still fall through to tagging).
        noshow = [
            p
            for p in prior
            if p.get("Status") == "No-show" and p.get("Specialty") == specialty
        ]

        # 5. Identify the patient's existing specialist (most recent matching
        # claim). The In-person rule exists for care continuity — "do I have a
        # claim with THIS specialist" — so the matcher recommends this provider.
        latest = max(
            matched, key=lambda c: c.get("ServiceDateFrom", ""), default=None
        ) or {}

        # 6. Build the unified claims signal dictionary
        signal = {
            "specialty": specialty,
            # True if there is at least one claim matching the target specialty
            "has_specialty_claim_12mo": bool(matched),
            # Quantity of claims matching the specialty
            "claim_count_12mo": len(matched),
            # Find the date of the most recent matching claim
            "most_recent_claim": latest.get("ServiceDateFrom"),
            # The rendering provider on that claim = the existing relationship
            "existing_specialist_npi": latest.get("RenderingProviderNpi"),
            "existing_specialist_name": latest.get("RenderingProviderName"),
            # True if the patient missed a prior referral to this specialty
            "prior_no_show": bool(noshow),
        }

        # 7. Yield the state delta update to save this signal in the session state
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            actions=EventActions(state_delta={"claims_signal": signal}),
        )
