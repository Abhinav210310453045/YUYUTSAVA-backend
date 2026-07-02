"""Single dispatch entry point used by every delivery adapter.

``render(kind, spec)`` is the one function tools / REST / SSE all call, so the
delivery layer never needs to know which renderer module handles a kind.
"""

from __future__ import annotations

from typing import Any

from .types import KINDS, RenderResult, VisualError


def _load(kind: str):
    """Import the renderer for *kind* lazily.

    Keeps ``import yuyutsava.visuals.tools`` (done at engine/subagent build time)
    from eagerly pulling matplotlib — the heavy deps load only when a visual is
    actually rendered, and a missing ``visuals`` extra surfaces as a clean error
    at call time instead of breaking agent startup.
    """
    if kind == "chart":
        from .charts import render_chart
        return render_chart
    if kind == "diagram":
        from .diagrams import render_diagram
        return render_diagram
    if kind == "table":
        from .tables import render_table
        return render_table
    if kind == "code":
        from .code import render_code
        return render_code
    if kind == "math":
        from .math import render_math
        return render_math
    if kind == "timeline":
        from .timeline import render_timeline
        return render_timeline
    return None


def render(kind: str, spec: dict[str, Any]) -> RenderResult:
    """Render *spec* for the given *kind*. Raises :class:`VisualError` on bad input."""
    kind = str(kind).lower().strip()
    if kind not in KINDS:
        raise VisualError(f"unknown visual kind {kind!r}; expected one of {list(KINDS)}")
    if not isinstance(spec, dict):
        raise VisualError("spec must be a dict/object")
    try:
        fn = _load(kind)
    except ImportError as exc:
        raise VisualError(
            f"visual rendering needs the 'visuals' extra (uv pip install -e '.[visuals]'): {exc}"
        ) from exc
    return fn(spec)
