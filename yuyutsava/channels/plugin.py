"""ChannelPlugin contract + InboundSink facade.

A channel plugin is a :class:`~yuyutsava.daemon.channels.UserChannel`
(outbound: ``post_event`` / ``post_proposal`` / ``post_ask``) that
additionally runs an inbound loop (e.g. Telegram ``getUpdates`` long-poll)
through which the user can *invoke* the daemon: submit tasks, answer
proposals and asks, query status.

The :class:`InboundSink` is the **only** daemon surface a plugin sees —
constructor-injected at :meth:`ChannelPlugin.start`. It fronts the
TaskSubmissionService (task submission), the DecisionService (proposal/ask
responses), the TaskRegistry (read-only listing), and a small key-value
state store (plugin persistence, e.g. the Telegram ``getUpdates`` offset).
Plugins never import daemon internals.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from typing import Any, Callable

from yuyutsava.daemon.channels import ProposalDecision, UserChannel
# Re-exported so plugins can catch decision conflicts without importing
# daemon web internals — the sink is their whole world.
from yuyutsava.daemon.web.services.decision_service import (  # noqa: F401
    DecisionConflictError,
)

logger = logging.getLogger("yuyutsava.channels.plugin")

# Capability strings a plugin may advertise.
CAP_NOTIFY = "notify"        # outbound events
CAP_PROPOSAL = "proposal"    # can render Tier-1 proposals + collect decisions
CAP_ASK = "ask"              # can render Tier-2 asks + collect responses
CAP_INVOKE = "invoke"        # inbound: user can submit tasks through it


class ChannelPlugin(UserChannel):
    """A runtime-managed channel: UserChannel + inbound lifecycle.

    Lifecycle is owned by the ChannelPluginRegistry:
    ``from_config(params)`` → ``await start(sink)`` → router fan-out …
    → ``await stop()``. ``start`` must return promptly (spawn loops, don't
    run them inline) and ``stop`` must be safe to call once after a
    successful ``start``.
    """

    plugin_id: str = "unnamed"
    capabilities: frozenset[str] = frozenset()

    @abstractmethod
    async def start(self, inbound: "InboundSink") -> None:
        """Begin the inbound loop(s). ``inbound`` is the daemon facade."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop loops and release resources (HTTP clients, tasks)."""

    @classmethod
    @abstractmethod
    def from_config(cls, params: dict[str, Any]) -> "ChannelPlugin":
        """Build an instance from ``channels_config.json`` params.

        Secrets (bot tokens, …) come from the environment, never params.
        Raise ``ValueError`` with a clear message when required config is
        missing — the registry surfaces it to the user.
        """


class InboundSink:
    """Constructor-injected facade: everything a plugin may do to the daemon.

    ``pending_proposals`` / ``pending_asks`` mirror the WebHub pattern: a
    plugin that blocks in ``post_proposal``/``post_ask`` parks its
    ``asyncio.Future`` here, and the shared DecisionService (which holds
    these maps via ``add_waiters``) resolves it when the response arrives —
    over HTTP or through the plugin's own inbound loop.
    """

    def __init__(
        self,
        *,
        task_submission: object,
        decision_service: object,
        task_registry: object | None = None,
        prefs_store: object | None = None,
        status_provider: Callable[[], str] | None = None,
    ) -> None:
        self._submission = task_submission
        self._decisions = decision_service
        self._registry = task_registry
        self._prefs = prefs_store
        self._status_provider = status_provider
        self.pending_proposals: dict[str, "asyncio.Future[ProposalDecision]"] = {}
        self.pending_asks: dict[str, "asyncio.Future[str]"] = {}

    # ------------------------------------------------------------------ #
    # Invoke                                                              #
    # ------------------------------------------------------------------ #

    async def submit_task(
        self, text: str, *, origin: str, session_hint: str | None = None,
    ) -> str:
        """Submit a user task (trusted/direct mode). Returns the task_id.

        Plugin-originated submissions are direct: the human typed the
        instruction at an allowlisted surface, which is implicit Tier-1
        consent (same trust as POST /tasks). Tier-2 asks still fire.
        """
        return await self._submission.submit_direct(
            text, origin=origin, session_hint=session_hint,
        )

    async def respond_proposal(
        self,
        proposal_id: str,
        decision: str,
        *,
        edited_instruction: str | None = None,
    ) -> Any:
        """Forward a Tier-1 decision. Raises DecisionConflictError when gone."""
        return await self._decisions.respond_proposal(
            proposal_id, decision, edited_instruction=edited_instruction,
        )

    async def respond_ask(self, ask_id: str, response: str) -> Any:
        """Forward a Tier-2 response. Raises DecisionConflictError when gone."""
        return await self._decisions.respond_ask(ask_id, response)

    # ------------------------------------------------------------------ #
    # Introspection                                                       #
    # ------------------------------------------------------------------ #

    async def list_pending(self) -> dict[str, Any]:
        """Queued + running tasks and ids of unanswered proposals/asks."""
        tasks: list[dict[str, Any]] = []
        if self._registry is not None:
            for status in ("running", "queued"):
                records, _cursor = await self._registry.list(status=status, limit=10)
                tasks.extend(r.as_dict() for r in records)
        proposal_ids, ask_ids = self._decisions.pending_ids()
        return {
            "tasks": tasks,
            "pending_proposal_ids": proposal_ids,
            "pending_ask_ids": ask_ids,
        }

    def daemon_status(self) -> str:
        """One human-readable health line for ``/status``-style commands."""
        if self._status_provider is not None:
            try:
                return self._status_provider()
            except Exception:  # noqa: BLE001
                logger.exception("status_provider failed")
        return "daemon: running"

    # ------------------------------------------------------------------ #
    # Plugin state (user_prefs-backed; e.g. telegram.offset)              #
    # ------------------------------------------------------------------ #

    def get_state(self, key: str, default: Any = None) -> Any:
        """Read a persisted plugin value (sync — prefs reads are sync)."""
        if self._prefs is None:
            return default
        return self._prefs.get(key, default)

    async def put_state(self, key: str, value: Any) -> None:
        """Persist a plugin value (no-op without a prefs store, e.g. tests)."""
        if self._prefs is not None:
            await self._prefs.set(key, value)
