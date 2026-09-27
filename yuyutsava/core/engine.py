"""
Build a **Deep Agents** graph with an OpenAI-compatible chat model and a real-disk backend.

Uses ``LocalShellBackend`` (filesystem + ``execute``) so built-in ``read_file`` /
``write_file`` / ``ls`` / … map to the workspace root, and ``execute`` runs shell
on the host per deepagents (see ``deepagents.backends.LocalShellBackend``).

This module is intentionally narrow: it owns the *build* side of the agent
graphs (CLI deepagent + daemon orchestrator). Streaming + interrupt handling
live in :mod:`yuyutsava.core.streaming`; the CLI system prompts live in
:mod:`yuyutsava.agents.prompts`; the tool-result size guard lives in
:mod:`yuyutsava.core.tool_result`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, LocalShellBackend
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph

from yuyutsava.agents.orchestrator.agent import make_ask_user_tool
from yuyutsava.agents.orchestrator.capabilities import render_capabilities_block
from yuyutsava.agents.orchestrator.prompts import render_system_prompt
from yuyutsava.agents.prompts import docker_system_prompt, local_system_prompt
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.agents.task_runner.exec_backend import DockerExecBackend
from yuyutsava.agents.task_runner.tools import bind_tools as _bind_task_runner_tools
from yuyutsava.agents.tinker.prompts import render_tinker_system_prompt
from yuyutsava.artifacts.tools import make_artifact_tools
from yuyutsava.async_subagents.cap_policy import BackgroundTaskCapPolicy
from yuyutsava.async_subagents.check_guard_policy import CheckAsyncTaskGuardPolicy
from yuyutsava.async_subagents.host_lock import release_host_lock
from yuyutsava.async_subagents.interrupt_patch_policy import AsyncTaskInterruptPatchPolicy
from yuyutsava.config.execution import DockerSettings, LocalSettings
from yuyutsava.config.search import SearchConfig
from yuyutsava.context.compaction import YuyutsavaCompactionMiddleware
from yuyutsava.context.conversation_injector import ConversationInjector
from yuyutsava.context.injector import MemoryInjector
from yuyutsava.context.meter import ContextMeterPolicy, note_bound_tools
from yuyutsava.context.offload_policy import ToolResultOffloadPolicy
from yuyutsava.context.prompt_inspector import PromptInspectorPolicy
from yuyutsava.context.tools import make_context_tools
from yuyutsava.context.transcript_policy import TranscriptRecorderPolicy
from yuyutsava.core.agent_profiles import (
    CLI_PROFILE,
    ORCHESTRATOR_PROFILE,
    TINKER_PROFILE,
    AgentProfile,
    PolicyKind,
    ToolFamily,
)
from yuyutsava.core.deps import TINKER_BUDGET_TOKENS, MasterDeps
from yuyutsava.discovery.tool_registry import ToolRegistry
from yuyutsava.docker_sandbox.backend import DockerSandboxBackend
from yuyutsava.events.tools import make_recall_tool
from yuyutsava.llm.factory import chat_model, model_name_of
from yuyutsava.llm.settings import LlmSettings
from yuyutsava.memory.agent_memory import AgentMemoryStore
from yuyutsava.memory.agent_memory_tools import make_agent_memory_tools
from yuyutsava.memory.tools import make_memory_tools
from yuyutsava.policy.adapter import (
    LangChainPolicyAdapter,
    collapse_policy_adapters,
)
from yuyutsava.policy.builtin.budget import BudgetPolicy
from yuyutsava.policy.builtin.filesystem_prompt import FilesystemPromptPolicy
from yuyutsava.policy.builtin.permission import PermissionPolicy
from yuyutsava.policy.builtin.retrieval_injection import RetrievalInjectionPolicy
from yuyutsava.policy.builtin.subagent_gate import SubagentGatePolicy
from yuyutsava.policy.builtin.tool_filter import ToolFilterPolicy
from yuyutsava.policy.builtin.usage import UsagePolicy
from yuyutsava.policy.builtin.voice_style import VoiceStylePolicy
from yuyutsava.prefs.injector import PrefsInjector
from yuyutsava.skills.injector import SkillInjector
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.skills.tools import make_skill_tools
from yuyutsava.storage.paths import (
    CONTAINER_STATE_MOUNT,
    CONTAINER_WORKSPACE_MOUNT,
    WORKSPACE_STATE_DIRNAME,
    WorkspaceLayout,
)
from yuyutsava.todoboard.recall import TodoNoteInjector
from yuyutsava.todoboard.tools import make_todo_tools
from yuyutsava.visuals.tools import make_visual_tools
from yuyutsava.websearch.tools import make_search_tools

if TYPE_CHECKING:
    from yuyutsava.agents.orchestrator.agent import OrchestratorDeps

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logger = logging.getLogger("yuyutsava")


# ---------------------------------------------------------------------------
# Agent bundle
# ---------------------------------------------------------------------------


@dataclass
class AgentBundle:
    """Compiled Deep Agent graph plus optional Docker resources to tear down.

    ``async_host`` and ``async_task_mirror`` are wired in when async (background)
    subagents are enabled. CLI Mode 1 owns these objects directly; the daemon
    keeps them on ``DaemonSubsystems`` and threads them through ``OrchestratorDeps``.

    ``async_host_url`` is the live URL of the shared LangGraph dev server —
    set whether or not this process owns the host. Code that needs to dial
    the host (watchers, SDK clients) should prefer this string over
    ``async_host.url`` so attached clients work too.

    ``async_host_attachment`` is the ownership handle returned by
    :func:`yuyutsava.async_subagents.host_lock.acquire_or_attach_host`.
    ``close()`` releases it (no-op when this process is an attacher, not
    the owner).
    """

    agent: CompiledStateGraph
    docker_backend: DockerSandboxBackend | None = None
    sandbox_root: Path | None = None
    output_dir: Path | None = None
    layout: WorkspaceLayout | None = None  # per-workspace .yuyutsava paths (post-task cleanup)
    async_host: Any | None = None  # AsyncSubagentHost — duck-typed to avoid cycle
    async_host_url: str | None = None
    async_host_attachment: Any | None = None  # HostAttachment
    async_task_mirror: Any | None = None  # AsyncTaskMirror
    pg_pool: Any | None = None  # PgPool owned by the CLI (closed in aclose)
    embedder: Any | None = None  # memory.Embedder owned by the CLI
    mcp_manager: Any | None = None  # MCPClientManager owned by the CLI (stopped in aclose)
    #: Things still writing through the pool in the background (each has an
    #: ``async drain()``), drained in aclose before the pool closes.
    background_writers: list[Any] = field(default_factory=list)
    usage_store: Any | None = None  # UsageStore the graph writes to — read for /usage

    async def aclose(self) -> None:
        """Async teardown: stop the CLI-owned MCP manager (its sessions live on
        this loop, so it must go first), let background writers finish, close
        the pool + embedder, then close()."""
        if self.mcp_manager is not None:
            try:
                await self.mcp_manager.stop()
            except Exception:
                logger.exception("AgentBundle: mcp_manager.stop failed")
        for writer in self.background_writers:
            try:
                await writer.drain()
            except Exception:
                logger.exception("AgentBundle: draining %r failed", writer)
        if self.embedder is not None:
            try:
                await self.embedder.aclose()
            except Exception:
                logger.exception("AgentBundle: embedder.aclose failed")
        if self.pg_pool is not None:
            try:
                await self.pg_pool.close()
            except Exception:
                logger.exception("AgentBundle: pg_pool.close failed")
        self.close()

    def close(self) -> None:
        if self.docker_backend is not None:
            self.docker_backend.stop()
        if self.async_host_attachment is not None:
            try:
                release_host_lock(self.async_host_attachment)
            except Exception:
                logger.exception("AgentBundle: release_host_lock failed")
        elif self.async_host is not None:
            # Legacy direct-ownership path (kept for callers that haven't
            # migrated to acquire_or_attach_host yet).
            try:
                self.async_host.shutdown()
            except Exception:
                logger.exception("AgentBundle: async_host.shutdown failed")


# ---------------------------------------------------------------------------
# Backend factory
# ---------------------------------------------------------------------------


def _local_shell_backend(
    workspace_root: Path | str,
    bash_timeout_sec: int,
    *,
    inherit_env: bool = True,
) -> LocalShellBackend:
    """One shared ``LocalShellBackend`` instance for a graph.

    ``inherit_env`` is a per-role policy, not a detail: the CLI and tinker
    masters inherit the user's environment (they run the user's own tooling),
    while the daemon orchestrator deliberately does not (``inherit_env=False``,
    with a tighter 10s timeout) because it executes unattended.

    Returns an **instance**, not a factory closure. deepagents accepts both
    today (``backend: BackendProtocol | BackendFactory | None``) but removes
    the callable form in **0.7.0**, which would otherwise break every
    non-Docker agent in the system. Passing an instance works on 0.6.x and
    0.7+, so this is the forward-compatible form.

    Sharing one instance is safe here: the previous factory ignored its
    ``_runtime`` argument entirely and rebuilt an identical object from
    closure-captured constants on every call, and ``LocalShellBackend``
    assigns no attributes outside ``__init__`` (verified against 0.6.3).

    The one observable difference is ``backend.id``: it was a fresh
    ``local-<uuid8>`` per model call and is now stable for the graph's
    lifetime. Nothing in this codebase reads it, and the Docker path already
    passes a single instance with a stable id — so this makes local execution
    consistent with Docker rather than diverging from it.
    """
    root = workspace_root if isinstance(workspace_root, str) else str(workspace_root.resolve())
    return LocalShellBackend(
        root_dir=root,
        virtual_mode=True,
        timeout=bash_timeout_sec,
        inherit_env=inherit_env,
    )


# Virtual path (relative to a backend whose root is the workspace) under which
# deepagents keeps its scratch: large_tool_results/ and conversation_history/.
_SCRATCH_ROOT = f"/{WORKSPACE_STATE_DIRNAME}/tmp"


def _scratch_root_backend(backend: Any, artifacts_root: str) -> CompositeBackend:
    """Route deepagents' scratch under *artifacts_root* without changing the root.

    ``FilesystemMiddleware`` and the summarization middleware write offloaded
    tool results and transcript dumps to ``{artifacts_root}/large_tool_results``
    and ``{artifacts_root}/conversation_history`` — but only read
    ``artifacts_root`` off a ``CompositeBackend`` (deepagents 0.6.3,
    ``middleware/filesystem.py`` and ``middleware/summarization.py``). With
    ``routes={}`` every file op and ``execute`` still go to *backend*
    unchanged, so the virtual root (and every backend docstring above) holds;
    only the scratch moves — into ``<ws>/.yuyutsava/tmp`` for a workspace-rooted
    backend. This is an instance, not a factory (see ``_local_shell_backend``).
    """
    return CompositeBackend(default=backend, routes={}, artifacts_root=artifacts_root)


# ---------------------------------------------------------------------------
# Build agent
# ---------------------------------------------------------------------------


def _build_tool_registry_and_tools(
    task_runner_tools: list,
    search_config: SearchConfig | None,
    skill_registry: SkillRegistry | None,
    extra_tools: list | None = None,
    skill_store: Any | None = None,
    agent_name: str | None = None,
    cap_enforcer: Any | None = None,
) -> tuple[list, Any]:
    """Build a ToolRegistry and return (startup_tools, registry).

    startup_tools = [tool_search] + all_custom_tools
    All custom tools go into the graph for execution; only tool_search is
    visible to the LLM upfront (ToolFilterPolicy hides tr_*/ws_* etc.).
    ``extra_tools`` rides along for families outside the fixed sets (e.g.
    the always-visible ctx_* artifact readers). ``cap_enforcer`` rate-caps the
    ws_* searches (daemon policy); None (standalone CLI, no events store)
    leaves them uncapped.
    """
    # Deferred: the db_* tools need sqlglot, which adds ~45 ms to every CLI
    # start, including commands that never build an agent.
    from yuyutsava.storage.introspect_tools import make_db_tools  # noqa: PLC0415

    search_tools = (
        make_search_tools(search_config, cap_enforcer=cap_enforcer) if search_config else []
    )
    skill_tools = (
        make_skill_tools(skill_registry, skill_store, agent_name=agent_name)
        if skill_registry
        else []
    )
    db_tools = make_db_tools()  # always available — read-only by construction
    all_custom_tools = (
        task_runner_tools + search_tools + skill_tools + db_tools + (extra_tools or [])
    )

    registry = ToolRegistry()
    registry.register_many(all_custom_tools)
    tool_search = registry.make_tool_search_tool()

    # tool_search first so it appears first in the graph's tool list.
    # ToolFilterPolicy hides all tr_* / ws_* / sk_* from the LLM; only
    # tool_search is visible, driving the lazy-discovery pattern.
    startup_tools = [tool_search] + all_custom_tools

    # Let the context meter price the prompt's tool schemas. Measured here
    # because the middleware is assembled before this function runs, and from
    # `startup_tools` rather than the registry because tool_search — the one
    # tool the model always sees — is built by the registry, not registered in
    # it, and would otherwise be counted as free.
    note_bound_tools(agent_name or "agent", registry, startup_tools)
    return startup_tools, registry


# Fixed pipeline order. The PROFILE says which policies a master has; this says
# where each one goes. Two separate facts — a profile is a set, and a set has no
# order, which is exactly why ordering could not live there.
#
# `pre` runs before the context controllers, `post` after: budget and usage must
# observe POST-compaction token counts, so they cannot be hoisted.
_POLICY_ORDER_PRE: tuple[PolicyKind, ...] = (
    PolicyKind.TOOL_FILTER,
    PolicyKind.FILESYSTEM_PROMPT,
    PolicyKind.VOICE_STYLE,
    PolicyKind.SUBAGENT_GATE,
)
_POLICY_ORDER_POST: tuple[PolicyKind, ...] = (
    PolicyKind.BUDGET,
    PolicyKind.USAGE,
    PolicyKind.PERMISSION,
)


def _policy_middleware(
    profile: AgentProfile,
    phase: str,
    *,
    role: str,
    runtime_settings: Any | None = None,
    budget_tokens: int | None = None,
    usage_store: Any | None = None,
    model: Any | None = None,
    workspace_root: Path | None = None,
    permission_check: bool = False,
    usage_ids: tuple[str | None, str | None] = (None, None),
) -> list:
    """Build one phase of a master's policy middleware, from its profile.

    A policy appears when the profile declares it **and** its wiring is present.
    Those are different questions and both have to be yes:

    * the **profile** says the master *may* have this policy — a capability, the
      thing ADR-001 makes data;
    * the **argument** supplies it — the standalone CLI passes no ``usage_store``
      and gets no accounting, without that being a different kind of agent.

    So dropping ``PolicyKind.BUDGET`` from a profile disables budgeting for that
    master everywhere, while passing ``budget_tokens=None`` just leaves it
    unwired here. Conflating the two is why this was assembled by hand three
    times.
    """
    order = _POLICY_ORDER_PRE if phase == "pre" else _POLICY_ORDER_POST
    out: list = []
    for policy in order:
        if policy not in profile.policies:
            continue
        if policy is PolicyKind.TOOL_FILTER:
            out.append(LangChainPolicyAdapter([ToolFilterPolicy()]))
        elif policy is PolicyKind.FILESYSTEM_PROMPT:
            # Strips the deepagents "## Filesystem Tools" block: those built-ins
            # are filtered out by ToolFilterPolicy and our prompt routes
            # filesystem work through tr_*, so the block only misleads the model
            # and burns ~700 cache-prefix tokens per turn.
            out.append(LangChainPolicyAdapter([FilesystemPromptPolicy()]))
        elif policy is PolicyKind.VOICE_STYLE:
            out.append(LangChainPolicyAdapter([VoiceStylePolicy()]))
        elif policy is PolicyKind.SUBAGENT_GATE:
            # Unconditional, and `runtime_settings` may be None: the gate treats
            # that as "no toggles configured" and passes everything. Guarding on
            # it here would silently drop the policy for the standalone CLI —
            # caught by the fingerprint gate when this was first written that way.
            out.append(LangChainPolicyAdapter([SubagentGatePolicy(runtime_settings)]))
        elif policy is PolicyKind.BUDGET and budget_tokens is not None:
            out.append(
                LangChainPolicyAdapter([BudgetPolicy(max_input_tokens=budget_tokens, role=role)])
            )
        elif policy is PolicyKind.USAGE and usage_store is not None:
            # task_id/thread_id are pinned only where the bundle IS one
            # conversation — the tinker graph is per-card. The chat bundle is
            # shared across threads, so its rows land task-less/thread-less,
            # which the store supports.
            usage_task_id, usage_thread_id = usage_ids
            extra = {}
            if usage_task_id is not None:
                extra["task_id"] = usage_task_id
            if usage_thread_id is not None:
                extra["thread_id"] = usage_thread_id
            out.append(
                LangChainPolicyAdapter(
                    [
                        UsagePolicy(
                            usage_store,
                            role=role,
                            model_name=model_name_of(model),
                            **extra,
                        )
                    ]
                )
            )
        elif policy is PolicyKind.PERMISSION and permission_check and workspace_root:
            # PermissionPolicy imports no framework and is tested without one
            # (test/policy/test_permission_parity.py); the adapter is what
            # attaches it to the graph.
            out.append(
                LangChainPolicyAdapter([PermissionPolicy(workspace_root=workspace_root.resolve())])
            )
    return out


def _context_middleware(
    *,
    model: BaseChatModel,
    artifact_store: Any | None,
    context_settings: Any | None,
    summary_store: Any | None = None,
    memory_store: Any | None = None,
    transcript_store: Any | None = None,
    transcript_index: Any | None = None,
    compaction_model: BaseChatModel | None = None,
    role: str = "agent",
    meter_window: bool = True,
) -> list:
    """Build the context-controller middleware for one agent.

    Order matters downstream: offload must run on the tool path before the
    compactor ever counts tokens, and both sit before BudgetPolicy (the
    absolute spend ceiling) in the caller's list. The transcript recorder is
    appended last — it only reads ``state["messages"]`` and never rewrites
    state, so its position is immaterial. Returns [] when the context
    controller is not wired (stores absent), preserving the
    pre-context-controller behaviour exactly.
    """

    out: list = []
    if artifact_store is not None and context_settings is not None:
        out.append(
            LangChainPolicyAdapter([ToolResultOffloadPolicy(artifact_store, context_settings)])
        )
    if context_settings is not None:
        out.append(
            YuyutsavaCompactionMiddleware(
                model=compaction_model or model,
                settings=context_settings,
                summary_store=summary_store,
                memory_sink=memory_store,
                role=role,
                # A transcript store means the ctx_history* readers exist, so
                # the summary may tell the model the evicted turns are still
                # readable. Without one, promising that would be false.
                history_readback=transcript_store is not None,
            )
        )
    if transcript_store is not None:
        out.append(
            LangChainPolicyAdapter(
                [TranscriptRecorderPolicy(transcript_store, index=transcript_index)]
            )
        )

    # Observability, last in the list so these before_model hooks report the
    # message list AFTER offload + compaction — i.e. exactly what the model
    # receives. Neither mutates state.
    #
    # The inspector is a debugging dump (no-op unless YUYUTSAVA_DEBUG_PROMPT);
    # the meter is always on, because the thing it measures — how full the
    # window is — is what a user needs *before* a session gets into trouble,
    # not after. It publishes onto a process-global bus that the CLI dashboard
    # and the daemon's websocket subscribe to; with no subscriber it is a
    # dataclass built and dropped.
    observers: list[Any] = [PromptInspectorPolicy(role=role)]
    if context_settings is not None:
        observers.append(
            ContextMeterPolicy(
                settings=context_settings,
                role=role,
                model=model,
                model_name=model_name_of(model),
                # A subagent's message list is its own, not the conversation's
                # window; it reports spend but must not redraw the panel.
                window=meter_window,
            )
        )
    out.append(LangChainPolicyAdapter(observers))
    return out


# Public alias: external builders (async_subagents host wiring in bootstrap /
# agent stacks) attach the same context controllers to their graphs.
context_middleware = _context_middleware


def _shared_master_tools(
    *,
    profile: "AgentProfile",
    artifact_store: Any | None = None,
    memory_store: Any | None = None,
    transcript_store: Any | None = None,
    output_dir: Path | None = None,
    mcp_tools: "Sequence[Any] | None" = None,
    extra_tools: "Sequence[Any] | None" = None,
    todo_author: str | None = None,
    board: Any | None = None,
    visual_store: Any | None = None,
) -> tuple[list, str]:
    """Tool families every master shares, selected by its declared profile.

    Returns ``(tools, agent_memory_index_block)``. The block is the ``MEMORY.md``
    index for this master's ``um_*`` store; the CLI and tinker masters append it
    to their system prompt, while the orchestrator's is injected per task by the
    daemon loop — so it is returned rather than applied.

    **This is the first place a builder reads its ``AgentProfile`` instead of
    hardcoding.** Which families are wired, the ``todo_*`` scope and the ``um_*``
    namespace all come from the profile, so the capability matrix is now the
    source of truth for them rather than a description of them. Adding a family
    to a master is a profile edit, and the conformance tests fail if the profile
    and the builder disagree.

    Families NOT handled here are the ones that are genuinely per-role: the
    orchestrator's ``ask_user``/``recall`` pair, and the ``tr_*``/``ws_*``/
    ``sk_*``/``db_*`` set that the CLI and tinker masters obtain through
    ``_build_tool_registry_and_tools``.

    Tool ORDER is not behavioural: the prompt catalog groups by prefix and sorts
    within each group (``discovery/keyword_provider.catalog_block``), so the two
    builders' historically different orderings produced identical prompts.
    """

    tools: list = []
    fams = profile.tools

    if ToolFamily.CONTEXT in fams and artifact_store is not None:
        # transcript_store adds the ctx_history* trio — the read-back path for
        # turns compaction evicted. Absent it (no transcript wiring) the
        # offload readers are still offered; only the history readers drop.
        tools.extend(make_context_tools(artifact_store, transcript_store))

    if ToolFamily.MEMORY in fams and memory_store is not None:
        tools.extend(make_memory_tools(memory_store))

    if ToolFamily.VISUALS in fams:
        if output_dir is None:
            raise ValueError(
                f"{profile.role}: profile declares the vis_* family but no output_dir "
                "was supplied for rendered visuals to land in."
            )

        if visual_store is None:
            raise ValueError(
                f"{profile.role}: profile declares the vis_* family but no visual store "
                "was supplied for it to record visuals in."
            )
        tools.extend(make_visual_tools(visual_store, output_dir=output_dir))

    if ToolFamily.TODO in fams:
        if board is None:
            raise ValueError(
                f"{profile.role}: profile declares the todo_* family but no board was "
                "supplied for it to write to."
            )
        kwargs = {"scope": profile.todo_scope}
        if todo_author:
            kwargs["author"] = todo_author
        tools.extend(make_todo_tools(board, **kwargs))

    # Per-agent user-behaviour memory. Namespaces must not collide — two masters
    # sharing one would cross-contaminate learned behaviours.
    agent_memory_block = ""
    if ToolFamily.AGENT_MEMORY in fams:
        store = AgentMemoryStore(profile.agent_memory_namespace)
        tools.extend(make_agent_memory_tools(store))
        agent_memory_block = store.read_index_block()

    if ToolFamily.ARTIFACTS in fams:
        # Inline artifacts (interactive HTML/JSX, docs, audio) rendered in the
        # reply itself — the richer, non-card twin of the vis_* visuals.
        tools.extend(make_artifact_tools())

    if ToolFamily.MCP in fams and mcp_tools:
        tools.extend(mcp_tools)

    # Caller-supplied one-offs (e.g. the daemon's orch_submit) ride the same
    # registry as everything else.
    if extra_tools:
        tools.extend(extra_tools)

    return tools, agent_memory_block


def _async_subagent_wiring(
    *,
    role: str,
    async_subagents: "Sequence[Any] | None",
    async_host_url: str | None,
    remote_async_subagents: "Sequence[Any] | None" = None,
    async_task_mirror: Any | None = None,
    async_max_concurrent: int = 8,
    disabled: "frozenset[str]" = frozenset(),
) -> tuple[list[dict], list]:
    """Background-subagent specs + the middleware that governs them.

    Returns ``(specs, middleware)``; both empty when no async subagents are
    wired. All three master builders had their own copy of this, including three
    copies of the same host-URL guard differing only in the function name they
    quoted.

    Three rules live here, and each was previously restated per builder:

    1. ``async_host_url`` is mandatory whenever local async subagents exist —
       their specs point at the Agent Protocol server hosting the graphs.
    2. A subagent must opt in via ``supports_async``; ones that do not are
       silently skipped rather than failing the build.
    3. An async spec is identified **by dict shape** (``graph_id`` + ``url``),
       which is how ``deepagents`` routes it to ``AsyncSubAgentMiddleware``.
       That duck-typed test was written out three times; it lives here now.

    Args:
        role: builder name, used only to make the guard's error message name the
            caller that misconfigured it.
        disabled: subagent names switched off at runtime. Only the orchestrator
            passed this — the CLI and tinker masters never filtered their async
            roster, which is why the gate is listed as an unreviewed divergence
            in :mod:`yuyutsava.core.agent_profiles`.
    """
    specs: list[dict] = []
    middleware: list = []

    local = list(async_subagents or [])
    remote = list(remote_async_subagents or [])

    if local:
        if not async_host_url:
            raise ValueError(
                f"{role}: async_subagents requires async_host_url. "
                "Build an AsyncSubagentHost and pass its .url here."
            )
        for sa in local:
            if not getattr(sa, "supports_async", False) or getattr(sa, "name", "") in disabled:
                continue
            specs.append(sa.as_async_subagent_spec(url=async_host_url))

    for r in remote:
        if getattr(r, "name", "") in disabled:
            continue
        specs.append(r.as_async_subagent_spec())

    if not (local or remote):
        return specs, middleware

    if async_task_mirror is not None:
        middleware.append(
            LangChainPolicyAdapter(
                [BackgroundTaskCapPolicy(async_task_mirror, max_concurrent=async_max_concurrent)]
            )
        )

    # One adapter per policy, in stack order; collapse_policy_adapters later
    # merges them into one adapter placed after every non-adapter entry, which
    # keeps each hook chain's order.
    async_specs = [s for s in specs if "graph_id" in s and "url" in s]
    middleware.append(LangChainPolicyAdapter([AsyncTaskInterruptPatchPolicy(async_specs)]))
    middleware.append(LangChainPolicyAdapter([CheckAsyncTaskGuardPolicy()]))
    return specs, middleware


def _retrieval_injection_middleware(
    *,
    memory_store: Any | None = None,
    skill_store: Any | None = None,
    skill_scope: str | None = None,
    transcript_index: Any | None = None,
    note_index: Any | None = None,
    prefs_store: Any | None = None,
) -> list:
    """Per-turn retrieval injectors, in prompt order. ``[]`` when none are wired.

    All three master builders assembled this list themselves, in three
    near-identical blocks that had already drifted: the CLI's ``SkillInjector``
    is unscoped while the orchestrator's and tinker's are scoped to themselves,
    and only tinker wires ``TodoNoteInjector``. Those differences are real and
    are preserved here as *arguments*, so they are now visible at the call site
    instead of buried in three separate code paths.

    **Order is behaviour**, not style: injected blocks are concatenated into the
    prompt in list order, and the prompt is a cached prefix. Reordering changes
    what the model reads first and invalidates prompt caches. The order below —
    memory, skills, conversation, board notes, prefs — is the one all three
    builders already used; it is asserted by
    ``test/core/test_agent_fingerprint.py``.

    Args:
        skill_scope: ``agent=`` for SkillInjector. ``None`` searches every
            agent's skills (the CLI's historical behaviour); a role name scopes
            retrieval to that agent's own skills.
        note_index: TODO-board note recall. Tinker-only — a master that is not
            pinned to a card has no board context to recall.
    """
    injectors: list = []

    if memory_store is not None:
        injectors.append(MemoryInjector(memory_store))

    if skill_store is not None:
        injectors.append(SkillInjector(skill_store, agent=skill_scope))

    if transcript_index is not None:
        # Recall this conversation's own earlier turns; survives the checkpoint sweep.
        injectors.append(ConversationInjector(transcript_index))

    if note_index is not None:
        # Board-wide: a related decision often lives on a DIFFERENT card than
        # the one this bundle is pinned to.
        injectors.append(TodoNoteInjector(note_index))

    if prefs_store is not None:
        # Whitelisted user preferences, refreshed per turn.
        injectors.append(PrefsInjector(prefs_store))

    if not injectors:
        return []

    return [LangChainPolicyAdapter([RetrievalInjectionPolicy(injectors)])]


def _subagent_spec(
    sa: Any,
    *,
    context_middleware: "Sequence[Any]",
    extra_middleware: "Sequence[Any]" = (),
    model: BaseChatModel | None = None,
    artifact_store: Any | None = None,
) -> dict:
    """One delegation spec, wired the way every builder wires one.

    What is the same everywhere: the filter that hides the master's prefixed
    tools from a subagent, the context controllers, and the ``ctx_*`` pair a
    subagent needs to read back its own offloaded results — fresh instances per
    spec, never shared across graphs.

    What differs is passed in. The orchestrator adds a token budget and usage
    accounting and names a model; the CLI and tinker builds add neither,
    because they have no subagent budget configured. Those are ADR-001
    divergences, so they stay visible at the call site rather than becoming
    flags here.
    """
    spec = sa.as_deepagents_subagent_spec()
    if model is not None:
        spec["model"] = model
    spec["middleware"] = collapse_policy_adapters(
        [
            LangChainPolicyAdapter([ToolFilterPolicy()]),
            *context_middleware,
            *extra_middleware,
        ]
    )
    if artifact_store is not None:
        spec["tools"] = list(spec.get("tools") or []) + make_context_tools(artifact_store)
    return spec


def _sync_subagent_specs(
    subagents: "Sequence[Any]",
    *,
    model: BaseChatModel,
    artifact_store: Any | None,
    context_settings: Any | None,
    summary_store: Any | None,
    memory_store: Any | None,
    compaction_model: BaseChatModel | None,
) -> list[dict]:
    """Sync-delegation specs with the orchestrator's context treatment.

    Mirrors build_orchestrator's per-spec wiring: ToolFilterPolicy +
    context middleware (tool-result offload, compaction) and the ctx_*
    readback pair — without this, a delegated general-purpose run keeps
    every tool result inline. No BudgetPolicy here: the CLI/tinker
    builds have no subagent token-budget config. transcript_store stays
    None — sync subagent runs live inside the master's turn; transcripts
    serve interactive resume.
    """
    return [
        _subagent_spec(
            sa,
            context_middleware=_context_middleware(
                model=model,
                artifact_store=artifact_store,
                context_settings=context_settings,
                summary_store=summary_store,
                memory_store=memory_store,
                transcript_store=None,
                compaction_model=compaction_model,
                role=sa.name,
                meter_window=False,
            ),
            artifact_store=artifact_store,
        )
        for sa in subagents
    ]


def _master_middleware(
    profile: AgentProfile,
    deps: MasterDeps,
    model: BaseChatModel,
    workspace_root: Path,
    *,
    usage_ids: tuple[str | None, str | None] = (None, None),
    note_index: Any | None = None,
) -> list:
    """A master's middleware in run order, before any background wiring.

    Pre-policies, then the context layer, then budget and usage — after
    compaction, so they see post-compaction counts (the ordering itself lives in
    ``_POLICY_ORDER_POST``). Each of those two attaches only when given its
    input: usage when a usage store is passed, budget when
    ``deps.budget_tokens`` is. ``usage_ids`` pins usage rows to a task and
    thread — the tinker graph IS one conversation; the chat bundle is shared
    across threads, so its rows land task-less and thread-less.

    Last, per-turn retrieval injection: a master's graph is persistent, so it
    cannot inject at build time the way the daemon orchestrator does. Skills are
    scoped to the master's own role plus the shared ones (the store filter is
    ``agent IS NULL OR agent = <role>``). ``note_index`` recalls board notes
    across all cards; only the tinker agent has one.
    """
    role = profile.role
    # FilesystemPromptPolicy (a pre-policy) strips the deepagents "## Filesystem
    # Tools" block: those built-in tools are filtered out by ToolFilterPolicy
    # and our own prompt routes filesystem ops through tr_*, so the block only
    # misleads the model and wastes cache-prefix tokens. The subagent gate is a
    # pre-policy too; a tinker bundle is cached per card and can outlive a
    # toggle change, so without it a disabled subagent would stay reachable.
    middleware = _policy_middleware(
        profile,
        "pre",
        role=role,
        runtime_settings=deps.runtime_settings,
    )
    middleware.extend(
        _context_middleware(
            model=model,
            artifact_store=deps.artifact_store,
            context_settings=deps.context_settings,
            summary_store=deps.summary_store,
            memory_store=deps.memory_store,
            transcript_store=deps.transcript_store,
            transcript_index=deps.transcript_index,
            compaction_model=deps.compaction_model,
            role=role,
        )
    )
    middleware.extend(
        _policy_middleware(
            profile,
            "post",
            role=role,
            budget_tokens=deps.budget_tokens,
            usage_store=deps.usage_store,
            model=model,
            workspace_root=workspace_root,
            permission_check=deps.permission_check,
            usage_ids=usage_ids,
        )
    )
    middleware.extend(
        _retrieval_injection_middleware(
            memory_store=deps.memory_store,
            skill_store=deps.skill_store,
            skill_scope=role,
            transcript_index=deps.transcript_index,
            note_index=note_index,
            prefs_store=deps.prefs_store,
        )
    )
    return middleware


def _master_subagents(deps: MasterDeps, model: BaseChatModel, role: str) -> tuple[list, list]:
    """A master's subagent specs, and the middleware background ones need.

    Every master delegates freely: any task, sync or async, local or on a peer
    daemon. Omitting ``remote_async_subagents`` would silently deny a master
    cross-daemon background work its siblings can do.
    """
    specs: list[dict] = []
    if deps.subagents:
        specs.extend(
            _sync_subagent_specs(
                deps.subagents,
                model=model,
                artifact_store=deps.artifact_store,
                context_settings=deps.context_settings,
                summary_store=deps.summary_store,
                memory_store=deps.memory_store,
                compaction_model=deps.compaction_model,
            )
        )
    async_specs, async_mw = _async_subagent_wiring(
        role=role,
        async_subagents=deps.async_subagents,
        async_host_url=deps.async_host_url,
        remote_async_subagents=deps.remote_async_subagents,
        async_task_mirror=deps.async_task_mirror,
        async_max_concurrent=deps.async_max_concurrent,
    )
    specs.extend(async_specs)
    return specs, async_mw


def _docker_sandbox(
    cfg: DockerSettings, layout: WorkspaceLayout, sandbox_root: Path, timeout: int
) -> DockerSandboxBackend:
    """The CLI agent's sandbox container.

    Its read-write mount is the workspace's ``.yuyutsava`` (sandbox, scripts,
    outputs, tmp) — also the deepagents virtual root; the workspace itself is
    mounted read-only beside it. A sandbox override outside that state dir is
    not mounted, which is worth a warning rather than a silent failure later.
    """
    backend = DockerSandboxBackend(
        cfg,
        workspace_host=layout.state,
        container_workdir=CONTAINER_STATE_MOUNT,
        extra_mounts=[(layout.root, CONTAINER_WORKSPACE_MOUNT, True)],
        timeout=timeout,
    )
    if not sandbox_root.is_relative_to(layout.state):
        logger.warning(
            "docker: sandbox %s is outside %s and is not mounted in the "
            "container — tr_execute_in_sandbox / tr_run_python will fail; "
            "drop the sandbox override in docker mode",
            sandbox_root,
            layout.state,
        )
    return backend


@dataclass(frozen=True)
class _CliSandbox:
    """Where the CLI agent's files and ``execute`` go."""

    backend: CompositeBackend
    #: Runs tr_execute_in_sandbox / tr_run_python; ``None`` means on the host.
    exec_backend: DockerExecBackend | None = None
    #: The container, which the bundle stops on close; ``None`` in local mode.
    docker: DockerSandboxBackend | None = None
    export_dir: Path | None = None


def _cli_sandbox(
    execution_mode: str,
    docker_settings: DockerSettings | None,
    layout: WorkspaceLayout,
    workspace_root: Path,
    timeout: int,
) -> _CliSandbox:
    """The one thing ``execution_mode`` decides.

    Where files and ``execute`` go, and therefore which prompt describes them.
    Tools, middleware and subagents are the same agent in both modes.
    """
    if execution_mode != "docker":
        return _CliSandbox(
            backend=_scratch_root_backend(
                _local_shell_backend(workspace_root, timeout),
                _SCRATCH_ROOT,
            )
        )
    docker_cfg = docker_settings or DockerSettings()
    docker_backend = _docker_sandbox(docker_cfg, layout, layout.sandbox, timeout)
    return _CliSandbox(
        # Virtual root = /yuyutsava, so "/tmp/large_tool_results" is
        # <ws>/.yuyutsava/tmp/large_tool_results on the host.
        backend=_scratch_root_backend(docker_backend, "/tmp"),
        # tr_execute_in_sandbox / tr_run_python run INSIDE the container; every
        # other tr_* tool stays host-side (the bind mounts share the files).
        exec_backend=DockerExecBackend(docker_backend, layout),
        docker=docker_backend,
        export_dir=docker_cfg.export_dir,
    )


def build_cli_deepagent(
    workspace_root: Path,
    settings: LlmSettings,
    *,
    deps: MasterDeps | None = None,
    execution_mode: Literal["local", "docker"] = "local",
    docker_settings: DockerSettings | None = None,
    local_settings: LocalSettings | None = None,
    front: str = "app",
) -> AgentBundle:
    """Build the CLI deepagent.

    Distinct factory from :func:`build_orchestrator`: the CLI is a single
    deepagent (no event-driven Triage, no ChannelRouter, no daemon Store);
    delegation to specialised subagents happens via deepagents' built-in
    ``task(subagent_type, ...)`` tool when ``deps.subagents`` is set.

    ``deps`` carries every optional dependency (see :class:`MasterDeps`); what
    stays a parameter is what only this agent has — where it runs and who reads
    its output.
    """
    deps = deps or MasterDeps()
    model = chat_model(settings)
    checkpointer = deps.checkpointer or MemorySaver()
    middleware = _master_middleware(CLI_PROFILE, deps, model, workspace_root)

    loc = local_settings or LocalSettings()
    layout = WorkspaceLayout.for_workspace(
        workspace_root,
        sandbox_override=loc.sandbox_dir,
        outputs_override=loc.output_dir,
    )
    ws, sandbox_root, output_dir = layout.root, layout.sandbox, layout.outputs

    context_tools, agent_memory_block = _shared_master_tools(
        profile=CLI_PROFILE,
        artifact_store=deps.artifact_store,
        memory_store=deps.memory_store,
        transcript_store=deps.transcript_store,
        output_dir=output_dir,
        mcp_tools=deps.mcp_tools,
        extra_tools=deps.extra_tools,
        board=deps.board,
        visual_store=deps.visual_store,
    )
    skill_registry = SkillRegistry(workspace_dir=layout.skills)

    subagent_specs, async_mw = _master_subagents(deps, model, "build_cli_deepagent")
    middleware.extend(async_mw)

    sandbox = _cli_sandbox(
        execution_mode, docker_settings, layout, workspace_root, deps.bash_timeout_sec
    )
    startup_tools, _registry = _build_tool_registry_and_tools(
        _bind_task_runner_tools(
            deps.task_runner or TaskRunnerAgent(ws, sandbox_root=sandbox_root),
            exec_backend=sandbox.exec_backend,
        ),
        deps.search_config,
        skill_registry,
        extra_tools=context_tools,
        skill_store=deps.skill_store,
        agent_name="cli",
        cap_enforcer=deps.cap_enforcer,
    )
    if sandbox.docker is not None:
        _prompt = docker_system_prompt(
            layout, sandbox.export_dir, _registry.catalog_block(), front=front
        )
    else:
        _prompt = local_system_prompt(layout, _registry.catalog_block(), front=front)
    if agent_memory_block:
        _prompt = f"{_prompt}\n\n{agent_memory_block}"
    graph = create_deep_agent(
        model=model,
        tools=startup_tools,
        backend=sandbox.backend,
        system_prompt=_prompt,
        checkpointer=checkpointer,
        middleware=collapse_policy_adapters(middleware),
        subagents=subagent_specs or None,
        debug=False,
    )
    return AgentBundle(
        agent=graph,
        docker_backend=sandbox.docker,
        sandbox_root=sandbox_root,
        output_dir=output_dir,
        layout=layout,
        async_host=deps.async_host,
        async_host_url=deps.async_host_url,
        async_host_attachment=deps.async_host_attachment,
        async_task_mirror=deps.async_task_mirror,
    )


# NOTE: the ``build_agent`` back-compat alias was removed once its two real
# callers (cli/cli.py, scripts/smoke_async.py) moved to ``build_cli_deepagent``.
# Use ``build_cli_deepagent``.


def _disabled_subagents(runtime_settings: Any | None) -> frozenset[str]:
    """Dedicated subagents the user switched off; empty when unknown.

    A fresh orchestrator graph is built per task, so reading the toggle at build
    time is both free and complete: a disabled subagent is absent from the
    prompt AND from the roster.
    """
    if runtime_settings is None:
        return frozenset()
    try:
        return runtime_settings.subagents().disabled
    except Exception:  # noqa: BLE001 — a toggle never blocks a build
        logger.debug("orchestrator: subagent toggle read failed", exc_info=True)
        return frozenset()


def _orchestrator_tools(
    deps: "OrchestratorDeps",
    skill_registry: "SkillRegistry | None",
    skill_store: Any | None,
) -> tuple[list, str]:
    """The orchestrator's tools, and the catalog block its prompt lists them in.

    What it does NOT get: no vis_*, no tr_*, no db_* — the orchestrator is a
    plain router that delegates execution to subagents, and those absences are
    recorded as by-design divergences in core/agent_profiles.py. No spawn tool
    either, by decision: it delegates dynamic work to the ``general-purpose``
    subagent rather than minting ad-hoc ones. A boundary, not an omission —
    don't add one back.
    """
    master_tools: list = [
        make_ask_user_tool(deps.channels),
        make_recall_tool(deps.store),
    ]
    if skill_registry:
        master_tools.extend(
            make_skill_tools(skill_registry, skill_store, agent_name="orchestrator")
        )
    if deps.search_config is not None:
        master_tools.extend(make_search_tools(deps.search_config, cap_enforcer=deps.cap_enforcer))
    # Shared families, selected by ORCHESTRATOR_PROFILE. The returned um_* index
    # block is discarded on purpose: the daemon loop injects it per task
    # alongside prefs and memory, so the build-time snapshot would be stale.
    shared, _ = _shared_master_tools(
        profile=ORCHESTRATOR_PROFILE,
        artifact_store=deps.artifact_store,
        memory_store=deps.memory_store,
        transcript_store=getattr(deps, "transcript_store", None),
        mcp_tools=(
            deps.mcp_manager.tools_for("orchestrator") if deps.mcp_manager is not None else None
        ),
        board=deps.board,
    )
    master_tools.extend(shared)

    # Lazy discovery: ToolFilterPolicy hides the prefixed master tools
    # (sk_/ws_/mem_/…) from the model. Register them so the model can pull a
    # schema on demand via tool_search, and surface the always-visible name
    # catalog in the system prompt so it knows what exists.
    registry = ToolRegistry()
    registry.register_many(master_tools)
    master_tools = [registry.make_tool_search_tool(), *master_tools]
    # This registry is assembled by hand rather than through the shared
    # helper, so the meter has to be told here too or the orchestrator's "tool
    # schemas" row reads zero. (Do not name that helper here:
    # test_agent_profiles derives a builder's "effective source" by
    # substring-matching helper names and splicing their bodies in, so a
    # mention in a comment makes this builder appear to wire tool families it
    # does not have.)
    note_bound_tools("orchestrator", registry, master_tools)
    return master_tools, registry.catalog_block()


def _orchestrator_context_mw(
    deps: "OrchestratorDeps", agent_model: BaseChatModel, role: str, *, meter_window: bool = True
) -> list:
    """The context layer for the orchestrator or one of its subagents."""
    return _context_middleware(
        model=agent_model,
        artifact_store=deps.artifact_store,
        context_settings=deps.context_settings,
        summary_store=deps.summary_store,
        memory_store=deps.memory_store,
        transcript_store=getattr(deps, "transcript_store", None),
        compaction_model=deps.compaction_model,
        role=role,
        meter_window=meter_window,
    )


def _orchestrator_subagents(
    deps: "OrchestratorDeps", disabled: frozenset[str], usage_context: Any | None
) -> tuple[list[dict], list]:
    """The orchestrator's subagent specs, and the middleware background ones need.

    Each sync subagent gets the orchestrator's own additions: a spend ceiling
    per subagent, and per-call cost accounting when a usage store is wired.
    ADR-001 — the CLI and tinker builds have neither.
    """
    usage_store = getattr(deps, "usage_store", None)

    def _usage_mw(role: str) -> list:
        if usage_store is None:
            return []
        return [
            LangChainPolicyAdapter(
                [
                    UsagePolicy(
                        usage_store,
                        role=role,
                        model_name=model_name_of(deps.subagent_model),
                        task_id=getattr(usage_context, "task_id", ""),
                        thread_id=getattr(usage_context, "thread_id", ""),
                    )
                ]
            )
        ]

    specs: list[dict] = [
        _subagent_spec(
            sa,
            context_middleware=_orchestrator_context_mw(
                deps, deps.subagent_model, sa.name, meter_window=False
            ),
            extra_middleware=[
                LangChainPolicyAdapter(
                    [BudgetPolicy(max_input_tokens=deps.subagent_token_budget, role=sa.name)]
                ),
                *_usage_mw(sa.name),
            ],
            model=deps.subagent_model,
            artifact_store=deps.artifact_store,
        )
        for sa in deps.subagents.values()
        if sa.name not in disabled
    ]
    # Background subagents join the same list; deepagents routes dicts shaped
    # like AsyncSubAgent to AsyncSubAgentMiddleware.
    async_specs, async_mw = _async_subagent_wiring(
        role="build_orchestrator",
        async_subagents=getattr(deps, "async_subagents", None),
        async_host_url=getattr(deps, "async_host_url", None),
        remote_async_subagents=getattr(deps, "remote_async_subagents", None),
        async_task_mirror=getattr(deps, "async_task_mirror", None),
        async_max_concurrent=getattr(deps, "async_max_concurrent", 8),
        # Only the orchestrator filters its async roster by the runtime toggle.
        disabled=disabled,
    )
    specs.extend(async_specs)
    return specs, async_mw


def _orchestrator_backend(deps: "OrchestratorDeps") -> Any:
    """A tight, isolated shell: the orchestrator runs unattended.

    An instance, not a factory closure — see _local_shell_backend. A shorter
    timeout than a chat's, and it does NOT inherit the user's environment.
    """
    workspace_root = str(deps.workspace_root.resolve()) if deps.workspace_root else "/"
    backend: Any = _local_shell_backend(workspace_root, bash_timeout_sec=10, inherit_env=False)
    if deps.workspace_root:
        # deepagents scratch (large_tool_results/, conversation_history/) goes
        # under <ws>/.yuyutsava/tmp — the dirs the daemon's sweeper TTL-sweeps.
        backend = _scratch_root_backend(backend, _SCRATCH_ROOT)
    return backend


def build_orchestrator(
    *,
    model: BaseChatModel,
    deps: "OrchestratorDeps",
    budget_tokens: int,
    skill_registry: "SkillRegistry | None" = None,
    skill_store: Any | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    prefs_block: str = "",
    usage_context: "Any | None" = None,
) -> CompiledStateGraph:
    """Build the daemon orchestrator: master deepagent + pre-registered subagents.

    ``OrchestratorDeps`` and ``make_ask_user_tool`` live in
    ``yuyutsava/agents/orchestrator/agent.py`` — they are agent *definition*,
    not build mechanics. ``usage_context`` (``yuyutsava.storage.usage.UsageContext``)
    carries the task's join keys; when ``deps.usage_store`` is set, a
    ``UsagePolicy`` rides every agent in the graph and writes one ``llm_usage``
    row per model call.
    """
    runtime_settings = getattr(deps, "runtime_settings", None)
    disabled = _disabled_subagents(runtime_settings)

    capabilities = render_capabilities_block(
        list(deps.subagents.values()),
        async_subagents=getattr(deps, "async_subagents", None),
        remote_async_subagents=getattr(deps, "remote_async_subagents", None),
        disabled=disabled,
    )
    # When a semantic skill store is wired, the SkillInjector adds only the
    # task-relevant skills at runtime (via prefs_block), so suppress the static
    # dump-everything catalogue to avoid context bloat. Fall back to index_block
    # only when there's no store (keyword-less / no-pgvector deployments).
    skills_index = (
        ""
        if skill_store is not None
        else (skill_registry.index_block(agent="orchestrator") if skill_registry else "")
    )
    system_prompt = render_system_prompt(
        capabilities, skills_index=skills_index, prefs_block=prefs_block
    )
    master_tools, catalog = _orchestrator_tools(deps, skill_registry, skill_store)
    if catalog:
        system_prompt = f"{system_prompt}\n\n## AVAILABLE TOOLS (load a schema with tool_search before calling)\n{catalog}"

    subagent_specs, async_mw = _orchestrator_subagents(deps, disabled, usage_context)

    # Order: tool filter → offload (tool path) → compaction (model path) →
    # budget (absolute ceiling, must see post-compaction usage last) →
    # usage recorder (passive accounting, sees the same final usage).
    # The orchestrator has no filesystem tools at all (it routes and delegates),
    # so FilesystemPromptOverride is not merely cosmetic here: the block would
    # advertise capabilities it does not have. SubagentGate is belt-and-braces —
    # the roster already omits disabled subagents, but a task that outlives a
    # toggle still gets a clean refusal instead of a silent retry.
    master_middleware: list = [
        *_policy_middleware(
            ORCHESTRATOR_PROFILE,
            "pre",
            role="orchestrator",
            runtime_settings=runtime_settings,
        ),
        *_orchestrator_context_mw(deps, model, "orchestrator"),
        *_policy_middleware(
            ORCHESTRATOR_PROFILE,
            "post",
            role="orchestrator",
            budget_tokens=budget_tokens,
            usage_store=getattr(deps, "usage_store", None),
            model=model,
            usage_ids=(
                getattr(usage_context, "task_id", ""),
                getattr(usage_context, "thread_id", ""),
            ),
        ),
    ]

    # Per-turn retrieval injection — memory/skills recalled against the
    # task message itself (the loop's build-time prefs_block keeps only the
    # non-similarity blocks: prefs + um index). ConversationInjector rides
    # along when the daemon wires a transcript index, so resumed orchestrator
    # threads recall their own swept turns like the CLI/tinker masters do.
    # No prefs_store: the orchestrator receives preferences as the build-time
    # ``prefs_block`` string assembled by the daemon loop, not a per-turn
    # injector. See ORCHESTRATOR_PROFILE (PrefsInjector absent).
    master_middleware.extend(
        _retrieval_injection_middleware(
            memory_store=deps.memory_store,
            skill_store=skill_store,
            skill_scope="orchestrator",
            transcript_index=getattr(deps, "transcript_index", None),
        )
    )
    master_middleware.extend(async_mw)

    return create_deep_agent(
        model=model,
        tools=master_tools,
        backend=_orchestrator_backend(deps),
        system_prompt=system_prompt,
        checkpointer=checkpointer if checkpointer is not None else MemorySaver(),
        middleware=collapse_policy_adapters(master_middleware),
        subagents=subagent_specs or None,
    )


def build_tinker_agent(
    card_id: str,
    card_workspace: Path,
    settings: LlmSettings,
    *,
    deps: MasterDeps | None = None,
    skill_registry: Any | None = None,
    note_index: Any | None = None,
) -> AgentBundle:
    """Build the TinkerAgent — third factory, sibling of :func:`build_cli_deepagent`
    and :func:`build_orchestrator`.

    ``deps.mcp_tools`` are the user-configured MCP server tools already scoped to
    this agent (``mcp_manager.tools_for("tinker")`` — the same wiring the
    orchestrator master uses); they join the ToolRegistry so schemas load
    lazily via tool_search. ``note_index``
    (:class:`~yuyutsava.todoboard.recall.TodoNoteIndex`) enables per-turn
    semantic recall of relevant board notes.

    One bundle per TODO card: the compiled graph carries the card's identity in
    its system prompt and binds the tr_* TaskRunner gateway to the card's own
    workspace dir (``blobs/todoboard/<card_id>/``), so a shared graph can't
    serve two cards. The conversation thread is pinned by the caller to
    ``todo:<card_id>`` — per-thread checkpointer isolation does the rest.

    Composition follows the CLI deepagent (same context chain, permission/
    consent, lazy tool discovery) plus the orchestrator's BudgetPolicy/
    UsagePolicy pair:
      tool filter → offload (tool path) → compaction (model path) →
      budget (absolute ceiling) → usage recorder (passive accounting).

    Tools: todo_* FULL scope (author="tinker"), tr_* bound to the card
    workspace with ``agent_name="tinker"``, vis_*, ws_* (when
    ``search_config``), mem_*/ctx_* (when stores wired), sk_* with the
    tinker skills namespace. ``subagents``/``async_subagents`` ride along
    exactly like the CLI factory — no spawn-subagent tool.
    """
    deps = deps or MasterDeps()
    if deps.budget_tokens is None:
        deps = deps.replace(budget_tokens=TINKER_BUDGET_TOKENS)

    model = chat_model(settings)
    checkpointer = deps.checkpointer or MemorySaver()
    layout = WorkspaceLayout.for_workspace(card_workspace)
    ws, sandbox_root, output_dir = layout.root, layout.sandbox, layout.outputs

    middleware = _master_middleware(
        TINKER_PROFILE,
        deps,
        model,
        ws,
        usage_ids=(f"tinker:{card_id}", f"todo:{card_id}"),
        note_index=note_index,
    )

    # todo_author="tinker": the board's full editing set, with notes attributed
    # to the tinker agent rather than the user.
    context_tools, agent_memory_block = _shared_master_tools(
        profile=TINKER_PROFILE,
        artifact_store=deps.artifact_store,
        memory_store=deps.memory_store,
        transcript_store=deps.transcript_store,
        output_dir=output_dir,
        mcp_tools=deps.mcp_tools,
        extra_tools=deps.extra_tools,
        todo_author="tinker",
        board=deps.board,
        visual_store=deps.visual_store,
    )
    skill_registry = skill_registry or SkillRegistry()

    subagent_specs, async_mw = _master_subagents(deps, model, "build_tinker_agent")
    middleware.extend(async_mw)

    backend = _scratch_root_backend(_local_shell_backend(ws, deps.bash_timeout_sec), _SCRATCH_ROOT)
    startup_tools, _registry = _build_tool_registry_and_tools(
        _bind_task_runner_tools(
            deps.task_runner or TaskRunnerAgent(ws, sandbox_root=sandbox_root),
            agent_name="tinker",
        ),
        deps.search_config,
        skill_registry,
        extra_tools=context_tools,
        skill_store=deps.skill_store,
        agent_name="tinker",
        cap_enforcer=deps.cap_enforcer,
    )

    # Semantic store present → SkillInjector surfaces the relevant modes per
    # turn; static index only as the keyword-less fallback (orchestrator rule).
    skills_index = (
        "" if deps.skill_store is not None else skill_registry.index_block(agent="tinker")
    )
    graph = create_deep_agent(
        model=model,
        tools=startup_tools,
        backend=backend,
        system_prompt=render_tinker_system_prompt(
            card_id=card_id,
            layout=layout,
            tool_catalog=_registry.catalog_block(),
            skills_index=skills_index,
            agent_memory_block=agent_memory_block,
        ),
        checkpointer=checkpointer,
        middleware=collapse_policy_adapters(middleware),
        subagents=subagent_specs or None,
        debug=False,
    )
    return AgentBundle(
        agent=graph,
        docker_backend=None,
        sandbox_root=sandbox_root,
        output_dir=output_dir,
        layout=layout,
        async_host=deps.async_host,
        async_host_url=deps.async_host_url,
        async_host_attachment=deps.async_host_attachment,
        async_task_mirror=deps.async_task_mirror,
    )


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Post-task cleanup (local mode only)
# ---------------------------------------------------------------------------
