# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Shared local-data access for the MCP servers.

Each MCP server is a standalone stdio process. When USE_BIGQUERY=1 they query
the smart_care_triage BigQuery dataset (production data path); otherwise they
serve the same shapes from the local synthetic dataset in data/ — so the full
MCP architecture runs identically on a laptop, in the demo container, and in
production. Only the transport behind the tool changes, never the tool contract.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
USE_BIGQUERY = os.getenv("USE_BIGQUERY", "0") == "1"

_BUNDLES: dict | None = None
_SPECIALISTS: list[dict] | None = None


def local_bundle(patient_id: str) -> dict | None:
    """Full per-patient record from data/patient_bundles.json."""
    global _BUNDLES
    if _BUNDLES is None:
        rows = json.loads((DATA_DIR / "patient_bundles.json").read_text())
        _BUNDLES = {b["patient"]["PatientId"]: b for b in rows}
    return _BUNDLES.get(patient_id)


def local_specialists() -> list[dict]:
    """Specialist directory rows from data/specialists.csv."""
    global _SPECIALISTS
    if _SPECIALISTS is None:
        with open(DATA_DIR / "specialists.csv", newline="") as f:
            _SPECIALISTS = list(csv.DictReader(f))
    return _SPECIALISTS
