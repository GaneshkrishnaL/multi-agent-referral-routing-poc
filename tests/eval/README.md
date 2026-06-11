# Evaluation runbook — smart-care-triage

Quality is tracked with the Agent Platform eval flywheel (`agents-cli eval`),
with three metrics mirroring the architecture's split:

| Metric | Type | What it grades |
|---|---|---|
| `triage_clinical_quality` | LLM judge (Vertex) | Brief grounding, question specificity (anti "eval and treat"), explainability, specialty tailoring |
| `routing_matches_policy` | deterministic code | Care path in the released report == spec-derived expectation per patient |
| `review_gate_behavior` | deterministic code | Urgent referrals pause at the HITL gate (report withheld); routine referrals release |

## Run one iteration

```bash
# Stage 2 — inference. agents-cli eval generate cannot drive the pre-GA ADK 2.0
# Workflow root agent yet, so this harness runs the real app and writes traces
# in the canonical EvaluationDataset shape to artifacts/traces/:
uv run python tests/eval/generate_traces.py

# Stage 3 — grading (managed metrics + judge on the Vertex global endpoint):
agents-cli eval grade --config tests/eval/eval_config.yaml
# -> artifacts/grade_results/results_<ts>.{json,html}
```

## Track improvement over time

1. Keep every `results_<ts>.json`. After each change (prompt, skill file,
   policy version, model), re-run the two commands above and diff:

   ```bash
   agents-cli eval compare artifacts/grade_results/results_<prev>.json \
                           artifacts/grade_results/results_<new>.json
   ```

2. The offline evals pair with the LIVE feedback loop: every triage decision
   is stamped with `policy_version` and joined to the PCP's accept/override by
   `session_id` (decisions.jsonl). `/feedback_stats` (rendered at
   `/dashboard/insights.html`) shows acceptance per policy version and per
   week — the production complement to these offline scores.

3. Managed/cloud runs (CI scale): submit the same dataset + config to the
   Agent Platform Eval Service:

   ```bash
   agents-cli eval submit --dataset artifacts/traces/<trace>.json --dest gs://<bucket>
   agents-cli eval results --run-id <run-resource-name>
   ```

## Datasets

- `datasets/triage-dataset.json` — the five requirement-doc test cases
  (eConsult + no-show, Virtual, In-person continuity, new patient, urgent
  gate).
- `datasets/basic-dataset.json` — generic smoke prompts.
- Judge variance: `triage_clinical_quality` samples the judge once, so scores
  can move ±0.5 run to run; trends matter, not single decimals. The two code
  metrics are exact.

## Baseline (2026-06-10, 3-agent + MCP data plane)

```
triage_clinical_quality  mean 4.8 / 5   (5 cases)
routing_matches_policy   5/5 exact
review_gate_behavior     5/5 exact
```

Known refinement: on urgent (gated) runs the draft Specialist Brief content
event still streams before the gate pauses the run; the routing decision and
final report are withheld. If full pre-review blackout is required, move the
generation tier behind the gate at the cost of reviewer wait time.

## Managed cloud experiments (Optimize → Evaluation)

The Agent Platform Eval Service runs the same judge as a managed
`evaluationRun` (visible in the console under Vertex AI → Evaluation,
location `global`). The working recipe — the cloud item builder drops the
response text when `agent_data` is present, so submit canonical cases
WITHOUT `agent_data`, using the agent_data-free judge config:

```bash
# 1. Generate traces locally (managed inference cannot drive the pre-GA
#    ADK 2.0 Workflow agent yet):
uv run python tests/eval/generate_traces.py

# 2. Strip agent_data for the cloud item builder:
python3 -c "
import json, glob
p = sorted(glob.glob('artifacts/traces/trace_triage_*.json'))[-1]
d = json.load(open(p))
out = {'eval_cases': [{k: c[k] for k in ('eval_case_id','prompt','responses')} for c in d['eval_cases']]}
json.dump(out, open('/tmp/cloud_dataset.json','w'))"

# 3. Submit the managed run:
agents-cli eval submit --dataset /tmp/cloud_dataset.json \
  --dest gs://smart-care-triage-eval-gem-ent-492719 \
  --config tests/eval/cloud_eval_config.yaml --project gem-ent-492719

# 4. Poll: agents-cli eval results --run-id <evaluationRuns resource>
```

Baseline managed run (2026-06-11): evaluationRuns/6014416564864942080,
triage_clinical_quality mean 5.0/5 over the 5 requirement-doc cases.
