"""Small text helpers shared across the codebase.

Provider- and domain-agnostic on purpose, and importing only the standard
library, so every layer may use them. The message helpers
(:func:`sanitize_message_metadata`, :func:`flatten_content`) read LangChain
message content by duck typing rather than importing LangChain.
"""

from __future__ import annotations

from typing import Any, Iterable

# Scalar ``response_metadata`` keys that providers send once per *logical*
# response but that some OpenAI-compatible backends (e.g. OpenRouter) echo in
# every streamed chunk. LangChain's chunk merge concatenates equal string
# values, so these accrete ("stop" -> "stopstop" -> ...). Tunable: callers may
# pass their own ``keys`` set.
DEFAULT_METADATA_SCALAR_KEYS: frozenset[str] = frozenset(
    {
        "finish_reason",
        "model_name",
        "system_fingerprint",
        "service_tier",
        "id",
        "model_provider",
    }
)


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


def flatten_content(content: object) -> str:
    """Flatten LangChain message content (str or block list) to text.

    List content may mix plain-string blocks with {"type":"text"} dicts —
    merge_content appends str chunks to a list-content message as raw strings,
    so both shapes carry prose and must be kept. Dropping the str blocks is
    what truncated the ``final`` event to the first chunk of a reply.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return ""


def humanize_duration(seconds: float) -> str:
    """A span of time as a short label: ``42s``, ``3m05s``, ``2h07m``.

    For *how long* something has run — a background task's elapsed time. Past
    an hour the seconds are dropped, because at that scale they are noise.

    Two neighbours deliberately do not use it. The sessions table answers
    *when* ("2h ago", with days), where a single coarse unit reads better in a
    column; ``daemon status`` prints a fixed ``HhMMmSSs`` readout. Different
    questions, so different shapes — this is the one for elapsed time.
    """
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60):02d}m"
