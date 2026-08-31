"""Tunable knobs for the sessions subsystem.

Kept separate from ``yuyutsava/core/config.py`` so a future Postgres backend
can add its own fields here without churning the LLM/Docker config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from yuyutsava.core.config import TIMING
from yuyutsava.storage.paths import interrupts_db_path, sessions_db_path


@dataclass(frozen=True)
class SessionsSettings:
    db_path: Path
    backend: str = "sqlite"
    busy_timeout_ms: int = TIMING.sqlite_busy_timeout_ms
    interrupts_db_path: Path | None = None

    @classmethod
    def from_env(cls) -> SessionsSettings:
        return cls(
            db_path=sessions_db_path(),
            backend=os.environ.get("YUYUTSAVA_SESSIONS_BACKEND", "sqlite").strip().lower(),
            busy_timeout_ms=int(
                os.environ.get(
                    "YUYUTSAVA_SESSIONS_BUSY_TIMEOUT_MS",
                    str(TIMING.sqlite_busy_timeout_ms),
                )
            ),
            interrupts_db_path=interrupts_db_path(),
        )
