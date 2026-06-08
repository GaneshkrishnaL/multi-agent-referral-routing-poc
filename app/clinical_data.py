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

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Backend toggle: USE_BIGQUERY=1 reads from the smart_care_triage BigQuery
# dataset (production path); otherwise from the local files (prototype path).
USE_BIGQUERY = os.getenv("USE_BIGQUERY", "0") == "1"

# Referral specialties (customer policy)
ECONSULT_SPECIALTIES = {
    "Nephrology",
    "Endocrinology",
    "Rheumatology",
    "Neurology",
    "Hematology",
    "Pulmonology",
}
VIRTUAL_SPECIALTY = "Cardiology"

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


def rank_specialists(
    specialty: str, lat: float, lon: float, in_network_only: bool = True, top: int = 3
) -> list[dict]:
    """Deterministic ranking: filter by specialty/network, sort by tier asc then
    distance asc. This is the rules-based Specialist Matcher (no LLM)."""
    if USE_BIGQUERY:
        from . import bq_data

        return bq_data.rank_specialists(specialty, lat, lon, in_network_only, top)
    out = []
    for s in _specialists():
        if s["Specialty"] != specialty:
            continue
        if in_network_only and s["Network"] != "In-Network":
            continue
        dist = haversine_miles(lat, lon, float(s["Lat"]), float(s["Lon"]))
        out.append(
            {
                "SpecialistId": s["SpecialistId"],
                "Name": f"{s['FirstName']} {s['LastName']}",
                "Specialty": s["Specialty"],
                "Tier": int(s["Tier"]),
                "PerformanceScore": int(s["PerformanceScore"]),
                "DistanceMi": dist,
                "ClinicName": s["ClinicName"],
            }
        )
    out.sort(key=lambda s: (s["Tier"], s["DistanceMi"]))
    return out[:top]
