"""Reading a test WebSocket under a deadline.

A converse regression is usually a hang — a turn parked on an answer nobody
delivers (``R-BUG-08``) — so a test that simply blocks on ``receive_json`` stops
the suite instead of failing it. :func:`collect_until` reads on a daemon thread
and fails when the deadline passes.
"""

from __future__ import annotations

import threading
from typing import Any


def collect_until(ws: Any, until: str, timeout: float = 15.0) -> list[dict]:
    """Frames up to and including the first of type *until*; fails after *timeout*."""
    frames: list[dict] = []
    arrived = threading.Event()

    def _read() -> None:
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["type"] == until:
                arrived.set()
                return

    threading.Thread(target=_read, daemon=True).start()
    assert arrived.wait(timeout), (
        f"no {until!r} frame within {timeout}s; saw {[f['type'] for f in frames]}"
    )
    return frames
