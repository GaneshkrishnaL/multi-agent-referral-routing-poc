# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Trace generator for the eval flywheel (ADK 2.0 Workflow agents).

`agents-cli eval generate` (v0.3.x) cannot drive the experimental ADK 2.0
Workflow root agent, so this harness performs Stage 2 (Run Inference) itself:
it executes the real app over the eval dataset via InMemoryRunner and writes
traces in the canonical EvaluationDataset grading shape. Stage 3 stays on the
managed path:

    uv run python tests/eval/generate_traces.py
    agents-cli eval grade --config tests/eval/eval_config.yaml

Results land in artifacts/grade_results/results_<ts>.{json,html}; diff
iterations with `agents-cli eval compare <prev>.json <new>.json`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

DATASET = ROOT / "tests" / "eval" / "datasets" / "triage-dataset.json"
OUT_DIR = ROOT / "artifacts" / "traces"


async def run_case(runner, case: dict) -> dict:
    from google.genai import types

    prompt_text = " ".join(
        p.get("text", "") for p in case["prompt"]["parts"] if isinstance(p, dict)
    )
    session = await runner.session_service.create_session(
        app_name="app", user_id="eval_user"
    )
    events = [{"author": "user", "content": {"role": "user", "parts": [{"text": prompt_text}]}}]
    async for ev in runner.run_async(
        user_id="eval_user",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=prompt_text)]),
    ):
        content = getattr(ev, "content", None)
        if content is None:
            continue
        events.append(
            {
                "author": getattr(ev, "author", None) or "smart_care_triage",
                "content": content.model_dump(exclude_none=True, mode="json"),
            }
        )
    # The grader requires an explicit final `response` per case: what the user
    # actually sees last. For a run paused at the clinical review gate that is
    # the RequestInput pause notice (a function_call, not a text part); for a
    # completed run it is the last text-bearing model event.
    final_text = ""
    for e in reversed(events):
        if e["author"] == "user":
            continue
        for p in e["content"].get("parts", []):
            fc = p.get("function_call") or {}
            if fc.get("name") == "adk_request_input":
                final_text = (fc.get("args") or {}).get("message", "")
                break
        if final_text:
            break
        texts = [p.get("text", "") for p in e["content"].get("parts", []) if p.get("text")]
        if texts:
            final_text = "\n".join(texts)
            break
    return {
        "eval_case_id": case["eval_case_id"],
        "prompt": case["prompt"],
        "responses": [
            {"response": {"role": "model", "parts": [{"text": final_text}]}}
        ],
        "agent_data": {
            "agents": {
                "smart_care_triage": {
                    "agent_id": "smart_care_triage",
                    "instruction": "Deterministic referral triage workflow with grounded clinical generation.",
                }
            },
            "turns": [{"turn_index": 0, "events": events}],
        },
    }


async def main() -> None:
    from google.adk.runners import InMemoryRunner

    from app.agent import app as triage_app

    runner = InMemoryRunner(app=triage_app)
    dataset = json.loads(DATASET.read_text())
    traces = []
    for case in dataset["eval_cases"]:
        print(f"[trace] running {case['eval_case_id']} ...", flush=True)
        try:
            traces.append(await run_case(runner, case))
            print(f"[trace] {case['eval_case_id']} done", flush=True)
        except Exception as e:  # keep going; grade what we have
            print(f"[trace] {case['eval_case_id']} FAILED: {e}", flush=True)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUT_DIR / f"trace_triage_{ts}.json"
    out.write_text(json.dumps({"eval_cases": traces}, indent=1))
    print(f"[trace] wrote {len(traces)} traces -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
