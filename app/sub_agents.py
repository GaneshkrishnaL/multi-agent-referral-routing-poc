# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Triage Sub-Agents Factory and Orchestrator.

This module contains factory functions to configure and instantiate our AI-driven, 
Gemini-powered sub-agents. These sub-agents handle the non-deterministic clinical parts 
of our triage pipeline, such as clinical assessment, guidelines-grounded evidence 
summarization, question generation, and document summarization.

Architecture & Performance Strategy:
- Lightweight Tasks (Assessment, Evidence, Questions, Orchestration): Run on Gemini 2.5 Flash 
  to minimize latency, overhead, and API costs.
- Heavy-Duty Synthesis Tasks (Specialist Brief): Upgrade to Gemini 2.5 Pro for premium, 
  clinician-grade medical narrative synthesis.
- Inter-process tools are integrated using the Model Context Protocol (MCP) to fetch live data.
"""

from __future__ import annotations

import sys
from pathlib import Path

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters

from collections.abc import AsyncGenerator
from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from .schemas import TriageDecision

# Define path configurations for loading skills and MCP servers locally
SKILLS_DIR = Path(__file__).resolve().parent / "skills"
MCP_DIR = Path(__file__).resolve().parent.parent / "mcp_servers"


def _clinical_knowledge_mcp() -> McpToolset:
    """Connects to the Clinical Knowledge MCP server over stdio.
    
    This acts as our local bridge to clinical guidelines, connecting to a sub-process 
    running the Clinical Knowledge FastMCP server.
    """
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,
                args=[str(MCP_DIR / "clinical_knowledge_mcp.py")],
            )
        ),
        tool_filter=["get_clinical_evidence"],
    )


# Model Selections
FLASH = "gemini-2.5-flash"
PRO = "gemini-2.5-pro"


def _model(name: str = FLASH) -> Gemini:
    """Helper to instantiate a Gemini model with standard retry strategies."""
    return Gemini(model=name, retry_options=types.HttpRetryOptions(attempts=3))


def _llm(**kwargs) -> Agent:
    """Helper to instantiate single-turn ADK LLM agents."""
    return Agent(mode="single_turn", **kwargs)


# =====================================================================
# Factory Functions for AI Agents
# =====================================================================

def create_reasoning() -> Agent:
    """Creates the Clinical Reasoning Agent.
    
    This agent parses the patient's record to construct a cohesive clinical assessment.
    It evaluates severity, comorbidities, and risk factors without adding external noise.
    """
    return _llm(
        name="clinical_reasoning",
        model=_model(),
        description="Interprets condition severity, comorbidity, and risk.",
        instruction=(
            "Using only the patient context below, give a brief clinical assessment: "
            "the primary problem and its severity/control, relevant comorbidities, and "
            "risk factors. 4-6 sentences. Do not invent values. Do not call a value "
            "controlled or at goal unless it meets the guideline target for this "
            "patient's risk category (e.g., post-MI LDL <55-70); a value that merely "
            "improved is not necessarily at goal.\n\n"
            "PATIENT CONTEXT:\n{patient_summary}"
        ),
        output_key="assessment",
        generate_content_config=types.GenerateContentConfig(temperature=0.1),
    )


def create_knowledge() -> Agent:
    """Creates the Clinical Knowledge Agent.
    
    This agent retrieves and distills the relevant clinical guidelines for the specific 
    referral condition via the Clinical Knowledge MCP tool.
    """
    return _llm(
        name="clinical_knowledge",
        model=_model(),
        description="Retrieves grounding evidence for the referral specialty.",
        instruction=(
            "Call get_clinical_evidence with specialty '{referral_specialty}' and "
            "condition '{referral_condition}'. Then summarize, in 2-3 sentences, the "
            "evidence most relevant to this patient, grounded strictly in the returned "
            "evidence. Do not give treatment orders."
        ),
        tools=[_clinical_knowledge_mcp()],
        output_key="evidence",
        generate_content_config=types.GenerateContentConfig(temperature=0.1),
    )


def create_summarizer() -> Agent:
    """Creates the Clinical Summarizer Agent (Gemini Pro).
    
    This agent generates the Specialist Brief. It requires complex reasoning over 
    longitudinal records, structured formatting, and professional phrasing, which 
    is why we upgrade this specific node to Gemini 2.5 Pro.
    """
    brief_skill = skill_toolset.SkillToolset(
        skills=[load_skill_from_dir(SKILLS_DIR / "specialist-brief")]
    )
    return _llm(
        name="clinical_summarizer",
        model=_model(PRO),  # Upgrade to Pro for high-fidelity clinical synthesis
        description="Writes the Specialist Brief from the chart.",
        instruction=(
            "You are an expert senior clinical consultant. Synthesize a premium, "
            "clinician-grade Specialist Brief for this referral. Use beautifully structured, "
            "premium Markdown formatting with bold section headers, bullet lists, bolded key metrics/dates, "
            "and clear paragraph breaks so it is highly readable and perfect for a clinical presentation.\n\n"
            "Section Structure (Use these exact headers in Markdown `###`):\n"
            "### REASON FOR REFERRAL\n"
            "A concise, professional summary of the core clinical question driving the referral.\n\n"
            "### PERTINENT HISTORY\n"
            "A comprehensive narrative of the primary condition's progression, active problems, and relevant comorbidities.\n\n"
            "### CURRENT MEDICATIONS\n"
            "Group medications logically by therapeutic class, specifying exact dosages, frequencies, and maximum-dose states.\n\n"
            "### PERTINENT LABS & TRENDS\n"
            "A chronological analysis of key lab markers, highlighting abnormal values and trends with precise dates (e.g. bolding key metrics like **8.7%**).\n\n"
            "### WHAT HAS BEEN TRIED / CARE CONTINUITY\n"
            "Summary of prior interventions, care compliance, and lifestyle modifications.\n\n"
            "### PERTINENT NEGATIVES\n"
            "A clear clinical narrative ruling out specific symptoms, complications, or absolute contraindications.\n\n"
            "Style Requirements:\n"
            "- Use clean Markdown headers (###), bold styling for numbers/metrics/dates (e.g. **8.7%**), and concise bullet lists where appropriate to maximize readability.\n"
            "- Integrate longitudinal chart records, progress notes, and historical records to capture the complete story.\n"
            "- Be thorough and detailed. Explain the physiological implications of key findings (e.g., how renal function affects medication dosing).\n"
            "- Strictly stay grounded in the provided facts; never invent, assume, or bluff details.\n\n"
            "PATIENT CONTEXT:\n{patient_summary}\n\nASSESSMENT:\n{assessment}\n\n"
            "GUIDELINE EVIDENCE:\n{evidence}"
        ),
        tools=[brief_skill],
        output_key="specialist_brief",
        generate_content_config=types.GenerateContentConfig(temperature=0.2),
    )


def create_question_generator() -> Agent:
    """Creates the Question Generator Agent.
    
    This agent generates 2-3 clinical questions targeted at the specialist to frame 
    the eConsult consultation, leveraging a pre-configured template skill.
    """
    q_skill = skill_toolset.SkillToolset(
        skills=[load_skill_from_dir(SKILLS_DIR / "clinical-questions")]
    )
    return _llm(
        name="question_generator",
        model=_model(),
        description="Generates patient-specific eConsult questions.",
        instruction=(
            "Generate 2-3 specific, answerable clinical questions for the specialist. "
            "First consult the clinical-questions skill for the format and gold examples, "
            "then tailor questions to THIS patient, grounded in the assessment and "
            "evidence below. Where a comorbidity changes the answer (e.g., eGFR, a prior "
            "no-show), build that in. Every question must be grounded in the patient's "
            "documented problems and labs; do not ask about screening or a workup for a "
            "condition that is not in the problem list. Return a numbered list, no "
            "preamble.\n\nASSESSMENT:\n{assessment}\n\nEVIDENCE:\n{evidence}"
        ),
        tools=[q_skill],
        output_key="clinical_questions",
        generate_content_config=types.GenerateContentConfig(temperature=0.3),
    )


# =====================================================================
# Orchestrator and Final Report Synthesis Node
# =====================================================================

class TriageOrchestrationEngine(BaseAgent):
    """Synthesizes the final triage decision into both a Pydantic schema and Markdown report."""

    def __init__(self, name: str = "triage_orchestrator"):
        super().__init__(name=name)

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        import json
        from google.genai import types

        # 1. Retrieve all structured and clinical details from downstream state variables
        routing = ctx.session.state.get("routing") or {}
        specialist_matches = ctx.session.state.get("specialist_matches") or []
        specialist_brief = ctx.session.state.get("specialist_brief") or ""
        clinical_questions = ctx.session.state.get("clinical_questions") or []

        # 2. Query Gemini directly to generate the validated Pydantic TriageDecision
        # By enforcing the schema `TriageDecision`, we guarantee JSON compliance and structure.
        client = _model(FLASH).api_client

        prompt = f"""Assemble the final structured triage decision schema from the clinical components.
The care path, tag, and confidence are DETERMINISTIC and must be copied exactly from the routing below.

INSTRUCTIONS:
1. care_path: Copy from ROUTING.care_path exactly. Must be one of: "eConsult", "Virtual", "In-person".
2. patient_tag: Copy from ROUTING.patient_tag exactly.
3. routing_rationale: Copy from ROUTING.rationale exactly.
4. specialist: The FIRST item in SPECIALIST MATCHES (extract its specialist_id, name, tier, and distance_mi). If SPECIALIST MATCHES is empty, set specialist to null.
5. top_alternatives: The remaining matches from SPECIALIST MATCHES as a list of strings formatted exactly as: 'ID (T<tier>, <dist>mi)'. (e.g. 'SPEC-0092 (T1, 9.2mi)'). If no other matches exist, return an empty list [].
6. specialist_brief: Copy the SPECIALIST BRIEF exactly word-for-word. You MUST preserve all markdown headings (e.g. ### REASON FOR REFERRAL, ### PERTINENT HISTORY, ### CURRENT MEDICATIONS, ### PERTINENT LABS & TRENDS, ### WHAT HAS BEEN TRIED / CARE CONTINUITY, ### PERTINENT NEGATIVES), all bullet points, bolding, and double newlines (\\n\\n). Do NOT strip formatting, do NOT alter a single word.
7. clinical_questions: Copy the CLINICAL QUESTIONS list exactly.
8. confidence: Copy from ROUTING.confidence exactly. Must be one of: "HIGH", "REVIEW", "LOW".
9. explanation: Write a clear, single short paragraph of plain-language summary for the primary care physician (PCP) explaining the recommendation, tying the referral together.

ROUTING:
{json.dumps(routing, indent=2)}

SPECIALIST MATCHES:
{json.dumps(specialist_matches, indent=2)}

SPECIALIST BRIEF:
{specialist_brief}

CLINICAL QUESTIONS:
{json.dumps(clinical_questions, indent=2)}
"""

        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=TriageDecision,
            temperature=0.1,
        )

        # Call Gemini directly to bypass standard event filters and extract a structured Pydantic object
        response = await client.aio.models.generate_content(
            model=FLASH,
            contents=prompt,
            config=config,
        )

        # Parse and validate the response against the schema
        td_pydantic = TriageDecision.model_validate_json(response.text)
        td_dict = td_pydantic.model_dump()

        # Update session state with the structured decision dictionary
        ctx.session.state["triage_decision"] = td_dict

        # Extract values to compile a beautifully presented Markdown report for the UI
        care_path = td_dict.get("care_path", "Specialist Triage")
        patient_tag = td_dict.get("patient_tag", "New Referral")
        routing_rationale = td_dict.get("routing_rationale", "")
        confidence = td_dict.get("confidence", "HIGH")
        explanation = td_dict.get("explanation", "")
        extracted_brief = td_dict.get("specialist_brief", "")

        questions = td_dict.get("clinical_questions", [])
        questions_md = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions)) if questions else "*No questions generated.*"

        spec = td_dict.get("specialist")
        if spec:
            if hasattr(spec, "model_dump"):
                spec_dict = spec.model_dump()
            else:
                spec_dict = spec
            spec_name = spec_dict.get("name", "N/A")
            spec_id = spec_dict.get("specialist_id", "N/A")
            spec_tier = spec_dict.get("tier", "N/A")
            spec_dist = spec_dict.get("distance_mi", "N/A")
            spec_md = f"**{spec_name}** ({spec_id}) | Tier {spec_tier} | {spec_dist} mi"
        else:
            spec_md = "*No specific specialist assigned (In-person care continuity).*"

        alts = td_dict.get("top_alternatives", [])
        alts_md = "\n".join(f"- {alt}" for alt in alts) if alts else "*No alternative specialists.*"

        # Construct a beautiful, dynamic clinical report with HSL tailored-feel dark details
        markdown_report = f"""# 🏥 Clinical Triage Decision Report

## 📋 Referral Care Routing Summary
*   **Recommended Pathway**: **{care_path} Path**
*   **Patient Engagement Tag**: `{patient_tag}`
*   **Triage Confidence Level**: `{confidence}`
*   **Primary Specialist Recommendation**: {spec_md}

---

## 💡 Clinical Routing Rationale
{routing_rationale}

---

## 🩺 AI Specialist Brief
{extracted_brief}

---

## ❓ Recommended Clinical Questions for Specialist
{questions_md}

---

## 🔮 Alternative Specialists in Network
{alts_md}

---

## 🩺 Care Team Notes & Explanation
{explanation}

---
*Report compiled by **Smart Care Triage Orchestrator** on Gemini 2.5.*
"""

        # Yield final event detailing the compilation output
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(parts=[types.Part.from_text(text=markdown_report)]),
            actions=EventActions(
                state_delta={
                    "triage_decision": td_dict
                }
            ),
        )


def create_orchestrator() -> BaseAgent:
    """Creates the Orchestration Engine which consolidates output from all previous nodes."""
    return TriageOrchestrationEngine(name="triage_orchestrator")
