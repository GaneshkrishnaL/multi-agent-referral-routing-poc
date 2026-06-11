# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Patient Context Extraction Agent.

This module is responsible for loading the patient's longitudinal record
into the triage session state. It deterministicly parses the patient's unique ID
from the incoming prompt using regular expressions, loads their clinical chart bundle,
validates the target referral specialty, and configures the context structure
for downstream processing.

This removes the need for a non-deterministic LLM step to "extract" the ID, saving
both inference cost and ensuring 100% extraction accuracy.

Data Flow:
- Input: Reads user prompt text from context.
- Output: Writes `patient_context`, `patient_summary`, `referral_specialty`,
  `referral_condition`, and `patient_id` back to the session state.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from . import clinical_data as cd
from . import mcp_clients
from .tools import build_summary

logger = logging.getLogger("smart_care_triage")


async def _load_bundle(pid: str) -> dict | None:
    """Assembles the patient context from the two MCP data domains:
    chart (patient_chart MCP / Athena equivalent) + claims and referral
    outcomes (claims MCP / data lake). Falls back to the direct local read if
    the MCP layer is disabled or unavailable — same data, same shape."""
    if mcp_clients.USE_MCP:
        try:
            chart = await mcp_clients.fetch_chart_bundle(pid)
            if chart is None:
                return None
            claims = await mcp_clients.fetch_claims_bundle(pid)
            return {**chart, **claims}
        except Exception as e:
            logger.warning("MCP chart/claims fetch failed (%s); falling back to local read", e)
    return cd.get_bundle(pid)

# Help message returned to users when a patient ID is not detected
_HELP = (
    "I triage specialist referrals for a patient. Tell me which "
    "patient to triage, for example:\n\n"
    "    Triage the referral for patient MINT-0006"
)

# Working-state keys written during a triage. Cleared when extraction halts so a
# previous turn's chart cannot leak through the gate or the HITL log (session
# state persists across turns in a conversation).
_RESET_KEYS = (
    "patient_context",
    "patient_summary",
    "referral_specialty",
    "referral_condition",
    "patient_id",
    "claims_signal",
    "routing",
    "specialist_matches",
    "assessment",
    "specialist_brief",
    "clinical_questions",
    "triage_decision",
    "review",
    "extraction_error",
)

# Regular expression to extract IDs matching either the 'MINT-####' or standard UUID formats
_ID = re.compile(
    r"(MINT-\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

def _routable() -> frozenset[str]:
    """The specialties routed by this engine, read from the business-owned
    policy at call time (so a policy reload is honored without restart).
    The target specialty is determined from the referral order or user
    request, and is never inferred from the diagnosis."""
    from .policy import policy

    return policy().routable_specialties


# Specialty-shaped words (Dermatology, Psychiatry, Orthopedics, Surgery, ...).
# Used to catch a REQUESTED specialty that is outside the routing policy —
# without this, an unsupported request would silently fall back to the
# patient's stored referral order and triage the wrong specialty.
_SPECIALTY_LIKE = re.compile(
    r"\b([A-Za-z]+(?:ology|iatry|edics|urgery|iatrics))\b", re.IGNORECASE
)


def _parse_specialty(text: str) -> str | None:
    """Detects a routable specialty mentioned in the prompt text.

    Sorts by length descending so multi-word names (e.g., 'Interventional Cardiology')
    are evaluated before shorter substrings (e.g., 'Cardiology').
    """
    low = text.lower()
    for s in sorted(_routable(), key=len, reverse=True):
        if s.lower() in low:
            return s
    return None


def _unsupported_specialty_mention(text: str) -> str | None:
    """Returns a specialty-shaped word from the prompt that is NOT in the
    routing policy (e.g. 'Dermatology'), or None."""
    routable_low = {s.lower() for s in _routable()}
    for m in _SPECIALTY_LIKE.finditer(text):
        word = m.group(1)
        if word.lower() not in routable_low:
            return word.capitalize()
    return None


def _primary_condition(b: dict, specialty: str) -> str:
    """Identifies the primary diagnosis driving this specialty referral.

    Inspects the patient problems list to find a diagnosis matching the ICD-10 code
    declared on the referral order, or falls back to the first problem listed under
    the requested specialty.
    """
    ref = b.get("referral_order") or {}
    ref_icds = set(ref.get("Icd10Codes") or [])
    problems = b.get("problems", [])
    for p in problems:
        if p.get("Icd10") in ref_icds:
            return p.get("Description", "")
    for p in problems:
        if p.get("Specialty") == specialty:
            return p.get("Description", "")
    return ""


def _synthesize_referral(b: dict, specialty: str) -> dict:
    """Synthesizes a referral order in state when triggered manually by prompt text.

    This ensures that referrals initialized via manual user queries (e.g., 'Triage patient X to Endocrinology')
    are processed identically to pre-existing electronic health record (EHR) referral orders.
    """
    stored = b.get("referral_order") or {}
    icd = [
        p["Icd10"] for p in b.get("problems", []) if p.get("Specialty") == specialty
    ][:3]
    return {
        **stored,
        "OrderTypeName": specialty,
        "OrderSubType": "Consult",
        "Description": f"Referral to {specialty}",
        "Icd10Codes": icd or stored.get("Icd10Codes", []),
        "StatUrgent": bool(stored.get("StatUrgent", False)),
    }


def _user_text(ctx: InvocationContext) -> str:
    """Helper to extract user message text from current and prior conversation turns."""
    uc = getattr(ctx, "user_content", None)
    if uc and getattr(uc, "parts", None):
        return " ".join((p.text or "") for p in uc.parts)
    for ev in reversed(getattr(ctx.session, "events", []) or []):
        c = getattr(ev, "content", None)
        if c and getattr(c, "role", None) == "user" and getattr(c, "parts", None):
            return " ".join((p.text or "") for p in c.parts)
    return ""


class ExtractionEngine(BaseAgent):
    """Load the patient's chart context for the referral being triaged."""

    def _halt(self, ctx: InvocationContext, message: str, **extra) -> Event:
        """Stops execution cleanly and returns an elegant help or error prompt to the user.

        This also clears the global working state memory to prevent leaks of clinical data
        across separate conversations.
        """
        ctx.end_invocation = True
        delta = dict.fromkeys(_RESET_KEYS)
        delta.update(extra)
        return Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=message)]),
            actions=EventActions(state_delta=delta),
        )

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        # 1. Parse user text to extract a patient ID
        text = _user_text(ctx)
        m = _ID.search(text)
        if not m:
            yield self._halt(
                ctx,
                _HELP,
                extraction_error="No patient id found in the referral request.",
            )
            return

        # 2. Retrieve patient chart bundle through the MCP data plane
        # (patient_chart MCP for the chart domain + claims MCP for the data
        # lake domain), with a local-read fallback.
        pid = m.group(1)
        b = await _load_bundle(pid)
        if not b:
            yield self._halt(
                ctx,
                f"I couldn't find a patient with id `{pid}`. Please check the "
                f"id and try again.\n\n{_HELP}",
                extraction_error=f"No patient found for id {pid}.",
            )
            return

        # 3. Determine the target specialty: prefer explicit request, else EHR referral order.
        asked = _parse_specialty(text)
        stored = (b.get("referral_order") or {}).get("OrderTypeName", "")
        # A REQUESTED specialty outside the policy must halt — never silently
        # fall back to the stored order and triage the wrong specialty.
        if not asked:
            mentioned = _unsupported_specialty_mention(text)
            if mentioned:
                opts = ", ".join(sorted(_routable()))
                yield self._halt(
                    ctx,
                    f"`{mentioned}` is not a specialty covered by the routing "
                    f"policy. Supported specialties: {opts}.",
                    extraction_error=f"Unsupported specialty {mentioned} for {pid}.",
                )
                return
        specialty = asked or stored
        # A stored order outside the routing policy must halt explicitly, not
        # silently default into the eConsult program.
        if specialty and specialty not in _routable():
            opts = ", ".join(sorted(_routable()))
            yield self._halt(
                ctx,
                f"`{specialty}` is not a specialty covered by the routing "
                f"policy. Supported specialties: {opts}.",
                extraction_error=f"Unsupported specialty {specialty} for {pid}.",
            )
            return
        if not specialty:
            opts = ", ".join(sorted(_routable()))
            yield self._halt(
                ctx,
                f"Patient `{pid}` has no pending referral on file. Tell me which "
                f"specialty to triage for, for example:\n\n"
                f"    Triage patient {pid} to Cardiology\n\n"
                f"Supported specialties: {opts}.",
                extraction_error=f"No specialty supplied and none on file for {pid}.",
            )
            return

        # 4. Handle urgent flag overrides if requested via prompt text
        is_urgent_text = "urgent" in text.lower()
        if is_urgent_text or (asked and asked != stored):
            ref = b.get("referral_order") or {}
            synthesized = _synthesize_referral(b, specialty)
            b = {
                **b,
                "referral_order": {
                    **synthesized,
                    "StatUrgent": is_urgent_text or bool(ref.get("StatUrgent", False)),
                }
            }

        # 5. Populate and yield state updates for the rest of the workflow
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            actions=EventActions(
                state_delta={
                    "patient_context": b,
                    "patient_summary": build_summary(b),
                    "referral_specialty": specialty,
                    "referral_condition": _primary_condition(b, specialty),
                    "patient_id": pid,
                }
            ),
        )
