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

import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

logger = logging.getLogger("yuyutsava.cli.remote_attach")


@dataclass
class StreamFrame:
    """One SSE frame off the daemon stream."""
    event: str               # "event" | "proposal" | "ask" | "hello" | ...
    data: dict[str, Any]


class CliAttachClient:
    """HTTP client + SSE consumer for ``yuyutsava attach``.

    Public API is async because the SSE loop is naturally async.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8765",
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
    """Print the ask + options to stderr, read a reply from stdin."""
    ask_id = frame_data.get("ask_id") or ""
    title = frame_data.get("title") or "Question"
    body = frame_data.get("body") or ""
    options = frame_data.get("options") or []
    agent_path = frame_data.get("agent_path") or ""

    tag = " [background]" if agent_path.endswith("#bg") else ""
    print(f"\n\033[36m▣ {title}{tag}\033[0m  \033[2m(ask={ask_id[:8]})\033[0m", file=sys.stderr)
    if agent_path:
        print(f"  \033[2mfrom: {agent_path}\033[0m", file=sys.stderr)
    if body:
        print(f"  {body}", file=sys.stderr)
    if options:
        print(f"  options: {' / '.join(options)}", file=sys.stderr)
    sys.stderr.flush()
    try:
        line = await asyncio.get_running_loop().run_in_executor(
            None, lambda: input("> ").strip()
        )
    except (EOFError, KeyboardInterrupt):
        return "reject"
    return line or "reject"


def render_event_frame(frame_data: dict) -> None:
    """Pretty-print a non-ask event to stderr (single line)."""
    kind = frame_data.get("kind") or "?"
    data = frame_data.get("data") or {}
    if kind == "log":
        print(f"\033[2m• {data.get('text', '')}\033[0m", file=sys.stderr, flush=True)
    elif kind == "token":
        sys.stdout.write(data.get("text", ""))
        sys.stdout.flush()
    elif kind == "tool_call":
        print(f"\033[33m🔧 {data.get('name', '?')}\033[0m", file=sys.stderr, flush=True)
    elif kind == "tool_result":
        preview = (data.get("preview") or "").splitlines()[0][:120]
        print(f"\033[2m← {data.get('name', '?')}: {preview}\033[0m", file=sys.stderr, flush=True)
    elif kind == "timeline":
        print(f"\033[36m• {data.get('line', '')}\033[0m", file=sys.stderr, flush=True)
    elif kind == "async_task_started":
        print(
            f"\033[36m[bg started]\033[0m {data.get('agent_name', '?')}  "
            f"task={(data.get('task_id') or '')[:8]}",
            file=sys.stderr, flush=True,
        )
    elif kind == "async_task_progress":
        print(
            f"\033[2m[bg progress]\033[0m {data.get('agent_name', '?')}  "
            f"{data.get('kind_hint', '?')}: {data.get('text', '')}",
            file=sys.stderr, flush=True,
        )
    elif kind == "async_task_completed":
        ok = bool(data.get("ok"))
        colour = "\033[32m" if ok else "\033[31m"
        print(
            f"{colour}[bg done {'OK' if ok else 'FAIL'}]\033[0m "
            f"{data.get('agent_name', '?')}  {(data.get('summary') or '')[:100]}",
            file=sys.stderr, flush=True,
        )
