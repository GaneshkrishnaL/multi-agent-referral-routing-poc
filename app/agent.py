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
from google.adk.apps import App
from google.adk.events import Event
from google.adk.workflow import JoinNode

# Import our custom components and sub-agents
from . import sub_agents
from .claims_engine import ClaimsEngine
from .extraction import ExtractionEngine
from .hitl import DecisionGatePlugin
from .observability import ObservabilityPlugin
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

# 2. AI-driven clinical reasoning and knowledge sub-agents (Gemini-backed)
clinical_reasoning = sub_agents.create_reasoning()
clinical_knowledge = sub_agents.create_knowledge()
clinical_summarizer = sub_agents.create_summarizer()
question_generator = sub_agents.create_question_generator()
triage_orchestrator = sub_agents.create_orchestrator()

# 3. Synchronizing barriers (ADK JoinNodes) to coordinate parallel execution paths
join_analyze = JoinNode(name="join_analyze")
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
        
        # Phase 4: Parallel clinical analysis (Reasoning & Knowledge retrieval)
        # Once the specialist is matched, we run parallel nodes:
        # - clinical_reasoning: Generates high-level clinical assessments.
        # - clinical_knowledge: Fetches guidelines from the clinical knowledge MCP.
        # Both paths merge at the `join_analyze` JoinNode once both are done.
        (specialist_matcher, clinical_reasoning, join_analyze),
        (specialist_matcher, clinical_knowledge, join_analyze),
        
        # Phase 5: Parallel document generation (Brief & Questions)
        # From the analyzed context, we run parallel generation nodes:
        # - clinical_summarizer: Synthesizes a high-fidelity clinician-grade Specialist Brief.
        # - question_generator: Tailors patient-specific clinical questions for the specialist.
        # Both paths synchronize at the `join_generate` JoinNode.
        (join_analyze, clinical_summarizer, join_generate),
        (join_analyze, question_generator, join_generate),
        
        # Phase 6: Synthesize final output
        # Once both the Brief and questions are ready, the orchestrator compiles them into 
        # a structured Pydantic schema and a beautiful clinical report.
        (join_generate, triage_orchestrator),
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
    # - DecisionGatePlugin handles human-in-the-loop (HITL) gate evaluation and file logging.
    plugins=[ObservabilityPlugin(), DecisionGatePlugin()],
    
    # Configure Gemini's Context Caching.
    # Caches the system instructions, tools, and stable prompt templates to cut repeated 
    # token processing and lower latency & cost for warm-path requests.
    context_cache_config=ContextCacheConfig(
        min_tokens=2048, ttl_seconds=1800, cache_intervals=10
    ),
)