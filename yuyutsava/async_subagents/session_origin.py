"""Origin-aware routing for HITL prompts.

When a task is submitted (web, CLI submit, voice, ...) we record which channel
originated it under that ``session_id``. When the orchestrator (or a background
subagent watcher) later emits an ``AskPrompt`` tagged with the same
``session_id``, ``ChannelRouter`` consults this map and tries the origin
channel first — so a question coming back from a CLI-issued task surfaces in
the CLI, even if the Electron renderer is also live.

In-memory only (v1). Cleared on daemon shutdown.
"""

from __future__ import annotations

import threading


class SessionOriginMap:
    """Thread-safe ``session_id -> origin_channel_name`` map.

    Used as a routing hint by ``ChannelRouter``. ``None`` means "no preference"
    and the router falls back to its existing ``primary_name``-first ordering.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._map: dict[str, str] = {}

    def set(self, session_id: str, channel_name: str) -> None:
        if not session_id or not channel_name:
            return
        with self._lock:
            self._map[session_id] = channel_name

    def get(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        with self._lock:
            return self._map.get(session_id)

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._map.pop(session_id, None)

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self._map)
