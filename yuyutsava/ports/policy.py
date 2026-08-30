"""Policy protocols — rate caps and consent, without importing the tool layer."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CapEnforcer(Protocol):
    """Daily per-tool rate cap for the ``ws_*`` searches.

    Previously ``cap_enforcer: object | None`` on both ``OrchestratorDeps`` and
    ``BaseSubAgent``, annotated ``# tools.search._CapEnforcer; untyped to avoid
    cycle`` — ``tools`` imports ``core``, and ``core`` builds the agents that
    carry this.
    """

    async def check_and_incr(self, tool_name: str) -> bool: ...


__all__ = ["CapEnforcer", "RuntimeToggles"]


@runtime_checkable
class RuntimeToggles(Protocol):
    """Hot runtime switches — voice mode, the dedicated-subagent deny-list.

    NOTE: ``prefs.runtime.RuntimeSettings`` has **zero** internal imports, so it
    is not part of any cycle and could be imported directly. It is declared here
    anyway for a different reason: consumers only ever call ``subagents()``, and
    a two-method protocol says that where the concrete class exposes far more.
    """

    def subagents(self) -> Any: ...


@runtime_checkable
class ContextTuning(Protocol):
    """The context-controller knobs a builder reads off ``ContextSettings``.

    Four attributes, not the settings class. ``ContextSettings`` is a concrete
    dataclass in ``yuyutsava.context.config``; annotating with it from
    ``agents/orchestrator`` is importable today but couples the dependency
    record to a module it otherwise has no reason to know. The Protocol says
    what is actually read.
    """

    offload_threshold_chars: int
    compact_trigger_tokens: int
    keep_messages: int
    semantic_recall: bool


@runtime_checkable
class TaskMirror(Protocol):
    """The background-task mirror, as the master graph uses it.

    ``AsyncTaskMirror`` has eleven public methods; the agent side reads three.
    Naming those keeps ``async_subagents`` — which pulls in the LangGraph host —
    off the orchestrator's import path.
    """

    # All three are SYNCHRONOUS. ``AsyncTaskMirror`` is an in-memory dict of
    # mirrored task records — nothing here awaits anything, and every caller
    # (`cap_policy`, `watcher`) calls them plainly.
    #
    # They were declared ``async`` when this Protocol was written, and nothing
    # caught it: ``runtime_checkable`` ``isinstance`` compares method *names*
    # only, so the mirror satisfied a contract it did not implement. A caller
    # trusting the annotation would have got ``TypeError: object int can't be
    # used in 'await' expression``. See finding BB.

    def render_block(self) -> str: ...

    def count_running(self) -> int: ...

    def list_non_terminal(self) -> list: ...


@runtime_checkable
class RemoteSubagentSpec(Protocol):
    """A background subagent hosted on a remote Agent Protocol server."""

    name: str
    description: str
    graph_id: str
    url: str
