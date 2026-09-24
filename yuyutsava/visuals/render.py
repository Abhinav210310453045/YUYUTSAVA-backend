"""Single dispatch entry point used by every delivery adapter.

``render(kind, spec)`` is the one function tools / REST / SSE all call, so the
delivery layer never needs to know which renderer module handles a kind.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any, Callable

from .types import RenderResult, VisualError

#: Each renderable kind, and the module and function that draw it. Named rather
#: than imported so ``import yuyutsava.visuals.tools`` (done at engine/subagent
#: build time) does not pull matplotlib: the heavy deps load only when a visual
#: is actually rendered, and a missing ``visuals`` extra surfaces as a clean
#: error at call time instead of breaking agent startup.
#:
#: A new kind is one row here; the list the error message offers follows.
RENDERERS: dict[str, tuple[str, str]] = {
    "chart": (".charts", "render_chart"),
    "diagram": (".diagrams", "render_diagram"),
    "table": (".tables", "render_table"),
    "code": (".code_image", "render_code"),
    "math": (".latex", "render_math"),
    "timeline": (".timeline", "render_timeline"),
}

#: The renderable kinds, in the order they are offered.
KINDS = tuple(RENDERERS)


def _load(kind: str) -> Callable[[dict[str, Any]], RenderResult]:
    """Import the renderer for *kind*. Raises ``ImportError`` without the extra."""
    module, name = RENDERERS[kind]
    return getattr(import_module(module, __package__), name)


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
