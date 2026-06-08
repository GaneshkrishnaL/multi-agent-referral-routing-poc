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


def rank_specialists(
    specialty: str, lat: float, lon: float, in_network_only: bool = True, top: int = 3
) -> list[dict]:
    """Specialist Directory MCP equivalent: filter by specialty/network, then
    rank by tier ascending, distance ascending (computed from geography)."""
    from google.cloud import bigquery

    sql = (
        f"SELECT SpecialistId, FirstName, LastName, Specialty, Tier, "
        f"PerformanceScore, Network, ClinicName, Lat, Lon "
        f"FROM `{PROJECT}.{DATASET}.specialists` WHERE Specialty=@sp"
    )
    if in_network_only:
        sql += " AND Network='In-Network'"
    job = _client().query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("sp", "STRING", specialty)]
        ),
    )
    out = []
    for s in job:
        out.append(
            {
                "SpecialistId": s["SpecialistId"],
                "Name": f"{s['FirstName']} {s['LastName']}",
                "Specialty": s["Specialty"],
                "Tier": int(s["Tier"]),
                "PerformanceScore": int(s["PerformanceScore"]),
                "DistanceMi": _haversine(lat, lon, float(s["Lat"]), float(s["Lon"])),
                "ClinicName": s["ClinicName"],
            }
        )
    out.sort(key=lambda s: (s["Tier"], s["DistanceMi"]))
    return out[:top]
