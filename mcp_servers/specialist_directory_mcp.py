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
    """Queries specialists from BigQuery matching the specified medical specialty."""
    from google.cloud import bigquery

    sql = (
        f"SELECT SpecialistId, FirstName, LastName, Tier, PerformanceScore, "
        f"Network, ClinicName, Lat, Lon FROM `{PROJECT}.{DATASET}.specialists` "
        f"WHERE Specialty=@sp"
    )
    if in_network_only:
        sql += " AND Network='In-Network'"
    job = _bq().query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("sp", "STRING", specialty)]
        ),
    )
    return list(job)


# =====================================================================
# MCP Server Tool Registrations
# =====================================================================

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
