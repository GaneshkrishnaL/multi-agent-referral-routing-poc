# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Triage Helper Tools and Formatters.

This module provides deterministic utility functions to format, parse, and trend 
clinical data stored in patient bundles. By converting raw data structures into beautifully 
formatted, clean textual summaries, we ensure downstream LLM prompts have high-density 
information while remaining clean, readable, and token-efficient.

Data structures processed:
- Lab results (synthesized into trend lines like 'HbA1c: 7.2 -> 6.8 -> 6.5 %')
- Problems list, medications, past encounters, and progress notes.
"""

from __future__ import annotations

from google.adk.tools import ToolContext

from . import clinical_data as cd


def _lab_trend(labs: dict) -> str:
    """Creates a concise, chronologically ordered trend line for each key laboratory test.
    
    Example output:
    'HbA1c: 8.4 -> 8.1 -> 7.8 %; Creatinine: 1.1 -> 1.2 mg/dL'
    """
    lines = []
    for name, series in labs.items():
        if not series:
            continue
        # Extract up to the last 4 laboratory values chronologically
        vals = [s["value"] for s in series][-4:]
        units = series[-1].get("units", "")
        lines.append(f"{name}: {' -> '.join(vals)} {units}".strip())
    return "; ".join(lines)


def build_summary(b: dict) -> str:
    """Assembles a highly structured, unified clinical narrative summary from raw bundle data.
    
    This text is injected directly into LLM sub-agent instructions to guarantee strict 
    grounding in facts.
    """
    p = b.get("patient", {})
    referral = b.get("referral_order") or {}
    
    # 1. Active Problems: Extract up to 8 active ICD-10 coded problems
    problems = ", ".join(
        f"{x['Description']} ({x['Icd10']})" for x in b.get("problems", [])[:8]
    )
    
    # 2. Medications & Labs Trend
    meds = ", ".join(b.get("medications", [])) or "none"
    labs = _lab_trend(b.get("labs", {})) or "none recorded"
    
    # 3. Prior Specialist Referrals history
    prior = b.get("prior_referrals", []) or []
    prior_s = (
        "; ".join(f"{x['Specialty']} {x['Status']} ({x['Date']})" for x in prior)
        or "none"
    )
    
    # 4. Encounter History & Historical Chart Notes
    encs_list = []
    for e in b.get("encounter_history", []):
        date = e.get("Date", "Unknown Date")
        cls = e.get("EncounterClass", "unknown")
        notes = e.get("ChartNotes", "").strip()
        if notes:
            encs_list.append(f"[{date} - {cls}]: {notes}")
    history_notes = "\n\n".join(encs_list) if encs_list else "none recorded"

    # 5. Compile the comprehensive, multi-line diagnostic text block
    return (
        f"Patient: {p.get('Age')}yo {p.get('Gender')}, {p.get('City')} {p.get('Zip')}\n"
        f"Referral specialty: {referral.get('OrderTypeName', '?')}\n"
        f"Active problems: {problems or 'none coded'}\n"
        f"Recent labs (trend): {labs}\n"
        f"Medications: {meds}\n"
        f"Prior referrals: {prior_s}\n"
        f"Recent encounters (24-36mo): {b.get('recent_encounters', 0)}\n"
        f"PCP progress note:\n{b.get('pcp_progress_note', '') or 'none'}\n\n"
        f"Historical Chart Notes:\n{history_notes}"
    )


def get_patient_context(patient_id: str, tool_context: ToolContext) -> dict:
    """Loads a patient's clinical context bundle into active session state.

    Args:
        patient_id: Unique medical record number/identifier for the patient.
        tool_context: ADK context used to store cross-node session parameters.

    Returns:
        dict: Status message indicating success or failure along with the summary.
    """
    b = cd.get_bundle(patient_id)
    if not b:
        return {"status": "error", "message": f"No patient found for id {patient_id}."}
        
    summary = build_summary(b)
    
    # Write variables into tool state context for downstream nodes to consume
    tool_context.state["patient_context"] = b
    tool_context.state["patient_summary"] = summary
    tool_context.state["referral_specialty"] = (b.get("referral_order") or {}).get(
        "OrderTypeName", ""
    )
    tool_context.state["patient_id"] = patient_id
    
    return {
        "status": "success",
        "message": "Patient context loaded.",
        "patient_summary": summary,
    }
