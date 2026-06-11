# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Truth-table unit tests for the deterministic routing core.

Every cell of the customer's four-tag taxonomy (checked-out x scheduled x
encounters x specialty-claim) is asserted against tests/spec_rules.py — the
independent transcription of the requirements doc — so any future drift
between code and spec fails immediately, without needing the dataset.
"""

from __future__ import annotations

import sys
from itertools import product
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from spec_rules import (  # noqa: E402
    ECONSULT_SIX,
    spec_route,
    spec_tag,
)

from app.routing import classify_tag, decide_route  # noqa: E402


@pytest.mark.parametrize(
    "has_claim,encounters,checked_out,scheduled",
    list(product([False, True], [0, 1], [False, True], [False, True])),
)
def test_tag_matches_customer_table(has_claim, encounters, checked_out, scheduled):
    """classify_tag must agree with the spec transcription on every cell."""
    assert classify_tag(has_claim, encounters, checked_out, scheduled) == spec_tag(
        has_claim, encounters, checked_out, scheduled
    )


def test_needs_first_visit_cell():
    """The cell the original code inverted: no encounters, NO checked-out
    appointment, appointment scheduled next month."""
    assert (
        classify_tag(False, 0, False, True) == "New Patient - Needs first visit"
    )


def test_checked_out_is_new_patient_regardless_of_scheduled():
    """Customer table: 'New Patient' requires a checked-out appointment;
    the scheduled flag is 'not relevant'."""
    assert classify_tag(False, 0, True, True) == "New Patient"
    assert classify_tag(False, 0, True, False) == "New Patient"


def test_unengaged_keeps_full_customer_string():
    assert (
        classify_tag(False, 0, False, False)
        == "Unengaged Patient - Needs first visit"
    )


def test_unrelated_claims_play_no_role_in_tagging():
    """Only TARGET-specialty claims matter; classify_tag has no any-claim
    input at all (the spurious guard was removed)."""
    import inspect

    params = inspect.signature(classify_tag).parameters
    assert "has_any_claim_12mo" not in params


@pytest.mark.parametrize("specialty", [*sorted(ECONSULT_SIX), "Cardiology"])
def test_route_matches_spec(specialty):
    for has_claim in (False, True):
        care_path, _ = decide_route(specialty, has_claim, 1)
        assert care_path == spec_route(specialty, has_claim)


def test_existing_relationship_wins_over_everything():
    care_path, tag = decide_route("Cardiology", True, 5, True, True)
    assert care_path == "In-person"
    assert tag == "Existing Specialist Relationship"


def test_unsupported_specialty_is_rejected_not_econsulted():
    """A non-program specialty must raise, never silently default to eConsult."""
    with pytest.raises(ValueError):
        decide_route("Dermatology", False, 0)


def test_policy_drives_the_specialty_lists():
    """Routing reads the business-owned policy artifact, not constants."""
    from app.policy import policy

    pol = policy()
    assert pol.econsult_specialties == ECONSULT_SIX
    assert "Cardiology" in pol.virtual_specialties
    assert pol.policy_version != "unversioned"


def test_unsupported_requested_specialty_is_detected_not_swallowed():
    """A prompt asking for a specialty outside the policy must be detectable so
    extraction halts instead of silently triaging the stored referral order."""
    from app.extraction import _parse_specialty, _unsupported_specialty_mention

    text = "Triage the referral for patient MINT-0006 to Dermatology"
    assert _parse_specialty(text) is None
    assert _unsupported_specialty_mention(text) == "Dermatology"
    # In-policy requests are untouched by the guard.
    ok = "Triage patient X to Cardiology"
    assert _parse_specialty(ok) == "Cardiology"
    assert _unsupported_specialty_mention(ok) is None


def test_claim_window_is_policy_driven():
    """The In-person rule only counts claims inside the policy's look-back
    window — an aged target-specialty claim must not fire it."""
    import datetime as dt

    from app.claims_engine import claim_window_start

    cutoff = claim_window_start(dt.date(2026, 6, 10))
    assert cutoff == "2025-06-10"
    old_claim = "2025-05-28"
    fresh_claim = "2025-09-12"
    assert not (old_claim >= cutoff)
    assert fresh_claim >= cutoff


def test_score_to_tier_thresholds():
    """Customer feedback: Tier 1 > 75%, Tier 2 > 50%, Tier 3 > 25%, else 4."""
    from app.policy import policy

    pol = policy()
    assert pol.score_to_tier(90) == 1
    assert pol.score_to_tier(76) == 1
    assert pol.score_to_tier(75) == 2
    assert pol.score_to_tier(51) == 2
    assert pol.score_to_tier(50) == 3
    assert pol.score_to_tier(26) == 3
    assert pol.score_to_tier(25) == 4
    assert pol.score_to_tier(10) == 4
