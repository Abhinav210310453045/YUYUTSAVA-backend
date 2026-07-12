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
    card's identity into its system prompt; its single thread is pinned to
    ``todo:<card_id>`` so reopening the card resumes the conversation.

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
from yuyutsava.storage.sessions import SessionStore, get_default_session_store
from yuyutsava.storage.sessions.store import SessionNotFound

logger = logging.getLogger("yuyutsava.daemon.conversation_manager")


def _bash_timeout_sec() -> int:
    try:
        return max(1, int(os.environ.get("YUYUTSAVA_BASH_TIMEOUT", "300")))
    except ValueError:
        return 300


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
    ) -> None:
        self._workspace = workspace
        self._checkpointer = checkpointer
        self._settings = settings
        self._search_config = search_config
        self._recursion_limit = recursion_limit
        self._store = store
        self.voice_store = voice_store  # storage.voice_store.VoiceMessageStore | None
        self._usage_store = usage_store  # daemon.usage.UsageStore | None (tinker accounting)
        # Bundle cache: "master" | "tinker:<card_id>" → AgentBundle. The
        # factory map below is per AGENT; the key carries the card because a
        # tinker bundle is card-specific (workspace + prompt).
        self._bundles: dict[str, AgentBundle] = {}
        self._build_locks: dict[str, asyncio.Lock] = {}

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
        )

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

        ``agent="tinker"`` requires ``card_id`` and pins the thread to
        ``todo:<card_id>`` — ``resume_id``/``continue_latest`` are ignored
        because the card IS the thread: reopening a card always resumes it.
        """
        store = self._store or get_default_session_store()

        if agent == "tinker":
            if not card_id:
                raise ValueError("tinker conversations require a card id (?card=)")
            return await self._open_tinker(store, card_id, origin=origin)

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
        self, store: SessionStore, card_id: str, *, origin: str
    ) -> tuple[ConversationService, bool]:
        from yuyutsava.todoboard.exchange import get_default_exchange

        # Validate the card up-front so a stale/bogus id fails the handshake
        # (typed TodoNotFoundError) instead of the first turn.
        card = await get_default_exchange().get_card(card_id)

        thread_id = f"todo:{card_id}"
        try:
            # Session rows are keyed by their thread_id, so the pin doubles as
            # the resume key: same card → same session → same checkpoint.
            session = await store.get(thread_id)
            await store.update_status(thread_id, "running")
            resuming = True
        except SessionNotFound:
            session = await store.create(
                workspace=self._workspace,
                task=f"(tinker: {card.title})",
                thread_id=thread_id,
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

    @property
    def bundle_ready(self) -> bool:
        return "master" in self._bundles

    async def aclose(self) -> None:
        bundles, self._bundles = self._bundles, {}
        for key, bundle in bundles.items():
            try:
                await bundle.aclose()
            except Exception:  # noqa: BLE001
                logger.debug("conversation bundle %s aclose failed", key, exc_info=True)
