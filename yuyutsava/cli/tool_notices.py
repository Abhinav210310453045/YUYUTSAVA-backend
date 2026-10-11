"""What the chat says about MCP servers that start after it opened.

The chat does not wait for its MCP servers (``npx -y`` servers can take seconds
each). This tells the user, in plain words and once each:

* at the start, which tools are still starting;
* when one comes up — its tools are part of the chat from the next message;
* when one gives up — its tools are off for this chat, and why.

In the split view the notices go to the side panel, never over the transcript.
In the classic REPL they are printed between turns: before the next prompt, or
as the next message starts.
"""

from __future__ import annotations

import sys
from typing import Any

from yuyutsava.ansi import DIM, RESET
from yuyutsava.mcp.status import CONNECTED, FAILED, STARTING

#: How long a notice stays in the side panel.
_NOTICE_SECONDS = 60.0


class ToolNotices:
    """Says when a starting MCP server comes up or gives up, once per change."""

    def __init__(self, manager: Any, dashboard: Any | None) -> None:
        self._manager = manager
        self._dashboard = dashboard
        self._pending: list[str] = []
        self._said = {s.name: s.state for s in manager.status()}
        self._unsubscribe = manager.subscribe(self._changed)
        starting = [name for name, state in self._said.items() if state == STARTING]
        if starting:
            self._say(
                f"starting your tools: {', '.join(starting)} — each joins this chat when it is ready"
            )

    def flush(self) -> None:
        """Print what is waiting to be said (the classic REPL, between turns)."""
        for text in self._pending:
            print(f"{DIM}{text}{RESET}", file=sys.stderr)
        self._pending.clear()

    def stop(self) -> None:
        self._unsubscribe()

    def _changed(self) -> None:
        for status in self._manager.status():
            if self._said.get(status.name) == status.state:
                continue
            self._said[status.name] = status.state
            if status.state == CONNECTED:
                self._say(f"✓ {status.name} is ready — its tools are part of this chat now")
            elif status.state == FAILED:
                self._say(
                    f"✗ {status.name} didn't start ({status.error}) — its tools are off for this chat"
                )

    def _say(self, text: str) -> None:
        if self._dashboard is not None:
            self._dashboard.notice(text, ttl=_NOTICE_SECONDS)
        else:
            self._pending.append(text)


__all__ = ["ToolNotices"]
