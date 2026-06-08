# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""FastAPI Application Server for Smart Care Triage.

This module acts as the primary web application gateway for the Smart Care Triage engine. 
It exposes standard rest APIs to retrieve patient records, collect clinician feedback, 
and log PCP actions (accept/override/edit decisions). Additionally, it mounts and serves 
the frontend dashboard from static directories.

API Endpoints:
- `/patients` (GET): Pulls and formats active patient registries.
- `/feedback` (POST): Records telemetry feedback for model performance audit.
- `/pcp_action` (POST): Tracks actual sign-off decisions to seed the feedback learning loop.
- `/dashboard` (STATIC): Serves the frontend web dashboard.
"""

import os
import google.auth
from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app
from google.cloud import logging as google_cloud_logging

from app.app_utils.telemetry import setup_telemetry
from app.app_utils.typing import Feedback

# 1. Initialize global system telemetry, GCP logging, and authentication configurations
setup_telemetry()
_, project_id = google.auth.default()
logging_client = google_cloud_logging.Client()
logger = logging_client.logger(__name__)

# Fetch origins permitted for cross-origin resource sharing (CORS) from environment
allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

# Artifact bucket for ADK (created by Terraform, passed via environment variable)
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")

# Get base path directory of the running agent
AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# In-memory session configuration - no persistent storage
session_service_uri = None
artifact_service_uri = f"gs://{logs_bucket_name}" if logs_bucket_name else None

# 2. Assemble and build the FastAPI app instance using ADK core helpers
app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=artifact_service_uri,
    allow_origins=allow_origins,
    session_service_uri=session_service_uri,
    otel_to_cloud=True,
)
app.title = "smart-care-triage"
app.description = "API for interacting with the Agent smart-care-triage"


# =====================================================================
# API Route Handlers
# =====================================================================

@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collects and registers user telemetry feedback.

    Logs model ratings and notes either to standard console outputs (for testing/integration) 
    or pushes structured payloads straight to GCP Cloud Logging.
    """
    if os.environ.get("INTEGRATION_TEST") == "TRUE":
        import logging as std_logging
        std_logging.info(f"[Test] Feedback received: {feedback.model_dump()}")
    else:
        try:
            logger.log_struct(feedback.model_dump(), severity="INFO")
        except Exception as e:
            import logging as std_logging
            std_logging.warning(f"Failed to log feedback to Cloud Logging: {e}")
            std_logging.info(f"Feedback received: {feedback.model_dump()}")
    return {"status": "success"}


@app.post("/pcp_action")
def log_pcp_action(action_data: dict) -> dict[str, str]:
    """Logs the final actions chosen by the PCP (Primary Care Physician).

    Captures sign-offs, overrides, selected clinical questions, custom referrals notes, 
    and override reasons. These entries are written directly to a local structured JSONL 
    log (`decisions.jsonl`), creating a high-fidelity dataset to support future 
    policy audits and active learning.
    """
    try:
        from pathlib import Path
        import json
        import datetime as _dt
        
        log_path = Path(__file__).resolve().parent.parent / "decisions.jsonl"
        
        # Build structured action entry
        entry = {
            "timestamp": _dt.datetime.now().isoformat(timespec="seconds"),
            "patient_id": action_data.get("patient_id"),
            "specialty": action_data.get("specialty"),
            "pcp_action": action_data.get("action"),  # "accept" or "override"
            "specialist": action_data.get("specialist_id"),
            "override_reason": action_data.get("override_reason"),
            "disposition": "PCP (acted)",
            "selected_questions": action_data.get("selected_questions", []),
            "referral_note": action_data.get("referral_note", ""),
        }
        
        # Append structured JSON entry to log file
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
            
        import logging as std_logging
        std_logging.info(f"[hitl] PCP Action Logged successfully: {entry}")
        return {"status": "success"}
    except Exception as e:
        import logging as std_logging
        std_logging.warning(f"Failed to log PCP action: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/patients")
def get_patients() -> list[dict]:
    """Exposes patient EHR registries parsed and mapped into dashboard formats.

    Iterates through patient bundles, formats lab trends, determines clinical 
    abnormalities, assigns mock insurance providers, and structures payloads 
    for real-time consumption by the frontend dashboard.
    """
    try:
        from app import clinical_data as cd
        import re
        
        # Helper to verify if specific lab values are abnormal based on medical metrics
        def is_lab_abnormal(name: str, val_str: str) -> bool:
            try:
                val_clean = re.sub(r"[^\d\.]", "", val_str)
                if not val_clean:
                    return False
                val = float(val_clean)
                name_lower = name.lower()
                
                # Apply standard clinical range thresholds
                if "blood pressure" in name_lower or "systolic" in name_lower:
                    return val > 130
                if "diastolic" in name_lower:
                    return val > 80
                if "hba1c" in name_lower or "a1c" in name_lower:
                    return val > 6.5
                if "cholesterol" in name_lower or "ldl" in name_lower:
                    return val > 100
                if "hemoglobin" in name_lower:
                    return val < 12.0
                if "ferritin" in name_lower:
                    return val < 12.0
                if "hematocrit" in name_lower:
                    return val < 36.0 or val > 50.0
                if "glucose" in name_lower:
                    return val > 125.0
                if "creatinine" in name_lower:
                    return val > 1.3
                if "potassium" in name_lower:
                    return val < 3.5 or val > 5.1
                if "egfr" in name_lower:
                    return val < 60.0
            except Exception:
                pass
            return False

        # Load raw data bundles
        bundles_dict = cd._bundles()
        out = []
        
        # Insurance mappings for visualization
        INSURANCES = [
            "Medicare Gold Advantage",
            "Blue Cross HMO Choice",
            "Aetna Preferred PPO",
            "Cigna Access Gold",
            "United Healthcare Core"
        ]
        
        for pid, b in bundles_dict.items():
            p = b.get("patient", {})
            ref = b.get("referral_order") or {}
            
            # Determine diagnosis
            diagnosis = ref.get("Description", "")
            if not diagnosis and b.get("problems"):
                diagnosis = b["problems"][0].get("Description", "")
                
            # Distribute mock insurances based on patient ID hashing
            idx = sum(ord(c) for c in pid) % len(INSURANCES)
            insurance = INSURANCES[idx]
            
            # Format problems list
            problems_mapped = []
            for prob in b.get("problems", []):
                problems_mapped.append({
                    "name": prob.get("Description", ""),
                    "icd": prob.get("Icd10", "")
                })
                
            # Format and flag abnormal lab values
            labs_mapped = []
            for test_name, series in b.get("labs", {}).items():
                if not series:
                    continue
                last_item = series[-1]
                val = last_item.get("value", "")
                units = last_item.get("units", "")
                val_with_units = f"{val} {units}".strip()
                abnormal = is_lab_abnormal(test_name, val)
                labs_mapped.append({
                    "name": test_name,
                    "value": val_with_units,
                    "abnormal": abnormal
                })
                
            # Append formatted patient object
            out.append({
                "id": pid,
                "name": f"{p.get('FirstName', '')} {p.get('LastName', '')}".strip(),
                "initials": f"{p.get('FirstName', 'P')[0]}{p.get('LastName', 'T')[0]}",
                "gender": p.get("Gender", "U"),
                "age": p.get("Age", 0),
                "dob": p.get("DOB", p.get("BirthDate", "")),
                "specialty": ref.get("OrderTypeName", "Cardiology"),
                "diagnosis": diagnosis,
                "insurance": insurance,
                "problems": problems_mapped,
                "labs": labs_mapped
            })
        return out
    except Exception as e:
        import logging as std_logging
        std_logging.error(f"Failed to get patients: {e}")
        return []


# =====================================================================
# Frontend Static Asset Hosting & Route Ordering
# =====================================================================

from fastapi.staticfiles import StaticFiles

# Mount static HTML frontend to serve the interactive web dashboard under `/dashboard`
app.mount("/dashboard", StaticFiles(directory=os.path.join(AGENT_DIR, "frontend"), html=True), name="frontend")

# IMPORTANT CRITICAL WORKAROUND:
# Ensure mounted static files route does not hijack any core API routes by relocating 
# the frontend mount entry to the absolute end of the FastAPI routing registry list.
for i, r in enumerate(app.routes):
    if r.name == "frontend":
        app.routes.append(app.routes.pop(i))
        break


# =====================================================================
# Main Process Entry Point
# =====================================================================

if __name__ == "__main__":
    import uvicorn
    # Launch uvicorn locally on port 8000
    uvicorn.run(app, host="0.0.0.0", port=8000)
