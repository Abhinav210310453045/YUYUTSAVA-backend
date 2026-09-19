"""What a master agent is built out of.

`build_cli_deepagent` and `build_tinker_agent` took 33 and 32 parameters, 27 of
them the same 27, repeated at every call site. Threading one new dependency to
an agent meant editing two signatures and three callers, and nothing checked
that the two lists still agreed — which is how `budget_tokens` came to default
differently in each (`R-SOL-03`).

What stays out of here is what genuinely differs: where the CLI agent runs and
who is reading its output, and which card the tinker agent is bound to.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver

from yuyutsava.config.search import SearchConfig


@dataclass(frozen=True)
class MasterDeps:
    """Everything both master-agent builders need.

    Frozen: a bundle is built once from these, and a field rewritten afterwards
    would describe an agent that no longer matches the one running.

    Every field defaults to "absent", and absent is meaningful — no memory
    store means no memory tools, no checkpointer means the graph does not
    persist. A default-constructed ``MasterDeps`` builds the smallest agent that
    still works, which is what the simpler tests want.
    """

    # -- execution ---------------------------------------------------------
    bash_timeout_sec: int = 120
    permission_check: bool = True
    search_config: SearchConfig | None = None
    checkpointer: BaseCheckpointSaver | None = None

    # -- delegation --------------------------------------------------------
    subagents: list[Any] | None = None
    async_subagents: list[Any] | None = None
    async_host_url: str | None = None
    remote_async_subagents: list[Any] | None = None
    async_task_mirror: Any | None = None
    async_max_concurrent: int = 8
    async_host: Any | None = None
    async_host_attachment: Any | None = None

    # -- stores ------------------------------------------------------------
    artifact_store: Any | None = None
    summary_store: Any | None = None
    memory_store: Any | None = None
    transcript_store: Any | None = None
    transcript_index: Any | None = None
    skill_store: Any | None = None
    usage_store: Any | None = None
    prefs_store: Any | None = None

    # -- context and cost --------------------------------------------------
    context_settings: Any | None = None
    compaction_model: BaseChatModel | None = None
    #: ``None`` means no budget policy at all, which is deliberate for chat: a
    #: conversation's input grows with its own history, so a cumulative budget
    #: that never resets would eventually refuse every turn. The tinker agent
    #: sets its own default instead — see ``build_tinker_agent``.
    budget_tokens: int | None = None

    # -- extras ------------------------------------------------------------
    cap_enforcer: Any | None = None
    mcp_tools: list[Any] | None = None
    extra_tools: list[Any] | None = None
    runtime_settings: Any | None = None

    def replace(self, **changes: Any) -> "MasterDeps":
        """A copy with some fields changed — the frozen equivalent of a setter."""
        return dataclass_replace(self, **changes)


#: The tinker agent's own budget. It works a single TODO card, so its context is
#: bounded by that card rather than by an open-ended conversation, and a cap
#: keeps a runaway think loop from spending the session.
TINKER_BUDGET_TOKENS = 60_000

__all__ = ["TINKER_BUDGET_TOKENS", "MasterDeps"]
