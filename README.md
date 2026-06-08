# smart-care-triage

Multi-agent Clinical Triage and Referral Routing Engine. Built on the Google Agent Development Kit (ADK 2.0) and backed by Google Gemini models, this application deterministically routes patient referrals, recommends in-network specialists, retrieves clinical guidelines, and synthesizes medical documentation.

---

##  System Architecture & Design

### System Overview Diagram
This diagram shows the relationship between the Frontend Dashboard, FastAPI application server, the Google ADK workflow engine, local Model Context Protocol (MCP) data servers, and the Gemini API layer.

```mermaid
graph TD
    %% Styling Class Definitions
    classDef client fill:#1a1b26,stroke:#7aa2f7,stroke-width:2px,color:#c0caf5;
    classDef api fill:#1f2335,stroke:#bb9af7,stroke-width:2px,color:#c0caf5;
    classDef engine fill:#24283b,stroke:#2ac3de,stroke-width:2px,color:#c0caf5;
    classDef mcp fill:#1a1b26,stroke:#9ece6a,stroke-width:2px,color:#c0caf5;
    classDef ai fill:#1a1b26,stroke:#ff9e64,stroke-width:2px,color:#c0caf5;

    %% Components
    User[Clinician / PCP]:::client
    UI[Frontend Web Dashboard<br/>index.html]:::client
    FastAPI[FastAPI App Server<br/>fast_api_app.py]:::api
    Workflow[ADK Workflow Graph<br/>agent.py]:::engine
    
    subgraph mcp_group [Model Context Protocol MCP Servers]
        ClinicalKnowledge[Clinical Knowledge MCP<br/>clinical_knowledge_mcp.py]:::mcp
        ClaimsMCP[Claims MCP<br/>claims_mcp.py]:::mcp
        PatientChart[Patient Chart MCP<br/>patient_chart_mcp.py]:::mcp
        SpecialistDirectory[Specialist Directory MCP<br/>specialist_directory_mcp.py]:::mcp
    end

    subgraph gemini_group [Google Gemini AI API]
        Flash[Gemini 2.5 Flash<br/>Lightweight Triage & Synthesis]:::ai
        Pro[Gemini 2.5 Pro<br/>High-Fidelity Clinical Summaries]:::ai
    end

    %% Relationships
    User -->|Interacts| UI
    UI -->|JSON REST APIs / Mount / Static| FastAPI
    FastAPI -->|Invokes Run| Workflow
    
    %% Workflow nodes interactions
    Workflow -->|Queries Guidelines| ClinicalKnowledge
    Workflow -->|Claims Analytics| ClaimsMCP
    Workflow -->|Demographics & Charts| PatientChart
    Workflow -->|Geographic Geo-ranking| SpecialistDirectory
    
    Workflow -->|Low-latency Reasoning / Orchestration| Flash
    Workflow -->|High-Fidelity specialist_brief| Pro
```

### Clinical Triage Sequence Diagram
Below is the chronological sequence of events and node transitions during a single patient referral triage. It highlights the transition from deterministic parsing to parallelized clinical analysis, parallelized report generation, and final compilation.

```mermaid
sequenceDiagram
    autonumber
    actor PCP as PCP / Clinician
    participant UI as Web Dashboard
    participant API as FastAPI Server
    participant EX as Extraction Engine
    participant CL as Claims Engine
    participant RT as Routing Engine
    participant SM as Specialist Matcher
    participant RM as Clinical Reasoning (Gemini Flash)
    participant KM as Clinical Knowledge (MCP Server)
    participant SMZ as Clinical Summarizer (Gemini Pro)
    participant QG as Question Generator (Gemini Flash)
    participant OR as Triage Orchestrator

    PCP->>UI: Select Patient & Specialty
    UI->>API: POST /run (patient_id, specialty)
    API->>EX: START Extraction Node
    EX->>EX: Parse Patient ID and Lookup Record
    alt Patient Record Found
        EX->>CL: Continue to Claims Node
    else Not Found
        EX-->>API: Halt Execution & Return Error Message
    end
    
    CL->>CL: Compute specialty claim logs & missed appointments
    CL->>RT: Pass claims_signal
    
    RT->>RT: Evaluate four-tag engagement & select pathway
    RT->>SM: Pass deterministic routing decision
    
    SM->>SM: Rank specialists (tier, haversine distance)
    SM->>SM: Set confidence flag (HIGH, REVIEW, LOW)
    
    par Parallel Clinical Analysis
        SM->>RM: Invoke Clinical Reasoning Node
        RM->>RM: Construct brief severity assessment
    and
        SM->>KM: Invoke Clinical Knowledge Node
        KM->>KM: Get clinical evidence from MCP server
    end
    
    RM-->>SMZ: Join node (join_analyze) complete
    KM-->>SMZ: Join node (join_analyze) complete
    RM-->>QG: Join node (join_analyze) complete
    KM-->>QG: Join node (join_analyze) complete
    
    par Parallel Output Generation
        SMZ->>SMZ: Synthesize Specialist Brief
    and
        QG->>QG: Generate tailored eConsult questions
    end
    
    SMZ-->>OR: Join node (join_generate) complete
    QG-->>OR: Join node (join_generate) complete
    
    OR->>OR: Synthesize structured Pydantic JSON & Markdown Report
    OR-->>API: Return final decision
    API->>UI: Display Report, Brief, Questions & Recommended Specialist
    UI->>PCP: Show Triage Dashboard
```

---

## 📂 Project Structure

```
smart-care-triage/
├── app/                        # Core agent workflow code
│   ├── app_utils/              # Shared App utilities and helper typings
│   ├── agent.py                # Main workflow routing graph configuration
│   ├── claims_engine.py        # Deterministic claims analyzer node
│   ├── clinical_data.py        # Patient bundle storage and mock directory lookups
│   ├── extraction.py           # Patient ID parser and loader node
│   ├── fast_api_app.py         # FastAPI Web app server and static mounters
│   ├── hitl.py                 # Human-In-The-Loop gate plugin and logger
│   ├── observability.py        # Telemetry, token-counters, and step tracer
│   ├── routing.py              # Care-pathway selection rules node
│   ├── schemas.py              # Pydantic structured output models
│   ├── specialist_matcher.py   # Specialist geo-ranking and confidence calculator
│   ├── sub_agents.py           # Factory configurations for AI-driven nodes
│   └── tools.py                # Formatting and trend-analysis utilities
├── frontend/                   # Interactive Web interface
│   └── index.html              # Core dashboard static webpage
├── mcp_servers/                # Model Context Protocol servers (EHR bridges)
│   ├── claims_mcp.py           # FastMCP Claims data bridge
│   ├── clinical_knowledge_mcp.py # FastMCP Medical guidelines bridge
│   └── patient_chart_mcp.py    # FastMCP Patient chart data bridge
├── tests/                      # Testing registries (unit, integration, load)
├── decisions.jsonl             # Active logger recording PCP triage decisions
├── pyproject.toml              # UV python dependencies config
└── uv.lock                     # UV locked package metadata
```

---

## 🚀 Quick Start (Running Locally)

### Prerequisites
Before running, ensure your local system is equipped with:
*   **uv**: High-performance Python packaging tool ([Installation Guide](https://docs.astral.sh/uv/getting-started/installation/)).
*   **Google Cloud SDK**: Set up and authenticated with a project ID to use Gemini and Cloud Logging ([SDK Guide](https://cloud.google.com/sdk/docs/install)).
*   **Active Application Default Credentials (ADC)**: Run `gcloud auth application-default login` from your terminal so the local app can authenticate to the Gemini API.

### Setup and Install Dependencies
Run the initial setup to install `agents-cli` and package components:

```bash
# Set up Google agents CLI and base skills
uvx google-agents-cli setup

# Install required local workspace python dependencies
agents-cli install
```

### Launching the Dashboard Server
You can launch the FastAPI server locally by running:

```bash
# Run using Python uvicorn
uv run python app/fast_api_app.py
```

Once running, navigate your web browser to:
👉 **[http://localhost:8000/dashboard](http://localhost:8000/dashboard)** to interact with the clinical triage dashboard.

---

## 🛠️ CLI Commands & Utilities

| Command | Description |
| :--- | :--- |
| `agents-cli install` | Installs local Python dependencies. |
| `agents-cli playground` | Launches a local ADK CLI terminal web playground. |
| `agents-cli lint` | Runs code quality and stylistic checks. |
| `uv run pytest tests/` | Runs all unit and integration tests. |
| `agents-cli deploy` | Deploys active nodes to Google Cloud Run. |

---

*Copyright 2026 Google LLC. Licensed under the Apache License, Version 2.0.*
