# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Deterministic accuracy harness for the triage core.

Validates the load-bearing decisions (care path, patient tag, specialist match,
confidence) without running the LLMs, so it is fast and exact:

  - SPEC CROSS-CHECK: every golden case's tag and route are recomputed from an
    INDEPENDENT transcription of the customer's rules (tests/spec_rules.py) and
    diffed against the application engines. The app is graded against the spec,
    never against its own output.
  - GOLDEN: every answer_key case is recomputed and diffed against the expected
    route/tag/specialist/confidence/top3. ALL fields count toward failure —
    a tag or confidence divergence fails the run.
  - INVARIANTS: a sweep of patient x specialty combinations checks structural
    rules that must always hold (route matches the specialty class, the matched
    specialist is in-network and of the right specialty, ranked by tier then
    distance, In-person recommends the patient's EXISTING specialist from
    claims, Virtual recommends only internal CenterWell specialists).

Usage:
  python tests/validate_routing.py                 # spec + golden + invariants
  python tests/validate_routing.py <patient_id> [specialty]   # one ad-hoc case
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from spec_rules import spec_route, spec_signals, spec_tag  # noqa: E402

from app import clinical_data as cd  # noqa: E402
from app.routing import decide_route  # noqa: E402

DATA = ROOT / "data"
ROUTABLE = cd.ROUTABLE_SPECIALTIES


def decide(patient_id: str, specialty: str | None = None) -> dict:
    """Recompute the deterministic decision exactly as the graph nodes do
    (claims_engine -> routing -> specialist_matcher), for one referral."""
    b = cd.get_bundle(patient_id)
    if not b:
        return {"error": f"no patient {patient_id}"}
    ref = b.get("referral_order") or {}
    specialty = specialty or ref.get("OrderTypeName", "")
    if not specialty:
        return {"error": "no specialty (stored or supplied)"}

    from app.claims_engine import claim_window_start

    claims = b.get("claims_12mo", []) or []
    recent = b.get("recent_encounters", 0) or 0
    cutoff = claim_window_start()  # claims_engine windowing
    matched = [
        c
        for c in claims
        if c.get("Specialty") == specialty
        and (c.get("ServiceDateFrom") or "9999") >= cutoff
    ]
    has_claim = bool(matched)
    latest = max(matched, key=lambda c: c.get("ServiceDateFrom", ""), default=None) or {}
    has_checked_out = bool(b.get("has_checked_out_appt"))
    scheduled_next_month = bool(b.get("appt_scheduled_next_month"))
    try:
        care_path, tag = decide_route(  # routing
            specialty,
            has_claim,
            recent,
            has_checked_out,
            scheduled_next_month,
        )
    except ValueError as exc:
        return {"error": str(exc)}

    pat = b.get("patient", {})
    lat, lon = float(pat.get("Lat", 0)), float(pat.get("Lon", 0))
    # SpecialistMatcher: continuity for In-person, internal pool for Virtual,
    # tier-then-distance directory ranking for eConsult.
    continuity_unresolved = False
    if care_path == "In-person":
        existing = cd.find_specialist_by_npi(
            latest.get("RenderingProviderNpi") or "", lat, lon
        )
        if existing:
            matches = [{**existing, "Continuity": True}]
        else:
            continuity_unresolved = True
            matches = cd.rank_specialists(specialty, lat, lon)
    elif care_path == "Virtual":
        matches = cd.rank_specialists(specialty, lat, lon, internal_only=True)
    else:
        matches = cd.rank_specialists(specialty, lat, lon)

    urgent = bool(ref.get("StatUrgent"))
    if urgent:
        conf = "REVIEW"
    elif continuity_unresolved:
        conf = "REVIEW"
    elif care_path in ("eConsult", "Virtual") and not matches:
        conf = "LOW"
    else:
        conf = "HIGH"
    return {
        "specialty": specialty,
        "care_path": care_path,
        "tag": tag,
        "specialist": matches[0]["SpecialistId"] if matches else None,
        "top3": [m["SpecialistId"] for m in matches[:3]],
        "confidence": conf,
        "matches": matches,
        "existing_npi": latest.get("RenderingProviderNpi"),
    }


def _conf_word(s: str) -> str:
    return (s or "").split()[0].upper() if s else ""


def _ids(top3_strs) -> list[str]:
    import re

    out = []
    for s in top3_strs or []:
        m = re.match(r"(SPEC-\d+)", s)
        if m:
            out.append(m.group(1))
    return out


def validate_spec_crosscheck() -> int:
    """Grade the app engines against the INDEPENDENT spec transcription."""
    ak = json.loads((DATA / "answer_key.json").read_text())
    fails = 0
    for r in ak:
        b = cd.get_bundle(r["PatientId"])
        specialty = (b.get("referral_order") or {}).get("OrderTypeName", "")
        sig = spec_signals(b, specialty)
        want_tag = spec_tag(**sig)
        want_route = spec_route(specialty, sig["has_specialty_claim"])
        g = decide(r["PatientId"])
        if g.get("tag") != want_tag:
            fails += 1
            print(f"  SPEC-TAG {r['Key']}: app={g.get('tag')} spec={want_tag}")
        if g.get("care_path") != want_route:
            fails += 1
            print(
                f"  SPEC-ROUTE {r['Key']}: app={g.get('care_path')} spec={want_route}"
            )
    n = len(ak)
    print(f"SPEC CROSS-CHECK ({n} cases): {n * 2 - fails}/{n * 2} assertions clean")
    return fails


def validate_golden() -> int:
    """Diff every field against the answer key. ALL divergences are failures."""
    ak = json.loads((DATA / "answer_key.json").read_text())
    fields = ("route", "tag", "specialist", "top3", "confidence")
    hits = dict.fromkeys(fields, 0)
    diffs = {f: [] for f in fields}
    for r in ak:
        g = decide(r["PatientId"])  # stored referral
        got = {
            "route": g.get("care_path"),
            "specialist": g.get("specialist"),
            "top3": g.get("top3"),
            "confidence": _conf_word(g.get("confidence")),
            # Graded against the spec-derived IntendedTag — never ComputedTag.
            "tag": g.get("tag"),
        }
        exp = {
            "route": r.get("ExpectedRoute"),
            "specialist": r.get("ExpectedSpecialistId") or None,
            "top3": _ids(r.get("ExpectedTop3")),
            "confidence": _conf_word(r.get("ExpectedConfidence")),
            "tag": r.get("IntendedTag"),
        }
        for f in fields:
            if got[f] == exp[f]:
                hits[f] += 1
            else:
                diffs[f].append((r.get("Key"), got[f], exp[f]))
    n = len(ak)
    print(f"GOLDEN ({n} cases) per-field match vs answer key:")
    for f in fields:
        print(f"  {f:11} {hits[f]}/{n}")
    fail = sum(len(diffs[f]) for f in fields)
    for f in fields:
        for key, got, exp in diffs[f]:
            print(f"    [{f}] {key}: got={got} exp={exp}")
    return fail


def validate_invariants(per_specialty: int = 8) -> int:
    """Sweep patients x specialties and assert rules that must always hold."""
    ids = cd.list_patient_ids()[:60]
    violations = []
    n = 0
    for sp in sorted(ROUTABLE):
        for pid in ids[:per_specialty]:
            g = decide(pid, sp)
            if "error" in g:
                continue
            n += 1
            ms = g["matches"]
            # route class: In-person only with an existing relationship;
            # virtual-designated specialties are Virtual; the rest eConsult.
            if g["care_path"] == "In-person":
                # Continuity: the recommendation must be the claim's provider
                # whenever the directory can resolve it.
                if ms and ms[0].get("Continuity"):
                    if str(ms[0].get("Npi", "")) != str(g.get("existing_npi", "")):
                        violations.append(
                            (pid, sp, "continuity pick is not the claim provider")
                        )
                elif g["confidence"] != "REVIEW" and not bool(
                    (cd.get_bundle(pid).get("referral_order") or {}).get("StatUrgent")
                ):
                    violations.append(
                        (pid, sp, "unresolved continuity must flag REVIEW")
                    )
            elif sp in cd.VIRTUAL_SPECIALTIES:
                if g["care_path"] != "Virtual":
                    violations.append((pid, sp, f"expected Virtual, got {g['care_path']}"))
                if any(not m.get("Internal") for m in ms):
                    violations.append((pid, sp, "virtual match is not internal"))
            elif sp in cd.ECONSULT_SPECIALTIES and g["care_path"] != "eConsult":
                violations.append((pid, sp, f"expected eConsult, got {g['care_path']}"))
            # specialist sanity: right specialty, tier-then-distance order
            if any(m["Specialty"] != sp for m in ms):
                violations.append((pid, sp, "matched wrong specialty"))
            if not (ms and ms[0].get("Continuity")):
                keys = [(m["Tier"], m["DistanceMi"]) for m in ms]
                if keys != sorted(keys):
                    violations.append(
                        (pid, sp, "matches not sorted by tier then distance")
                    )
    print(f"INVARIANTS: {n - len(violations)}/{n} combos clean")
    for pid, sp, msg in violations[:20]:
        print(f"  VIOLATION {pid} [{sp}]: {msg}")
    return len(violations)


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        pid = sys.argv[1]
        sp = sys.argv[2] if len(sys.argv) > 2 else None
        print(
            json.dumps(
                {k: v for k, v in decide(pid, sp).items() if k != "matches"}, indent=2
            )
        )
    else:
        f0 = validate_spec_crosscheck()
        f1 = validate_golden()
        f2 = validate_invariants()
        print(
            "\nRESULT:",
            "ALL PASS"
            if f0 == f1 == f2 == 0
            else f"{f0} spec + {f1} golden + {f2} invariant failures",
        )
        sys.exit(1 if (f0 or f1 or f2) else 0)
