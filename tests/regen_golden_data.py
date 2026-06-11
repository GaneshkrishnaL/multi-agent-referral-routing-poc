# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Golden-data maintenance for the deterministic triage core.

Run after a routing-policy or directory change:

    python tests/regen_golden_data.py

What it does (idempotent):
1. specialists.csv: ensures the `Internal` column exists (In-Network
   cardiologists form the internal CenterWell virtual pool in this synthetic
   directory).
2. patient_bundles.json: makes claims joinable to the directory — every claim
   in a routable specialty gets the NPI/name of a real directory specialist.
   Deliberately the SECOND-ranked candidate, so the continuity expectation
   diverges from what plain tier/distance ranking would return: a regression
   that ignores the claim NPI and just ranks the directory FAILS the golden
   cases instead of passing by coincidence. Also seeds two truth-table
   coverage patients (MINT-0901 scheduled-only, MINT-0902 unrelated-claim-only)
   that the original dataset was missing.
3. answer_key.json: regenerates expectations with IntendedTag/ExpectedRoute
   derived from tests/spec_rules.py — the INDEPENDENT transcription of the
   customer's table — never from the application code. ComputedTag records
   what the app produced at regen time; the validator asserts they agree.

NOTE (production data path): the BigQuery smart_care_triage dataset mirrors
these local files. After running this script, reload the `specialists` and
`patient_bundles` tables (the Internal column, remapped claim NPIs, and the
two coverage patients must exist there too, or USE_BIGQUERY=1 runs will
diverge from the golden expectations).
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from spec_rules import (  # noqa: E402
    ECONSULT_SIX,
    VIRTUAL,
    spec_route,
    spec_signals,
    spec_tag,
)

DATA = ROOT / "data"
ROUTABLE = ECONSULT_SIX | {VIRTUAL}


# ---------------------------------------------------------------------------
# 1. Specialist directory: Internal pool column
# ---------------------------------------------------------------------------

def ensure_internal_column() -> list[dict]:
    path = DATA / "specialists.csv"
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["Internal"] = (
            "Yes"
            if r["Specialty"] == VIRTUAL and r["Network"] == "In-Network"
            else "No"
        )
    fields = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    n_int = sum(r["Internal"] == "Yes" for r in rows)
    print(f"specialists.csv: {len(rows)} rows, {n_int} internal (virtual pool)")
    return rows


# ---------------------------------------------------------------------------
# 2. Patient bundles: joinable claim NPIs + truth-table coverage patients
# ---------------------------------------------------------------------------

def _haversine(lat1, lon1, lat2, lon2) -> float:
    import math

    r = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 1)


def _continuity_target(specialists, specialty, lat, lon):
    """The directory specialist a routable claim is remapped onto.

    Picks the SECOND-ranked in-network candidate so the continuity pick is
    distinguishable from default ranking — an implementation that ignores the
    claim NPI cannot pass the golden cases by accident."""
    cands = [
        s
        for s in specialists
        if s["Specialty"] == specialty and s["Network"] == "In-Network"
    ]
    cands.sort(
        key=lambda s: (
            int(s["Tier"]),
            _haversine(lat, lon, float(s["Lat"]), float(s["Lon"])),
        )
    )
    if not cands:
        return None
    return cands[1] if len(cands) > 1 else cands[0]


def _coverage_bundles() -> list[dict]:
    """Two synthetic patients covering taxonomy cells absent from the dataset."""

    def base(pid, first, last, age, gender, specialty, problem, icd):
        return {
            "patient": {
                "PatientId": pid,
                "FirstName": first,
                "LastName": last,
                "Age": age,
                "Gender": gender,
                "DOB": f"{2026 - age}-03-15",
                "Address": "100 Synthetic Way",
                "City": "Miami",
                "State": "FL",
                "Zip": "33130",
                "Lat": 25.766,
                "Lon": -80.19,
                "MemberId": f"MEM-{pid}",
                "PolicyNumber": f"POL-{pid}",
                "PcpProviderId": "PCP-0001",
            },
            "referral_order": {
                "OrderId": f"ORD-{pid}",
                "PatientId": pid,
                "ProviderId": "PCP-0001",
                "OrderTypeId": 0,
                "OrderTypeName": specialty,
                "OrderSubType": "Consult",
                "Description": f"Referral to {specialty}",
                "Icd10Codes": [icd],
                "CptCodes": [],
                "SnomedCodes": [],
                "OrderDate": "2026-06-01",
                "CreatedDate": "2026-06-01",
                "IsReferralTrigger": True,
                "CompletionStatus": "Open",
                "StatUrgent": False,
            },
            "problems": [
                {"Description": problem, "Icd10": icd, "Specialty": specialty}
            ],
            "medications": [],
            "labs": {},
            "encounter_history": [],
            "recent_encounters": 0,
            "visits": [],
            "orders_history": [],
            "claims_12mo": [],
            "prior_referrals": [],
            "pcp": {"Name": "Dr. Casey Synth", "ProviderId": "PCP-0001"},
            "pcp_progress_note": (
                f"New member; no visits on record. PCP creating {specialty} "
                f"referral for {problem.lower()} based on intake review."
            ),
            "has_checked_out_appt": False,
            "appt_scheduled_next_month": False,
        }

    # Cell: no encounters, NO checked-out appt, appt scheduled next month
    # -> spec tag: New Patient - Needs first visit
    b1 = base(
        "MINT-0901", "Nora", "Fields", 58, "F", "Endocrinology",
        "Type 2 diabetes mellitus", "E11.9",
    )
    b1["appt_scheduled_next_month"] = True
    b1["labs"] = {
        "HbA1c": [
            {"date": "2026-04-02", "value": "8.1", "units": "%"},
            {"date": "2026-05-28", "value": "8.4", "units": "%"},
        ]
    }
    b1["medications"] = ["Metformin 500mg BID"]

    # Cell: no encounters, no appts at all, but ONE unrelated (non-target)
    # claim -> spec tag: Unengaged Patient - Needs first visit
    b2 = base(
        "MINT-0902", "Omar", "Reyes", 64, "M", "Pulmonology",
        "Chronic obstructive pulmonary disease", "J44.9",
    )
    b2["claims_12mo"] = [
        {
            "ClaimId": "CLM-MINT-0902-01",
            "PatientId": "MINT-0902",
            "MemberId": "MEM-MINT-0902",
            "Specialty": "Family Medicine",
            "CptCode": "99213",
            "Icd10Codes": ["Z00.00"],
            "ServiceDateFrom": "2025-09-12",
            "ServiceDateTo": "2025-09-12",
            "AdmissionDate": "",
            "DischargeDate": "",
            "RenderingProviderName": "Walk-in Clinic Provider",
            "RenderingProviderNpi": "1999999901",
            "RenderingProviderSpecialtyCode": "08",
            "ProviderTaxonomyCode": "207Q00000X",
        }
    ]
    return [b1, b2]


def fix_bundles(specialists) -> list[dict]:
    path = DATA / "patient_bundles.json"
    bundles = json.loads(path.read_text())
    ids = {b["patient"]["PatientId"] for b in bundles}

    # 2a. Remap routable-specialty claim providers onto real directory rows so
    # the continuity join (claim NPI -> directory) resolves — deliberately NOT
    # the rank-1 candidate (see _continuity_target).
    remapped = 0
    for b in bundles:
        pat = b.get("patient", {})
        lat, lon = float(pat.get("Lat", 0)), float(pat.get("Lon", 0))
        for c in b.get("claims_12mo", []) or []:
            if c.get("Specialty") in ROUTABLE:
                s = _continuity_target(specialists, c["Specialty"], lat, lon)
                if s:
                    c["RenderingProviderNpi"] = s["Npi"]
                    c["RenderingProviderName"] = f"{s['FirstName']} {s['LastName']}"
                    remapped += 1

    # 2b. Seed truth-table coverage patients (idempotent upsert so schema
    # improvements to the synthetic bundles propagate on re-run).
    added = 0
    for nb in _coverage_bundles():
        pid = nb["patient"]["PatientId"]
        if pid in ids:
            bundles = [b for b in bundles if b["patient"]["PatientId"] != pid]
        else:
            added += 1
        bundles.append(nb)

    path.write_text(json.dumps(bundles, indent=1))
    print(f"patient_bundles.json: {remapped} claims remapped, {added} patients added")
    return bundles


# ---------------------------------------------------------------------------
# 3. Answer key: expectations derived from the SPEC, not the code
# ---------------------------------------------------------------------------

_NEW_CASES = {
    "MINT-0901": {
        "Key": "tag-new-needs-first-visit",
        "PriorReferral": "None",
        "ExpectedQuestionTheme": "Initial T2DM management plan pending first visit",
        "ExpectedVisitCadence": "4 visits/year",
        "Notes": (
            "Coverage case for the taxonomy cell the original dataset missed: "
            "no encounters, no checked-out appointment, appointment scheduled "
            "within the next month -> 'New Patient - Needs first visit' per "
            "the customer table."
        ),
    },
    "MINT-0902": {
        "Key": "tag-unengaged-unrelated-claim",
        "PriorReferral": "None",
        "ExpectedQuestionTheme": "COPD severity staging and inhaler optimization",
        "ExpectedVisitCadence": "2 visits/year",
        "Notes": (
            "Coverage case: a single NON-target-specialty claim (Family "
            "Medicine) must NOT change the tag — spec conditions only count "
            "claims to the target specialty -> 'Unengaged Patient - Needs "
            "first visit'."
        ),
    },
}


def regen_answer_key(bundles) -> None:
    # The app's deterministic engines are imported ONLY to record ComputedTag /
    # specialist projections. Intended* fields come from spec_rules.
    from app import clinical_data as cd
    from app.routing import decide_route

    by_id = {b["patient"]["PatientId"]: b for b in bundles}
    path = DATA / "answer_key.json"
    ak = json.loads(path.read_text())
    keyed = {r["PatientId"]: r for r in ak}

    # Ensure rows exist for the coverage patients.
    for pid, extra in _NEW_CASES.items():
        if pid not in keyed:
            row = {"OrderId": f"ORD-{pid}", "PatientId": pid}
            row.update(extra)
            ak.append(row)
            keyed[pid] = row

    for r in ak:
        pid = r["PatientId"]
        b = by_id[pid]
        specialty = (b.get("referral_order") or {}).get("OrderTypeName", "")
        sig = spec_signals(b, specialty)

        # --- Spec-derived truth (independent of app code) ---
        intended_tag = spec_tag(**sig)
        expected_route = spec_route(specialty, sig["has_specialty_claim"])

        # --- App-computed values, recorded for drift detection ---
        _, computed_tag = decide_route(
            specialty,
            sig["has_specialty_claim"],
            sig["encounters"],
            sig["has_checked_out"],
            sig["scheduled_next_month"],
        )

        # --- Specialist expectation per the continuity/virtual/eConsult contract ---
        pat = b.get("patient", {})
        lat, lon = float(pat.get("Lat", 0)), float(pat.get("Lon", 0))
        if expected_route == "In-person":
            from spec_rules import spec_twelve_months_ago

            cutoff = spec_twelve_months_ago()
            claims = [
                c
                for c in (b.get("claims_12mo") or [])
                if c.get("Specialty") == specialty
                and (c.get("ServiceDateFrom") or "9999") >= cutoff
            ]
            latest = max(claims, key=lambda c: c.get("ServiceDateFrom", ""))
            found = cd.find_specialist_by_npi(
                latest.get("RenderingProviderNpi", ""), lat, lon
            )
            matches = [found] if found else cd.rank_specialists(specialty, lat, lon)
            continuity_unresolved = found is None
        elif expected_route == "Virtual":
            matches = cd.rank_specialists(specialty, lat, lon, internal_only=True)
            continuity_unresolved = False
        else:
            matches = cd.rank_specialists(specialty, lat, lon)
            continuity_unresolved = False

        urgent = bool((b.get("referral_order") or {}).get("StatUrgent"))
        if urgent:
            conf = "REVIEW (route to clinical reviewer)"
        elif continuity_unresolved:
            conf = "REVIEW (route to clinical reviewer)"
        elif expected_route in ("eConsult", "Virtual") and not matches:
            conf = "LOW (route to clinical reviewer)"
        else:
            conf = "HIGH (route to PCP)"

        top = matches[0] if matches else None
        r.update(
            {
                "Specialty": specialty,
                "IntendedTag": intended_tag,
                "ComputedTag": computed_tag,
                "TagMatches": intended_tag == computed_tag,
                "HasSpecialtyClaim12mo": sig["has_specialty_claim"],
                "ExpectedRoute": expected_route,
                "ExpectedSpecialistId": top["SpecialistId"] if top else None,
                "ExpectedSpecialistName": top["Name"] if top else None,
                "ExpectedTier": top["Tier"] if top else None,
                "ExpectedDistanceMi": top["DistanceMi"] if top else None,
                "ExpectedTop3": [
                    f"{m['SpecialistId']} (T{m['Tier']}, {m['DistanceMi']}mi)"
                    for m in matches[:3]
                ],
                "Urgent": urgent,
                "ExpectedConfidence": conf,
            }
        )

    path.write_text(json.dumps(ak, indent=1))
    n_bad = sum(not r["TagMatches"] for r in ak)
    print(f"answer_key.json: {len(ak)} cases regenerated, {n_bad} tag divergences")
    if n_bad:
        for r in ak:
            if not r["TagMatches"]:
                print(
                    f"  DIVERGENCE {r['Key']}: spec={r['IntendedTag']} "
                    f"code={r['ComputedTag']}"
                )


if __name__ == "__main__":
    specialists = ensure_internal_column()
    bundles = fix_bundles(specialists)
    regen_answer_key(bundles)
