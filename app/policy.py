# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Business-owned routing policy loader.

The customer requirement is that routing rules are a fluid INPUT to the
system owned by the business, not logic hardcoded by engineering. This module
loads the versioned policy artifact (config/routing_policy.yaml by default,
overridable via the ROUTING_POLICY_PATH environment variable) and exposes it
as a validated, immutable Pydantic model.

Every routing decision stamps `policy_version` into its output and audit row,
so any historical decision can be reproduced against the rules in force.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "routing_policy.yaml"


class TagTaxonomy(BaseModel):
    """The customer's exact patient engagement tag strings."""

    existing_relationship: str = "Existing Specialist Relationship"
    established: str = "Established Patient"
    new_patient: str = "New Patient"
    new_needs_first_visit: str = "New Patient - Needs first visit"
    unengaged: str = "Unengaged Patient - Needs first visit"


class CarePaths(BaseModel):
    virtual_specialties: list[str] = Field(default_factory=lambda: ["Cardiology"])
    econsult_specialties: list[str] = Field(
        default_factory=lambda: [
            "Endocrinology",
            "Nephrology",
            "Rheumatology",
            "Neurology",
            "Hematology",
            "Pulmonology",
        ]
    )


class Windows(BaseModel):
    specialty_claim_months: int = 12
    scheduled_appt_horizon_days: int = 30


class TierThresholds(BaseModel):
    tier_1_min_score: int = 75
    tier_2_min_score: int = 50
    tier_3_min_score: int = 25


class RoutingPolicy(BaseModel):
    """Validated, versioned routing policy (see config/routing_policy.yaml)."""

    policy_version: str = "unversioned"
    care_paths: CarePaths = Field(default_factory=CarePaths)
    windows: Windows = Field(default_factory=Windows)
    tags: TagTaxonomy = Field(default_factory=TagTaxonomy)
    tiers: TierThresholds = Field(default_factory=TierThresholds)

    @property
    def virtual_specialties(self) -> frozenset[str]:
        return frozenset(self.care_paths.virtual_specialties)

    @property
    def econsult_specialties(self) -> frozenset[str]:
        return frozenset(self.care_paths.econsult_specialties)

    @property
    def routable_specialties(self) -> frozenset[str]:
        return self.virtual_specialties | self.econsult_specialties

    def score_to_tier(self, score: float) -> int:
        """Maps a 0-100 quality score to the customer's 1-4 tier."""
        t = self.tiers
        if score > t.tier_1_min_score:
            return 1
        if score > t.tier_2_min_score:
            return 2
        if score > t.tier_3_min_score:
            return 3
        return 4


@lru_cache(maxsize=1)
def policy() -> RoutingPolicy:
    """Loads the routing policy once per process (env override supported).

    An EXPLICIT ROUTING_POLICY_PATH that does not exist raises — a typo'd
    deployment path must never silently revert the business's rules to the
    engineering defaults. Only the bare default location may fall back (for
    minimal test environments), and that fallback is stamped 'unversioned' so
    it is detectable in every audit row.
    """
    override = os.getenv("ROUTING_POLICY_PATH")
    if override:
        path = Path(override)
        if not path.exists():
            raise FileNotFoundError(
                f"ROUTING_POLICY_PATH is set but no policy file exists at {path}"
            )
        return RoutingPolicy.model_validate(yaml.safe_load(path.read_text()) or {})
    if _DEFAULT_PATH.exists():
        return RoutingPolicy.model_validate(
            yaml.safe_load(_DEFAULT_PATH.read_text()) or {}
        )
    return RoutingPolicy()


def reload_policy() -> RoutingPolicy:
    """Clears the cache and reloads. Test helper: routing reads the policy at
    call time, but data-layer constants snapshot at import — a production
    policy change is a restart/redeploy, not a hot reload."""
    policy.cache_clear()
    return policy()
