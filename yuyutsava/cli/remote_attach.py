"""CLI Mode 2 client — attach to a running daemon and surface HITL prompts in the terminal.

Pairs with ``yuyutsava/daemon/web/routers/cli_attach.py``. Lifecycle:

  1. ``CliAttachClient.attach()``: POST /cli/attach → registers a CliRemoteChannel
     on the daemon's ChannelRouter (idempotent). Returns when registered.
  2. ``CliAttachClient.stream()``: GET /stream (SSE). Yields ``StreamFrame``
     objects for ``event`` / ``proposal`` / ``ask`` frames. ``ask`` frames are
     also looped through the in-process ``ask_handler`` which prompts on
     stdin and POSTs the reply via ``respond_ask()``.
  3. ``CliAttachClient.detach()``: POST /cli/detach on shutdown.

Wire format: each SSE event is ``{"event": "<type>", "data": <json>}`` where
``data`` is ``StreamItem.to_wire_dict()`` from
``yuyutsava/daemon/web/services/stream_service.py``.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from yuyutsava.ansi import CYAN, DIM, RESET, YELLOW
from yuyutsava.cli import bg_labels as bg
from yuyutsava.cli.ask_prompt import AskView, prompt_for_ask

logger = logging.getLogger("yuyutsava.cli.remote_attach")


@dataclass
class StreamFrame:
    """One SSE frame off the daemon stream."""

    event: str  # "event" | "proposal" | "ask" | "hello" | ...
    data: dict[str, Any]


class CliAttachClient:
    """HTTP client + SSE consumer for ``yuyutsava attach``.

    Public API is async because the SSE loop is naturally async.
    """

    def __init__(
        self,
        base_url: str,
        *,
        session_id: str | None = None,
        label: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._session_id = session_id
        self._label = label or "yuyutsava-cli"
        self._client = httpx.AsyncClient(base_url=self._base, timeout=timeout)
        self._attached = False

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def attach(self) -> dict:
        body = {"session_id": self._session_id, "label": self._label}
        r = await self._client.post("/cli/attach", json=body)
        r.raise_for_status()
        self._attached = True
        return r.json()

    async def detach(self) -> None:
        if not self._attached:
            return
        try:
            await self._client.post("/cli/detach", json={"session_id": self._session_id})
        except Exception:
            logger.debug("detach POST failed", exc_info=True)
        finally:
            self._attached = False

    # ------------------------------------------------------------------
    # Stream consumer
    # ------------------------------------------------------------------

    async def stream(self) -> AsyncIterator[StreamFrame]:
        """Yield ``StreamFrame``s from the daemon's /stream SSE endpoint."""
        async with self._client.stream("GET", "/stream", timeout=None) as resp:
            resp.raise_for_status()
            current_event = "message"
            async for raw in resp.aiter_lines():
                if not raw:
                    continue
                if raw.startswith("event:"):
                    current_event = raw.split(":", 1)[1].strip()
                    continue
                if raw.startswith("data:"):
                    payload = raw.split(":", 1)[1].strip()
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    yield StreamFrame(event=current_event, data=data)

    # ------------------------------------------------------------------
    # Replies
    # ------------------------------------------------------------------

    async def respond_ask(self, ask_id: str, response: str) -> bool:
        try:
            r = await self._client.post(
                f"/ask/{ask_id}/respond",
                json={"response": response},
            )
            return r.status_code == 200
        except Exception:
            logger.debug("respond_ask failed", exc_info=True)
            return False


# ---------------------------------------------------------------------------
# Default stdin-based ask handler — used by `yuyutsava attach`.
# ---------------------------------------------------------------------------


async def prompt_user_for_ask(frame_data: dict) -> str:
    """Ask the user about one `ask` frame off the daemon's stream."""
    return await prompt_for_ask(
        AskView(
            title=frame_data.get("title") or "Question",
            body=frame_data.get("body") or "",
            options=frame_data.get("options") or [],
            agent_path=frame_data.get("agent_path") or "",
            ask_id=frame_data.get("ask_id") or "",
        )
    )


def render_event_frame(frame_data: dict) -> None:
    """Pretty-print a non-ask event to stderr (single line)."""
    kind = frame_data.get("kind") or "?"
    data = frame_data.get("data") or {}
    if kind == "log":
        print(f"{DIM}• {data.get('text', '')}{RESET}", file=sys.stderr, flush=True)
    elif kind == "token":
        sys.stdout.write(data.get("text", ""))
        sys.stdout.flush()
    elif kind == "tool_call":
        print(f"{YELLOW}🔧 {data.get('name', '?')}{RESET}", file=sys.stderr, flush=True)
    elif kind == "tool_result":
        preview = (data.get("preview") or "").splitlines()[0][:120]
        print(f"{DIM}← {data.get('name', '?')}: {preview}{RESET}", file=sys.stderr, flush=True)
    elif kind == "timeline":
        print(f"{CYAN}• {data.get('line', '')}{RESET}", file=sys.stderr, flush=True)
    elif kind == "async_task_started":
        print(
            f"{bg.started()} {data.get('agent_name', '?')}  task={(data.get('task_id') or '')[:8]}",
            file=sys.stderr,
            flush=True,
        )
    elif kind == "async_task_progress":
        print(
            f"{bg.progress()} {data.get('agent_name', '?')}  "
            f"{data.get('kind_hint', '?')}: {data.get('text', '')}",
            file=sys.stderr,
            flush=True,
        )
    elif kind == "async_task_completed":
        ok = bool(data.get("ok"))
        print(
            f"{bg.done(ok=ok)} {data.get('agent_name', '?')}  {(data.get('summary') or '')[:100]}",
            file=sys.stderr,
            flush=True,
        )
