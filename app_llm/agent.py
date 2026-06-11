# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Smart Care Triage — LLM Manager/Worker variant (comparison architecture).

This is the FULL agentic counterpart to the production `app` (deterministic
workflow + MCP data plane). Here an LLM MANAGER orchestrates four LLM WORKER
agents, each connected to its MCP server(s) and skill file:

    triage_manager (LlmAgent, Flash)
      ├── router_worker      claims MCP + patient_chart MCP + routing-rules skill
      ├── matcher_worker     specialist_directory MCP
      ├── summarizer_worker  patient_chart MCP + clinical_knowledge MCP + specialist-brief skill
      └── question_worker    patient_chart MCP + clinical_knowledge MCP + clinical-questions skill

Workers are exposed to the manager as AgentTools: the manager delegates, the
worker fetches via MCP and reasons, and the result returns to the manager.

THE DETERMINISTIC CRITIC LOOP — the piece that disciplines the LLM router:
after router_worker proposes a route, the manager MUST call `verify_route`,
a deterministic tool that recomputes the decision with the production rules
engine (app.routing — policy-driven, date-windowed claims, exact taxonomy).
On a mismatch the manager sends the router back with the verifier's evidence
(max 1 retry), and if it still disagrees, the DETERMINISTIC result is adopted
and the override is reported. The LLM proposes; the rules engine disposes.

This variant exists for comparison/demo purposes. Tradeoffs vs `app`:
slower (LLM-driven control flow), costlier, non-reproducible orchestration —
but maximally flexible. The critic loop is what keeps its route auditable.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import google.auth
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.skills import load_skill_from_dir
from google.adk.tools import FunctionTool, skill_toolset
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

_, _project_id = google.auth.default()
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", _project_id or "")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")

ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = ROOT / "mcp_servers"
LLM_SKILLS = Path(__file__).resolve().parent / "skills"
APP_SKILLS = ROOT / "app" / "skills"

FLASH = "gemini-2.5-flash"
PRO = "gemini-2.5-pro"


def _model(name: str = FLASH) -> Gemini:
    from google.genai import types

    return Gemini(model=name, retry_options=types.HttpRetryOptions(attempts=3))


def _mcp(script: str, tools: list[str]) -> McpToolset:
    """Stdio McpToolset for one of the four MCP servers."""
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable, args=[str(MCP_DIR / script)]
            )
        ),
        tool_filter=tools,
    )


def _skill(path: Path) -> skill_toolset.SkillToolset:
    return skill_toolset.SkillToolset(skills=[load_skill_from_dir(path)])


# =====================================================================
# Deterministic critic: the production rules engine as a verification tool
# =====================================================================

def verify_route(patient_id: str, specialty: str, proposed_care_path: str,
                 proposed_patient_tag: str) -> dict:
    """Verifies a proposed care path and patient tag against the health system's
    deterministic routing rules engine (the authoritative implementation).

    Always call this after the router worker proposes a route. The rules
    engine recomputes the decision from raw data with the business policy
    (date-windowed specialty claims, exact tag taxonomy) and returns a
    verdict. On 'mismatch', the expected_* fields are the authoritative
    decision per the internal rules.

    Args:
        patient_id: The patient identifier (UUID or MINT-####).
        specialty: The referral target specialty, e.g. Endocrinology.
        proposed_care_path: The router worker's proposed route.
        proposed_patient_tag: The router worker's proposed engagement tag.
    """
    sys.path.insert(0, str(ROOT))
    from app import clinical_data as cd
    from app.claims_engine import claim_window_start
    from app.policy import policy
    from app.routing import decide_route

    b = cd.get_bundle(patient_id)
    if not b:
        return {"verdict": "error", "detail": f"patient {patient_id} not found"}
    cutoff = claim_window_start()
    matched = [
        c for c in (b.get("claims_12mo") or [])
        if c.get("Specialty") == specialty
        and (c.get("ServiceDateFrom") or "9999") >= cutoff
    ]
    try:
        care_path, tag = decide_route(
            specialty,
            bool(matched),
            int(b.get("recent_encounters", 0) or 0),
            bool(b.get("has_checked_out_appt")),
            bool(b.get("appt_scheduled_next_month")),
        )
    except ValueError as exc:
        return {"verdict": "unsupported_specialty", "detail": str(exc)}

    ok = (proposed_care_path == care_path) and (proposed_patient_tag == tag)
    return {
        "verdict": "match" if ok else "mismatch",
        "expected_care_path": care_path,
        "expected_patient_tag": tag,
        "policy_version": policy().policy_version,
        "evidence": {
            "specialty_claims_in_window": len(matched),
            "claim_window_start": cutoff,
            "recent_encounters": int(b.get("recent_encounters", 0) or 0),
            "has_checked_out_appt": bool(b.get("has_checked_out_appt")),
            "appt_scheduled_next_month": bool(b.get("appt_scheduled_next_month")),
        },
    }


# =====================================================================
# Worker agents (each: MCP data plane + skill where applicable)
# =====================================================================

router_worker = Agent(
    name="router_worker",
    model=_model(FLASH),
    description=(
        "Decides the care path (eConsult/Virtual/In-person) and patient "
        "engagement tag for a referral, using claims and chart data via MCP "
        "and the routing-rules skill."
    ),
    instruction=(
        "You are the routing worker. Given a patient id and referral "
        "specialty: first consult the routing-rules skill, then call "
        "get_claims_bundle and get_patient_bundle for the patient, apply the "
        "rules EXACTLY as written, and return ONLY the JSON contract from the "
        "skill. If you previously proposed a route and are given verifier "
        "feedback, re-read the rules and the evidence, and correct yourself."
    ),
    tools=[
        _skill(LLM_SKILLS / "routing-rules"),
        _mcp("claims_mcp.py", ["get_claims_bundle"]),
        _mcp("patient_chart_mcp.py", ["get_patient_bundle"]),
    ],
)

matcher_worker = Agent(
    name="matcher_worker",
    model=_model(FLASH),
    description=(
        "Ranks in-network specialists for a specialty and patient location "
        "via the specialist directory MCP."
    ),
    instruction=(
        "You are the specialist matching worker. Given a patient id, the "
        "specialty, and the care path: first call get_patient_bundle to read "
        "the patient's Lat/Lon, then call list_specialists (set "
        "internal_only=true ONLY for Virtual care paths). Derive each "
        "candidate's tier from PerformanceScore (>75 Tier 1, >50 Tier 2, >25 "
        "Tier 3, else Tier 4), estimate straight-line distance from the "
        "coordinates, rank by tier then distance, and return the top 3 as "
        "JSON: [{specialist_id, name, tier, performance_score, clinic, "
        "approx_miles}]. For In-person care paths, instead call "
        "get_claims_bundle, take the RenderingProviderNpi of the most recent "
        "claim in the target specialty, and call find_specialist_by_npi with "
        "it to return the patient's EXISTING specialist (continuity)."
    ),
    tools=[
        _mcp(
            "specialist_directory_mcp.py",
            ["list_specialists", "find_specialist_by_npi"],
        ),
        _mcp("patient_chart_mcp.py", ["get_patient_bundle"]),
        _mcp("claims_mcp.py", ["get_claims_bundle"]),
    ],
)

summarizer_worker = Agent(
    name="summarizer_worker",
    model=_model(PRO),
    description=(
        "Writes the clinician-grade Specialist Brief from the patient's "
        "24-month chart (via MCP) using the specialist-brief skill."
    ),
    instruction=(
        "You are the Specialist Brief worker. Given a patient id and referral "
        "specialty: call get_patient_bundle for the full 24-month chart, call "
        "get_clinical_evidence for the relevant guideline grounding, consult "
        "the specialist-brief skill for structure and rules, and write the "
        "brief. Ground strictly in the fetched chart; never invent findings; "
        "no treatment orders."
    ),
    tools=[
        _skill(APP_SKILLS / "specialist-brief"),
        _mcp("patient_chart_mcp.py", ["get_patient_bundle"]),
        _mcp("clinical_knowledge_mcp.py", ["get_clinical_evidence"]),
    ],
)

question_worker = Agent(
    name="question_worker",
    model=_model(FLASH),
    description=(
        "Writes 2-3 patient-specific, guideline-anchored clinical questions "
        "for the specialist, from chart data via MCP."
    ),
    instruction=(
        "You are the clinical questions worker. Given a patient id and "
        "referral specialty: call get_patient_bundle for the chart, call "
        "get_clinical_evidence for guideline anchors, consult the "
        "clinical-questions skill for the format and gold examples, and "
        "return a numbered list of 2-3 specific, decision-focused questions "
        "citing concrete values with dates. Never ask 'evaluate and treat'."
    ),
    tools=[
        _skill(APP_SKILLS / "clinical-questions"),
        _mcp("patient_chart_mcp.py", ["get_patient_bundle"]),
        _mcp("clinical_knowledge_mcp.py", ["get_clinical_evidence"]),
    ],
)


# =====================================================================
# The LLM manager (workers exposed as AgentTools; critic loop enforced)
# =====================================================================

root_agent = Agent(
    name="triage_manager",
    model=_model(FLASH),
    description=(
        "LLM manager for specialist referral triage: delegates to routing, "
        "matching, brief, and question workers, and verifies the route "
        "against the deterministic rules engine before releasing it."
    ),
    instruction=(
        "You are the Smart Care Triage MANAGER (LLM manager/worker variant). "
        "For a referral request (patient id + specialty), run this exact "
        "procedure:\n\n"
        "1. ROUTE: call router_worker with the patient id and specialty. It "
        "returns {care_path, patient_tag, rationale}.\n\n"
        "2. VERIFY (mandatory, every time): call verify_route with the "
        "patient id, specialty, and the router's proposed care_path and "
        "patient_tag.\n"
        "   - verdict 'match': proceed.\n"
        "   - verdict 'mismatch': call router_worker ONCE more, quoting the "
        "verifier's expected values and evidence, and verify again. If it "
        "still mismatches, ADOPT the verifier's expected_care_path and "
        "expected_patient_tag — the deterministic rules engine is "
        "authoritative — and record that an override occurred.\n"
        "   - verdict 'unsupported_specialty': stop and report that the "
        "specialty is not covered by the routing policy.\n\n"
        "3. MATCH: call matcher_worker with the final care path, specialty, "
        "and the patient location from the router's data (or ask "
        "summarizer_worker's chart if needed).\n\n"
        "4. BRIEF: call summarizer_worker with the patient id and specialty.\n\n"
        "5. QUESTIONS: call question_worker with the patient id and specialty.\n\n"
        "6. REPORT in Markdown with sections: Care Path (with tag and "
        "rationale), Route Verification (verdict, policy_version, whether a "
        "correction loop or override occurred), Specialist Shortlist, "
        "Specialist Brief, Clinical Questions. Be transparent about the "
        "verification outcome — it is a feature, not a footnote."
    ),
    tools=[
        AgentTool(agent=router_worker),
        AgentTool(agent=matcher_worker),
        AgentTool(agent=summarizer_worker),
        AgentTool(agent=question_worker),
        FunctionTool(func=verify_route),
    ],
)

app = App(name="app_llm", root_agent=root_agent)
