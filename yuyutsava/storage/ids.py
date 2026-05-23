"""Thread-id minting and parsing — single source for the format the sweepers parse back.

The LangGraph ``checkpoints`` table has no ``created_ts`` column, so the
timestamp is encoded into the thread_id itself: ``<role>-<unix_ts>-<uuid4>``.
The TTL sweeper splits on ``-`` and parses index ``[1]`` as a unix timestamp,
which means the format is load-bearing for the sweeper's correctness.

Anything that mints a thread_id must use :func:`mint_thread_id`. Anything
that needs to recover the timestamp must use :func:`parse_thread_id_ts`.
Don't reimplement.
"""

from __future__ import annotations

import time
import uuid


THREAD_ID_TEMPLATE = "{role}-{ts}-{uuid}"
"""Canonical format string. ``role`` is a short tag (``cli``, ``orch``, …);
``ts`` is unix-seconds; ``uuid`` is a uuid4 hex with dashes."""


def mint_thread_id(role: str = "cli") -> str:
    """Mint a thread_id whose timestamp the sweeper can parse.

    ``role`` is a short tag that helps when skimming the DB by hand; the
    sweeper itself doesn't look at it. Both the CLI session store and the
    daemon orchestrator loop call into this.
    """
    return f"{role}-{int(time.time())}-{uuid.uuid4()}"


def parse_thread_id_ts(thread_id_value: str) -> float | None:
    """Recover the unix timestamp from a thread_id minted by :func:`mint_thread_id`.

    Returns ``None`` for thread_ids that don't fit the expected shape — the
    sweeper leaves those alone rather than guessing at a timestamp. Treat the
    None case as "not ours, ignore."
    """
    parts = thread_id_value.split("-")
    # role - <ts> - <uuid-with-4-dashes>
    if len(parts) < 3:
        return None
    try:
        return float(parts[1])
    except (ValueError, IndexError):
        return None
