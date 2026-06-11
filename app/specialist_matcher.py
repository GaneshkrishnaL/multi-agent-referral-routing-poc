# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Deterministic Specialist Matcher Agent.

This module is responsible for identifying and ranking in-network specialists who are
ideally matched to the patient based on clinical tier and geographic proximity.

Crucially, it also computes the dynamic, availability-dependent confidence of the
routing decision. If an in-network specialist is unavailable nearby, the confidence is downgraded
to flag a manual human reviewer, maintaining safety without relying on non-deterministic LLM flags.

Data Flow:
- Input: Reads `patient_context` (location), `routing`, and `claims_signal`
  (existing-specialist NPI for the In-person continuity path).
- Output: Writes `specialist_matches` and updates `routing.confidence`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from . import clinical_data as cd
from . import mcp_clients

logger = logging.getLogger("smart_care_triage")


async def _rank_via_directory(
    specialty: str, lat: float, lon: float, internal_only: bool = False, top: int = 3
) -> list[dict]:
    """Fetches RAW rows from the specialist_directory MCP and applies the
    business policy locally (tier from score thresholds, tier-then-distance
    ranking) — data plane via MCP, decision plane deterministic. Falls back to
    the direct local read on MCP failure."""
    if mcp_clients.USE_MCP:
        try:
            rows = await mcp_clients.fetch_specialist_rows(
                specialty, in_network_only=True, internal_only=internal_only
            )
            out = [cd._match_entry(s, lat, lon) for s in rows]
            out.sort(key=lambda s: (s["Tier"], s["DistanceMi"]))
            return out[:top]
        except Exception as e:
            logger.warning("MCP directory fetch failed (%s); falling back to local read", e)
    return cd.rank_specialists(specialty, lat, lon, internal_only=internal_only, top=top)


async def _continuity_via_directory(npi: str, lat: float, lon: float) -> dict | None:
    """Continuity lookup (claim NPI -> directory row) via the MCP data plane."""
    if mcp_clients.USE_MCP:
        try:
            row = await mcp_clients.fetch_specialist_by_npi(npi)
            return cd._match_entry(row, lat, lon) if row else None
        except Exception as e:
            logger.warning("MCP NPI lookup failed (%s); falling back to local read", e)
    return cd.find_specialist_by_npi(npi, lat, lon)


class SpecialistMatcher(BaseAgent):
    """Ranks in-network specialists and computes availability-dependent routing confidence."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        # 1. Fetch patient, routing, and claims details from session state
        pc = ctx.session.state.get("patient_context") or {}
        routing = dict(ctx.session.state.get("routing") or {})
        claims_signal = ctx.session.state.get("claims_signal") or {}
        pat = pc.get("patient", {})
        care_path = routing.get("care_path")
        specialty = routing.get("specialty", "")
        lat, lon = float(pat.get("Lat", 0)), float(pat.get("Lon", 0))

        # 2. Query directory and rank specialists, branching on the care path:
        # - In-person (existing relationship): recommend the patient's EXISTING
        #   specialist, resolved from the most recent target-specialty claim's
        #   rendering provider NPI. Continuity is the entire point of this path.
        # - Virtual: select from the internal specialist pool
        #   (telehealth — distance is a tiebreaker, not a constraint).
        # - eConsult: rank the in-network directory by tier asc, distance asc.
        continuity_unresolved = False
        if care_path == "In-person":
            existing = await _continuity_via_directory(
                claims_signal.get("existing_specialist_npi") or "", lat, lon
            )
            if existing:
                matches = [{**existing, "Continuity": True}]
            else:
                # Claim provider not in the directory: fall back to the nearest
                # in-network specialist, flagged for human review below.
                continuity_unresolved = True
                matches = await _rank_via_directory(specialty, lat, lon)
        elif care_path == "Virtual":
            matches = await _rank_via_directory(specialty, lat, lon, internal_only=True)
        else:
            matches = await _rank_via_directory(specialty, lat, lon)

        # 3. Dynamic Confidence Calculation -> Drives the Human-In-The-Loop (HITL) gateway.
        # This is a rules-based, deterministic decision-making system.
        # - Urgent: Always flagged for review ("REVIEW"), bypasses direct PCP routing.
        # - In-person with unresolved continuity: flagged for review ("REVIEW").
        # - No matches: Care path requires a specialist, but none are available ("LOW").
        # - Normal: Clean pathway, high confidence ("HIGH"), routed straight to the PCP.
        urgent = bool(routing.get("urgent"))
        if urgent:
            confidence = "REVIEW"  # Urgent cases must be reviewed by a human clinical specialist
        elif continuity_unresolved:
            confidence = "REVIEW"  # Existing specialist not resolvable from the directory
        elif care_path in ("eConsult", "Virtual") and not matches:
            confidence = "LOW"     # No available matching specialist lowers triage confidence
        else:
            confidence = "HIGH"    # Solid match, safe for auto-routing/PCP sign-off

        routing["confidence"] = confidence
        routing["continuity_unresolved"] = continuity_unresolved

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
