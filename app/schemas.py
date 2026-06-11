# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Structured output schema for the final triage decision (explainability)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SpecialistPick(BaseModel):
    specialist_id: str = Field(description="Recommended specialist id, e.g. SPEC-0017")
    name: str = Field(description="Specialist name")
    tier: int = Field(description="Quality tier 1-4 (1 is best)")
    distance_mi: float = Field(description="Distance from the patient in miles")


class TriageDecision(BaseModel):
    """The final, explainable triage recommendation shown to the PCP."""

    care_path: Literal["eConsult", "Virtual", "In-person"] = Field(
        description="Recommended care path"
    )
    patient_tag: str = Field(
        description="Engagement tag, e.g. Established Patient or Existing Specialist Relationship"
    )
    routing_rationale: str = Field(
        description="Why this care path, grounded in the deterministic routing rules"
    )
    specialist: SpecialistPick | None = Field(
        default=None,
        description=(
            "Top recommended specialist. For In-person routes this is the "
            "patient's EXISTING specialist resolved from claims (continuity); "
            "null only when no specialist could be matched."
        ),
    )
    top_alternatives: list[str] = Field(
        default_factory=list,
        description="Other ranked specialists, e.g. 'SPEC-0026 (T1, 9.2mi)'",
    )
    specialist_brief: str = Field(
        description="The Specialist Brief: concise clinical summary for the consult"
    )
    clinical_questions: list[str] = Field(
        description="2-3 patient-specific questions for the specialist"
    )
    confidence: Literal["HIGH", "REVIEW", "LOW"] = Field(
        description="HIGH routes to PCP; REVIEW/LOW routes to a clinical reviewer (HITL)"
    )
    explanation: str = Field(
        description="One-paragraph plain-language explanation for the PCP"
    )
