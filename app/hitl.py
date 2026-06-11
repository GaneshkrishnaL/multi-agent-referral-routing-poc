# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Human-In-The-Loop (HITL) Decision Gate Plugin.

This module acts as an ADK post-run lifecycle plugin. Rather than running as an 
active agent node inside the graph (which would add token overhead and model latency), 
this plugin intercepts the session state once the entire workflow completes execution.

Responsibilities:
1. Parse the completed orchestrator's TriageDecision.
2. Determine final human routing (PCP vs Clinical Reviewer) based on urgency and confidence rules.
3. Log audit parameters (patient, specialty, care path, confidence, token usage, latency) 
   to the local structured database (`decisions.jsonl`) to feed downstream analytics.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import time
from pathlib import Path

from google.adk.agents.invocation_context import InvocationContext
from google.adk.plugins.base_plugin import BasePlugin

logger = logging.getLogger("smart_care_triage")

# Local storage path for triage decision logging
DECISION_LOG = Path(__file__).resolve().parent.parent / "decisions.jsonl"


def _as_dict(decision) -> dict:
    """Safely converts any input decision object (Pydantic model, JSON, string) into a dict."""
    if decision is None:
        return {}
    if isinstance(decision, dict):
        return decision
    if hasattr(decision, "model_dump"):
        return decision.model_dump()
    try:
        return json.loads(decision)
    except Exception:
        return {}


class DecisionGatePlugin(BasePlugin):
    """Enforces clinical routing rules and records session telemetry log entries."""

    def __init__(self) -> None:
        super().__init__(name="decision_gate")

    async def after_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> None:
        """Lifecycle hook triggered automatically by the ADK once the workflow completes a run."""
        state = invocation_context.session.state

        # 1. Safe extraction of the synthesized decision object
        decision = _as_dict(state.get("triage_decision"))
        if not decision:
            return None

        # 2. Extract urgency, confidence, and the deterministic routing record.
        # The audit row records the DETERMINISTIC source of truth (routing
        # state) for care path / tag / confidence — never an LLM transcription.
        pc = state.get("patient_context") or {}
        routing = state.get("routing") or {}
        urgent = bool((pc.get("referral_order") or {}).get("StatUrgent"))
        confidence = routing.get("confidence") or decision.get("confidence", "HIGH")

        # 3. Disposition: prefer the live review-gate outcome (the workflow's
        # clinical_review_gate node actually blocks these referrals); fall back
        # to the same deterministic rule for runs without the gate.
        review = state.get("review") or {}
        disposition = review.get("disposition") or (
            "Clinical Reviewer (HITL)"
            if urgent or confidence in ("LOW", "REVIEW")
            else "PCP (accept / edit / override)"
        )

        # 4. Measure execution latency (using monotic timers)
        latency_ms = state.get("latency_ms")
        if latency_ms is None and state.get("temp:t0") is not None:
            latency_ms = round((time.monotonic() - state["temp:t0"]) * 1000)

        # 5. Extract primary specialist recommendation
        spec = decision.get("specialist") or {}
        matches = state.get("specialist_matches") or []
        specialist_id = (
            (spec.get("specialist_id") if spec else None)
            or (matches[0].get("SpecialistId") if matches else None)
        )

        # 6. Build structured telemetry record. session_id + invocation_id are
        # the correlation keys that let /pcp_action rows (PCP accept/override)
        # join back to this decision — closing the feedback loop.
        entry = {
            "timestamp": _dt.datetime.now().isoformat(timespec="seconds"),
            "session_id": invocation_context.session.id,
            "invocation_id": invocation_context.invocation_id,
            "patient_id": state.get("patient_id"),
            "specialty": state.get("referral_specialty"),
            "care_path": routing.get("care_path") or decision.get("care_path"),
            "patient_tag": routing.get("patient_tag") or decision.get("patient_tag"),
            "policy_version": routing.get("policy_version"),
            "specialist": specialist_id,
            "confidence": confidence,
            "urgent": urgent,
            "disposition": disposition,
            "review": review or None,
            "latency_ms": latency_ms,
            "total_tokens": state.get("total_tokens"),
            "pcp_action": None,          # Filled by the joined /pcp_action row
            "override_reason": None,     # (correlated via session_id)
        }
        
        # 7. Write telemetry record to decisions log file
        try:
            with open(DECISION_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.warning("decision log write failed: %s", e)
            
        logger.info(
            "[hitl] disposition=%s confidence=%s urgent=%s",
            disposition,
            confidence,
            urgent,
        )
        return None
