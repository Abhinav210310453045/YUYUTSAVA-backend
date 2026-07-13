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
from yuyutsava.core.llm import chat_model
from yuyutsava.core.filesystem_prompt_middleware import FilesystemPromptOverrideMiddleware
from yuyutsava.core.voice_style_middleware import VoiceStyleMiddleware
from yuyutsava.core.permission_middleware import PermissionMiddleware
from yuyutsava.core.prompts import docker_system_prompt, local_system_prompt
from yuyutsava.core.tool_filter_middleware import ToolFilterMiddleware
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


def _local_shell_backend_factory(workspace_root: Path, bash_timeout_sec: int):
    root = str(workspace_root.resolve())

    def factory(_runtime: Any) -> LocalShellBackend:
        return LocalShellBackend(
            root_dir=root,
            virtual_mode=True,
            timeout=bash_timeout_sec,
            inherit_env=True,
        )

    return factory


# ---------------------------------------------------------------------------
# Build agent
# ---------------------------------------------------------------------------


def _build_tool_registry_and_tools(
    task_runner_tools: list,
    search_config: SearchConfig | None,
    skill_registry: SkillRegistry | None,
    extra_tools: list | None = None,
    skill_store: Any | None = None,
) -> tuple[list, Any]:
    """Build a ToolRegistry and return (startup_tools, registry).

    startup_tools = [tool_search] + all_custom_tools
    All custom tools go into the graph for execution; only tool_search is
    visible to the LLM upfront (the ToolFilterMiddleware hides tr_*/ws_* etc.).
    ``extra_tools`` rides along for families outside the fixed sets (e.g.
    the always-visible ctx_* artifact readers).
    """
    from yuyutsava.agents.db_tools import make_db_tools

    search_tools = make_search_tools(search_config) if search_config else []
    skill_tools = make_skill_tools(skill_registry, skill_store) if skill_registry else []
    db_tools = make_db_tools()  # always available — read-only by construction
    all_custom_tools = (
        task_runner_tools + search_tools + skill_tools + db_tools + (extra_tools or [])
    )

    registry = ToolRegistry()
    registry.register_many(all_custom_tools)
    tool_search = registry.make_tool_search_tool()

    # tool_search first so it appears first in the graph's tool list.
    # ToolFilterMiddleware hides all tr_* / ws_* / sk_* from the LLM; only
    # tool_search is visible, driving the lazy-discovery pattern.
    startup_tools = [tool_search] + all_custom_tools
    return startup_tools, registry


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
    compactor ever counts tokens, and both sit before BudgetMiddleware (the
    absolute spend ceiling) in the caller's list. The transcript recorder is
    appended last — it only reads ``state["messages"]`` and never rewrites
    state, so its position is immaterial. Returns [] when the context
    controller is not wired (stores absent), preserving the
    pre-context-controller behaviour exactly.
    """
    from yuyutsava.context.compaction import YuyutsavaCompactionMiddleware
    from yuyutsava.context.offload_middleware import ToolResultOffloadMiddleware

    out: list = []
    if artifact_store is not None and context_settings is not None:
        out.append(ToolResultOffloadMiddleware(artifact_store, context_settings))
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
        from yuyutsava.context.transcript_middleware import TranscriptRecorderMiddleware

        out.append(TranscriptRecorderMiddleware(transcript_store, index=transcript_index))

    # Observability (no-op unless YUYUTSAVA_DEBUG_PROMPT). Last in the list so
    # its before_model hook reports the message list AFTER offload + compaction
    # — i.e. exactly what the model receives. Never mutates state.
    from yuyutsava.context.prompt_inspector import PromptInspectorMiddleware

    out.append(PromptInspectorMiddleware(role=role))
    return out


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
            async subagents, ``BackgroundTaskCapMiddleware`` is installed.
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
    model = chat_model(settings)
    checkpointer = checkpointer or MemorySaver()
    # FilesystemPromptOverrideMiddleware strips the deepagents "## Filesystem Tools"
    # block: those built-in tools are filtered out by ToolFilterMiddleware and our
    # own prompt routes filesystem ops through tr_*, so the block only misleads the
    # model and wastes cache-prefix tokens. Pass replacement="..." to reword instead.
    middleware = [
        ToolFilterMiddleware(),
        FilesystemPromptOverrideMiddleware(),
        VoiceStyleMiddleware(),
    ]
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
    if permission_check:
        middleware.append(PermissionMiddleware(workspace_root=workspace_root.resolve()))

    # Per-turn retrieval injection: surface the memory + skills relevant to the
    # user's latest message into the prompt (the CLI is a persistent graph, so
    # it can't inject at build time the way the daemon orchestrator does).
    _injectors: list = []
    if memory_store is not None:
        from yuyutsava.context.injector import MemoryInjector
        _injectors.append(MemoryInjector(memory_store))
    if skill_store is not None:
        from yuyutsava.skills.injector import SkillInjector
        _injectors.append(SkillInjector(skill_store))
    if transcript_index is not None:
        # Recall this conversation's own earlier turns (survives checkpoint sweep).
        from yuyutsava.context.conversation_injector import ConversationInjector
        _injectors.append(ConversationInjector(transcript_index))
    if _injectors:
        from yuyutsava.core.retrieval_injection_middleware import (
            RetrievalInjectionMiddleware,
        )
        middleware.append(RetrievalInjectionMiddleware(_injectors))

    context_tools: list = []
    if artifact_store is not None:
        from yuyutsava.context.tools import make_context_tools
        context_tools.extend(make_context_tools(artifact_store))
    if memory_store is not None:
        from yuyutsava.memory.tools import make_memory_tools
        context_tools.extend(make_memory_tools(memory_store))

    loc = local_settings or LocalSettings()
    ws = workspace_root.resolve()
    sandbox_root = (loc.sandbox_dir.resolve() if loc.sandbox_dir else ws / "_sandbox")
    output_dir   = (loc.output_dir.resolve()  if loc.output_dir  else ws / "_output")

    # Visual tools (vis_*): always available, like ctx_*/mem_*. Files land in the
    # workspace _output/visuals so the CLI can point the user at them; the daemon
    # serves them by id from the same SQLite index.
    from yuyutsava.visuals.tools import make_visual_tools
    context_tools.extend(make_visual_tools(output_dir=output_dir))

    # TODO board capture (todo_add/todo_list/todo_get): the user can file/read
    # TODOs from any chat; the full editing set belongs to the TinkerAgent.
    from yuyutsava.todoboard.tools import make_todo_tools
    context_tools.extend(make_todo_tools(scope="capture"))

    skill_registry = SkillRegistry(workspace_dir=ws)

    subagent_specs: list[dict] = []
    if subagents:
        subagent_specs.extend(sa.as_deepagents_subagent_spec() for sa in subagents)

    if async_subagents:
        if not async_host_url:
            raise ValueError(
                "build_cli_deepagent: async_subagents requires async_host_url. "
                "Build an AsyncSubagentHost and pass its .url here."
            )
        subagent_specs.extend(
            sa.as_async_subagent_spec(url=async_host_url)
            for sa in async_subagents
            if getattr(sa, "supports_async", False)
        )

    if remote_async_subagents:
        subagent_specs.extend(r.as_async_subagent_spec() for r in remote_async_subagents)

    if async_task_mirror is not None and (async_subagents or remote_async_subagents):
        # Local import: keeps engine.py importable from contexts that don't
        # ship langgraph_api (e.g. very minimal CLI scripts).
        from yuyutsava.async_subagents.cap_middleware import BackgroundTaskCapMiddleware
        middleware.append(
            BackgroundTaskCapMiddleware(async_task_mirror, max_concurrent=async_max_concurrent)
        )

    if async_subagents or remote_async_subagents:
        from yuyutsava.async_subagents.interrupt_middleware import AsyncTaskInterruptPatchMiddleware
        from yuyutsava.async_subagents.check_guard_middleware import CheckAsyncTaskGuardMiddleware
        async_specs = [s for s in subagent_specs if "graph_id" in s and "url" in s]
        middleware.append(AsyncTaskInterruptPatchMiddleware(async_specs))
        middleware.append(CheckAsyncTaskGuardMiddleware())

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
            extra_tools=context_tools, skill_store=skill_store,
        )
        graph = create_deep_agent(
            model=model,
            tools=startup_tools,
            backend=docker_backend,
            system_prompt=docker_system_prompt(
                workspace_root, docker_cfg.export_dir, _registry.catalog_block()
            ),
            checkpointer=checkpointer,
            middleware=middleware,
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

    backend = _local_shell_backend_factory(workspace_root, bash_timeout_sec)
    startup_tools, _registry = _build_tool_registry_and_tools(
        _bind_task_runner_tools(ws, sandbox_root), search_config, skill_registry,
        extra_tools=context_tools, skill_store=skill_store,
    )
    graph = create_deep_agent(
        model=model,
        tools=startup_tools,
        backend=backend,
        system_prompt=local_system_prompt(
            workspace_root, sandbox_root, output_dir, _registry.catalog_block()
        ),
        checkpointer=checkpointer,
        middleware=middleware,
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


# Back-compat alias. Old callers that still say ``build_agent`` keep working
# for one cycle; new code should prefer ``build_cli_deepagent``.
build_agent = build_cli_deepagent


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
    task's join keys; when ``deps.usage_store`` is set, a ``UsageRecorder``
    rides every agent in the graph and writes one ``llm_usage`` row per
    model call.
    """
    # Lazy imports: this function is in engine.py but pulls daemon-only modules.
    # Keeping these imports inside the function avoids loading the daemon stack
    # when the CLI imports the engine.
    from yuyutsava.agents.orchestrator.agent import _make_ask_user_tool
    from yuyutsava.agents.orchestrator.capabilities import render_capabilities_block
    from yuyutsava.agents.orchestrator.prompts import render_system_prompt
    from yuyutsava.daemon.budget import BudgetMiddleware
    from yuyutsava.events.tools import make_recall_tool

    capabilities = render_capabilities_block(
        list(deps.subagents.values()),
        async_subagents=getattr(deps, "async_subagents", None),
        remote_async_subagents=getattr(deps, "remote_async_subagents", None),
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
        master_tools.extend(make_skill_tools(skill_registry, skill_store))
    if deps.search_config is not None:
        master_tools.extend(make_search_tools(deps.search_config, cap_enforcer=deps.cap_enforcer))
    if deps.mcp_manager is not None:
        master_tools.extend(deps.mcp_manager.tools_for("orchestrator"))
    if deps.artifact_store is not None:
        from yuyutsava.context.tools import make_context_tools
        master_tools.extend(make_context_tools(deps.artifact_store))
    if deps.memory_store is not None:
        from yuyutsava.memory.tools import make_memory_tools
        master_tools.extend(make_memory_tools(deps.memory_store))
    # TODO board capture — same subset as the CLI deepagent.
    from yuyutsava.todoboard.tools import make_todo_tools
    master_tools.extend(make_todo_tools(scope="capture"))

    # Lazy discovery: ToolFilterMiddleware hides the prefixed master tools
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
        from yuyutsava.core.llm import model_name_of
        from yuyutsava.daemon.usage import UsageRecorder
        return [UsageRecorder(
            usage_store,
            role=role,
            model_name=model_name_of(agent_model),
            task_id=getattr(usage_context, "task_id", ""),
            thread_id=getattr(usage_context, "thread_id", ""),
        )]

    subagent_specs: list[dict] = []
    for sa in deps.subagents.values():
        spec = sa.as_deepagents_subagent_spec()
        spec["model"] = deps.subagent_model
        spec["middleware"] = [
            ToolFilterMiddleware(),
            *_ctx_mw(deps.subagent_model, sa.name),
            BudgetMiddleware(max_input_tokens=deps.subagent_token_budget, role=sa.name),
            *_usage_mw(deps.subagent_model, sa.name),
        ]
        if deps.artifact_store is not None:
            # Subagents read their own offloaded results, so the ctx_* pair
            # must exist in their graphs too (fresh instances per spec).
            from yuyutsava.context.tools import make_context_tools
            spec["tools"] = list(spec.get("tools") or []) + make_context_tools(deps.artifact_store)
        subagent_specs.append(spec)

    # Async (background) subagents — same `subagents=` list; deepagents auto-
    # routes dicts shaped like AsyncSubAgent to AsyncSubAgentMiddleware.
    async_subagents = getattr(deps, "async_subagents", None) or []
    async_host_url = getattr(deps, "async_host_url", None)
    if async_subagents:
        if not async_host_url:
            raise ValueError(
                "build_orchestrator: deps.async_subagents requires deps.async_host_url. "
                "Build an AsyncSubagentHost at daemon boot and store its .url on deps."
            )
        for sa in async_subagents:
            if not getattr(sa, "supports_async", False):
                continue
            subagent_specs.append(sa.as_async_subagent_spec(url=async_host_url))

    for r in (getattr(deps, "remote_async_subagents", None) or []):
        subagent_specs.append(r.as_async_subagent_spec())

    budget = BudgetMiddleware(max_input_tokens=budget_tokens, role="orchestrator")
    workspace_root = str(deps.workspace_root.resolve()) if deps.workspace_root else "/"

    def _backend_factory(_runtime):
        return LocalShellBackend(
            root_dir=workspace_root,
            virtual_mode=True,
            timeout=10,
            inherit_env=False,
        )

    # Order: tool filter → offload (tool path) → compaction (model path) →
    # budget (absolute ceiling, must see post-compaction usage last) →
    # usage recorder (passive accounting, sees the same final usage).
    master_middleware: list = [
        ToolFilterMiddleware(),
        VoiceStyleMiddleware(),
        *_ctx_mw(model, "orchestrator"),
        budget,
        *_usage_mw(model, "orchestrator"),
    ]
    mirror = getattr(deps, "async_task_mirror", None)
    if mirror is not None and (async_subagents or getattr(deps, "remote_async_subagents", None)):
        from yuyutsava.async_subagents.cap_middleware import BackgroundTaskCapMiddleware
        max_conc = getattr(deps, "async_max_concurrent", 8)
        master_middleware.append(
            BackgroundTaskCapMiddleware(mirror, max_concurrent=max_conc)
        )

    if async_subagents or getattr(deps, "remote_async_subagents", None):
        from yuyutsava.async_subagents.interrupt_middleware import AsyncTaskInterruptPatchMiddleware
        from yuyutsava.async_subagents.check_guard_middleware import CheckAsyncTaskGuardMiddleware
        async_specs = [s for s in subagent_specs if "graph_id" in s and "url" in s]
        master_middleware.append(AsyncTaskInterruptPatchMiddleware(async_specs))
        master_middleware.append(CheckAsyncTaskGuardMiddleware())

    return create_deep_agent(
        model=model,
        tools=master_tools,
        backend=_backend_factory,
        system_prompt=system_prompt,
        checkpointer=checkpointer if checkpointer is not None else MemorySaver(),
        middleware=master_middleware,
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
    skill_registry: SkillRegistry | None = None,
    budget_tokens: int = 60_000,
    usage_store: Any | None = None,
    mcp_tools: "list[Any] | None" = None,
    note_index: Any | None = None,
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
    consent, lazy tool discovery) plus the orchestrator's BudgetMiddleware/
    UsageRecorder pair:
      tool filter → offload (tool path) → compaction (model path) →
      budget (absolute ceiling) → usage recorder (passive accounting).

    Tools: todo_* FULL scope (author="tinker"), tr_* bound to the card
    workspace with ``agent_name="tinker"``, vis_*, ws_* (when
    ``search_config``), mem_*/ctx_* (when stores wired), sk_* with the
    tinker skills namespace. ``subagents``/``async_subagents`` ride along
    exactly like the CLI factory — no spawn-subagent tool.
    """
    model = chat_model(settings)
    checkpointer = checkpointer or MemorySaver()
    ws = card_workspace.resolve()
    sandbox_root = ws / "_sandbox"
    output_dir = ws / "_output"

    middleware: list = [
        ToolFilterMiddleware(),
        FilesystemPromptOverrideMiddleware(),
        VoiceStyleMiddleware(),
    ]
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
    # Absolute spend ceiling + passive accounting, after compaction (the
    # orchestrator's ordering rule). Lazy imports: daemon-only modules.
    from yuyutsava.daemon.budget import BudgetMiddleware
    middleware.append(BudgetMiddleware(max_input_tokens=budget_tokens, role="tinker"))
    if usage_store is not None:
        from yuyutsava.core.llm import model_name_of
        from yuyutsava.daemon.usage import UsageRecorder
        middleware.append(UsageRecorder(
            usage_store,
            role="tinker",
            model_name=model_name_of(model),
            task_id=f"tinker:{card_id}",
            thread_id=f"todo:{card_id}",
        ))
    if permission_check:
        middleware.append(PermissionMiddleware(workspace_root=ws))

    # Per-turn retrieval injection — the tinker graph is as persistent as the
    # CLI's, so memory/skills/transcript recall must be per-turn, and the
    # skills search is scoped to the tinker namespace (agent column).
    _injectors: list = []
    if memory_store is not None:
        from yuyutsava.context.injector import MemoryInjector
        _injectors.append(MemoryInjector(memory_store))
    if skill_store is not None:
        from yuyutsava.skills.injector import SkillInjector
        _injectors.append(SkillInjector(skill_store, agent="tinker"))
    if transcript_index is not None:
        from yuyutsava.context.conversation_injector import ConversationInjector
        _injectors.append(ConversationInjector(transcript_index))
    if note_index is not None:
        # Board-wide note recall: a related decision often lives on a
        # DIFFERENT card than the one this bundle is pinned to.
        from yuyutsava.todoboard.recall import TodoNoteInjector
        _injectors.append(TodoNoteInjector(note_index))
    if _injectors:
        from yuyutsava.core.retrieval_injection_middleware import (
            RetrievalInjectionMiddleware,
        )
        middleware.append(RetrievalInjectionMiddleware(_injectors))

    context_tools: list = []
    if artifact_store is not None:
        from yuyutsava.context.tools import make_context_tools
        context_tools.extend(make_context_tools(artifact_store))
    if memory_store is not None:
        from yuyutsava.memory.tools import make_memory_tools
        context_tools.extend(make_memory_tools(memory_store))

    from yuyutsava.visuals.tools import make_visual_tools
    context_tools.extend(make_visual_tools(output_dir=output_dir))

    # The whole point: FULL board scope, notes authored as "tinker".
    from yuyutsava.todoboard.tools import make_todo_tools
    context_tools.extend(make_todo_tools(scope="full", author="tinker"))

    # User-configured MCP servers, scoped to "tinker" — registered like every
    # other master tool so unprefixed names stay model-visible and the rest
    # ride tool_search, exactly as build_orchestrator wires them.
    if mcp_tools:
        context_tools.extend(mcp_tools)

    skill_registry = skill_registry or SkillRegistry()

    subagent_specs: list[dict] = []
    if subagents:
        subagent_specs.extend(sa.as_deepagents_subagent_spec() for sa in subagents)
    if async_subagents:
        if not async_host_url:
            raise ValueError(
                "build_tinker_agent: async_subagents requires async_host_url."
            )
        subagent_specs.extend(
            sa.as_async_subagent_spec(url=async_host_url)
            for sa in async_subagents
            if getattr(sa, "supports_async", False)
        )
    if async_task_mirror is not None and async_subagents:
        from yuyutsava.async_subagents.cap_middleware import BackgroundTaskCapMiddleware
        middleware.append(
            BackgroundTaskCapMiddleware(async_task_mirror, max_concurrent=async_max_concurrent)
        )
    if async_subagents:
        from yuyutsava.async_subagents.interrupt_middleware import AsyncTaskInterruptPatchMiddleware
        from yuyutsava.async_subagents.check_guard_middleware import CheckAsyncTaskGuardMiddleware
        async_specs = [s for s in subagent_specs if "graph_id" in s and "url" in s]
        middleware.append(AsyncTaskInterruptPatchMiddleware(async_specs))
        middleware.append(CheckAsyncTaskGuardMiddleware())

    backend = _local_shell_backend_factory(ws, bash_timeout_sec)
    startup_tools, _registry = _build_tool_registry_and_tools(
        _bind_task_runner_tools(ws, sandbox_root, agent_name="tinker"),
        search_config, skill_registry,
        extra_tools=context_tools, skill_store=skill_store,
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
        ),
        checkpointer=checkpointer,
        middleware=middleware,
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
