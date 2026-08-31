"""Keyword-fallback tokenizer.

When the embedder is unreachable (or the SQLite twin has no vectors at all),
retrieval degrades to per-word ``LIKE`` matching ranked by hit count then
recency. This tokenizer is shared so the SQLite twin and the Postgres
embed-outage fallback rank identically across every domain.
"""

from __future__ import annotations


def keyword_tokens(query: str) -> list[str]:
    """Words worth matching for keyword fallback: >=3 chars, capped at 8.

    Falls back to a single truncated token so a short query still matches.
    """
    words = [w for w in query.lower().split() if len(w) >= 3][:8]
    return words or [query.lower()[:80]]
