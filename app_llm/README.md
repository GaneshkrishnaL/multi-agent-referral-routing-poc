# app_llm — LLM Manager/Worker variant (comparison architecture)

The fully agentic counterpart to the production `app/`. An **LLM manager**
(`triage_manager`) orchestrates four **LLM workers**, each connected to its
MCP server(s) and skill file, and a **deterministic critic loop** disciplines
the route.

```
triage_manager (LlmAgent, Flash)
  ├── router_worker      claims MCP + patient_chart MCP + routing-rules SKILL.md
  │       │ proposes {care_path, patient_tag}
  │       ▼
  │   verify_route()  ←── deterministic critic: re-runs the PRODUCTION rules
  │       │               engine (app.routing, policy-driven, date-windowed)
  │       │  match → proceed
  │       │  mismatch → router retries once with the verifier's evidence;
  │       │             still wrong → deterministic result OVERRIDES (logged)
  │       ▼
  ├── matcher_worker     specialist_directory MCP (tiers, continuity by NPI)
  ├── summarizer_worker  patient_chart MCP + clinical_knowledge MCP + specialist-brief SKILL.md (Pro)
  └── question_worker    patient_chart MCP + clinical_knowledge MCP + clinical-questions SKILL.md
```

## How the deterministic character is enforced

The router worker reads the SAME internal rules (encoded in
`skills/routing-rules/SKILL.md`) and proposes a route — but an LLM reading
rules is a proposal, not a guarantee. `verify_route` recomputes the decision
with `app.routing.decide_route` (the tested, policy-driven engine: date-
windowed specialty claims, exact tag taxonomy, versioned policy). The manager
must call it on every referral:

1. **match** → the LLM route is confirmed deterministically correct.
2. **mismatch** → one correction loop with the verifier's evidence.
3. **still wrong** → the deterministic result is adopted and the override is
   reported in the final answer. The LLM proposes; the rules engine disposes.

## Try it

The ADK API server discovers this app automatically (it lives next to `app/`):
open the playground at `/dev-ui/`, switch the app dropdown to **app_llm**, and
ask:

```
Triage the referral for patient 00fa1d1d-d444-7652-9b80-04c230e3df21 to Endocrinology
```

The final report includes a "Route Verification" section showing the verdict,
the policy version, and whether the critic loop had to correct the router.

## Honest tradeoffs vs the production `app/`

| | `app` (deterministic workflow + MCP) | `app_llm` (LLM manager/worker + critic) |
|---|---|---|
| Care-path decision | rules engine directly | LLM proposal, verified/overridden by the same rules engine |
| Orchestration | fixed graph (reproducible) | LLM-chosen (flexible, non-reproducible) |
| Latency / cost | lower (4 LLM calls) | higher (manager + 4 workers + retries) |
| Audit story | decision = rule + policy version | decision = verified-against rule + policy version |

`app/` remains the production PoC. This variant exists to demonstrate the
manager/worker pattern 
