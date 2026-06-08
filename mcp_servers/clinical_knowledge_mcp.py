# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Clinical Knowledge MCP Server.

This module implements a standard Stdio-based Model Context Protocol (MCP) server 
using the FastMCP framework. It exposes tools to fetch clinical evidence and authority 
guidelines matched to specific patient conditions.

In a production environment, this is backed by real-time vector databases and RAG (Retrieval-
Augmented Generation) corpora. In this local configuration, it queries a curated, 
medical-consensus library stored in `guidelines.py`.
"""

import guidelines
from mcp.server.fastmcp import FastMCP

# Instantiate the Clinical Knowledge FastMCP server
mcp = FastMCP("clinical_knowledge")


@mcp.tool(annotations={"title": "Get clinical evidence", "readOnlyHint": True})
def get_clinical_evidence(specialty: str, condition: str = "") -> str:
    """Retrieves a grounding guideline for a referral, matched to the patient's condition.

    Args:
        specialty: The referral specialty, e.g. Endocrinology.
        condition: The patient's primary diagnosis for this referral, e.g.
            "Prediabetes" or "Ischemic heart disease". Drives the lookup so the
            evidence matches the actual problem, not just the specialty.
    """
    return guidelines.lookup(specialty, condition)


if __name__ == "__main__":
    # Run the server using Stdio transport communication
    mcp.run(transport="stdio")