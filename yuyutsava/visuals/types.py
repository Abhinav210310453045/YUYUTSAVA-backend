"""Core value types for the visuals library.

These types are intentionally free of any dependency on LangChain, FastAPI, or
the agent runtime — the renderers return a plain :class:`RenderResult` that any
delivery adapter (LLM tool / REST / SSE) can persist and describe.
"""

from __future__ import annotations

from dataclasses import dataclass

# The set of renderable families. Keep in sync with render.render() dispatch.
KINDS = ("chart", "diagram", "table", "code", "math", "timeline")


@dataclass(frozen=True)
class RenderResult:
    """A rendered visual plus the metadata a store/adapter needs.

    ``image_bytes`` is the encoded image (PNG unless a renderer opts into SVG).
    ``source`` is the spec/script that produced it, kept for reproducibility and
    so a caller can show "how this was made".
    """

    image_bytes: bytes
    mime: str  # "image/png" | "image/svg+xml"
    kind: str  # one of KINDS
    title: str | None = None
    width: int | None = None
    height: int | None = None
    source: str | None = None


class VisualError(Exception):
    """Raised when a spec is invalid or a backend is unavailable.

    Adapters catch this and turn it into a clean error payload rather than a
    stack trace, so a missing diagram backend never crashes a turn.
    """
