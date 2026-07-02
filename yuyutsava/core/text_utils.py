"""Small, dependency-light text helpers shared across the codebase.

Kept provider- and domain-agnostic on purpose: anything here should be safe to
import from any layer (no langchain/db imports at module top level except the
optional message helper, which only touches duck-typed attributes).
"""

from __future__ import annotations

from typing import Any, Iterable

# Scalar ``response_metadata`` keys that providers send once per *logical*
# response but that some OpenAI-compatible backends (e.g. OpenRouter) echo in
# every streamed chunk. LangChain's chunk merge concatenates equal string
# values, so these accrete ("stop" -> "stopstop" -> ...). Tunable: callers may
# pass their own ``keys`` set.
DEFAULT_METADATA_SCALAR_KEYS: frozenset[str] = frozenset({
    "finish_reason",
    "model_name",
    "system_fingerprint",
    "service_tier",
    "id",
    "model_provider",
})


def collapse_periodic_repeat(s: str) -> str:
    """Collapse a string that is an exact N-fold repetition of a smaller unit.

    ``"stopstop" -> "stop"``, ``"abcabcabc" -> "abc"``, and an already-clean
    value like ``"default"`` is returned unchanged. Only exact integer
    repetitions collapse — a string that merely *starts* with a repeat is left
    alone. Uses the KMP failure function so it is O(n) and handles 3+ folds.
    """
    n = len(s)
    if n < 2:
        return s
    # Longest-proper-prefix-which-is-also-suffix (KMP) table.
    lps = [0] * n
    k = 0
    for i in range(1, n):
        while k and s[i] != s[k]:
            k = lps[k - 1]
        if s[i] == s[k]:
            k += 1
        lps[i] = k
    period = n - lps[-1]
    if period < n and n % period == 0:
        return s[:period]
    return s


def sanitize_message_metadata(
    message: Any,
    *,
    keys: Iterable[str] = DEFAULT_METADATA_SCALAR_KEYS,
) -> Any:
    """De-accrete repeated scalar strings in a message's ``response_metadata``.

    Mutates *message* in place (the same object LangChain hands us) and returns
    it for convenience. Non-string values, keys outside *keys*, and values that
    are not exact repetitions are left untouched, so clean messages pass through
    byte-for-byte. Safe to call on any object — a missing/!dict
    ``response_metadata`` is a no-op.
    """
    keyset = frozenset(keys)
    meta = getattr(message, "response_metadata", None)
    if isinstance(meta, dict):
        for key in keyset:
            val = meta.get(key)
            if isinstance(val, str) and val:
                collapsed = collapse_periodic_repeat(val)
                if collapsed != val:
                    meta[key] = collapsed
    return message
