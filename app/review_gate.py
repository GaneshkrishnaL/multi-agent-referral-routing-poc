# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Server-side Clinical Review Gate (ADK 2.0 human-in-the-loop node).

The deterministic confidence rules mark a referral for human review when it is
urgent, when In-person continuity could not be resolved, or when no specialist
matched (LOW). Previously that disposition was only a log label; this node makes
the gate REAL — and it sits BEFORE the triage orchestrator in the graph, so the
user-facing recommendation report has not been emitted yet when the workflow
pauses on the `RequestInput` interrupt. Nothing is released to the PCP until a
clinical reviewer responds (approve / modify / reject). Non-review referrals
pass straight through.

The reviewer response is recorded in session state (`review`) and lands in the
decisions.jsonl audit row written by the DecisionGatePlugin, joined by
session_id/invocation_id.
"""

from __future__ import annotations

from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.workflow import node

# Reviewer response contract (RequestInput response_schema)
_REVIEW_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["approve", "modify", "reject"],
            "description": "Clinical reviewer decision on the triage recommendation",
        },
        "reviewer_id": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["action"],
}


def _needs_review(routing: dict, patient_context: dict) -> tuple[bool, str]:
    """Deterministic gate condition: urgent OR confidence LOW/REVIEW."""
    urgent = bool((patient_context.get("referral_order") or {}).get("StatUrgent"))
    confidence = (routing.get("confidence") or "HIGH").upper()
    if urgent:
        return True, "urgent referral"
    if confidence in ("LOW", "REVIEW"):
        reason = (
            "in-person continuity unresolved"
            if routing.get("continuity_unresolved")
            else f"confidence {confidence}"
        )
        return True, reason
    return False, ""


@node(name="clinical_review_gate", rerun_on_resume=True)
async def clinical_review_gate(ctx: Context, node_input=None):
    """Pauses urgent / LOW / REVIEW referrals for a clinical reviewer sign-off."""
    routing = ctx.state.get("routing") or {}
    pc = ctx.state.get("patient_context") or {}

    required, reason = _needs_review(routing, pc)

    # Clean pathway: release straight to the PCP for accept/edit/override.
    if not required:
        yield Event(
            output={"released_to": "PCP"},
            state={
                "review": {
                    "required": False,
                    "status": "auto_released",
                    "disposition": "PCP (accept / edit / override)",
                }
            },
        )
        return

    # Review required and no response yet: interrupt the workflow and wait.
    if not ctx.resume_inputs:
        yield RequestInput(
            interrupt_id="clinical_review",
            message=(
                f"Clinical Reviewer sign-off required ({reason}). "
                f"Recommendation: {routing.get('care_path')} "
                f"(tag: {routing.get('patient_tag')}) "
                f"for {ctx.state.get('patient_id')} -> "
                f"{ctx.state.get('referral_specialty')}. "
                "Respond with action approve | modify | reject."
            ),
            response_schema=_REVIEW_RESPONSE_SCHEMA,
        )
        return

    # Resumed with the reviewer's response: record it, then release or reject.
    resp = ctx.resume_inputs.get("clinical_review") or {}
    if not isinstance(resp, dict):
        resp = {"action": str(resp)}
    review_state = {
        "review": {
            "required": True,
            "reason": reason,
            "status": "reviewed",
            "action": resp.get("action"),
            "reviewer_id": resp.get("reviewer_id"),
            "notes": resp.get("notes"),
            "disposition": "Clinical Reviewer (HITL)",
        }
    }
    if resp.get("action") == "reject":
        # Rejected: do NOT release the recommendation — end the run with a
        # clear message instead of letting the orchestrator emit the report.
        from google.genai import types

        ctx.end_invocation = True
        yield Event(
            state=review_state,
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text=(
                            "This referral was REJECTED by the clinical "
                            "reviewer and no recommendation was released. "
                            f"Reviewer notes: {resp.get('notes') or 'none'}."
                        )
                    )
                ],
            ),
        )
        return
    yield Event(
        output={"released_to": "Clinical Reviewer", "review_action": resp.get("action")},
        state=review_state,
    )
