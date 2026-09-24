"""Standing up the host that background subagent runs execute on.

Three composition roots need one — the CLI's chat stack, the tinker stack and
the daemon — and they had a copy each. The copies drifted on the one flag that
matters most, and the drift was invisible: two opted background runs out of
publishing the context panel's window and the third did not, so a background
subagent launched from `yuyutsava chat` redrew the panel with its own
conversation's shape (fixed in `e516057`).

Two things here are easy to get wrong and are therefore not left to callers:

* **The compaction model is built here, on the host's own loop.** Gemini SDK
  clients bind to the first event loop that uses them, and host graphs run on
  the uvicorn loop in the host thread — so a compaction model belonging to the
  caller's loop must never serve host runs. See
  :mod:`yuyutsava.llm.quirks.loop_affinity`.
* **Background runs report spend, never the window.** They have host-minted
  thread ids and their own message list, and the dashboard subscribes to every
  thread, so a window snapshot from one becomes the panel.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel

from yuyutsava.async_subagents.host import AsyncSubagentHost, resolve_allow_blocking
from yuyutsava.async_subagents.host_lock import acquire_or_attach_host
from yuyutsava.async_subagents.mirror import AsyncTaskMirror
from yuyutsava.config.env import env_flag
from yuyutsava.context.tools import make_context_tools
from yuyutsava.core.engine import context_middleware
from yuyutsava.llm.factory import chat_model
from yuyutsava.llm.settings import LlmSettings, llm_settings_from_env


@dataclass(frozen=True)
class BackgroundStores:
    """What a background host's context controllers read and write.

    All optional: absent stores mean the controller is not wired, and
    ``context_middleware`` returns an empty list rather than half a pipeline.
    """

    artifact_store: Any | None = None
    context_settings: Any | None = None
    summary_store: Any | None = None
    memory_store: Any | None = None


def build_async_host(
    subagents: Any,
    *,
    model: BaseChatModel,
    checkpointer: Any,
    allow_blocking: bool,
    stores: BackgroundStores,
) -> AsyncSubagentHost:
    """One background host, with the same context controllers the masters get.

    Background runs are the longest-lived agents in the system and were the
    only ones without tool-result offload, compaction and ``ctx_*`` readback.
    """
    host_compaction_model = chat_model(llm_settings_from_env("compaction"), temperature=0.0)

    return AsyncSubagentHost.from_subagents(
        subagents,
        model=model,
        checkpointer=checkpointer,
        allow_blocking=allow_blocking,
        middleware_factory=lambda sa: context_middleware(
            model=model,
            artifact_store=stores.artifact_store,
            context_settings=stores.context_settings,
            summary_store=stores.summary_store,
            memory_store=stores.memory_store,
            # Background thread ids are host-minted; transcripts serve
            # interactive resume, so there is nothing to record against.
            transcript_store=None,
            compaction_model=host_compaction_model,
            role=f"{sa.name}-bg",
            meter_window=False,
        ),
        extra_tools_factory=(
            (lambda: make_context_tools(stores.artifact_store))
            if stores.artifact_store is not None
            else None
        ),
    )


__all__ = ["BackgroundStores", "build_async_host"]


def async_subagents_enabled() -> bool:
    """Whether background subagents are on (``YUYUTSAVA_ASYNC_SUBAGENTS``)."""
    return env_flag("YUYUTSAVA_ASYNC_SUBAGENTS", true=("1", "true", "yes"))


@dataclass(frozen=True)
class Background:
    """The background-subagent host a stack uses; all ``None`` when off."""

    subagents: list[Any] | None = None
    host_url: str | None = None
    #: ``None`` when attached to a host another process owns.
    host: AsyncSubagentHost | None = None
    attachment: Any | None = None
    mirror: AsyncTaskMirror | None = None


async def start_background(
    settings: LlmSettings,
    checkpointer: Any,
    subagents: list[Any],
    stores: BackgroundStores,
) -> Background:
    """Own or attach to the shared background-subagent host.

    First-come-wins: if a daemon (or another chat) already runs the LangGraph
    dev server, attach to its URL instead of starting a second one — inside the
    daemon that is always the case. Permissive (warn-only) about blocking I/O
    by default; ``YUYUTSAVA_ALLOW_BLOCKING`` still overrides.
    """
    model = chat_model(settings)
    allow_blocking = resolve_allow_blocking(default=True)

    def _build_host() -> AsyncSubagentHost:
        return build_async_host(
            subagents,
            model=model,
            checkpointer=checkpointer,
            allow_blocking=allow_blocking,
            stores=stores,
        )

    attachment = await asyncio.to_thread(acquire_or_attach_host, factory=_build_host)
    return Background(
        subagents=subagents,
        host_url=attachment.url,
        host=attachment.host,
        attachment=attachment,
        mirror=AsyncTaskMirror(),
    )
