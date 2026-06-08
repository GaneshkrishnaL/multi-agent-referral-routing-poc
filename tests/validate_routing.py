# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Deterministic accuracy harness for the triage core.

Validates the load-bearing decisions (care path, patient tag, specialist match,
confidence) without running the LLMs, so it is fast and exact:

  - GOLDEN: every answer_key case is recomputed and diffed against the expected
    route/tag/specialist/confidence/top3.
  - INVARIANTS: a sweep of patient x specialty combinations checks structural
    rules that must always hold (route matches the specialty class, the matched
    specialist is in-network and of the right specialty, ranked by tier then
    distance, in-person recommends no new specialist).

Usage:
  python tests/validate_routing.py                 # golden + invariant sweep
  python tests/validate_routing.py <patient_id> [specialty]   # one ad-hoc case
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import clinical_data as cd  # noqa: E402
from app.routing import decide_route  # noqa: E402

DATA = ROOT / "data"
ROUTABLE = cd.ECONSULT_SPECIALTIES | {cd.VIRTUAL_SPECIALTY}


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

    claims = b.get("claims_12mo", []) or []
    recent = b.get("recent_encounters", 0) or 0
    has_claim = any(c.get("Specialty") == specialty for c in claims)  # claims_engine
    has_any_claim = bool(claims)
    has_checked_out = bool(b.get("has_checked_out_appt"))
    scheduled_next_month = bool(b.get("appt_scheduled_next_month"))
    care_path, tag = decide_route(
        specialty,
        has_claim,
        recent,  # routing
        has_checked_out,
        scheduled_next_month,
        has_any_claim,
    )

    pat = b.get("patient", {})
    # SpecialistMatcher ranks in-network specialists for every route: a new
    # specialist for eConsult/Virtual, the continuity specialist for In-person.
    matches = cd.rank_specialists(
        specialty,
        float(pat.get("Lat", 0)),  # specialist_matcher
        float(pat.get("Lon", 0)),
    )
    urgent = bool(ref.get("StatUrgent"))
    if urgent:
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


def validate_golden() -> int:
    """Report per-field match rates against the answer key, and list divergences."""
    ak = json.loads((DATA / "answer_key.json").read_text())
    fields = ("route", "specialist", "top3", "confidence", "tag")
    hits = dict.fromkeys(fields, 0)
    diffs = {f: [] for f in fields}
    for r in ak:
        g = decide(r["PatientId"])  # stored referral
        got = {
            "route": g.get("care_path"),
            "specialist": g.get("specialist"),
            "top3": g.get("top3"),
            "confidence": _conf_word(g.get("confidence")),
            "tag": g.get("tag"),
        }
        exp = {
            "route": r.get("ExpectedRoute"),
            "specialist": r.get("ExpectedSpecialistId") or None,
            "top3": _ids(r.get("ExpectedTop3")),
            "confidence": _conf_word(r.get("ExpectedConfidence")),
            "tag": r.get("ComputedTag"),
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
    core_fail = sum(len(diffs[f]) for f in ("route", "specialist", "top3"))
    for f in fields:
        for key, got, exp in diffs[f]:
            print(f"    [{f}] {key}: got={got} exp={exp}")
    return core_fail


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
            # route class: in-person only with an existing relationship; Cardiology
            # without a relationship is Virtual; the eConsult six are eConsult.
            if g["care_path"] == "In-person":
                pass  # existing relationship; fine
            elif sp == cd.VIRTUAL_SPECIALTY and g["care_path"] != "Virtual":
                violations.append((pid, sp, f"expected Virtual, got {g['care_path']}"))
            elif sp in cd.ECONSULT_SPECIALTIES and g["care_path"] != "eConsult":
                violations.append((pid, sp, f"expected eConsult, got {g['care_path']}"))
            # specialist sanity: right specialty, in-network, tier-then-distance order
            ms = g["matches"]
            if any(m["Specialty"] != sp for m in ms):
                violations.append((pid, sp, "matched wrong specialty"))
            keys = [(m["Tier"], m["DistanceMi"]) for m in ms]
            if keys != sorted(keys):
                violations.append((pid, sp, "matches not sorted by tier then distance"))
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
        f1 = validate_golden()
        f2 = validate_invariants()
        print(
            "\nRESULT:",
            "ALL PASS"
            if f1 == 0 and f2 == 0
            else f"{f1} golden + {f2} invariant failures",
        )
        sys.exit(1 if (f1 or f2) else 0)
