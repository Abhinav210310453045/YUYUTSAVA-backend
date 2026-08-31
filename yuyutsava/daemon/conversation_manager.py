"""Daemon-side host for interactive agent conversations (text + voice).

The daemon already owns the orchestrator and the shared ``AsyncSubagentHost``.
This manager lets the web layer run *interactive conversations* — the same
deepagent the CLI drives — inside the daemon so the Electron/mobile UIs can talk
to it over a WebSocket. Conversations are served by per-agent
:class:`~yuyutsava.core.engine.AgentBundle`\\ s held in ``self._bundles``:

  * ``"master"`` — the shared conversational deepagent (text chat + voice),
    one bundle for every thread; per-conversation state is isolated by
    ``thread_id`` via the checkpointer.
  * ``"tinker:<card_id>"`` — one TinkerAgent bundle per TODO card. Per-card
    because the bundle binds tr_* to the card's workspace dir and bakes the
    card's identity into its system prompt. A card can hold many chats, each
    a thread in the ``todo:<card_id>[:<suffix>]`` family sharing that one
    bundle (the legacy pre-multi-chat pin is the bare ``todo:<card_id>``).

Lazy by design: each bundle is built on its first ``open()``, not at daemon
startup — no cost or startup latency when a surface is unused. Because the
stack builders route through ``acquire_or_attach_host`` (first-come-wins), the
builds *attach* to the daemon's already-running async host rather than starting
a second one, so conversations delegate background work to the same
orchestrator the CLI uses.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from pathlib import Path
from typing import Any

from yuyutsava.conversation import ConversationService
from yuyutsava.core.config import (
    DockerSettings,
    LlmSettings,
    LocalSettings,
    SearchConfig,
    llm_settings_from_env,
)
from yuyutsava.core.engine import AgentBundle
from yuyutsava.daemon.turn_registry import TurnRegistry, TurnRun
from yuyutsava.storage.ids import (
    is_tinker_thread_of,
    mint_tinker_thread_id,
    tinker_thread_base,
)
from yuyutsava.storage.sessions import SessionStore, get_default_session_store

logger = logging.getLogger("yuyutsava.daemon.conversation_manager")


def _bash_timeout_sec() -> int:
    try:
        return max(1, int(os.environ.get("YUYUTSAVA_BASH_TIMEOUT", "300")))
    except ValueError:
        return 300


def _chat_budget_tokens() -> int:
    """Absolute per-call input-token ceiling for the shared chat master."""
    try:
        return max(1, int(os.environ.get("YUYUTSAVA_CHAT_BUDGET_TOKENS", "120000")))
    except ValueError:
        return 120_000


class ConversationManager:
    """Lazily builds per-agent conversational bundles; opens services per chat."""

    def __init__(
        self,
        *,
        workspace: Path,
        checkpointer: Any,
        settings: LlmSettings | None = None,
        search_config: SearchConfig | None = None,
        recursion_limit: int = 200,
        store: SessionStore | None = None,
        voice_store: Any | None = None,
        usage_store: Any | None = None,
        mcp_manager: Any | None = None,
        launch_index: Any | None = None,
        prefs_store: Any | None = None,
        runtime_settings: Any | None = None,
        cap_enforcer: Any | None = None,
        task_submission: Any | None = None,
        extra_subagents: Any | None = None,
    ) -> None:
        self._workspace = workspace
        self._checkpointer = checkpointer
        self._settings = settings
        self._search_config = search_config
        self._recursion_limit = recursion_limit
        self._store = store
        self.voice_store = voice_store  # storage.voice_store.VoiceMessageStore | None
        self._usage_store = usage_store  # daemon.usage.UsageStore | None (chat + tinker accounting)
        self._mcp_manager = mcp_manager  # mcp.loader.MCPClientManager | None (chat + tinker MCP tools)
        self._prefs_store = prefs_store  # storage.prefs.PrefsStore | None (per-turn prefs injection)
        # prefs.runtime.RuntimeSettings | None — the dedicated-subagent switches.
        # The master bundle below is built once and reused by every conversation,
        # so the toggle can't be baked into its roster; it rides along and is
        # enforced per model/tool call by SubagentGatePolicy.
        self._runtime_settings = runtime_settings
        self._cap_enforcer = cap_enforcer  # tools.search._CapEnforcer | None (ws_* rate cap)
        self._task_submission = task_submission  # daemon.task_submission.TaskSubmissionService | None
        self._extra_subagents = list(extra_subagents or [])  # extra sync task roster (file-organizer, …)
        # async_subagents.launch_index.LaunchIndex | None — links bg tasks a
        # conversation launches back to its thread, so the watcher's completion
        # bridge can wake the master on THIS conversation (the orchestrator loop
        # records its own launches; conversations must record theirs here).
        self._launch_index = launch_index
        # Bundle cache: "master" | "tinker:<card_id>" → AgentBundle. The
        # factory map below is per AGENT; the key carries the card because a
        # tinker bundle is card-specific (workspace + prompt).
        self._bundles: dict[str, AgentBundle] = {}
        self._build_locks: dict[str, asyncio.Lock] = {}
        # Every in-flight conversation turn, owned by the daemon and addressed
        # by thread_id — NOT by the socket that asked for it. Sockets attach as
        # viewers, so closing a tinker pane or reloading the renderer no longer
        # kills the agent mid-node. It doubles as the per-thread turn gate the
        # WS handler used to keep in a bare set: a LangGraph checkpoint is
        # single-writer per thread, and two turns streaming the same thread
        # concurrently interleave message writes (duplicate leading human
        # message, empty messages the model 400s on — duplicated bubbles and
        # truncated "half" replies). See daemon/turn_registry.py.
        self._turns = TurnRegistry()

    # ------------------------------------------------------------------ #
    # Bundle builds                                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _bundle_key(agent: str, card_id: str | None) -> str:
        return "master" if agent == "master" else f"{agent}:{card_id}"

    def _resolved_settings(self) -> LlmSettings:
        return self._settings or llm_settings_from_env("orchestrator")

    def _resolved_search(self) -> SearchConfig:
        return self._search_config or SearchConfig.from_env()

    async def _ensure_bundle(
        self, agent: str = "master", card_id: str | None = None
    ) -> AgentBundle:
        key = self._bundle_key(agent, card_id)
        bundle = self._bundles.get(key)
        if bundle is not None:
            return bundle
        lock = self._build_locks.setdefault(key, asyncio.Lock())
        async with lock:
            bundle = self._bundles.get(key)
            if bundle is not None:
                return bundle
            # Per-agent factory map — the imports stay lazy so the (heavy)
            # agent-stack import graph is off the daemon's hot path until a
            # conversation is actually opened.
            if agent == "master":
                bundle = await self._build_master_bundle()
            elif agent == "tinker":
                assert card_id, "tinker bundles are card-bound"
                bundle = await self._build_tinker_bundle(card_id)
            else:
                raise ValueError(f"unknown conversation agent {agent!r}")
            self._bundles[key] = bundle
            if bundle.async_host_url:
                logger.info(
                    "conversation[%s]: bundle attached to async host @ %s",
                    key, bundle.async_host_url,
                )
        return bundle

    def _orch_submit_tools(self, origin: str) -> list:
        """The orch_submit hand-off tool, when the daemon wired a submission
        service. Events/background work stay orchestrator-only — this is the
        conversation masters' sanctioned path to that side of the system."""
        if self._task_submission is None:
            return []
        from yuyutsava.daemon.submit_tool import make_orch_submit_tool
        return [make_orch_submit_tool(self._task_submission, origin=origin)]

    async def _build_master_bundle(self) -> AgentBundle:
        from yuyutsava.cli.agent_stack import build_agent_stack

        logger.info("conversation: building shared master bundle (lazy, first use)")
        return await build_agent_stack(
            self._workspace,
            self._resolved_settings(),
            bash_timeout_sec=_bash_timeout_sec(),
            execution_mode="local",
            docker_settings=DockerSettings.from_env(),
            local_settings=LocalSettings.from_env(),
            permission_check=True,
            search_config=self._resolved_search(),
            checkpointer=self._checkpointer,
            mcp_manager=self._mcp_manager,
            usage_store=self._usage_store,
            budget_tokens=_chat_budget_tokens(),
            prefs_store=self._prefs_store,
            runtime_settings=self._runtime_settings,
            cap_enforcer=self._cap_enforcer,
            extra_subagents=self._extra_subagents,
            extra_tools=self._orch_submit_tools("chat"),
        )

    async def _build_tinker_bundle(self, card_id: str) -> AgentBundle:
        from yuyutsava.agents.tinker.agent import build_tinker_stack
        from yuyutsava.todoboard.exchange import get_default_exchange

        # Board access only via the exchange — also 404s a bogus card before
        # the (expensive) stack build starts.
        card = await get_default_exchange().get_card(card_id)
        card_ws = Path(card.workspace_path)
        await asyncio.to_thread(card_ws.mkdir, parents=True, exist_ok=True)
        logger.info(
            "conversation: building tinker bundle for card %s (lazy, first use)",
            card_id,
        )
        return await build_tinker_stack(
            self._workspace,
            self._resolved_settings(),
            card_id=card_id,
            card_workspace=card_ws,
            bash_timeout_sec=_bash_timeout_sec(),
            search_config=self._resolved_search(),
            checkpointer=self._checkpointer,
            usage_store=self._usage_store,
            mcp_manager=self._mcp_manager,
            prefs_store=self._prefs_store,
            cap_enforcer=self._cap_enforcer,
            extra_tools=self._orch_submit_tools("tinker"),
        )

    # ------------------------------------------------------------------ #
    # Background-task launch correlation                                   #
    # ------------------------------------------------------------------ #

    def record_async_launch(
        self, ev: Any, *, thread_id: str, origin: str | None = None
    ) -> None:
        """Link a bg task launched during a conversation turn to its thread.

        Mirror of ``OrchestratorLoop._record_async_launch``: sniff the
        ``start_async_task`` tool result (which carries the new task_id) out of
        the event stream and record it in the shared ``LaunchIndex``, so the
        watcher's completion sink wakes the master on this conversation's
        thread instead of leaving the task un-notified. No-op when async
        subagents are disabled or the event is anything else.
        """
        if self._launch_index is None or getattr(ev, "kind", "") != "tool_result":
            return
        if ev.data.get("name") != "start_async_task":
            return
        from yuyutsava.async_subagents.launch_index import parse_async_task_id

        tid = parse_async_task_id(ev.data.get("full") or ev.data.get("preview") or "")
        if tid:
            self._launch_index.record(tid, thread_id, origin)

    # ------------------------------------------------------------------ #
    # Opening conversations                                                #
    # ------------------------------------------------------------------ #

    async def open(
        self,
        *,
        agent: str = "master",
        card_id: str | None = None,
        origin: str = "cli",
        resume_id: str | None = None,
        continue_latest: bool = False,
    ) -> tuple[ConversationService, bool]:
        """Resolve a session and return a ready-to-run conversation service.

        ``origin`` tags the session ("cli" for the Electron text chat, "voice"
        for the voice agent, "tinker" for card chats) so the Sessions UI can
        split them. ``agent_path`` is seeded equal to ``origin`` for interrupt
        attribution.

        ``agent="tinker"`` requires ``card_id``. A card can hold many chats
        (thread ids ``todo:<card_id>[:<suffix>]``): ``resume_id`` resumes one
        of them, ``continue_latest`` resumes the newest, and neither means a
        fresh chat on the card.
        """
        store = self._store or get_default_session_store()

        if agent == "tinker":
            if not card_id:
                raise ValueError("tinker conversations require a card id (?card=)")
            return await self._open_tinker(
                store, card_id, origin=origin,
                resume_id=resume_id, continue_latest=continue_latest,
            )

        # Master path: defer the heavy bundle build to the first turn (inside
        # the cancellable turn task) so the WS handshake stays responsive.
        master = self._bundles.get("master")
        return await ConversationService.resolve(
            store=store,
            bundle=master,                  # reuse if already built
            bundle_factory=None if master else self._ensure_bundle,
            workspace=self._workspace,
            origin=origin,
            resume_id=resume_id,
            continue_latest=continue_latest,
            agent_path=origin,
            recursion_limit=self._recursion_limit,
            task="(interactive chat)",
        )

    async def _open_tinker(
        self,
        store: SessionStore,
        card_id: str,
        *,
        origin: str,
        resume_id: str | None = None,
        continue_latest: bool = False,
    ) -> tuple[ConversationService, bool]:
        from yuyutsava.todoboard.exchange import get_default_exchange

        # Validate the card up-front so a stale/bogus id fails the handshake
        # (typed TodoNotFoundError) instead of the first turn.
        card = await get_default_exchange().get_card(card_id)

        # Session rows are keyed by their thread_id: resolve which of the
        # card's chats to open. Legacy single-chat cards used the bare
        # ``todo:<card_id>`` pin; new chats mint ``todo:<card_id>:<ULID>`` —
        # both are members of the same family.
        session = None
        if resume_id:
            if not is_tinker_thread_of(card_id, resume_id):
                raise ValueError(
                    f"resume id {resume_id!r} does not belong to card {card_id!r}"
                )
            session = await store.get(resume_id)  # SessionNotFound propagates
        elif continue_latest:
            rows = await store.list_thread_family(
                tinker_thread_base(card_id), limit=1
            )
            session = rows[0] if rows else None

        if session is not None:
            await store.update_status(session.id, "running")
            resuming = True
        else:
            session = await store.create(
                workspace=self._workspace,
                task=f"(tinker: {card.title})",
                thread_id=mint_tinker_thread_id(card_id),
                origin=origin,
            )
            resuming = False

        key = self._bundle_key("tinker", card_id)
        bundle = self._bundles.get(key)
        svc = ConversationService(
            store=store,
            session=session,
            workspace=self._workspace,
            bundle=bundle,                  # reuse if already built
            bundle_factory=(
                None if bundle
                else functools.partial(self._ensure_bundle, "tinker", card_id)
            ),
            agent_path=origin,
            recursion_limit=self._recursion_limit,
        )
        return svc, resuming

    # ------------------------------------------------------------------ #
    # Daemon-owned turns                                                   #
    # ------------------------------------------------------------------ #

    @property
    def turns(self) -> TurnRegistry:
        """The daemon's live conversation runs (see ``daemon/turn_registry``).

        Transports use it to *attach* to a thread rather than to own its turn:
        ``registry.attach(thread_id, since_seq)`` for replay + live frames,
        ``registry.start(...)`` to run one (which also serializes turns per
        thread), and ``registry.cancel(thread_id)`` for the Stop button.
        """
        return self._turns

    def start_turn(self, thread_id: str, **kwargs) -> TurnRun | None:
        """Launch a turn on the daemon loop; ``None`` when one already runs.

        Same mutual-exclusion guarantee the old ``try_begin_turn`` gate gave —
        but the caller now holds a real handle instead of a bare thread id, so
        the turn survives the connection that asked for it.
        """
        return self._turns.start(thread_id=thread_id, **kwargs)

    @property
    def bundle_ready(self) -> bool:
        return "master" in self._bundles

    async def aclose(self) -> None:
        await self._turns.aclose()
        bundles, self._bundles = self._bundles, {}
        for key, bundle in bundles.items():
            try:
                await bundle.aclose()
            except Exception:  # noqa: BLE001
                logger.debug("conversation bundle %s aclose failed", key, exc_info=True)
