# MCP layer

Four FastMCP (stdio) servers expose the customer's data and knowledge to the
agents through the Model Context Protocol. This is the production interface:
the deterministic engines read the same data via `bq_data.py` for speed, while
the LLM agents reach knowledge/chart context through these tools.

| Server | Tools | Backing | Used by |
|--------|-------|---------|---------|
| `clinical_knowledge_mcp.py` | `get_clinical_evidence` | curated guidelines (RAG + MedGemma in prod) | Knowledge agent, **live via McpToolset** |
| `patient_chart_mcp.py` | `get_patient_chart`, `get_recent_labs`, `get_prior_referrals` | BQ `patient_bundles` | chart context |
| `claims_mcp.py` | `get_claims_history`, `get_specialty_claim_12mo`, `get_order_history` | BQ `patient_bundles.claims_12mo` | prior-engagement signal |
| `specialist_directory_mcp.py` | `rank_specialists`, `search_specialists` | BQ `specialists` | specialist matching |

All tools are `readOnlyHint: True`.

## Run / inspect a server

```bash
# launch one over stdio (Ctrl-C to stop)
.venv/bin/python mcp_servers/patient_chart_mcp.py

# smoke-test all four (lists tools, calls one each)
.venv/bin/python - <<'PY'
import asyncio, sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def go(f, tool, args):
    p = StdioServerParameters(command=sys.executable, args=[str(Path("mcp_servers")/f)])
    async with stdio_client(p) as (r,w):
        async with ClientSession(r,w) as s:
            await s.initialize()
            print(f, [t.name for t in (await s.list_tools()).tools])
            print((await s.call_tool(tool, args)).content[0].text[:160])
asyncio.run(go("specialist_directory_mcp.py","search_specialists",{"specialty":"Endocrinology"}))
PY
```

## Wiring into an agent (ADK McpToolset)

`app/sub_agents.py::_clinical_knowledge_mcp()` shows the pattern: launch the
server over stdio with `sys.executable` and an **absolute** script path, then
filter to the tools the agent should see. The Knowledge agent uses it live.
The other three are wired the same way when those reads move off `bq_data.py`.

## Env

- `GOOGLE_CLOUD_PROJECT` (default `gem-ent-492719`)
- `BQ_DATASET` (default `smart_care_triage`)
- ADC must be valid for the three BigQuery-backed servers
  (`gcloud auth application-default login`).
