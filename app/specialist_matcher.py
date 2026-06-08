# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Deterministic Specialist Matcher Agent.

This module is responsible for identifying and ranking in-network specialists who are 
ideally matched to the patient based on clinical tier and geographic proximity. 

Crucially, it also computes the dynamic, availability-dependent confidence of the 
routing decision. If an in-network specialist is unavailable nearby, the confidence is downgraded 
to flag a manual human reviewer, maintaining safety without relying on non-deterministic LLM flags.

Data Flow:
- Input: Reads `patient_context` (for location coordinates) and `routing`.
- Output: Writes `specialist_matches` and updates `routing.confidence`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from . import clinical_data as cd


class SpecialistMatcher(BaseAgent):
    """Ranks in-network specialists and computes availability-dependent routing confidence."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        # 1. Fetch patient and routing details from session state
        pc = ctx.session.state.get("patient_context") or {}
        routing = dict(ctx.session.state.get("routing") or {})
        pat = pc.get("patient", {})
        care_path = routing.get("care_path")
        specialty = routing.get("specialty", "")

        # 2. Query directory and rank specialists.
        # Specialist ranking criteria: Tier ascending (Tier 1 first), then Geographic Distance ascending.
        # - For eConsult/Virtual: Recommends the specific specialist to perform the consult.
        # - For In-person: Locates the nearest in-network specialist of this specialty for continuity.
        matches = cd.rank_specialists(
            specialty, float(pat.get("Lat", 0)), float(pat.get("Lon", 0))
        )

        # 3. Dynamic Confidence Calculation -> Drives the Human-In-The-Loop (HITL) gateway.
        # This is a rules-based, deterministic decision-making system.
        # - Urgent: Always flagged for review ("REVIEW"), bypasses direct PCP routing.
        # - No matches: Care path requires specialist consultation, but none are in-network ("LOW").
        # - Normal: Clean pathway, high confidence ("HIGH"), routed straight to the PCP.
        urgent = bool(routing.get("urgent"))
        if urgent:
            confidence = "REVIEW"  # Urgent cases must be reviewed by a human clinical specialist
        elif care_path in ("eConsult", "Virtual") and not matches:
            confidence = "LOW"     # No available matching specialist lowers triage confidence
        else:
            confidence = "HIGH"    # Solid match, safe for auto-routing/PCP sign-off

        routing["confidence"] = confidence
        
        # 4. Save specialist rankings and finalized confidence back into session state
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            actions=EventActions(
                state_delta={
                    "routing": routing,
                    "specialist_matches": matches,
                }
            ),
        )
