"""Pluggable digest builders for offloaded tool results.

When :class:`~yuyutsava.context.offload_middleware.ToolResultOffloadMiddleware`
moves a tool result into the artifact store, it replaces the inline content
with a *digest* — the small, load-bearing summary that stays in context. The
quality of that summary determines whether the model can act without paging the
full body back in, so it is worth shaping per result *kind* — but **without**
hardcoding tool names into the middleware.

This module is that indirection: a tiny registry mapping a *matcher* (tool-name
prefix or predicate) to a :data:`DigestBuilder`. The middleware calls
:func:`build_digest` and never knows about Tavily, Exa, or anything else.

Two builders ship by default:

- :func:`default_digest` — the original head/tail char slice. Used for anything
  unmatched, so existing offloads are byte-for-byte unchanged.
- :func:`structured_list_digest` — a provider-agnostic shaper for JSON results
  shaped like ``{"results": [ {...}, ... ]}`` (or a bare list). It emits a
  compact per-item summary (title / url / short snippet / score) and demotes any
  top-level synthesized ``answer`` to ``provider_answer_unverified`` so the model
  grounds claims in the source URLs rather than a provider's (often wrong) gloss.
  Registered for the ``ws_`` prefix, but it works for *any* future tool whose
  output is a list of records.

Anything in the codebase can register more builders at import time::

    from yuyutsava.context.digests import register_digest
    register_digest("db_", my_row_digest)
"""

from __future__ import annotations

import json
from typing import Any, Callable

# A builder receives the tool name, the new artifact id, and the full content
# string, and returns the digest dict that will be JSON-serialized into the
# ToolMessage the model sees.
DigestBuilder = Callable[[str, str, str], dict]

# Tunable shaping constants (kept here, not scattered as literals in callers).
HEAD_CHARS = 1_500
TAIL_CHARS = 500
SNIPPET_CHARS = 160
MAX_ITEMS = 8

_HINT_FETCH = (
    "Full output stored. Use ctx_fetch_artifact(artifact_id, offset, length) to "
    "page through it or ctx_grep_artifact(artifact_id, pattern) to search it."
)
_HINT_GROUND = (
    " Ground any claim in the listed result URLs — provider_answer_unverified is "
    "an unverified provider summary, not a source."
)

# Heuristic key names for the generic list shaper — order = preference.
_LIST_KEYS = ("results", "items", "data", "hits", "matches", "documents")
_TITLE_KEYS = ("title", "name", "heading", "headline")
_URL_KEYS = ("url", "link", "source", "id")
_SNIPPET_KEYS = ("content", "text", "snippet", "summary", "description", "body")
_SCORE_KEYS = ("score", "relevance", "rank", "similarity")
_ANSWER_KEYS = ("answer", "summary", "response", "overview")


def _base_envelope(tool_name: str, artifact_id: str, content: str) -> dict:
    return {
        "offloaded": True,
        "artifact_id": artifact_id,
        "tool": tool_name,
        "size_chars": len(content),
    }


def default_digest(tool_name: str, artifact_id: str, content: str) -> dict:
    """Head/tail char-slice digest — the universal fallback."""
    digest = _base_envelope(tool_name, artifact_id, content)
    digest["head"] = content[:HEAD_CHARS]
    digest["tail"] = content[-TAIL_CHARS:]
    digest["hint"] = _HINT_FETCH
    return digest


def _first(record: Any, keys: tuple[str, ...]) -> Any:
    if not isinstance(record, dict):
        return None
    for key in keys:
        val = record.get(key)
        if val not in (None, ""):
            return val
    return None


def _find_list(obj: Any) -> list | None:
    """Return the records list from a parsed JSON object, or None."""
    if isinstance(obj, list):
        return obj if obj and isinstance(obj[0], dict) else None
    if isinstance(obj, dict):
        for key in _LIST_KEYS:
            val = obj.get(key)
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val
    return None


def structured_list_digest(tool_name: str, artifact_id: str, content: str) -> dict:
    """Compact summary for list-shaped JSON results; provider-agnostic.

    Falls back to :func:`default_digest` when the content is not JSON or has no
    recognizable list of records, so it is always safe to register broadly.
    """
    try:
        obj = json.loads(content)
    except (ValueError, TypeError):
        return default_digest(tool_name, artifact_id, content)

    records = _find_list(obj)
    if records is None:
        return default_digest(tool_name, artifact_id, content)

    digest = _base_envelope(tool_name, artifact_id, content)
    digest["result_count"] = len(records)
    items: list[dict] = []
    for i, rec in enumerate(records[:MAX_ITEMS]):
        snippet = _first(rec, _SNIPPET_KEYS)
        items.append({
            "i": i,
            "title": _first(rec, _TITLE_KEYS),
            "url": _first(rec, _URL_KEYS),
            "snippet": (str(snippet)[:SNIPPET_CHARS] if snippet else None),
            "score": _first(rec, _SCORE_KEYS),
        })
    digest["results"] = items
    if len(records) > MAX_ITEMS:
        digest["results_truncated"] = True

    answer = _first(obj, _ANSWER_KEYS) if isinstance(obj, dict) else None
    hint = _HINT_FETCH
    if answer:
        digest["provider_answer_unverified"] = answer
        hint += _HINT_GROUND
    digest["hint"] = hint
    return digest


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_Matcher = Callable[[str], bool]
_registry: list[tuple[int, _Matcher, DigestBuilder]] = []


def _compile_matcher(matcher: str | _Matcher) -> tuple[int, _Matcher]:
    """Return ``(specificity, predicate)``. String matchers are prefixes;
    longer prefixes win ties. Callables get a low fixed specificity unless they
    expose their own."""
    if isinstance(matcher, str):
        prefix = matcher
        return len(prefix), (lambda name: name.startswith(prefix))
    return 0, matcher


def register_digest(matcher: str | _Matcher, builder: DigestBuilder) -> None:
    """Register *builder* for tool names matched by *matcher*.

    *matcher* is a tool-name prefix (str) or a ``name -> bool`` predicate. On
    overlap the most specific (longest prefix) registration wins; among equal
    specificity, the most recently registered wins.
    """
    specificity, predicate = _compile_matcher(matcher)
    _registry.append((specificity, predicate, builder))


def build_digest(tool_name: str, artifact_id: str, content: str) -> dict:
    """Pick the best-matching builder for *tool_name* and run it."""
    best: tuple[int, int, DigestBuilder] | None = None
    for order, (specificity, predicate, builder) in enumerate(_registry):
        if predicate(tool_name):
            key = (specificity, order)
            if best is None or key > (best[0], best[1]):
                best = (specificity, order, builder)
    builder = best[2] if best else default_digest
    return builder(tool_name, artifact_id, content)


# Default registrations. Reference-class web-search tools get the structured
# shaper; everything else falls through to default_digest.
register_digest("ws_", structured_list_digest)
