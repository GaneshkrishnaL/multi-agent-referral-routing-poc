# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
# Lazy export (PEP 562): keeps `import app_llm` light; the agent graph (and its
# GCP auth) loads only when the ADK server actually requests the app.
__all__ = ["app", "root_agent"]


def __getattr__(name):
    if name in ("app", "root_agent"):
        from . import agent

        return getattr(agent, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
