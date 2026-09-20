"""What the active LangGraph run is: its thread, and which agent in the call tree.

``current_context()`` reads the `RunnableConfig` to surface session_id + agent_path;
``thread_id_from_runtime()`` reads the thread id alone.

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

from langchain_core.runnables.config import var_child_runnable_config
from langgraph.config import get_config


class AgentContext(TypedDict, total=False):
    session_id: str | None
    agent_path: str | None


def current_context() -> AgentContext:
    """Return {"session_id": <thread_id>, "agent_path": <path>} from the active
    LangGraph RunnableConfig. Returns empty dict if called outside a graph run.
    """
    cfg = var_child_runnable_config.get() or {}
    if not isinstance(cfg, dict):
        return {}
    conf = cfg.get("configurable", {}) or {}
    return {
        "session_id": conf.get("thread_id"),
        "agent_path": conf.get("agent_path") or "orchestrator",
    }


def thread_id_from_runtime(*, default: str = "unknown") -> str:
    """Best-effort thread id from the active LangGraph run config.

    *default* is what a caller gets outside a run, and it is a choice, not a
    detail. ``"unknown"`` is a usable key — every caller that passes it is
    storing something under the thread id and would rather file it under one
    bucket than under ``""``. The cost is that those buckets collide: two
    offloads made outside a run are indistinguishable. Callers that would
    rather have nothing than a shared bucket pass ``default=""``.
    """
    try:
        cfg = get_config() or {}
        return str(cfg.get("configurable", {}).get("thread_id", "") or default)
    except Exception:  # noqa: BLE001 — outside a graph run, or the shape changed
        return default
