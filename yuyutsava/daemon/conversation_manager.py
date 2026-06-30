"""Daemon-side host for interactive agent conversations (text + voice).

The daemon already owns the orchestrator and the shared ``AsyncSubagentHost``.
This manager lets the web layer run *interactive conversations* — the same
deepagent the CLI drives — inside the daemon so the Electron/mobile UIs can talk
to it over a WebSocket. Each conversation reuses one shared
:class:`~yuyutsava.core.engine.AgentBundle`; per-conversation state is isolated
by ``thread_id`` via the checkpointer, so a single compiled graph serves many
concurrent chats.

Lazy by design: the bundle is built on the first ``open()`` (first time a user
opens chat or voice), not at daemon startup — no cost or startup latency when
the feature is unused. Because :func:`build_agent_stack` routes through
``acquire_or_attach_host`` (first-come-wins), the build *attaches* to the
daemon's already-running host rather than starting a second one, so a
conversation delegates background work to the same orchestrator the CLI uses.
"""

from __future__ import annotations

import asyncio
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

logger = logging.getLogger("yuyutsava.daemon.conversation_manager")


def _bash_timeout_sec() -> int:
    try:
        return max(1, int(os.environ.get("YUYUTSAVA_BASH_TIMEOUT", "300")))
    except ValueError:
        return 300


class ConversationManager:
    """Lazily builds one shared conversational bundle; opens services per chat."""

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
    ) -> None:
        self._workspace = workspace
        self._checkpointer = checkpointer
        self._settings = settings
        self._search_config = search_config
        self._recursion_limit = recursion_limit
        self._store = store
        self.voice_store = voice_store  # storage.voice_store.VoiceMessageStore | None
        self._bundle: AgentBundle | None = None
        self._build_lock = asyncio.Lock()

    async def _ensure_bundle(self) -> AgentBundle:
        if self._bundle is not None:
            return self._bundle
        async with self._build_lock:
            if self._bundle is not None:
                return self._bundle
            # Imported lazily so the (heavy) agent-stack import graph stays off
            # the daemon's hot path until a conversation is actually opened.
            from yuyutsava.cli.agent_stack import build_agent_stack

            settings = self._settings or llm_settings_from_env("orchestrator")
            search = self._search_config or SearchConfig.from_env()
            logger.info("conversation: building shared agent bundle (lazy, first use)")
            self._bundle = await build_agent_stack(
                self._workspace,
                settings,
                bash_timeout_sec=_bash_timeout_sec(),
                execution_mode="local",
                docker_settings=DockerSettings.from_env(),
                local_settings=LocalSettings.from_env(),
                permission_check=True,
                search_config=search,
                checkpointer=self._checkpointer,
            )
            if self._bundle.async_host_url:
                logger.info(
                    "conversation: bundle attached to async host @ %s",
                    self._bundle.async_host_url,
                )
        return self._bundle

    async def open(
        self,
        *,
        origin: str = "cli",
        resume_id: str | None = None,
        continue_latest: bool = False,
    ) -> tuple[ConversationService, bool]:
        """Resolve a session and return a ready-to-run conversation service.

        ``origin`` tags the session ("cli" for the Electron text chat, "voice"
        for the voice agent) so the Sessions UI can split them. ``agent_path``
        is seeded equal to ``origin`` for interrupt attribution.
        """
        store = self._store or get_default_session_store()
        # Defer the heavy bundle build to the first turn (inside the cancellable
        # turn task) so the WS handshake + receive loop stay instant/responsive.
        return await ConversationService.resolve(
            store=store,
            bundle=self._bundle,            # reuse if already built
            bundle_factory=None if self._bundle else self._ensure_bundle,
            workspace=self._workspace,
            origin=origin,
            resume_id=resume_id,
            continue_latest=continue_latest,
            agent_path=origin,
            recursion_limit=self._recursion_limit,
            task="(interactive chat)",
        )

    @property
    def bundle_ready(self) -> bool:
        return self._bundle is not None

    async def aclose(self) -> None:
        if self._bundle is not None:
            try:
                await self._bundle.aclose()
            except Exception:  # noqa: BLE001
                logger.debug("conversation bundle aclose failed", exc_info=True)
            self._bundle = None
