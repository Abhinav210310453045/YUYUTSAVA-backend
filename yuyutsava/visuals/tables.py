"""Styled-table renderer.

Renders tabular data to a clean dark-themed PNG using matplotlib's native table
(no browser/selenium needed, unlike a pandas ``.style`` HTML export — so it runs
headless in the daemon and in sub-agents).

Spec shape::

    {
      "columns": [str, ...],
      "rows": [[cell, ...], ...],   # each row same length as columns
      "title": str | None,
      "highlight": {"row": int, "col": int}  # optional single-cell emphasis
    }
"""

from __future__ import annotations

from typing import Any

from . import theme
from ._mpl import figure_to_png, plt
from .types import RenderResult, VisualError


def render_table(spec: dict[str, Any]) -> RenderResult:
    columns = spec.get("columns")
    rows = spec.get("rows")
    if not columns or rows is None:
        raise VisualError("table requires 'columns' and 'rows'")
    for r in rows:
        if len(r) != len(columns):
            raise VisualError(
                f"row {r!r} has {len(r)} cells but there are {len(columns)} columns"
            )

    n_rows = len(rows) + 1
    fig, ax = plt.subplots(figsize=(min(2 + 1.6 * len(columns), 16), 0.5 + 0.42 * n_rows))
    fig.patch.set_facecolor(theme.BG_DEEP)
    ax.axis("off")

    cell_text = [[str(c) for c in r] for r in rows]
    table = ax.table(
        cellText=cell_text or [[""] * len(columns)],
        colLabels=[str(c) for c in columns],
        cellLoc="center",
        loc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 1.4)

    highlight = spec.get("highlight") or {}
    hl = (highlight.get("row"), highlight.get("col")) if highlight else (None, None)
    for (row, col), cell in table.get_celld().items():
        cell.set_edgecolor(theme.GRID)
        if row == 0:  # header
            cell.set_facecolor(theme.BG_CARD)
            cell.set_text_props(color=theme.ACCENTS[0], fontweight="bold")
        else:
            cell.set_facecolor(theme.BG_DEEP if row % 2 else theme.BG_CARD)
            cell.set_text_props(color=theme.TEXT)
        # highlight is expressed in data coords (0-based); table row 0 is header
        if hl[0] is not None and row == hl[0] + 1 and col == hl[1]:
            cell.set_facecolor("#1c3a2a")
            cell.set_text_props(color=theme.ACCENTS[0], fontweight="bold")

    if spec.get("title"):
        ax.set_title(str(spec["title"]), color=theme.TEXT, fontsize=13, pad=14)

    data, w, h = figure_to_png(fig)
    return RenderResult(
        image_bytes=data,
        mime="image/png",
        kind="table",
        title=spec.get("title"),
        width=w,
        height=h,
        source=str(spec),
    )
