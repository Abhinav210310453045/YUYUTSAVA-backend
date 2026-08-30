"""
Build a **Deep Agents** graph with an OpenAI-compatible chat model and a real-disk backend.

Uses ``LocalShellBackend`` (filesystem + ``execute``) so built-in ``read_file`` /
``write_file`` / ``ls`` / … map to the workspace root, and ``execute`` runs shell
on the host per deepagents (see ``deepagents.backends.LocalShellBackend``).

This module is intentionally narrow: it owns the *build* side of the agent
graphs (CLI deepagent + daemon orchestrator). Streaming + interrupt handling
live in :mod:`yuyutsava.core.streaming`; the CLI system prompts live in
:mod:`yuyutsava.core.prompts`; the tool-result size guard lives in
:mod:`yuyutsava.core.tool_result`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph

from yuyutsava.core.config import DockerSettings, LocalSettings, LlmSettings, SearchConfig
from yuyutsava.core.docker_sandbox_backend import DockerSandboxBackend
from yuyutsava.core.filesystem_prompt_policy import FilesystemPromptPolicy
from yuyutsava.core.voice_style_policy import VoiceStylePolicy
from yuyutsava.core.permission_policy import PermissionPolicy
from yuyutsava.core.prompts import docker_system_prompt, local_system_prompt
from yuyutsava.core.subagent_gate_policy import SubagentGatePolicy
from yuyutsava.core.tool_filter_policy import ToolFilterPolicy
from yuyutsava.policy.adapter import (
    LangChainPolicyAdapter,
    collapse_policy_adapters,
)
from yuyutsava.core.agent_profiles import (
    CLI_PROFILE,
    ORCHESTRATOR_PROFILE,
    TINKER_PROFILE,
    AgentProfile,
    Policy,
)
from yuyutsava.core.tool_registry import ToolRegistry
from yuyutsava.agents.task_runner.tools import bind_tools as _bind_task_runner_tools
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.skills.tools import make_skill_tools
from yuyutsava.tools.search import make_search_tools

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logger = logging.getLogger("yuyutsava")


# Loggers raised to WARNING by silence_plumbing_loggers — these emit chatty
# HTTP / runtime traffic (langgraph dev server, uvicorn access lines,
# httpx client logs) that pollute interactive output. Real problems still
# surface at WARNING/ERROR.
_PLUMBING_WARN_LOGGERS = (
    "langgraph_api",
    "langgraph_runtime_inmem",
    "langgraph_runtime",
    "langgraph_api.auth.custom",
    "langgraph_api.auth.middleware",
    "langgraph_api.timing.timer",
    "langgraph_api.cron_scheduler",
    "langgraph_api.metadata",
    "langgraph_api.lifespan",
    "langgraph_api.queue",
    "langgraph_runtime_inmem.queue",
    "langgraph_runtime_inmem.lifespan",
    "langgraph_runtime_inmem._persistence",
    "httpx",
    "httpcore",
    "uvicorn",
    "uvicorn.access",
    "uvicorn.error",
)

# Loggers raised to ERROR — these emit chatty WARNING-level lines that
# aren't actionable for interactive users (langfuse handshake, warnings hook).
_PLUMBING_ERROR_LOGGERS = (
    "langfuse",
    # OTEL exporter retry spam when Langfuse drops mid-session (the whole
    # opentelemetry.* tree, incl. exporter.otlp.proto.http.trace_exporter).
    "opentelemetry",
    "py.warnings",
    # Vertex re-converts every tool schema on each bind and warns once per
    # `additionalProperties` key Pydantic emitted — pure noise, N× per turn.
    "langchain_google_vertexai.functions_utils",
)


def silence_plumbing_loggers() -> None:
    """Raise plumbing loggers above the chatter floor.

    The ``yuyutsava`` namespace is left untouched — application warnings
    still surface. Cross-call idempotent; ``langgraph_api`` re-imports
    ``logging`` inside its server thread, so callers that build a LangGraph
    host should call this *after* the build to re-silence handlers that
    were just installed.
    """
    import warnings

    for name in _PLUMBING_WARN_LOGGERS:
        lg = logging.getLogger(name)
        lg.setLevel(logging.WARNING)
        lg.propagate = False
    for name in _PLUMBING_ERROR_LOGGERS:
        lg = logging.getLogger(name)
        lg.setLevel(logging.ERROR)
        lg.propagate = False
        lg.disabled = True
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    warnings.filterwarnings("ignore", category=PendingDeprecationWarning)


def setup_logging(verbose: bool = False, *, debug_plumbing: bool = False) -> None:
    """Configure the ``yuyutsava`` logger for the CLI to print to stderr.

    Root logger is kept at WARNING; only the ``yuyutsava`` namespace is
    flipped to DEBUG (verbose) or INFO (default). Plumbing loggers
    (uvicorn / langgraph_api / httpx / …) are silenced regardless of
    verbose so the chat REPL stays uncluttered. Pass
    ``debug_plumbing=True`` (or set ``YUYUTSAVA_DEBUG_PLUMBING=1``) to
    see those — useful only when debugging the runtime itself.
    """
    if not debug_plumbing:
        debug_plumbing = os.environ.get("YUYUTSAVA_DEBUG_PLUMBING", "").lower() in ("1", "true", "yes")

    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)

    class _Fmt(logging.Formatter):
        _COLOURS = {
            logging.DEBUG:    "\033[2m",       # dim
            logging.INFO:     "\033[0m",        # normal
            logging.WARNING:  "\033[33m",       # yellow
            logging.ERROR:    "\033[31m",       # red
            logging.CRITICAL: "\033[1;31m",     # bold red
        }
        _RESET = "\033[0m"

        def format(self, record: logging.LogRecord) -> str:
            colour = self._COLOURS.get(record.levelno, "")
            msg = super().format(record)
            return f"{colour}{msg}{self._RESET}"

    # Verbose: prefix with timestamp + logger name so the user can tell which
    # subsystem is talking. Plain ``%(message)s`` otherwise to keep
    # non-verbose output uncluttered.
    fmt = "%(asctime)s %(name)s | %(message)s" if verbose else "%(message)s"
    handler.setFormatter(_Fmt(fmt, datefmt="%H:%M:%S"))

    # Root stays at WARNING so unrelated libraries don't flood stderr.
    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level < logging.WARNING:
        root.setLevel(logging.WARNING)

    log = logging.getLogger("yuyutsava")
    log.setLevel(level)
    log.handlers.clear()
    log.addHandler(handler)
    log.propagate = False

    if not debug_plumbing:
        silence_plumbing_loggers()


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
    async_host: Any | None = None          # AsyncSubagentHost — duck-typed to avoid cycle
    async_host_url: str | None = None
    async_host_attachment: Any | None = None  # HostAttachment
    async_task_mirror: Any | None = None   # AsyncTaskMirror
    pg_pool: Any | None = None             # PgPool owned by the CLI (closed in aclose)
    embedder: Any | None = None            # memory.Embedder owned by the CLI

    async def aclose(self) -> None:
        """Async teardown: close the CLI-owned pool + embedder, then close()."""
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
                from yuyutsava.async_subagents.host_lock import release_host_lock
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


def builtin_tools_reference_json() -> str:
    """Static reference for ``yuyutsava --print-tools`` (names match Deep Agents middleware)."""
    doc = [
        {
            "tool": "read_file",
            "note": "Read text/binary via FilesystemBackend; use virtual paths under workspace (e.g. /yuyutsava/foo.txt).",
        },
        {
            "tool": "write_file",
            "note": "Create/overwrite files under the same virtual root.",
        },
        {
            "tool": "execute",
            "note": "Shell on the local machine (LocalShellBackend); prefer over ad-hoc bash wrappers. Timeout matches CLI --bash-timeout.",
        },
        {
            "tool": "ls, glob, grep, edit_file, write_todos, task, …",
            "note": "Other built-in Deep Agents tools; see https://docs.langchain.com/oss/python/deepagents/overview",
        },
    ]
    return json.dumps(doc, indent=2)


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
    from yuyutsava.agents.db_tools import make_db_tools

    search_tools = (
        make_search_tools(search_config, cap_enforcer=cap_enforcer)
        if search_config else []
    )
    skill_tools = (
        make_skill_tools(skill_registry, skill_store, agent_name=agent_name)
        if skill_registry else []
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
    return startup_tools, registry



# Fixed pipeline order. The PROFILE says which policies a master has; this says
# where each one goes. Two separate facts — a profile is a set, and a set has no
# order, which is exactly why ordering could not live there.
#
# `pre` runs before the context controllers, `post` after: budget and usage must
# observe POST-compaction token counts, so they cannot be hoisted.
_POLICY_ORDER_PRE: tuple[Policy, ...] = (
    Policy.TOOL_FILTER,
    Policy.FILESYSTEM_PROMPT,
    Policy.VOICE_STYLE,
    Policy.SUBAGENT_GATE,
)
_POLICY_ORDER_POST: tuple[Policy, ...] = (
    Policy.BUDGET,
    Policy.USAGE,
    Policy.PERMISSION,
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
    usage_task_id: str | None = None,
    usage_thread_id: str | None = None,
) -> list:
    """Build one phase of a master's policy middleware, from its profile.

    A policy appears when the profile declares it **and** its wiring is present.
    Those are different questions and both have to be yes:

    * the **profile** says the master *may* have this policy — a capability, the
      thing ADR-001 makes data;
    * the **argument** supplies it — the standalone CLI passes no ``usage_store``
      and gets no accounting, without that being a different kind of agent.

    So dropping ``Policy.BUDGET`` from a profile disables budgeting for that
    master everywhere, while passing ``budget_tokens=None`` just leaves it
    unwired here. Conflating the two is why this was assembled by hand three
    times.
    """
    order = _POLICY_ORDER_PRE if phase == "pre" else _POLICY_ORDER_POST
    out: list = []
    for policy in order:
        if policy not in profile.policies:
            continue
        if policy is Policy.TOOL_FILTER:
            out.append(LangChainPolicyAdapter([ToolFilterPolicy()]))
        elif policy is Policy.FILESYSTEM_PROMPT:
            # Strips the deepagents "## Filesystem Tools" block: those built-ins
            # are filtered out by ToolFilterPolicy and our prompt routes
            # filesystem work through tr_*, so the block only misleads the model
            # and burns ~700 cache-prefix tokens per turn.
            out.append(LangChainPolicyAdapter([FilesystemPromptPolicy()]))
        elif policy is Policy.VOICE_STYLE:
            out.append(LangChainPolicyAdapter([VoiceStylePolicy()]))
        elif policy is Policy.SUBAGENT_GATE:
            # Unconditional, and `runtime_settings` may be None: the gate treats
            # that as "no toggles configured" and passes everything. Guarding on
            # it here would silently drop the policy for the standalone CLI —
            # caught by the fingerprint gate when this was first written that way.
            out.append(LangChainPolicyAdapter([SubagentGatePolicy(runtime_settings)]))
        elif policy is Policy.BUDGET and budget_tokens is not None:
            from yuyutsava.daemon.budget_policy import BudgetPolicy

            out.append(LangChainPolicyAdapter(
                [BudgetPolicy(max_input_tokens=budget_tokens, role=role)]))
        elif policy is Policy.USAGE and usage_store is not None:
            from yuyutsava.daemon.usage import UsagePolicy
            from yuyutsava.llm import model_name_of

            # task_id/thread_id are pinned only where the bundle IS one
            # conversation — the tinker graph is per-card. The chat bundle is
            # shared across threads, so its rows land task-less/thread-less,
            # which the store supports.
            extra = {}
            if usage_task_id is not None:
                extra["task_id"] = usage_task_id
            if usage_thread_id is not None:
                extra["thread_id"] = usage_thread_id
            out.append(LangChainPolicyAdapter([UsagePolicy(
                usage_store, role=role, model_name=model_name_of(model), **extra,
            )]))
        elif policy is Policy.PERMISSION and permission_check and workspace_root:
            # PermissionPolicy imports no framework and is tested without one
            # (test/policy/test_permission_parity.py); the adapter is what
            # attaches it to the graph.
            out.append(LangChainPolicyAdapter(
                [PermissionPolicy(workspace_root=workspace_root.resolve())]
            ))
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
    from yuyutsava.context.compaction import YuyutsavaCompactionMiddleware
    from yuyutsava.context.offload_policy import ToolResultOffloadPolicy

    out: list = []
    if artifact_store is not None and context_settings is not None:
        out.append(LangChainPolicyAdapter(
            [ToolResultOffloadPolicy(artifact_store, context_settings)]))
    if context_settings is not None:
        out.append(
            YuyutsavaCompactionMiddleware(
                model=compaction_model or model,
                settings=context_settings,
                summary_store=summary_store,
                memory_sink=memory_store,
                role=role,
            )
        )
    if transcript_store is not None:
        from yuyutsava.context.transcript_policy import TranscriptRecorderPolicy

        out.append(LangChainPolicyAdapter(
            [TranscriptRecorderPolicy(transcript_store, index=transcript_index)]))

    # Observability (no-op unless YUYUTSAVA_DEBUG_PROMPT). Last in the list so
    # its before_model hook reports the message list AFTER offload + compaction
    # — i.e. exactly what the model receives. Never mutates state.
    from yuyutsava.context.prompt_inspector import PromptInspectorPolicy

    out.append(LangChainPolicyAdapter([PromptInspectorPolicy(role=role)]))
    return out


# Public alias: external builders (async_subagents host wiring in bootstrap /
# agent stacks) attach the same context controllers to their graphs.
context_middleware = _context_middleware


def _shared_master_tools(
    *,
    profile: "AgentProfile",
    artifact_store: Any | None = None,
    memory_store: Any | None = None,
    output_dir: Path | None = None,
    mcp_tools: "Sequence[Any] | None" = None,
    extra_tools: "Sequence[Any] | None" = None,
    todo_author: str | None = None,
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
    from yuyutsava.core.agent_profiles import ToolFamily

    tools: list = []
    fams = profile.tools

    if ToolFamily.CONTEXT in fams and artifact_store is not None:
        from yuyutsava.context.tools import make_context_tools
        tools.extend(make_context_tools(artifact_store))

    if ToolFamily.MEMORY in fams and memory_store is not None:
        from yuyutsava.memory.tools import make_memory_tools
        tools.extend(make_memory_tools(memory_store))

    if ToolFamily.VISUALS in fams:
        if output_dir is None:
            raise ValueError(
                f"{profile.role}: profile declares the vis_* family but no output_dir "
                "was supplied for rendered visuals to land in."
            )
        from yuyutsava.visuals.tools import make_visual_tools
        tools.extend(make_visual_tools(output_dir=output_dir))

    if ToolFamily.TODO in fams:
        from yuyutsava.todoboard.tools import make_todo_tools
        kwargs = {"scope": profile.todo_scope}
        if todo_author:
            kwargs["author"] = todo_author
        tools.extend(make_todo_tools(**kwargs))

    # Per-agent user-behaviour memory. Namespaces must not collide — two masters
    # sharing one would cross-contaminate learned behaviours.
    agent_memory_block = ""
    if ToolFamily.AGENT_MEMORY in fams:
        from yuyutsava.memory.agent_memory import AgentMemoryStore
        from yuyutsava.memory.agent_memory_tools import make_agent_memory_tools
        store = AgentMemoryStore(profile.agent_memory_namespace)
        tools.extend(make_agent_memory_tools(store))
        agent_memory_block = store.read_index_block()

    if ToolFamily.ARTIFACTS in fams:
        # Inline artifacts (interactive HTML/JSX, docs, audio) rendered in the
        # reply itself — the richer, non-card twin of the vis_* visuals.
        from yuyutsava.artifacts.tools import make_artifact_tools
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

    # Local imports: keep engine.py importable where langgraph_api is absent
    # (minimal CLI scripts that never touch background tasks).

    if async_task_mirror is not None:
        from yuyutsava.async_subagents.cap_policy import BackgroundTaskCapPolicy
        middleware.append(LangChainPolicyAdapter([
            BackgroundTaskCapPolicy(async_task_mirror, max_concurrent=async_max_concurrent)
        ]))

    from yuyutsava.async_subagents.check_guard_policy import CheckAsyncTaskGuardPolicy
    from yuyutsava.async_subagents.interrupt_patch_policy import (
        AsyncTaskInterruptPatchPolicy,
    )

    # One adapter per policy, each at the position its middleware held. Phase 4
    # step 4.4 migrates the policies; collapsing the adapters into one is a
    # separate step (4.7) so that the ordering change can be proven on its own
    # rather than riding along with five behavioural rewrites.
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
        from yuyutsava.context.injector import MemoryInjector
        injectors.append(MemoryInjector(memory_store))

    if skill_store is not None:
        from yuyutsava.skills.injector import SkillInjector
        injectors.append(SkillInjector(skill_store, agent=skill_scope))

    if transcript_index is not None:
        # Recall this conversation's own earlier turns; survives the checkpoint sweep.
        from yuyutsava.context.conversation_injector import ConversationInjector
        injectors.append(ConversationInjector(transcript_index))

    if note_index is not None:
        # Board-wide: a related decision often lives on a DIFFERENT card than
        # the one this bundle is pinned to.
        from yuyutsava.todoboard.recall import TodoNoteInjector
        injectors.append(TodoNoteInjector(note_index))

    if prefs_store is not None:
        # Whitelisted user preferences, refreshed per turn.
        from yuyutsava.prefs.injector import PrefsInjector
        injectors.append(PrefsInjector(prefs_store))

    if not injectors:
        return []

    from yuyutsava.core.retrieval_injection_policy import RetrievalInjectionPolicy
    return [LangChainPolicyAdapter([RetrievalInjectionPolicy(injectors)])]


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
    specs: list[dict] = []
    for sa in subagents:
        spec = sa.as_deepagents_subagent_spec()
        spec["middleware"] = collapse_policy_adapters([
            LangChainPolicyAdapter([ToolFilterPolicy()]),
            *_context_middleware(
                model=model,
                artifact_store=artifact_store,
                context_settings=context_settings,
                summary_store=summary_store,
                memory_store=memory_store,
                transcript_store=None,
                compaction_model=compaction_model,
                role=sa.name,
            ),
        ])
        if artifact_store is not None:
            # Fresh ctx_* instances per spec — never shared across graphs.
            from yuyutsava.context.tools import make_context_tools
            spec["tools"] = list(spec.get("tools") or []) + make_context_tools(artifact_store)
        specs.append(spec)
    return specs


def build_cli_deepagent(
    workspace_root: Path,
    settings: LlmSettings,
    *,
    bash_timeout_sec: int = 120,
    execution_mode: Literal["local", "docker"] = "local",
    docker_settings: DockerSettings | None = None,
    local_settings: LocalSettings | None = None,
    permission_check: bool = True,
    search_config: SearchConfig | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    subagents: "list[Any] | None" = None,
    async_subagents: "list[Any] | None" = None,
    async_host_url: str | None = None,
    remote_async_subagents: "list[Any] | None" = None,
    async_task_mirror: Any | None = None,
    async_max_concurrent: int = 8,
    async_host: Any | None = None,
    async_host_attachment: Any | None = None,
    artifact_store: Any | None = None,
    summary_store: Any | None = None,
    memory_store: Any | None = None,
    transcript_store: Any | None = None,
    context_settings: Any | None = None,
    compaction_model: BaseChatModel | None = None,
    skill_store: Any | None = None,
    transcript_index: Any | None = None,
    mcp_tools: "list[Any] | None" = None,
    cap_enforcer: Any | None = None,
    budget_tokens: int | None = None,
    usage_store: Any | None = None,
    prefs_store: Any | None = None,
    runtime_settings: Any | None = None,
    extra_tools: "list[Any] | None" = None,
) -> AgentBundle:
    """Build the CLI deepagent.

    Distinct factory from :func:`build_orchestrator`: the CLI is a single
    deepagent (no event-driven Triage, no ChannelRouter, no daemon Store);
    delegation to specialised subagents happens via deepagents' built-in
    ``task(subagent_type, ...)`` tool when ``subagents=[…]`` is passed.

    Args:
        permission_check: When ``True`` (default), attaches ``PermissionMiddleware`` so
            the agent pauses and asks the user before running dangerous shell commands.
        search_config: When provided, ws_* web search tools are added and made
            discoverable via the tool catalog (load with tool_search('select:ws_tavily_search')).
        checkpointer: Optional persistent checkpointer. When omitted, falls back
            to in-process ``MemorySaver()``.
        subagents: Optional list of ``BaseSubAgent`` instances. Each is converted
            via ``as_deepagents_subagent_spec()`` and passed to ``create_deep_agent``.
            Passing ``GeneralPurposeAgent`` here name-match-overrides deepagents'
            built-in default (see ``deepagents.graph`` ~line 240-246), so
            ``task('general-purpose', …)`` calls hit our tighter spec.
        async_subagents: Local-mode background subagents. Each is converted via
            ``as_async_subagent_spec(url=async_host_url)`` and added to the same
            ``subagents=`` list. ``deepagents`` auto-routes by dict shape and
            attaches ``AsyncSubAgentMiddleware``, which injects ``start_/check_/
            update_/cancel_/list_async_tasks`` onto the master.
        async_host_url: Base URL of the in-process LangGraph Agent Protocol
            server hosting the compiled graphs (typically the
            ``AsyncSubagentHost``). Required if ``async_subagents`` is provided.
        remote_async_subagents: ``RemoteAsyncSubagentSpec`` entries whose graphs
            live on a different Agent Protocol server (e.g. another YUYUTSAVA
            daemon). Treated as first-class peers of local async subagents.
        async_task_mirror: ``AsyncTaskMirror`` instance for cross-turn task
            awareness and the concurrency cap. When provided alongside any
            async subagents, ``BackgroundTaskCapPolicy`` is installed.
        async_max_concurrent: Cap for in-flight bg tasks. Defaults to ``8``.
        async_host: Optional ``AsyncSubagentHost`` reference recorded on the
            returned bundle so ``AgentBundle.close`` can tear it down.
        artifact_store / summary_store / memory_store / transcript_store /
            context_settings / compaction_model: context-controller wiring (see
            ``yuyutsava.context``). All optional; None disables the layer.
            ``transcript_store`` persists the full verbatim conversation to the
            DB (durable beyond checkpoint sweeps); see
            ``yuyutsava.context.transcript_store``.
            CLI chat threads are the longest-lived in the system, so the
            stack factory (``cli/agent_stack.py``) wires these by default.
    """
    # Imported here, not at module scope: `yuyutsava.llm` imports core.config,
    # whose package __init__ re-exports this module — a module-level import would
    # close that loop. Same reason model_name_of is imported lazily below.
    from yuyutsava.llm import chat_model

    model = chat_model(settings)
    checkpointer = checkpointer or MemorySaver()
    # FilesystemPromptPolicy strips the deepagents "## Filesystem Tools"
    # block: those built-in tools are filtered out by ToolFilterPolicy and our
    # own prompt routes filesystem ops through tr_*, so the block only misleads the
    # model and wastes cache-prefix tokens. Pass replacement="..." to reword instead.
    middleware = _policy_middleware(
        CLI_PROFILE, "pre", role="cli", runtime_settings=runtime_settings,
    )
    middleware.extend(_context_middleware(
        model=model,
        artifact_store=artifact_store,
        context_settings=context_settings,
        summary_store=summary_store,
        memory_store=memory_store,
        transcript_store=transcript_store,
        transcript_index=transcript_index,
        compaction_model=compaction_model,
        role="cli",
    ))
    # Absolute spend ceiling + passive accounting, after compaction (the
    # orchestrator's ordering rule). Both daemon-wired: the standalone CLI
    # passes neither and keeps its historical unbounded behavior. Lazy
    # imports: daemon-only modules.
    # Budget + usage sit AFTER compaction so they see post-compaction counts;
    # the ordering lives in _POLICY_ORDER_POST, not here. The chat bundle is
    # shared across threads, so usage rows land task-less/thread-less.
    middleware.extend(_policy_middleware(
        CLI_PROFILE, "post", role="cli",
        budget_tokens=budget_tokens, usage_store=usage_store, model=model,
        workspace_root=workspace_root, permission_check=permission_check,
    ))

    # Per-turn retrieval injection: surface the memory + skills relevant to the
    # user's latest message into the prompt (the CLI is a persistent graph, so
    # it can't inject at build time the way the daemon orchestrator does).
    # Scoped to "cli" — every master retrieves only its OWN skills plus the
    # shared ones. The store filter is ``agent IS NULL OR agent = <role>``, so
    # scoping keeps globally-authored skills available and excludes skills
    # another agent wrote for itself. This was previously unscoped, letting the
    # CLI master pull tinker/orchestrator-specific skills into its context.
    middleware.extend(_retrieval_injection_middleware(
        memory_store=memory_store,
        skill_store=skill_store,
        skill_scope="cli",
        transcript_index=transcript_index,
        prefs_store=prefs_store,
    ))

    loc = local_settings or LocalSettings()
    ws = workspace_root.resolve()
    sandbox_root = (loc.sandbox_dir.resolve() if loc.sandbox_dir else ws / "_sandbox")
    output_dir   = (loc.output_dir.resolve()  if loc.output_dir  else ws / "_output")

    context_tools, agent_memory_block = _shared_master_tools(
        profile=CLI_PROFILE,
        artifact_store=artifact_store,
        memory_store=memory_store,
        output_dir=output_dir,
        mcp_tools=mcp_tools,
        extra_tools=extra_tools,
    )

    skill_registry = SkillRegistry(workspace_dir=ws)

    subagent_specs: list[dict] = []
    if subagents:
        subagent_specs.extend(_sync_subagent_specs(
            subagents,
            model=model,
            artifact_store=artifact_store,
            context_settings=context_settings,
            summary_store=summary_store,
            memory_store=memory_store,
            compaction_model=compaction_model,
        ))

    _async_specs, _async_mw = _async_subagent_wiring(
        role="build_cli_deepagent",
        async_subagents=async_subagents,
        async_host_url=async_host_url,
        remote_async_subagents=remote_async_subagents,
        async_task_mirror=async_task_mirror,
        async_max_concurrent=async_max_concurrent,
    )
    subagent_specs.extend(_async_specs)
    middleware.extend(_async_mw)

    final_subagent_specs = subagent_specs or None

    if execution_mode == "docker":
        docker_cfg = docker_settings or DockerSettings()
        export = docker_cfg.export_dir.resolve() if docker_cfg.export_dir else None
        docker_backend = DockerSandboxBackend(
            image=docker_cfg.image,
            workspace_host=ws,
            export_host=export,
            network=docker_cfg.network,
            timeout=bash_timeout_sec,
            memory=docker_cfg.memory,
            cpus=docker_cfg.cpus,
            pids_limit=docker_cfg.pids_limit,
        )
        startup_tools, _registry = _build_tool_registry_and_tools(
            _bind_task_runner_tools(ws), search_config, skill_registry,
            extra_tools=context_tools, skill_store=skill_store, agent_name="cli",
            cap_enforcer=cap_enforcer,
        )
        _prompt = docker_system_prompt(
            workspace_root, docker_cfg.export_dir, _registry.catalog_block()
        )
        if agent_memory_block:
            _prompt = f"{_prompt}\n\n{agent_memory_block}"
        graph = create_deep_agent(
            model=model,
            tools=startup_tools,
            backend=docker_backend,
            system_prompt=_prompt,
            checkpointer=checkpointer,
            middleware=collapse_policy_adapters(middleware),
            subagents=final_subagent_specs,
            debug=False,
        )
        return AgentBundle(
            agent=graph,
            docker_backend=docker_backend,
            async_host=async_host,
            async_host_url=async_host_url,
            async_host_attachment=async_host_attachment,
            async_task_mirror=async_task_mirror,
        )

    backend = _local_shell_backend(workspace_root, bash_timeout_sec)
    startup_tools, _registry = _build_tool_registry_and_tools(
        _bind_task_runner_tools(ws, sandbox_root), search_config, skill_registry,
        extra_tools=context_tools, skill_store=skill_store, agent_name="cli",
        cap_enforcer=cap_enforcer,
    )
    _prompt = local_system_prompt(
        workspace_root, sandbox_root, output_dir, _registry.catalog_block()
    )
    if agent_memory_block:
        _prompt = f"{_prompt}\n\n{agent_memory_block}"
    graph = create_deep_agent(
        model=model,
        tools=startup_tools,
        backend=backend,
        system_prompt=_prompt,
        checkpointer=checkpointer,
        middleware=collapse_policy_adapters(middleware),
        subagents=final_subagent_specs,
        debug=False,
    )
    return AgentBundle(
        agent=graph,
        docker_backend=None,
        sandbox_root=sandbox_root,
        output_dir=output_dir,
        async_host=async_host,
        async_host_url=async_host_url,
        async_host_attachment=async_host_attachment,
        async_task_mirror=async_task_mirror,
    )


# NOTE: the ``build_agent`` back-compat alias was removed once its two real
# callers (cli/cli.py, test/test_async.py) moved to ``build_cli_deepagent``.
# Use ``build_cli_deepagent``.


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

    Moved here from ``yuyutsava/agents/orchestrator/agent.py`` so all master
    agents are built from the shared engine. ``OrchestratorDeps`` and the
    ``_make_ask_user_tool`` helper still live in that module — they are agent
    *definition*, not build mechanics.

    ``usage_context`` (``yuyutsava.daemon.usage.UsageContext``) carries the
    task's join keys; when ``deps.usage_store`` is set, a ``UsagePolicy``
    rides every agent in the graph and writes one ``llm_usage`` row per
    model call.
    """
    # Lazy imports: this function is in engine.py but pulls daemon-only modules.
    # Keeping these imports inside the function avoids loading the daemon stack
    # when the CLI imports the engine.
    from yuyutsava.agents.orchestrator.agent import _make_ask_user_tool
    from yuyutsava.agents.orchestrator.capabilities import render_capabilities_block
    from yuyutsava.agents.orchestrator.prompts import render_system_prompt
    from yuyutsava.daemon.budget_policy import BudgetPolicy
    from yuyutsava.events.tools import make_recall_tool

    # Dedicated subagents the user switched off. A fresh orchestrator graph is
    # built per task, so reading the toggle here is both free and complete: a
    # disabled subagent is absent from the prompt AND from the roster below.
    _runtime_settings = getattr(deps, "runtime_settings", None)
    disabled_subagents: frozenset[str] = frozenset()
    if _runtime_settings is not None:
        try:
            disabled_subagents = _runtime_settings.subagents().disabled
        except Exception:  # noqa: BLE001 — a toggle never blocks a build
            logger.debug("orchestrator: subagent toggle read failed", exc_info=True)

    capabilities = render_capabilities_block(
        list(deps.subagents.values()),
        async_subagents=getattr(deps, "async_subagents", None),
        remote_async_subagents=getattr(deps, "remote_async_subagents", None),
        disabled=disabled_subagents,
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
    system_prompt = render_system_prompt(capabilities, skills_index=skills_index, prefs_block=prefs_block)

    master_tools: list = [
        _make_ask_user_tool(deps.channels),
        make_recall_tool(deps.store),
        # spawn_subagent is intentionally NOT registered. The orchestrator
        # delegates dynamic tasks to the `general-purpose` subagent instead.
    ]
    if skill_registry:
        master_tools.extend(
            make_skill_tools(skill_registry, skill_store, agent_name="orchestrator")
        )
    if deps.search_config is not None:
        master_tools.extend(make_search_tools(deps.search_config, cap_enforcer=deps.cap_enforcer))
    # Shared families, selected by ORCHESTRATOR_PROFILE. Note what it does NOT
    # get: no vis_*, no tr_*, no db_* — the orchestrator is a plain router that
    # delegates execution to subagents, and those absences are recorded as
    # by-design divergences in core/agent_profiles.py.
    #
    # The returned um_* index block is discarded here on purpose: the daemon
    # loop injects it per task alongside prefs and memory, so the build-time
    # snapshot would be stale.
    _shared, _ = _shared_master_tools(
        profile=ORCHESTRATOR_PROFILE,
        artifact_store=deps.artifact_store,
        memory_store=deps.memory_store,
        mcp_tools=(
            deps.mcp_manager.tools_for("orchestrator")
            if deps.mcp_manager is not None else None
        ),
    )
    master_tools.extend(_shared)

    # Lazy discovery: ToolFilterPolicy hides the prefixed master tools
    # (sk_/ws_/mem_/…) from the model. Register them so the model can pull a
    # schema on demand via tool_search, and surface the always-visible name
    # catalog in the system prompt so it knows what exists.
    _master_registry = ToolRegistry()
    _master_registry.register_many(master_tools)
    master_tools = [_master_registry.make_tool_search_tool(), *master_tools]
    _catalog = _master_registry.catalog_block()
    if _catalog:
        system_prompt = f"{system_prompt}\n\n## AVAILABLE TOOLS (load a schema with tool_search before calling)\n{_catalog}"

    def _ctx_mw(agent_model: BaseChatModel, role: str) -> list:
        return _context_middleware(
            model=agent_model,
            artifact_store=deps.artifact_store,
            context_settings=deps.context_settings,
            summary_store=deps.summary_store,
            memory_store=deps.memory_store,
            transcript_store=getattr(deps, "transcript_store", None),
            compaction_model=deps.compaction_model,
            role=role,
        )

    def _usage_mw(agent_model: BaseChatModel, role: str) -> list:
        """Per-call cost accounting (Phase 4); [] when no store is wired."""
        usage_store = getattr(deps, "usage_store", None)
        if usage_store is None:
            return []
        from yuyutsava.llm import model_name_of
        from yuyutsava.daemon.usage import UsagePolicy
        return [LangChainPolicyAdapter([UsagePolicy(
            usage_store,
            role=role,
            model_name=model_name_of(agent_model),
            task_id=getattr(usage_context, "task_id", ""),
            thread_id=getattr(usage_context, "thread_id", ""),
        )])]

    subagent_specs: list[dict] = []
    for sa in deps.subagents.values():
        if sa.name in disabled_subagents:
            continue
        spec = sa.as_deepagents_subagent_spec()
        spec["model"] = deps.subagent_model
        spec["middleware"] = collapse_policy_adapters([
            LangChainPolicyAdapter([ToolFilterPolicy()]),
            *_ctx_mw(deps.subagent_model, sa.name),
            LangChainPolicyAdapter([BudgetPolicy(
                max_input_tokens=deps.subagent_token_budget, role=sa.name)]),
            *_usage_mw(deps.subagent_model, sa.name),
        ])
        if deps.artifact_store is not None:
            # Subagents read their own offloaded results, so the ctx_* pair
            # must exist in their graphs too (fresh instances per spec).
            from yuyutsava.context.tools import make_context_tools
            spec["tools"] = list(spec.get("tools") or []) + make_context_tools(deps.artifact_store)
        subagent_specs.append(spec)

    # Async (background) subagents — same `subagents=` list; deepagents auto-
    # routes dicts shaped like AsyncSubAgent to AsyncSubAgentMiddleware.
    # Specs are collected here but the middleware is appended further down,
    # after master_middleware exists — hence computing both up front.
    _async_specs, _async_mw = _async_subagent_wiring(
        role="build_orchestrator",
        async_subagents=getattr(deps, "async_subagents", None),
        async_host_url=getattr(deps, "async_host_url", None),
        remote_async_subagents=getattr(deps, "remote_async_subagents", None),
        async_task_mirror=getattr(deps, "async_task_mirror", None),
        async_max_concurrent=getattr(deps, "async_max_concurrent", 8),
        # Only the orchestrator filters its async roster by the runtime toggle.
        disabled=disabled_subagents,
    )
    subagent_specs.extend(_async_specs)

    budget = LangChainPolicyAdapter(
        [BudgetPolicy(max_input_tokens=budget_tokens, role="orchestrator")])
    workspace_root = str(deps.workspace_root.resolve()) if deps.workspace_root else "/"

    # Instance, not a factory closure — see _local_shell_backend. The
    # orchestrator runs unattended, so it takes a tighter timeout and does NOT
    # inherit the user's environment.
    orchestrator_backend = _local_shell_backend(
        workspace_root, bash_timeout_sec=10, inherit_env=False
    )

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
            ORCHESTRATOR_PROFILE, "pre", role="orchestrator",
            runtime_settings=_runtime_settings,
        ),
        *_ctx_mw(model, "orchestrator"),
        budget,
        *_usage_mw(model, "orchestrator"),
    ]

    # Per-turn retrieval injection — memory/skills recalled against the
    # task message itself (the loop's build-time prefs_block keeps only the
    # non-similarity blocks: prefs + um index). ConversationInjector rides
    # along when the daemon wires a transcript index, so resumed orchestrator
    # threads recall their own swept turns like the CLI/tinker masters do.
    # No prefs_store: the orchestrator receives preferences as the build-time
    # ``prefs_block`` string assembled by the daemon loop, not a per-turn
    # injector. See ORCHESTRATOR_PROFILE (PrefsInjector absent).
    master_middleware.extend(_retrieval_injection_middleware(
        memory_store=deps.memory_store,
        skill_store=skill_store,
        skill_scope="orchestrator",
        transcript_index=getattr(deps, "transcript_index", None),
    ))

    master_middleware.extend(_async_mw)

    return create_deep_agent(
        model=model,
        tools=master_tools,
        backend=orchestrator_backend,
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
    bash_timeout_sec: int = 120,
    permission_check: bool = True,
    search_config: SearchConfig | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    subagents: "list[Any] | None" = None,
    async_subagents: "list[Any] | None" = None,
    async_host_url: str | None = None,
    remote_async_subagents: "list[Any] | None" = None,
    async_task_mirror: Any | None = None,
    async_max_concurrent: int = 8,
    async_host: Any | None = None,
    async_host_attachment: Any | None = None,
    runtime_settings: Any | None = None,
    artifact_store: Any | None = None,
    summary_store: Any | None = None,
    memory_store: Any | None = None,
    transcript_store: Any | None = None,
    context_settings: Any | None = None,
    compaction_model: BaseChatModel | None = None,
    skill_store: Any | None = None,
    transcript_index: Any | None = None,
    skill_registry: SkillRegistry | None = None,
    budget_tokens: int = 60_000,
    usage_store: Any | None = None,
    mcp_tools: "list[Any] | None" = None,
    note_index: Any | None = None,
    cap_enforcer: Any | None = None,
    prefs_store: Any | None = None,
    extra_tools: "list[Any] | None" = None,
) -> AgentBundle:
    """Build the TinkerAgent — third factory, sibling of :func:`build_cli_deepagent`
    and :func:`build_orchestrator`.

    ``mcp_tools`` are the user-configured MCP server tools already scoped to
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
    from yuyutsava.llm import chat_model  # lazy: see build_cli_deepagent

    model = chat_model(settings)
    checkpointer = checkpointer or MemorySaver()
    ws = card_workspace.resolve()
    sandbox_root = ws / "_sandbox"
    output_dir = ws / "_output"

    # Tinker honours the same subagent toggle as its siblings: a bundle is
    # cached per card and can outlive a toggle change, so without the gate a
    # disabled subagent stays reachable from the board after it was switched off.
    middleware: list = _policy_middleware(
        TINKER_PROFILE, "pre", role="tinker", runtime_settings=runtime_settings,
    )
    middleware.extend(_context_middleware(
        model=model,
        artifact_store=artifact_store,
        context_settings=context_settings,
        summary_store=summary_store,
        memory_store=memory_store,
        transcript_store=transcript_store,
        transcript_index=transcript_index,
        compaction_model=compaction_model,
        role="tinker",
    ))
    # Spend ceiling + accounting, after compaction. Unlike the chat bundle, a
    # tinker graph IS one conversation, so usage rows are pinned to the card.
    middleware.extend(_policy_middleware(
        TINKER_PROFILE, "post", role="tinker",
        budget_tokens=budget_tokens, usage_store=usage_store, model=model,
        workspace_root=ws, permission_check=permission_check,
        usage_task_id=f"tinker:{card_id}", usage_thread_id=f"todo:{card_id}",
    ))

    # Per-turn retrieval injection — the tinker graph is as persistent as the
    # CLI's, so memory/skills/transcript recall must be per-turn, and the
    # skills search is scoped to the tinker namespace (agent column).
    # note_index is tinker-only: board-wide note recall across ALL cards, not
    # just the one this bundle is pinned to.
    middleware.extend(_retrieval_injection_middleware(
        memory_store=memory_store,
        skill_store=skill_store,
        skill_scope="tinker",
        transcript_index=transcript_index,
        note_index=note_index,
        prefs_store=prefs_store,
    ))

    # todo_author="tinker": the board's full editing set, with notes attributed
    # to the tinker agent rather than the user.
    context_tools, agent_memory_block = _shared_master_tools(
        profile=TINKER_PROFILE,
        artifact_store=artifact_store,
        memory_store=memory_store,
        output_dir=output_dir,
        mcp_tools=mcp_tools,
        extra_tools=extra_tools,
        todo_author="tinker",
    )

    skill_registry = skill_registry or SkillRegistry()

    subagent_specs: list[dict] = []
    if subagents:
        subagent_specs.extend(_sync_subagent_specs(
            subagents,
            model=model,
            artifact_store=artifact_store,
            context_settings=context_settings,
            summary_store=summary_store,
            memory_store=memory_store,
            compaction_model=compaction_model,
        ))
    # Tinker delegates freely: any task, sync or async, local or on a peer
    # daemon. ``remote_async_subagents`` used to be omitted here, which silently
    # denied it cross-daemon background work its siblings could do.
    _async_specs, _async_mw = _async_subagent_wiring(
        role="build_tinker_agent",
        async_subagents=async_subagents,
        async_host_url=async_host_url,
        remote_async_subagents=remote_async_subagents,
        async_task_mirror=async_task_mirror,
        async_max_concurrent=async_max_concurrent,
    )
    subagent_specs.extend(_async_specs)
    middleware.extend(_async_mw)

    backend = _local_shell_backend(ws, bash_timeout_sec)
    startup_tools, _registry = _build_tool_registry_and_tools(
        _bind_task_runner_tools(ws, sandbox_root, agent_name="tinker"),
        search_config, skill_registry,
        extra_tools=context_tools, skill_store=skill_store, agent_name="tinker",
        cap_enforcer=cap_enforcer,
    )

    # Semantic store present → SkillInjector surfaces the relevant modes per
    # turn; static index only as the keyword-less fallback (orchestrator rule).
    from yuyutsava.agents.tinker.prompts import render_tinker_system_prompt
    skills_index = (
        "" if skill_store is not None else skill_registry.index_block(agent="tinker")
    )
    graph = create_deep_agent(
        model=model,
        tools=startup_tools,
        backend=backend,
        system_prompt=render_tinker_system_prompt(
            card_id=card_id,
            card_workspace=ws,
            sandbox_root=sandbox_root,
            output_dir=output_dir,
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
        async_host=async_host,
        async_host_url=async_host_url,
        async_host_attachment=async_host_attachment,
        async_task_mirror=async_task_mirror,
    )


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------


_STATE_GRAPH_PNG_PATTERN = re.compile(r"^State_Graph_v(\d+)\.png$", re.IGNORECASE)


def next_state_graph_version(output_dir: Path) -> int:
    """Next integer for ``State_Graph_v{n}.png`` in ``output_dir`` (starts at 1 if none)."""
    output_dir = output_dir.resolve()
    if not output_dir.is_dir():
        return 1
    best = 0
    for p in output_dir.iterdir():
        if not p.is_file():
            continue
        m = _STATE_GRAPH_PNG_PATTERN.match(p.name)
        if m:
            best = max(best, int(m.group(1)))
    return best + 1


def export_agent_state_graph_png(
    agent: CompiledStateGraph,
    output_dir: Path,
    *,
    xray: bool = True,
) -> Path:
    """Render the compiled LangGraph to PNG via Mermaid (default: Mermaid.Ink API).

    Requires network access unless you switch ``draw_mermaid_png`` to a non-API method.
    """
    out = output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    n = next_state_graph_version(out)
    path = out / f"State_Graph_v{n}.png"
    agent.get_graph(xray=xray).draw_mermaid_png(output_file_path=str(path))
    return path


# ---------------------------------------------------------------------------
# Post-task cleanup (local mode only)
# ---------------------------------------------------------------------------


def cleanup_local_sandbox(workspace_root: Path, sandbox_root: Path) -> None:
    """Delete sandbox + deepagents scratch dirs after a local task completes.

    - sandbox_root: deleted entirely (temp work; output files go to output_dir)
    - workspace_root/large_tool_results: deepagents eviction cache (the durable
      copy is in the artifacts DB table)
    - workspace_root/conversation_history: deepagents summarization transcript
      dumps (the durable copy is in the transcript_messages DB table)

    The daemon cleans the same scratch dirs via the UnifiedSweeper's TTL
    targets instead, since its workspace is long-lived and shared across tasks.
    """
    for target in (
        sandbox_root,
        workspace_root / "large_tool_results",
        workspace_root / "conversation_history",
    ):
        if target.exists():
            try:
                shutil.rmtree(target)
                logger.debug("Cleaned up %s", target)
            except Exception as exc:
                logger.warning("Could not remove %s: %s", target, exc)
