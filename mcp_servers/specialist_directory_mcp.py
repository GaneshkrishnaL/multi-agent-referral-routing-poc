# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Specialist Directory MCP Server.

This module implements a standard Stdio-based Model Context Protocol (MCP) server 
using the FastMCP framework. It exposes tools to locate, query, and rank 
in-network specialists by tier and distance from the patient.

Calculations:
- Haversine formula is used to calculate geodesic distance (in miles) between 
  the patient and specialists' physical clinic coordinates.
- Specialist ranking criteria: Tier ascending, then Distance ascending.
"""

import json
import math
import os

from mcp.server.fastmcp import FastMCP

PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "gem-ent-492719")
DATASET = os.getenv("BQ_DATASET", "smart_care_triage")

# Instantiate the Specialist Directory FastMCP server
mcp = FastMCP("specialist_directory")

_client = None


def _bq():
    """Helper to lazily instantiate and cache the BigQuery client."""
    global _client
    if _client is None:
        from google.cloud import bigquery
        _client = bigquery.Client(project=PROJECT)
    return _client


def _haversine(lat1, lon1, lat2, lon2):
    """Calculates the geodesic distance between two points in miles using the Haversine formula."""
    r = 3958.7613  # Radius of Earth in miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 1)


def _rows(specialty: str, in_network_only: bool):
    """Queries specialists matching the specified medical specialty.

    USE_BIGQUERY=1 -> BigQuery (production); otherwise the local synthetic
    directory — same shape either way."""
    from local_data import USE_BIGQUERY, local_specialists

    if not USE_BIGQUERY:
        return [
            s
            for s in local_specialists()
            if s["Specialty"] == specialty
            and (not in_network_only or s["Network"] == "In-Network")
        ]
    from google.cloud import bigquery

    sql = (
        f"SELECT SpecialistId, FirstName, LastName, Specialty, Npi, Tier, "
        f"PerformanceScore, Network, ClinicName, Internal, Lat, Lon "
        f"FROM `{PROJECT}.{DATASET}.specialists` WHERE Specialty=@sp"
    )
    if in_network_only:
        sql += " AND Network='In-Network'"
    job = _bq().query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("sp", "STRING", specialty)]
        ),
    )
    return [dict(r) for r in job]


# =====================================================================
# MCP Server Tool Registrations
# =====================================================================

@mcp.tool(annotations={"title": "List specialists (raw directory rows)", "readOnlyHint": True})
def list_specialists(
    specialty: str, in_network_only: bool = True, internal_only: bool = False
) -> str:
    """Returns raw directory rows for a specialty: id, name, NPI, performance
    score, stored tier, network status, internal (CenterWell virtual pool)
    flag, clinic, and coordinates.

    This is the DATA-plane tool: ranking, tier thresholds, and distance policy
    are applied by the deterministic decision layer, not by this server.

    Args:
        specialty: The specialty medical field, e.g., Endocrinology.
        in_network_only: Restrict to in-network specialists (default true).
        internal_only: Restrict to the internal CenterWell pool (virtual visits).
    """
    rows = [
        s
        for s in _rows(specialty, in_network_only)
        if not internal_only or s.get("Internal", "No") == "Yes"
    ]
    return json.dumps(rows)


@mcp.tool(annotations={"title": "Find specialist by NPI", "readOnlyHint": True})
def find_specialist_by_npi(npi: str) -> str:
    """Resolves a claim's rendering provider NPI to a raw directory row — the
    continuity lookup for the In-person path ('do I have a claim with THIS
    specialist').

    Args:
        npi: The provider's National Provider Identifier from the claim.
    """
    from local_data import USE_BIGQUERY, local_specialists

    if not USE_BIGQUERY:
        for s in local_specialists():
            if str(s.get("Npi", "")) == str(npi):
                return json.dumps(s)
        return json.dumps(None)
    from google.cloud import bigquery

    sql = (
        f"SELECT * FROM `{PROJECT}.{DATASET}.specialists` "
        "WHERE CAST(Npi AS STRING)=@npi LIMIT 1"
    )
    job = _bq().query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("npi", "STRING", str(npi))]
        ),
    )
    for row in job:
        return json.dumps(dict(row))
    return json.dumps(None)

@mcp.tool(annotations={"title": "Rank specialists", "readOnlyHint": True})
def rank_specialists(specialty: str, lat: float, lon: float, top: int = 3) -> str:
    """Ranks in-network specialists for a specialty by tier (1 best) then distance.

    Args:
        specialty: The specialty medical field, e.g., Endocrinology.
        lat: Patient physical latitude coordinates.
        lon: Patient physical longitude coordinates.
        top: Number of top matching entries to return (default is 3).
    """
    out = []
    for s in _rows(specialty, True):
        out.append(
            {
                "SpecialistId": s["SpecialistId"],
                "Name": f"{s['FirstName']} {s['LastName']}",
                "Tier": int(s["Tier"]),
                "PerformanceScore": int(s["PerformanceScore"]),
                "DistanceMi": _haversine(lat, lon, float(s["Lat"]), float(s["Lon"])),
                "ClinicName": s["ClinicName"],
            }
        )
    # Primary sort: Tier (ascending, lower is better Tier 1 vs Tier 2)
    # Secondary sort: DistanceMi (ascending, closer is better)
    out.sort(key=lambda s: (s["Tier"], s["DistanceMi"]))
    return json.dumps(out[:top])


@mcp.tool(annotations={"title": "Search specialists", "readOnlyHint": True})
def search_specialists(specialty: str) -> str:
    """Retrieves list of in-network specialists for a specialty without geographic ranking.

    Args:
        specialty: The specialty medical field, e.g., Cardiology.
    """
    rows = [
        {
            "SpecialistId": s["SpecialistId"],
            "Tier": int(s["Tier"]),
            "PerformanceScore": int(s["PerformanceScore"]),
        }
        for s in _rows(specialty, True)
    ]
    return json.dumps(rows)


if __name__ == "__main__":
    # Run the server using Stdio transport communication
    mcp.run(transport="stdio")
