"""What the daemon's HTTP/WS app is built with.

``create_app`` took 25 keyword arguments and ``make_app`` repeated all 25 to
forward them, so adding one dependency meant editing three lists that nothing
checked against each other (`R-RDB-03`).

Not to be confused with :mod:`yuyutsava.daemon.web.deps`, which is the other
half of the same story: this is what the app is *given* at startup, and that is
how a router *asks* for it at request time (``Depends(get_usage_store)`` and
friends, reading ``app.state``).

Most fields are deliberately duck-typed. The app only stashes them for routers
to fetch by attribute, so naming each concrete class here would pull most of the
daemon into any module that merely wants to describe an app.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from typing import Any, Callable

from yuyutsava.daemon.web.auth import AuthSettings
from yuyutsava.skills.registry import SkillRegistry


@dataclass
class AppDeps:
    """The long-lived objects the app stashes on ``app.state``.

    Every field defaults to absent, and absent is meaningful: no
    ``conversation_manager`` and ``/ws/converse`` refuses the connection; no
    ``usage_store`` and the usage endpoints report nothing. A
    default-constructed ``AppDeps`` is the smallest app that still answers
    ``/health``, which is what most tests want.

    Mutable on purpose: ``ask_resume`` needs the conversation manager, which the
    daemon only has after the app exists, so it is installed afterwards.
    """

    skill_registry: SkillRegistry | None = None
    #: Re-reads the events config on demand, behind the config endpoint.
    config_reload: Callable[[], Any] | None = None
    auth: AuthSettings | None = None

    # -- HITL, and the surfaces that answer it -----------------------------
    channels: Any | None = None  # ChannelRouter
    session_origin: Any | None = None  # SessionOriginMap
    decision_service: Any | None = None  # DecisionService
    ask_registry: Any | None = None  # daemon.ask_registry.AskRegistry
    ask_resume: Any | None = None  # daemon.ask_resume.AskResumeService
    channel_plugins: Any | None = None  # ChannelPluginRegistry

    # -- background work ---------------------------------------------------
    task_registry: Any | None = None  # TaskRegistry
    task_submission: Any | None = None  # TaskSubmissionService
    #: Whether the background subagent host is enabled at all.
    async_subagents: bool = False
    async_task_watcher: Any | None = None  # async_subagents.watcher
    subagent_roster: dict | None = None  # {name: BaseSubAgent}
    #: The MCP servers' manager, for ``GET /mcp/status``; absent, it lists none.
    mcp_manager: Any | None = None

    # -- stores ------------------------------------------------------------
    #: The TODO board (``TodoExchange``); without it the board routes answer 503.
    todo_exchange: Any | None = None
    #: Sessions, rendered visuals and message feedback. Without one, the routes
    #: that need it answer 503.
    session_store: Any | None = None
    visual_store: Any | None = None
    feedback_store: Any | None = None
    usage_store: Any | None = None
    memory_store: Any | None = None
    voice_store: Any | None = None
    transcript_store: Any | None = None

    # -- conversations, capacity, routing ----------------------------------
    conversation_manager: Any | None = None
    resource_monitor: Any | None = None
    admission_controller: Any | None = None
    model_router: Any | None = None
    runtime_settings: Any | None = None

    def replace(self, **changes: Any) -> "AppDeps":
        """A copy with some fields changed."""
        return dataclass_replace(self, **changes)


__all__ = ["AppDeps"]
