# ruff: noqa
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
"""Smart Care Triage: Multi-Agent Referral Routing Engine.

This module acts as the orchestrator and entry point for the Smart Care Triage workflow.
It defines a structured, rules-based, and AI-augmented execution graph using the 
Google Agent Development Kit (ADK 2.0). By combining deterministic calculations with 
advanced LLMs, the engine ensures medical decisions remain auditable while clinical 
summaries and eConsult questions leverage AI reasoning.
"""

import os
import google.auth
from google.adk import Workflow
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps import App, ResumabilityConfig
from google.adk.events import Event
from google.adk.workflow import JoinNode

# Import our custom components and sub-agents
from . import sub_agents
from .claims_engine import ClaimsEngine
from .extraction import ExtractionEngine
from .hitl import DecisionGatePlugin
from .observability import ObservabilityPlugin
from .review_gate import clinical_review_gate
from .routing import RoutingEngine
from .specialist_matcher import SpecialistMatcher

# Authenticate with Google Cloud and default environmental variables to run locally or in the cloud.
# The project ID is fetched from the system's ADC (Application Default Credentials).
_, project_id = google.auth.default()
os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"


# =====================================================================
# Workflow Graph Nodes Instantiation
# =====================================================================

# 1. Deterministic code-based engines (Rules-based, fast, and 100% auditable)
extraction = ExtractionEngine(name="extraction")
claims_engine = ClaimsEngine(name="claims_engine")
routing_engine = RoutingEngine(name="routing_engine")
specialist_matcher = SpecialistMatcher(name="specialist_matcher")

# 2. AI-driven clinical sub-agents (Gemini-backed) — three agents:
#    one grounded analysis (Clinical Analyst, guidelines-MCP-backed), and two
#    deliverable generators (Specialist Brief, clinical questions) that share it.
clinical_analyst = sub_agents.create_analyst()
clinical_summarizer = sub_agents.create_summarizer()
question_generator = sub_agents.create_question_generator()
triage_orchestrator = sub_agents.create_orchestrator()

# 3. Synchronizing barrier (ADK JoinNode) for the parallel generation paths
join_generate = JoinNode(name="join_generate")


# =====================================================================
# Routing Helper & Branching Nodes
# =====================================================================

def extraction_router(ctx) -> Event:
    """Branches the workflow depending on whether valid patient context was extracted.
    
    If the extraction agent failed to locate the patient ID or context, we trigger HALT to 
    gracefully exit without wasting computation or model tokens. Otherwise, we CONTINUE.
    """
    if not ctx.session.state.get("patient_context"):
        return Event(route="HALT")
    return Event(route="CONTINUE")


def halt_node(ctx) -> Event:
    """A terminal node for non-referral inputs, ensuring the workflow exits cleanly.
    
    This acts as a safety valve in case of missing data, ensuring the pipeline terminates 
    gracefully instead of throwing execution errors.
    """
    return Event()


# =====================================================================
# Workflow Execution Graph (Topological Routing)
# =====================================================================

root_agent = Workflow(
    name="smart_care_triage",
    description=(
        "Triages a specialist referral: routes the care path (eConsult/virtual/"
        "in-person), recommends a specialist, writes a Specialist Brief, and "
        "generates clinical questions."
    ),
    edges=[
        # Phase 1: Start and Extraction
        # We start by running extraction to pull the patient's longitudinal record.
        ("START", extraction, extraction_router),
        
        # Phase 2: Extraction Route (HALT / CONTINUE)
        # We branch here. If extraction failed, we stop. If it succeeded, we go to the Claims Engine.
        (
            extraction_router,
            {
                "HALT": halt_node,
                "CONTINUE": claims_engine,
            },
        ),
        
        # Phase 3: Sequential deterministic engines
        # Runs Claims -> care-path routing -> specialist matching sequentially.
        # These are entirely rules-based, making the decision logic highly auditable.
        (claims_engine, routing_engine, specialist_matcher),
        
        # Phase 4: Grounded clinical analysis (single shared spine)
        # The Clinical Analyst retrieves guideline evidence via the clinical
        # knowledge MCP server and produces ONE guideline-grounded assessment
        # that both generators consume — so the Brief and the questions always
        # tell the same clinical story.
        (specialist_matcher, clinical_analyst),

        # Phase 5: Parallel document generation (Brief & Questions)
        # From the shared analysis, we run parallel generation nodes:
        # - clinical_summarizer: Synthesizes a high-fidelity clinician-grade Specialist Brief.
        # - question_generator: Tailors patient-specific clinical questions for the specialist.
        # Both paths synchronize at the `join_generate` JoinNode.
        (clinical_analyst, clinical_summarizer, join_generate),
        (clinical_analyst, question_generator, join_generate),
        
        # Phase 6: Server-side clinical review gate (human-in-the-loop).
        # The gate runs BEFORE the orchestrator emits anything user-facing:
        # urgent / LOW / REVIEW referrals pause here on a RequestInput
        # interrupt, so the recommendation report is genuinely withheld until
        # a clinical reviewer responds. Clean referrals pass straight through.
        (join_generate, clinical_review_gate),

        # Phase 7: Synthesize final output (released only after the gate).
        # The orchestrator compiles the Brief, questions, and routing into a
        # structured Pydantic schema and the clinical report shown to the PCP.
        (clinical_review_gate, triage_orchestrator),
    ],
)


# =====================================================================
# App Assembly & Middleware configuration
# =====================================================================

app = App(
    name="app",
    root_agent=root_agent,
    # Plug in cross-cutting concerns:
    # - ObservabilityPlugin handles end-to-end latency and token counting.
    # - DecisionGatePlugin writes the joinable audit row for every decision.
    plugins=[ObservabilityPlugin(), DecisionGatePlugin()],

    # Resumability checkpoints the workflow so the clinical_review_gate's
    # RequestInput interrupt pauses the run and resumes at the gate when the
    # clinical reviewer responds (ADK 2.0 HITL).
    resumability_config=ResumabilityConfig(is_resumable=True),

    # Configure Gemini's Context Caching.
    # Caches the system instructions, tools, and stable prompt templates to cut repeated
    # token processing and lower latency & cost for warm-path requests.
    context_cache_config=ContextCacheConfig(
        min_tokens=2048, ttl_seconds=1800, cache_intervals=10
    ),
)