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

from . import clinical_data as cd


def classify_tag(
    has_specialty_claim: bool,
    recent_encounters: int,
    has_checked_out: bool,
    scheduled_next_month: bool,
    has_any_claim_12mo: bool,
) -> str:
    """Classifies patients into one of four customer-specific taxonomy tags.

    This taxonomy maps exactly to the healthcare provider's data pipeline classification model, 
    ensuring consistency between analytical registers and active triage recommendations.

    Tag Conditions:
    - Existing Relationship: Patient visited this specialty within the last 12 months.
    - Established Patient: No specialty claims, but has visited the PCP/clinic recently.
    - New Patient - Needs first visit: Checked out but needs onboarding scheduling.
    - New Patient / Unengaged Patient: High-friction groups with low prior contact.
    """
    if has_specialty_claim:
        return "Existing Specialist Relationship"  # Automatic in-person route
    if recent_encounters > 0:
        return "Established Patient"
    if has_checked_out and scheduled_next_month and not has_any_claim_12mo:
        return "New Patient - Needs first visit"
    if has_checked_out and not has_any_claim_12mo:
        return "New Patient"
    if not has_checked_out and not scheduled_next_month and not has_any_claim_12mo:
        return "Unengaged Patient"
    return "New Patient"


def decide_route(
    specialty: str,
    has_specialty_claim: bool,
    recent_encounters: int,
    has_checked_out: bool = False,
    scheduled_next_month: bool = False,
    has_any_claim_12mo: bool = False,
):
    """Applies clinical routing rules to determine care pathway and patient classification.

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
        has_any_claim_12mo,
    )
    
    # 2. Apply pathway rules:
    # Rule 2a: If they have an existing relationship, route to In-person for care continuity
    if has_specialty_claim:
        return "In-person", tag
        
    # Rule 2b: If the target specialty is designated as Virtual-only, route to Virtual
    care_path = "Virtual" if specialty == cd.VIRTUAL_SPECIALTY else "eConsult"
    return care_path, tag


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

        # 2. Safety check: Ensure a referral specialty is defined
        if not specialty:
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
        has_any_claim = bool(claims_signal.get("has_any_claim_12mo"))
        noshow = bool(claims_signal.get("prior_no_show"))
        
        # 4. Compute optimal care pathway and taxonomy tag
        care_path, tag = decide_route(
            specialty,
            has_claim,
            recent,
            has_checked_out,
            scheduled_next_month,
            has_any_claim,
        )
        urgent = bool(referral.get("StatUrgent"))

        # 5. Compile the comprehensive routing summary
        routing = {
            "care_path": care_path,
            "patient_tag": tag,
            "specialty": specialty,
            "has_specialty_claim_12mo": has_claim,
            "prior_no_show": noshow,
            "urgent": urgent,
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
            f"(tag: {tag}); route to a virtual visit with an internal cardiologist."
        )
    base = (
        f"{specialty} referral with no prior {specialty} visit on record "
        f"(tag: {tag}); eligible for an asynchronous eConsult."
    )
    if noshow:
        base += " Note a prior referral resulted in a no-show."
    return base
