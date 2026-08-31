"""SQLite-backed embedding store for the DeepFace MCP server.

Schema is intentionally tiny: one row per (identity, embedding sample). The
embedding is stored as a raw little-endian float32 BLOB so we can read it back
into a numpy array with zero parsing overhead. Cosine similarity is computed
in-process against the full table — fine for ~hundreds of identities.

The DB lives at ``$YUYUTSAVA_HOME/deepface/db.sqlite``.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("yuyutsava.mcp_servers.deepface.store")


@dataclass(frozen=True)
class Match:
    identity: str
    similarity: float  # cosine, in [-1, 1]; higher = closer


_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    identity    TEXT    NOT NULL,
    dim         INTEGER NOT NULL,
    vec         BLOB    NOT NULL,
    model       TEXT    NOT NULL,
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_embeddings_identity ON embeddings(identity);
"""


class EmbeddingStore:
    """Thread-safe sqlite store of named face embeddings."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def add(self, identity: str, vec: list[float], model: str) -> int:
        blob = _pack(vec)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO embeddings(identity, dim, vec, model, created_at) VALUES (?,?,?,?,?)",
                (identity, len(vec), blob, model, time.time()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def delete_identity(self, identity: str) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM embeddings WHERE identity = ?", (identity,))
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_identities(self) -> list[tuple[str, int]]:
        """Return ``[(identity, sample_count), ...]`` sorted by identity name."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT identity, COUNT(*) FROM embeddings GROUP BY identity ORDER BY identity"
            ).fetchall()
        return [(r[0], int(r[1])) for r in rows]

    def best_match(self, query: list[float], *, model: str | None = None) -> Match | None:
        """Cosine-similarity search across stored embeddings.

        When *model* is given, only rows produced by the same model are
        considered (embeddings from different models are not comparable).
        """
        with self._lock:
            if model is None:
                rows = self._conn.execute("SELECT identity, dim, vec FROM embeddings").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT identity, dim, vec FROM embeddings WHERE model = ?", (model,)
                ).fetchall()
        if not rows:
            return None

        q = list(query)
        q_norm = _norm(q)
        if q_norm == 0.0:
            return None

        best: Match | None = None
        for identity, dim, blob in rows:
            if dim != len(q):
                continue
            v = _unpack(blob, dim)
            sim = _cosine(q, v, q_norm)
            if best is None or sim > best.similarity:
                best = Match(identity=str(identity), similarity=float(sim))
        return best


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"<{dim}f", blob))


def _norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def _cosine(q: list[float], v: list[float], q_norm: float) -> float:
    v_norm = _norm(v)
    if v_norm == 0.0:
        return 0.0
    dot = sum(a * b for a, b in zip(q, v))
    return dot / (q_norm * v_norm)
