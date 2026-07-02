"""Code-to-image renderer (Pygments ImageFormatter → PNG).

Produces a syntax-highlighted snapshot of a code snippet on the shared dark
theme. Requires Pillow (pulled in by the ``visuals`` extra).

Spec shape::

    {"source": str, "language": str | None, "title": str | None}
"""

from __future__ import annotations

from typing import Any

from . import theme
from .types import RenderResult, VisualError


def render_code(spec: dict[str, Any]) -> RenderResult:
    source = spec.get("source")
    if not source:
        raise VisualError("code image requires 'source'")

    try:
        from pygments import highlight
        from pygments.formatters import ImageFormatter
        from pygments.lexers import get_lexer_by_name, guess_lexer
        from pygments.util import ClassNotFound
    except ImportError as exc:  # pragma: no cover - install guard
        raise VisualError(
            "code rendering needs pygments + pillow (install the 'visuals' extra)"
        ) from exc

    language = spec.get("language")
    try:
        lexer = get_lexer_by_name(language) if language else guess_lexer(source)
    except ClassNotFound:
        from pygments.lexers.special import TextLexer

        lexer = TextLexer()

    formatter = ImageFormatter(
        style=theme.CODE_STYLE,
        line_numbers=True,
        font_size=18,
        line_pad=6,
        image_pad=18,
    )
    try:
        data = highlight(source, lexer, formatter)
    except Exception as exc:  # PIL font/render failure
        raise VisualError(f"could not render code image: {exc}") from exc

    return RenderResult(
        image_bytes=data,
        mime="image/png",
        kind="code",
        title=spec.get("title") or (language or None),
        width=None,
        height=None,
        source=source,
    )
