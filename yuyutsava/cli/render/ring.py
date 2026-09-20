"""The payload ring both renderers keep behind `/ring`, `/last` and `/expand`.

A tool call's preview is truncated, so the untruncated body has to live
somewhere the user can ask for later. That is this: a bounded deque of the last
``YUYUTSAVA_REPL_RING`` events, each body capped at
``YUYUTSAVA_REPL_RING_ENTRY_KB`` so one enormous tool result cannot grow the
REPL's memory without limit.

Its own object because both renderers need it and neither owns it: sharing it
through a base class would also share the fifteen formatting helpers each
renderer defines for itself, which is the coupling `R-BUG-03` came out of.
"""

from __future__ import annotations

import sys
from collections import deque
from dataclasses import dataclass

from yuyutsava.ansi import CYAN, DIM, RESET
from yuyutsava.config.env import env_int


@dataclass
class RingEntry:
    """One captured event with its full untruncated payload."""

    index: int
    kind: str  # "tool_call" | "tool_result"
    name: str  # tool name
    full: str  # pretty-printed full body (already capped)
    truncated: bool  # True if the body was clipped to the per-entry cap


class PayloadRing:
    """The last N tool calls and results, addressable by index.

    Prints to stderr in both renderers, including the rich one: the ring is a
    debugging surface, and stderr keeps it out of a piped transcript of the
    conversation itself.
    """

    def __init__(self, *, size: int | None = None, entry_cap: int | None = None) -> None:
        if size is None:
            size = max(1, env_int("YUYUTSAVA_REPL_RING", default=50))
        if entry_cap is None:
            entry_cap = max(1024, env_int("YUYUTSAVA_REPL_RING_ENTRY_KB", default=100) * 1024)
        #: Per-entry body cap. Renderers read it to word their own "truncated"
        #: notes, which have to agree with what was actually stored.
        self.entry_cap = entry_cap
        self._entries: deque[RingEntry] = deque(maxlen=size)
        self._next_index = 1

    def __len__(self) -> int:
        return len(self._entries)

    def push(self, kind: str, name: str, full: str) -> int:
        """Store one payload and return the index the user can `/expand`."""
        truncated = False
        if len(full) > self.entry_cap:
            full = full[: self.entry_cap] + f"\n…[truncated to {self.entry_cap // 1024}KB]"
            truncated = True
        idx = self._next_index
        self._next_index += 1
        self._entries.append(
            RingEntry(index=idx, kind=kind, name=name, full=full, truncated=truncated)
        )
        return idx

    def find(self, idx: int) -> RingEntry | None:
        for e in self._entries:
            if e.index == idx:
                return e
        return None

    def print_all(self) -> None:
        if not self._entries:
            print(f"{DIM}(ring empty){RESET}", file=sys.stderr)
            return
        print(f"{CYAN}ring (most recent last):{RESET}", file=sys.stderr)
        for e in self._entries:
            summary = e.full.replace("\n", " ⏎ ")
            if len(summary) > 100:
                summary = summary[:100] + "…"
            tag = "·" if e.kind == "tool_call" else "↳"
            print(f"  {DIM}[#{e.index}] {tag} {e.name}: {summary}{RESET}", file=sys.stderr)

    def print_one(self, idx: int) -> None:
        e = self.find(idx)
        if e is None:
            print(f"{DIM}no ring entry #{idx} (try /ring){RESET}", file=sys.stderr)
            return
        header = f"[#{e.index}] {e.kind} {e.name}"
        if e.truncated:
            header += " (truncated)"
        print(f"{CYAN}{header}{RESET}", file=sys.stderr)
        print(e.full, file=sys.stderr)

    def print_last(self, k: int = 1) -> None:
        if not self._entries:
            print(f"{DIM}(ring empty){RESET}", file=sys.stderr)
            return
        k = max(1, k)
        for e in list(self._entries)[-k:]:
            self.print_one(e.index)


__all__ = ["PayloadRing", "RingEntry"]
