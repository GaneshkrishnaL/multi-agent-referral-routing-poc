# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Patient Chart MCP Server.

This module implements a standard Stdio-based Model Context Protocol (MCP) server 
using the FastMCP framework. It exposes tools to fetch consolidated views of a patient's 
24-month clinical chart (problems, medications, lab readings, and referral histories) 
stored in BigQuery.
"""

import json
import os

from mcp.server.fastmcp import FastMCP

PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "gem-ent-492719")
DATASET = os.getenv("BQ_DATASET", "smart_care_triage")

# Instantiate the Patient Chart FastMCP server
mcp = FastMCP("patient_chart")

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


def _latest(series):
    """Retrieves the most recent chronological reading from a [{date,value,units}] series list."""
    if not series:
        return None
    last = sorted(series, key=lambda r: r.get("date", ""))[-1]
    return {
        "value": last.get("value"),
        "units": last.get("units"),
        "date": last.get("date"),
    }


# =====================================================================
# MCP Server Tool Registrations
# =====================================================================

@mcp.tool(annotations={"title": "Get patient chart", "readOnlyHint": True})
def get_patient_chart(patient_id: str) -> str:
    """Retrieves the consolidated demographics, active problems, medications, and latest labs.

    Args:
        patient_id: Unique UUID identifier for the patient.
    """
    b = _bundle(patient_id)
    if not b:
        return json.dumps({"error": f"patient {patient_id} not found"})
    p = b["patient"]
    return json.dumps(
        {
            "patient": {
                "PatientId": p["PatientId"],
                "Age": p["Age"],
                "Gender": p["Gender"],
                "City": p["City"],
                "State": p["State"],
            },
            "problems": b.get("problems", []),
            "medications": b.get("medications", []),
            "latest_labs": {k: _latest(v) for k, v in (b.get("labs") or {}).items()},
        }
    )


@mcp.tool(annotations={"title": "Get recent labs", "readOnlyHint": True})
def get_recent_labs(patient_id: str) -> str:
    """Retrieves the latest chronological value of each tracked lab for a patient.

    Args:
        patient_id: Unique UUID identifier for the patient.
    """
    b = _bundle(patient_id)
    if not b:
        return json.dumps({"error": f"patient {patient_id} not found"})
    return json.dumps({k: _latest(v) for k, v in (b.get("labs") or {}).items()})


@mcp.tool(annotations={"title": "Get prior referrals", "readOnlyHint": True})
def get_prior_referrals(patient_id: str) -> str:
    """Retrieves prior specialist referrals and their status (e.g. Completed, No-show).

    Args:
        patient_id: Unique UUID identifier for the patient.
    """
    b = _bundle(patient_id)
    if not b:
        return json.dumps({"error": f"patient {patient_id} not found"})
    return json.dumps(b.get("prior_referrals", []))


if __name__ == "__main__":
    # Run the server using Stdio transport communication
    mcp.run(transport="stdio")
