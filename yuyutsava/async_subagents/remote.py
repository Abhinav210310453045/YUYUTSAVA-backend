"""Remote async subagent specs.

A *local* async subagent has its compiled graph living inside this process,
hosted by ``AsyncSubagentHost``. A *remote* async subagent's graph lives on
a different Agent Protocol server — could be another YUYUTSAVA daemon, a
deployed LangGraph platform endpoint, or any Agent Protocol-compatible
FastAPI service.

deepagents' ``AsyncSubAgent`` already supports either via its ``url`` field;
this dataclass is just a typed YUYUTSAVA-side wrapper so the factory
(``build_orchestrator`` / ``build_cli_deepagent``) can accept a uniform list
without callers having to construct deepagents dicts directly.

Out of scope for v1: auth-token refresh, retry/timeout policies, network-loss
handling. The watcher will mark a remote task ``error`` if the SDK raises.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RemoteAsyncSubagentSpec:
    """A background subagent hosted on a remote Agent Protocol server."""

    name: str                          # e.g. "research-bg" — name the master calls
    description: str
    graph_id: str                      # assistant_id / graph name on the remote
    url: str                           # "https://research.example.com/" — Agent Protocol root
    headers: dict[str, str] | None = None   # auth, e.g. {"Authorization": "Bearer ..."}

    def as_async_subagent_spec(self) -> dict:
        """Return the ``AsyncSubAgent`` TypedDict to hand to ``create_deep_agent``."""
        spec: dict = {
            "name": self.name,
            "description": self.description,
            "graph_id": self.graph_id,
            "url": self.url,
        }
        if self.headers:
            spec["headers"] = dict(self.headers)
        return spec
