"""Storage backend selection: SQLite (zero-config default) vs Postgres.

One switch — ``YUYUTSAVA_STORAGE_BACKEND`` — decides where LangGraph
checkpoints and the new context/memory tables (artifacts, thread_summaries,
memories) live. The events store (``state.db``: proposals / decisions /
rules / prefs) stays on SQLite regardless; it is small, working, and
``BaseSqliteStore``-backed.

Postgres mode expects the pgvector-enabled container from the unified
``docker-compose.yml`` (Postgres always runs; Langfuse is opt-in)::

    docker compose up -d
    export YUYUTSAVA_STORAGE_BACKEND=postgres

When Postgres is unreachable at boot the daemon falls back to SQLite and
says so loudly — unless ``YUYUTSAVA_STORAGE_REQUIRE=1``, in which case it
refuses to start (fallback checkpoints written to SQLite are invisible to
Postgres, so silent divergence is worse than a failed boot for users who
opted in to durability).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_PG_DSN = "postgresql://yuyutsava:yuyutsava@127.0.0.1:5433/yuyutsava"


@dataclass(frozen=True)
class StorageSettings:
    """Backend switch + Postgres connection knobs."""

    backend: str = "sqlite"  # "sqlite" | "postgres"
    pg_dsn: str = DEFAULT_PG_DSN
    pool_min: int = 1
    pool_max: int = 10
    require: bool = False  # fail boot instead of falling back to SQLite

    @classmethod
    def from_env(cls) -> StorageSettings:
        backend = os.environ.get("YUYUTSAVA_STORAGE_BACKEND", "sqlite").strip().lower()
        if backend not in ("sqlite", "postgres"):
            raise RuntimeError(
                f"Unknown YUYUTSAVA_STORAGE_BACKEND={backend!r}; "
                "use 'sqlite' or 'postgres'."
            )
        dsn = os.environ.get("YUYUTSAVA_PG_DSN", "").strip() or DEFAULT_PG_DSN

        def _int(name: str, default: int) -> int:
            raw = os.environ.get(name, "").strip()
            try:
                return int(raw) if raw else default
            except ValueError:
                return default

        require_raw = os.environ.get("YUYUTSAVA_STORAGE_REQUIRE", "").strip().lower()
        return cls(
            backend=backend,
            pg_dsn=dsn,
            pool_min=_int("YUYUTSAVA_PG_POOL_MIN", 1),
            pool_max=_int("YUYUTSAVA_PG_POOL_MAX", 10),
            require=require_raw in ("1", "true", "yes"),
        )

    def is_postgres(self) -> bool:
        return self.backend == "postgres"
