# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Deterministic Care-Path Routing Engine.

This module houses the deterministic logic used to route a patient referral to
one of three core pathways:
1. eConsult: Asynchronous specialist question/review (default for lower complexity).
2. Virtual: Real-time telehealth visit (used for virtual-only specialties).
3. In-person: Traditional face-to-face visit (mandated for existing specialist relationships).

Routing is 100% rules-based and auditable. It consumes the claims signals computed upstream
and classifies the patient using a strict four-tag customer engagement taxonomy, ensuring
compliance, medical safety, and transparency.

Data Flow:
- Input: Reads `patient_context` and `claims_signal`.
- Output: Writes the deterministic `routing` decision.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from .policy import policy


def classify_tag(
    has_specialty_claim: bool,
    recent_encounters: int,
    has_checked_out: bool,
    scheduled_next_month: bool,
) -> str:
    """Classifies patients using the customer's four-tag engagement taxonomy.

    This is a direct transcription of the customer's corrected decision tree
    (precondition for the four tags: no claim to the TARGET specialty in the
    last 12 months; that precondition is the in-person rule, checked first):

        if >= 1 recorded encounter:                  Established Patient
        elif has a checked-out appointment:          New Patient
        elif appt scheduled in the next 1 month:     New Patient - Needs first visit
        else:                                        Unengaged Patient - Needs first visit

    Note: claims to OTHER specialties play no role in tagging — the only claims
    condition in the taxonomy is the target-specialty check above.
    """
    tags = policy().tags
    if has_specialty_claim:
        return tags.existing_relationship  # Automatic in-person route
    if recent_encounters > 0:
        return tags.established
    if has_checked_out:
        return tags.new_patient
    if scheduled_next_month:
        return tags.new_needs_first_visit
    return tags.unengaged


def decide_route(
    specialty: str,
    has_specialty_claim: bool,
    recent_encounters: int,
    has_checked_out: bool = False,
    scheduled_next_month: bool = False,
):
    """Applies the deterministic routing rules (first rule wins).

    Step 1: claim to the target specialty in the last 12 months -> In-person.
    Step 2: classify the patient into exactly one engagement tag.
    Step 3: virtual-designated specialties (e.g. Cardiology) -> Virtual;
            the six eConsult specialties -> eConsult. Anything else is NOT
            silently routed — unsupported specialties raise so callers halt
            explicitly instead of defaulting into the eConsult program.

    Returns:
        tuple: (care_path, patient_tag)
        where care_path is one of: "In-person", "Virtual", "eConsult".
    """
    # 1. Classify the patient's level of engagement
    tag = classify_tag(
        has_specialty_claim,
        recent_encounters,
        has_checked_out,
        scheduled_next_month,
    )

    # 2a. Existing relationship: route In-person for care continuity and stop.
    if has_specialty_claim:
        return "In-person", tag

    # 2b. Route by the business-owned specialty policy.
    pol = policy()
    if specialty in pol.virtual_specialties:
        return "Virtual", tag
    if specialty in pol.econsult_specialties:
        return "eConsult", tag
    raise ValueError(f"Specialty '{specialty}' is not in the routing policy.")


class RoutingEngine(BaseAgent):
    """Deterministic care-path router executing clinical-business routing guidelines."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        # 1. Retrieve clinical context and claim signals
        pc = ctx.session.state.get("patient_context") or {}
        claims_signal = ctx.session.state.get("claims_signal") or {}
        referral = pc.get("referral_order") or {}
        specialty = referral.get("OrderTypeName", "")
        recent = pc.get("recent_encounters", 0) or 0
        has_checked_out = bool(pc.get("has_checked_out_appt"))
        scheduled_next_month = bool(pc.get("appt_scheduled_next_month"))

        # 2. Safety check: Ensure a referral specialty is defined.
        # Error paths END the invocation — downstream nodes must never stamp
        # confidence or fabricate a care path on top of an error record.
        if not specialty:
            ctx.end_invocation = True
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                actions=EventActions(
                    state_delta={
                        "routing": {"error": "no referral specialty in patient context"}
                    }
                ),
            )
            return

        # 3. Pull signals from claims_signal dictionary
        has_claim = bool(claims_signal.get("has_specialty_claim_12mo"))
        noshow = bool(claims_signal.get("prior_no_show"))

        # 4. Compute optimal care pathway and taxonomy tag
        try:
            care_path, tag = decide_route(
                specialty,
                has_claim,
                recent,
                has_checked_out,
                scheduled_next_month,
            )
        except ValueError as exc:
            # Unsupported specialty: halt explicitly rather than defaulting a
            # non-program specialty into eConsult.
            ctx.end_invocation = True
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                actions=EventActions(state_delta={"routing": {"error": str(exc)}}),
            )
            return
        urgent = bool(referral.get("StatUrgent"))

        # 5. Compile the comprehensive routing summary
        routing = {
            "care_path": care_path,
            "patient_tag": tag,
            "specialty": specialty,
            "has_specialty_claim_12mo": has_claim,
            "prior_no_show": noshow,
            "urgent": urgent,
            # The exact policy revision these rules came from (audit/reproducibility)
            "policy_version": policy().policy_version,
            # Note: Confidence is calculated dynamically downstream by SpecialistMatcher
            "rationale": _rationale(care_path, tag, specialty, has_claim, noshow),
        }

        # 6. Yield the state update to save our decision
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            actions=EventActions(
                state_delta={
                    "routing": routing,
                }
            ),
        )


def _rationale(care_path, tag, specialty, has_claim, noshow):
    """Generates a structured, clinical-facing rationale statement for the selected pathway."""
    if has_claim:
        return (
            f"Patient has a {specialty} claim in the last 12 months (existing "
            f"relationship), so route in-person to continue with their specialist."
        )
    if care_path == "Virtual":
        return (
            f"{specialty} referral with no prior {specialty} visit on record "
            f"(tag: {tag}); route to a virtual visit with an internal "
            f"{specialty.lower()} specialist."
        )
    base = (
        f"{specialty} referral with no prior {specialty} visit on record "
        f"(tag: {tag}); eligible for an asynchronous eConsult."
    )
    if noshow:
        base += " Note a prior referral resulted in a no-show."
    return base
