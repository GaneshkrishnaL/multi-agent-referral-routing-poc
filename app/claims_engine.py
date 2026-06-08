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

from collections.abc import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions


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
        matched = [c for c in claims if c.get("Specialty") == specialty]
        
        # 4. Analyze referrals: Find if any past referrals were marked as "No-show"
        noshow = [p for p in prior if p.get("Status") == "No-show"]

        # 5. Build the unified claims signal dictionary
        signal = {
            "specialty": specialty,
            # True if there is at least one claim matching the target specialty
            "has_specialty_claim_12mo": bool(matched),
            # True if there is any medical claim in the last 12mo (used as an engagement guard)
            "has_any_claim_12mo": bool(claims),
            # Quantity of claims matching the specialty
            "claim_count_12mo": len(matched),
            # Find the date of the most recent matching claim
            "most_recent_claim": max(
                (c.get("ServiceDateFrom", "") for c in matched), default=None
            ),
            # True if the patient missed a prior referral appointment
            "prior_no_show": bool(noshow),
        }
        
        # 6. Yield the state delta update to save this signal in the session state
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            actions=EventActions(state_delta={"claims_signal": signal}),
        )
