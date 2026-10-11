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

from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver

from yuyutsava.config.search import SearchConfig
from yuyutsava.ports.models import TaskModelSource


@dataclass(frozen=True)
class MasterDeps:
    """Everything both master-agent builders need.

    Frozen: a bundle is built once from these, and a field rewritten afterwards
    would describe a different agent from the one running.

    Every field defaults to "absent", and absent is meaningful — no memory
    store means no memory tools, no checkpointer means the graph does not
    persist. A default-constructed ``MasterDeps`` builds the smallest agent that
    still works, which is what the simpler tests want.
    """

    # -- execution ---------------------------------------------------------
    bash_timeout_sec: int = 120
    #: Attach ``PermissionPolicy``, which asks before dangerous shell commands.
    permission_check: bool = True
    #: Adds the ws_* web search tools to the discoverable catalog.
    search_config: SearchConfig | None = None
    #: Persists the graph; ``None`` falls back to an in-process ``MemorySaver``.
    checkpointer: BaseCheckpointSaver | None = None

    # -- delegation --------------------------------------------------------
    #: ``BaseSubAgent`` instances reached through deepagents' ``task`` tool. A
    #: ``GeneralPurposeAgent`` here replaces deepagents' built-in one by name.
    subagents: list[Any] | None = None
    #: Background subagents hosted at ``async_host_url``; deepagents then adds
    #: the ``start_/check_/update_/cancel_/list_async_tasks`` tools.
    async_subagents: list[Any] | None = None
    #: The Agent Protocol server hosting them; required with ``async_subagents``.
    async_host_url: str | None = None
    #: Background subagents on another server (e.g. a peer daemon).
    remote_async_subagents: list[Any] | None = None
    #: Cross-turn task awareness; with any background subagent it also installs
    #: the concurrency cap (``async_max_concurrent``).
    async_task_mirror: Any | None = None
    async_max_concurrent: int = 8
    #: Recorded on the bundle so closing it tears the host down.
    async_host: Any | None = None
    async_host_attachment: Any | None = None

    # -- stores ------------------------------------------------------------
    # The context layer (see ``yuyutsava.context``); each is optional and
    # ``None`` disables what it backs. ``transcript_store`` keeps the verbatim
    # conversation beyond checkpoint sweeps.
    artifact_store: Any | None = None
    summary_store: Any | None = None
    memory_store: Any | None = None
    transcript_store: Any | None = None
    transcript_index: Any | None = None
    skill_store: Any | None = None
    usage_store: Any | None = None
    prefs_store: Any | None = None

    # -- models ------------------------------------------------------------
    #: Chooses each turn's model — the conversation's own choice, else its
    #: task's (``core.model_choice.ModelChooser``). Absent, every turn runs on
    #: the model the graph was built with.
    model_chooser: Any | None = None

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
    #: Prompt lines naming this master's MCP connectors and their state
    #: (``MCPClientManager.connectors_block``); ``""`` without MCP.
    mcp_connectors: str = ""
    extra_tools: list[Any] | None = None
    runtime_settings: Any | None = None
    #: The TaskRunnerAgent every tr_* tool of this master goes through (zones,
    #: policy, consent). A stack passes the one its subagents share; absent,
    #: the builder makes one for the workspace with neither policy nor consent.
    task_runner: Any | None = None
    #: The TODO board (``TodoExchange``) behind the todo_* tools; required by a
    #: profile that declares that family.
    board: Any | None = None
    #: The visual store behind the vis_* tools; required by a profile that
    #: declares that family.
    visual_store: Any | None = None

    def replace(self, **changes: Any) -> "MasterDeps":
        """A copy with some fields changed — the frozen equivalent of a setter."""
        return dataclass_replace(self, **changes)


@dataclass(frozen=True)
class StackWiring:
    """What a daemon hands a chat or tinker stack that a standalone CLI does not.

    Every field defaults to absent, which is the standalone CLI: it starts its
    own MCP manager from the workspace config and builds its own usage store.
    ``runtime_settings`` carries the user's dedicated-subagent switches (the
    chat bundle is cached across conversations, so the toggle is enforced per
    call rather than baked into the roster); ``extra_subagents`` join the sync
    ``task`` roster beside general-purpose; ``extra_tools`` are one-off daemon
    tools (e.g. orch_submit); ``mcp_manager_factory`` is the test seam for the
    manager a standalone stack starts.
    """

    mcp_manager: Any | None = None
    mcp_manager_factory: Any | None = None
    usage_store: Any | None = None
    budget_tokens: int | None = None
    prefs_store: Any | None = None
    runtime_settings: Any | None = None
    cap_enforcer: Any | None = None
    extra_subagents: list[Any] | None = None
    extra_tools: list[Any] | None = None
    #: The daemon's permissions policy and consent registry. Every gateway a
    #: stack builds uses these, so a grant made in a chat is the daemon's grant
    #: (persisted, visible to every surface). Absent: no policy, and the stack
    #: starts its own in-memory registry.
    policy: Any | None = None
    consent: Any | None = None
    #: The daemon's board (``TodoExchange``: its routed store and note index),
    #: so a chat or tinker writes the same board the app shows. Absent: the
    #: stack builds its own (``core.stores.standalone_board``).
    board: Any | None = None
    #: The daemon's visual store (routed on Postgres), so a chart made in a chat
    #: shows in the app. Absent: the stack's own factory store.
    visual_store: Any | None = None
    #: Which model each task uses — the daemon's or the CLI's own
    #: ``TaskModels``, following ``models.toml``. Absent: models from the
    #: environment (``LLM_PROVIDER``), as a test builds them.
    models: TaskModelSource | None = None
    #: The session store whose rows carry each conversation's own model choice
    #: (``Session.models``). Absent: conversations follow ``models.toml`` alone.
    sessions: Any | None = None
    #: An interactive chat: the stack's own MCP servers start in the background,
    #: and their tools join the conversation, between messages, as each comes up.
    #: Absent (a one-shot task has no next message): the build waits for them.
    mcp_in_background: bool = False
    #: Told, in plain words, what the build is doing now — for a status line.
    progress: Callable[[str], None] | None = None


#: The tinker agent's own budget. It works a single TODO card, so its context is
#: bounded by that card rather than by an open-ended conversation, and a cap
#: keeps a runaway think loop from spending the session.
TINKER_BUDGET_TOKENS = 60_000

__all__ = ["TINKER_BUDGET_TOKENS", "MasterDeps", "StackWiring"]
