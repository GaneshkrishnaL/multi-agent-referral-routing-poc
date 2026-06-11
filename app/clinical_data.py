# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Local clinical data access for the prototype.
Reads the synthetic dataset (Synthea-derived) from data/. This is the
USE_LOCAL_DATA path; in production these reads are replaced by the MCP layer
over BigQuery without changing the agents.
"""

from __future__ import annotations
import csv
import json
import math
import os
from pathlib import Path

from .policy import policy

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Backend toggle: USE_BIGQUERY=1 reads from the smart_care_triage BigQuery
# dataset (production path); otherwise from the local files (prototype path).
USE_BIGQUERY = os.getenv("USE_BIGQUERY", "0") == "1"

# Referral specialties come from the business-owned policy artifact
# (config/routing_policy.yaml), never from code constants.
ECONSULT_SPECIALTIES = policy().econsult_specialties
VIRTUAL_SPECIALTIES = policy().virtual_specialties
ROUTABLE_SPECIALTIES = policy().routable_specialties

# ---------------------------------------------------------------------------
# Lazy singletons (loaded once per process, like the reference agents)
# ---------------------------------------------------------------------------
_BUNDLES: dict | None = None
_SPECIALISTS: list[dict] | None = None


def _bundles() -> dict:
    global _BUNDLES
    if _BUNDLES is None:
        rows = json.loads((DATA_DIR / "patient_bundles.json").read_text())
        _BUNDLES = {b["patient"]["PatientId"]: b for b in rows}
    return _BUNDLES


def _specialists() -> list[dict]:
    global _SPECIALISTS
    if _SPECIALISTS is None:
        with open(DATA_DIR / "specialists.csv", newline="") as f:
            _SPECIALISTS = list(csv.DictReader(f))
    return _SPECIALISTS


def get_bundle(patient_id: str) -> dict | None:
    """Full per-patient record, or None if not found."""
    if USE_BIGQUERY:
        from . import bq_data

        return bq_data.get_bundle(patient_id)
    return _bundles().get(patient_id)


def list_patient_ids() -> list[str]:
    return list(_bundles().keys())


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 1)


def _tier_of(s: dict) -> int:
    """Quality tier derived from the performance score via the business-owned
    thresholds in the policy artifact (Tier 1 > 75%, Tier 2 > 50%, Tier 3 > 25%,
    else Tier 4). Falls back to the directory's precomputed Tier column."""
    score = s.get("PerformanceScore")
    if score not in (None, ""):
        return policy().score_to_tier(float(score))
    return int(s.get("Tier", 4))


def _match_entry(s: dict, lat: float, lon: float) -> dict:
    dist = haversine_miles(lat, lon, float(s["Lat"]), float(s["Lon"]))
    return {
        "SpecialistId": s["SpecialistId"],
        "Name": f"{s['FirstName']} {s['LastName']}",
        "Specialty": s["Specialty"],
        "Npi": s.get("Npi", ""),
        "Tier": _tier_of(s),
        "PerformanceScore": int(s["PerformanceScore"]),
        "DistanceMi": dist,
        "ClinicName": s["ClinicName"],
        "Internal": s.get("Internal", "No") == "Yes",
    }


def find_specialist_by_npi(npi: str, lat: float = 0.0, lon: float = 0.0) -> dict | None:
    """Resolves a claim's rendering provider NPI to a directory entry (used to
    recommend the patient's EXISTING specialist on the In-person continuity path)."""
    if not npi:
        return None
    if USE_BIGQUERY:
        from . import bq_data

        found = bq_data.find_specialist_by_npi(str(npi))
        return _match_entry(found, lat, lon) if found else None
    for s in _specialists():
        if str(s.get("Npi", "")) == str(npi):
            return _match_entry(s, lat, lon)
    return None


def rank_specialists(
    specialty: str,
    lat: float,
    lon: float,
    in_network_only: bool = True,
    top: int = 3,
    internal_only: bool = False,
) -> list[dict]:
    """Deterministic ranking: filter by specialty/network (and the internal
    internal pool when `internal_only`, used for Virtual visits), sort by
    tier asc then distance asc. This is the rules-based Specialist Matcher (no LLM)."""
    if USE_BIGQUERY:
        from . import bq_data

        return bq_data.rank_specialists(
            specialty, lat, lon, in_network_only, top, internal_only
        )
    out = []
    for s in _specialists():
        if s["Specialty"] != specialty:
            continue
        if in_network_only and s["Network"] != "In-Network":
            continue
        if internal_only and s.get("Internal", "No") != "Yes":
            continue
        out.append(_match_entry(s, lat, lon))
    out.sort(key=lambda s: (s["Tier"], s["DistanceMi"]))
    return out[:top]
