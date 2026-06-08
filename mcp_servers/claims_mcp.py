# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Claims MCP Server.

This module implements a standard Stdio-based Model Context Protocol (MCP) server 
using the FastMCP framework. It exposes tools to fetch a patient's historical medical 
claims, specialty claims, and historical laboratory/imaging orders from BigQuery.

These signals are critical to establishing prior specialist relationships (needed for 
routing continuity) and analyzing historical healthcare utilization.
"""

import json
import os

from mcp.server.fastmcp import FastMCP

# 1. Fetch GCP configurations from environment
PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "gem-ent-492719")
DATASET = os.getenv("BQ_DATASET", "smart_care_triage")

# 2. Instantiate the Claims FastMCP server
mcp = FastMCP("claims")

_client = None


def _bq():
    """Helper to lazily instantiate and cache the BigQuery client."""
    global _client
    if _client is None:
        from google.cloud import bigquery
        _client = bigquery.Client(project=PROJECT)
    return _client


def _bundle(patient_id: str):
    """Retrieves the full JSON-formatted EHR clinical data bundle for a patient from BigQuery."""
    from google.cloud import bigquery

    sql = (
        f"SELECT bundle_json FROM `{PROJECT}.{DATASET}.patient_bundles` "
        "WHERE PatientId=@pid LIMIT 1"
    )
    job = _bq().query(
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


# =====================================================================
# MCP Server Tool Registrations
# =====================================================================

@mcp.tool(annotations={"title": "Get claims history", "readOnlyHint": True})
def get_claims_history(patient_id: str) -> str:
    """Retrieves trailing 12-month claims: date, rendering provider, and specialty.

    Args:
        patient_id: Unique UUID identifier for the patient.
    """
    b = _bundle(patient_id)
    if not b:
        return json.dumps({"error": f"patient {patient_id} not found"})
    rows = [
        {
            "ClaimId": c.get("ClaimId"),
            "ServiceDate": c.get("ServiceDateFrom"),
            "Provider": c.get("RenderingProviderName"),
            "Specialty": c.get("Specialty"),
        }
        for c in b.get("claims_12mo", [])
    ]
    return json.dumps(rows)


@mcp.tool(annotations={"title": "Check specialty claim (12mo)", "readOnlyHint": True})
def get_specialty_claim_12mo(patient_id: str, specialty: str) -> str:
    """Checks if the patient has a claim with the given specialty in the last 12 months.

    This prior-engagement signal can shift routing to asynchronous eConsults or in-person visits.

    Args:
        patient_id: Unique UUID identifier for the patient.
        specialty: The specialty medical field, e.g., Endocrinology.
    """
    b = _bundle(patient_id)
    if not b:
        return json.dumps({"error": f"patient {patient_id} not found"})
    hits = [c for c in b.get("claims_12mo", []) if c.get("Specialty") == specialty]
    return json.dumps(
        {
            "specialty": specialty,
            "has_claim_12mo": bool(hits),
            "count": len(hits),
            "most_recent": max(
                (c.get("ServiceDateFrom", "") for c in hits), default=None
            ),
        }
    )


@mcp.tool(annotations={"title": "Get order history", "readOnlyHint": True})
def get_order_history(patient_id: str) -> str:
    """Retrieves ordered laboratory tests, imaging orders, and prescriptions over the chart window.

    Args:
        patient_id: Unique UUID identifier for the patient.
    """
    b = _bundle(patient_id)
    if not b:
        return json.dumps({"error": f"patient {patient_id} not found"})
    return json.dumps(b.get("orders_history", {}))


if __name__ == "__main__":
    # Run the server using Stdio transport communication
    mcp.run(transport="stdio")
