# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""MCP client layer for the deterministic data plane (Option 1 hybrid).

The deterministic engine nodes (extraction, claims, specialist matcher) are NOT
LLM agents, so they cannot carry an McpToolset — instead they act as plain MCP
CLIENTS of the three data servers:

    patient_chart MCP        -> chart domain (Athena equivalent)
    claims MCP               -> data-lake domain (claims + referral outcomes)
    specialist_directory MCP -> directory domain (raw rows; policy applied here)

Design rules:
- MCP servers return RAW data. All business policy (tier thresholds, ranking,
  routing) is applied in the deterministic decision layer, never in the server.
- Sessions are lazy, persistent (one stdio subprocess per server per app
  process), and serialized with a per-server lock.
- USE_MCP=0 disables the layer; callers fall back to direct local reads. Any
  MCP failure also falls back, with a logged warning — the demo never bricks
  on a transport problem, and the fallback is the same data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger("smart_care_triage")

MCP_DIR = Path(__file__).resolve().parent.parent / "mcp_servers"

USE_MCP = os.getenv("USE_MCP", "1") == "1"


class _StdioMcpClient:
    """One persistent stdio MCP session (lazy start, serialized calls)."""

    def __init__(self, script: str) -> None:
        self._script = script
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> ClientSession:
        if self._session is None:
            self._stack = AsyncExitStack()
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(MCP_DIR / self._script)],
                env={**os.environ},
            )
            read, write = await self._stack.enter_async_context(stdio_client(params))
            self._session = await self._stack.enter_async_context(
                ClientSession(read, write)
            )
            await self._session.initialize()
        return self._session

    async def call(self, tool: str, args: dict):
        """Calls a tool and parses the JSON text content it returns."""
        async with self._lock:
            try:
                session = await self._ensure()
                result = await session.call_tool(tool, args)
            except Exception:
                # Broken pipe / dead subprocess: reset so the next call respawns.
                await self._reset()
                raise
        text = "".join(
            c.text for c in result.content if getattr(c, "text", None)
        )
        return json.loads(text) if text else None

    async def _reset(self) -> None:
        try:
            if self._stack is not None:
                await self._stack.aclose()
        except Exception:
            pass
        self._session = None
        self._stack = None


_chart = _StdioMcpClient("patient_chart_mcp.py")
_claims = _StdioMcpClient("claims_mcp.py")
_directory = _StdioMcpClient("specialist_directory_mcp.py")


# ---------------------------------------------------------------------------
# Domain fetch functions used by the deterministic nodes
# ---------------------------------------------------------------------------

async def fetch_chart_bundle(patient_id: str) -> dict | None:
    """Chart-domain bundle (no claims/referral outcomes) via patient_chart MCP."""
    out = await _chart.call("get_patient_bundle", {"patient_id": patient_id})
    if isinstance(out, dict) and out.get("error"):
        return None
    return out


async def fetch_claims_bundle(patient_id: str) -> dict:
    """Claims + prior referral outcomes via the claims (data lake) MCP."""
    out = await _claims.call("get_claims_bundle", {"patient_id": patient_id})
    if not isinstance(out, dict) or out.get("error"):
        return {"claims_12mo": [], "prior_referrals": []}
    return out


async def fetch_specialist_rows(
    specialty: str, in_network_only: bool = True, internal_only: bool = False
) -> list[dict]:
    """Raw directory rows via the specialist_directory MCP."""
    out = await _directory.call(
        "list_specialists",
        {
            "specialty": specialty,
            "in_network_only": in_network_only,
            "internal_only": internal_only,
        },
    )
    return out if isinstance(out, list) else []


async def fetch_specialist_by_npi(npi: str) -> dict | None:
    """Continuity lookup: claim NPI -> raw directory row."""
    if not npi:
        return None
    out = await _directory.call("find_specialist_by_npi", {"npi": str(npi)})
    return out if isinstance(out, dict) else None
