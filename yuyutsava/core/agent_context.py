"""Reads the LangGraph `RunnableConfig` to surface session_id + agent_path.

Every tool/middleware that emits an `interrupt(...)` payload merges
`current_context()` into the dict, so the daemon's channel router can attribute
each user-facing question to a specific session (thread_id) and a specific
agent in the call tree (orchestrator / orchestrator/file_organizer#1 / ...).

Why a free function rather than a class: LangChain already exposes the active
config via a ContextVar (`var_child_runnable_config`) inside a running graph.
We just read from it — no plumbing through every tool signature.
"""

from __future__ import annotations

from typing import TypedDict


class AgentContext(TypedDict, total=False):
    session_id: str | None
    agent_path: str | None


def current_context() -> AgentContext:
    """Return {"session_id": <thread_id>, "agent_path": <path>} from the active
    LangGraph RunnableConfig. Returns empty dict if called outside a graph run.
    """
    try:
        from langchain_core.runnables.config import var_child_runnable_config
    except ImportError:
        return {}
    cfg = var_child_runnable_config.get() or {}
    if not isinstance(cfg, dict):
        return {}
    conf = cfg.get("configurable", {}) or {}
    return {
        "session_id": conf.get("thread_id"),
        "agent_path": conf.get("agent_path") or "orchestrator",
    }
