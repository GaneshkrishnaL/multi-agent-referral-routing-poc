# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""INDEPENDENT transcription of the customer's routing spec (requirements doc).

This module intentionally does NOT import app code or the policy config: it is
the reference the implementation is graded against. If app.routing and this
file ever disagree, the validator fails loudly — that is the point. Sources:
the corrected four-tag taxonomy table and the care-path decision flow from the
CenterWell requirements document ("procedure code" corrected to "specialty").
"""

from __future__ import annotations

# The six eConsult specialties + the cardiology virtual branch, verbatim from
# the requirements doc (NOT read from config — this is the reference).
ECONSULT_SIX = frozenset(
    {
        "Endocrinology",
        "Nephrology",
        "Rheumatology",
        "Neurology",
        "Hematology",
        "Pulmonology",
    }
)
VIRTUAL = "Cardiology"

TAG_EXISTING = "Existing Specialist Relationship"
TAG_ESTABLISHED = "Established Patient"
TAG_NEW = "New Patient"
TAG_NEW_NEEDS_FIRST = "New Patient - Needs first visit"
TAG_UNENGAGED = "Unengaged Patient - Needs first visit"


def spec_tag(
    has_specialty_claim: bool,
    encounters: int,
    has_checked_out: bool,
    scheduled_next_month: bool,
) -> str:
    """The customer's decision tree, verbatim (requirements doc p.13):

    (precondition: no specialty claim in last 12 months)
    if patient has >= 1 recorded encounter:                tag = Established Patient
    elif patient has a checked-out appointment:            tag = New Patient
    elif appointment scheduled in the next 1 month:        tag = New Patient - Needs first visit
    else:                                                  tag = Unengaged Patient - Needs first visit
    """
    if has_specialty_claim:
        return TAG_EXISTING
    if encounters >= 1:
        return TAG_ESTABLISHED
    if has_checked_out:
        return TAG_NEW
    if scheduled_next_month:
        return TAG_NEW_NEEDS_FIRST
    return TAG_UNENGAGED


def spec_route(specialty: str, has_specialty_claim: bool) -> str:
    """The customer's decision flow (first rule wins):

    Step 1: any claim to the target specialty in last 12 months -> In-person.
    Step 3: Cardiology -> Virtual; the six eConsult specialties -> eConsult.
    """
    if has_specialty_claim:
        return "In-person"
    if specialty == VIRTUAL:
        return "Virtual"
    if specialty in ECONSULT_SIX:
        return "eConsult"
    raise ValueError(f"specialty {specialty!r} is outside the documented program")


def spec_twelve_months_ago(today=None) -> str:
    """ISO date 12 months before today — the doc's claim look-back window."""
    import datetime as _dt

    today = today or _dt.date.today()
    y, m = divmod((today.year * 12 + today.month - 1) - 12, 12)
    return _dt.date(y, m + 1, min(today.day, 28)).isoformat()


def spec_signals(bundle: dict, specialty: str) -> dict:
    """Raw routing inputs read straight off a patient bundle (no app code).

    The doc's rule is claims to the target specialty "in the last 1 year" —
    enforced by date here, not by trusting the bundle field's name.
    """
    cutoff = spec_twelve_months_ago()
    claims = bundle.get("claims_12mo", []) or []
    return {
        "has_specialty_claim": any(
            c.get("Specialty") == specialty
            and (c.get("ServiceDateFrom") or "9999") >= cutoff
            for c in claims
        ),
        "encounters": int(bundle.get("recent_encounters", 0) or 0),
        "has_checked_out": bool(bundle.get("has_checked_out_appt")),
        "scheduled_next_month": bool(bundle.get("appt_scheduled_next_month")),
    }
