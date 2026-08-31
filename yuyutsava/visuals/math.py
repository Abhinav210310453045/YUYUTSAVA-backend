"""Math / LaTeX renderer (matplotlib mathtext → PNG).

Renders equations without a full LaTeX install by using matplotlib's built-in
mathtext engine. Input is a LaTeX math string *without* surrounding ``$`` (they
are added automatically).

Spec shape::

    {"latex": str, "fontsize": int | None, "title": str | None}
"""

from __future__ import annotations

from typing import Any

from . import theme
from ._mpl import figure_to_png, plt
from .types import RenderResult, VisualError


def render_math(spec: dict[str, Any]) -> RenderResult:
    latex = spec.get("latex")
    if not latex:
        raise VisualError("math image requires 'latex'")
    expr = latex.strip()
    if not (expr.startswith("$") and expr.endswith("$")):
        expr = f"${expr}$"

    fontsize = int(spec.get("fontsize", 26))
    fig = plt.figure(figsize=(0.1, 0.1))
    fig.patch.set_facecolor(theme.BG_DEEP)
    try:
        fig.text(0.5, 0.5, expr, ha="center", va="center",
                 color=theme.TEXT, fontsize=fontsize)
        # size the canvas to the text via tight bbox on save (figure_to_png uses it)
        fig.set_size_inches(max(2, len(expr) * 0.12), 1.4)
        data, w, h = figure_to_png(fig)
    except Exception as exc:  # invalid mathtext syntax
        plt.close(fig)
        raise VisualError(f"could not render math (check LaTeX syntax): {exc}") from exc

    return RenderResult(
        image_bytes=data,
        mime="image/png",
        kind="math",
        title=spec.get("title"),
        width=w,
        height=h,
        source=latex,
    )
