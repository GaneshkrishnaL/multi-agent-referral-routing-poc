# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""BigQuery-backed data source (the production data path).

Same shapes as the local clinical_data reads, sourced from the
smart_care_triage BigQuery dataset. Selected via USE_BIGQUERY=1.
"""

from __future__ import annotations

import functools
import json
import math
import os

PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "gem-ent-492719")
DATASET = os.getenv("BQ_DATASET", "smart_care_triage")


@functools.lru_cache(maxsize=1)
def _client():
    from google.cloud import bigquery  # lazy import

    return bigquery.Client(project=PROJECT)


def _haversine(lat1, lon1, lat2, lon2) -> float:
    r = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 1)


def get_bundle(patient_id: str) -> dict | None:
    """Patient Chart MCP equivalent: the consolidated 24-month chart context."""
    from google.cloud import bigquery

    sql = (
        f"SELECT bundle_json FROM `{PROJECT}.{DATASET}.patient_bundles` "
        "WHERE PatientId=@pid LIMIT 1"
    )
    job = _client().query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("pid", "STRING", patient_id)
            ]
        ),
    )
    for row in job:
        return json.loads(row["bundle_json"])
    return None


def find_specialist_by_npi(npi: str) -> dict | None:
    """Specialist Directory MCP equivalent: resolve a claim's rendering provider
    NPI to a directory row (continuity lookup for the In-person path)."""
    from google.cloud import bigquery

    sql = (
        f"SELECT * FROM `{PROJECT}.{DATASET}.specialists` "
        "WHERE CAST(Npi AS STRING)=@npi LIMIT 1"
    )
    job = _client().query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("npi", "STRING", str(npi))]
        ),
    )
    for row in job:
        return dict(row)
    return None


def rank_specialists(
    specialty: str,
    lat: float,
    lon: float,
    in_network_only: bool = True,
    top: int = 3,
    internal_only: bool = False,
) -> list[dict]:
    """Specialist Directory MCP equivalent: filter by specialty/network (and
    the internal specialist pool for Virtual visits), then rank by tier
    ascending, distance ascending (computed from geography)."""
    from google.cloud import bigquery

    sql = (
        f"SELECT SpecialistId, FirstName, LastName, Specialty, Npi, Tier, "
        f"PerformanceScore, Network, ClinicName, Internal, Lat, Lon "
        f"FROM `{PROJECT}.{DATASET}.specialists` WHERE Specialty=@sp"
    )
    if in_network_only:
        sql += " AND Network='In-Network'"
    if internal_only:
        sql += " AND Internal='Yes'"
    job = _client().query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("sp", "STRING", specialty)]
        ),
    )
    # Tier comes from the business-owned score thresholds (same as the local
    # path) so a policy edit applies to both backends identically.
    from .policy import policy

    out = []
    for s in job:
        score = s["PerformanceScore"]
        out.append(
            {
                "SpecialistId": s["SpecialistId"],
                "Name": f"{s['FirstName']} {s['LastName']}",
                "Specialty": s["Specialty"],
                "Npi": str(s["Npi"]) if s["Npi"] is not None else "",
                "Tier": policy().score_to_tier(float(score))
                if score is not None
                else int(s["Tier"]),
                "PerformanceScore": int(score),
                "DistanceMi": _haversine(lat, lon, float(s["Lat"]), float(s["Lon"])),
                "ClinicName": s["ClinicName"],
                "Internal": s["Internal"] == "Yes",
            }
        )
    out.sort(key=lambda s: (s["Tier"], s["DistanceMi"]))
    return out[:top]
