"""Timeline / Gantt renderer (matplotlib horizontal bars).

Offline by design — for a Mermaid ``gantt`` diagram instead, use the diagram
renderer with ``language="mermaid"``.

Spec shape::

    {
      "title": str | None,
      "items": [
        {"label": str, "start": <date|number>, "end": <date|number>, "status": str?},
        ...
      ],
    }

``start``/``end`` may be ISO date strings ("2026-07-01") or plain numbers; both
ends of every item must use the same convention.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from . import theme
from ._mpl import figure_to_png, plt
from .types import RenderResult, VisualError

_STATUS_COLORS = {
    "done": theme.ACCENTS[0],
    "active": theme.ACCENTS[1],
    "todo": theme.ACCENTS[2],
    "blocked": theme.ACCENTS[3],
}


def render_timeline(spec: dict[str, Any]) -> RenderResult:
    items = spec.get("items")
    if not items:
        raise VisualError("timeline requires a non-empty 'items' list")

    labels = [str(it.get("label", f"item {i+1}")) for i, it in enumerate(items)]
    try:
        starts = [_coerce(it["start"]) for it in items]
        ends = [_coerce(it["end"]) for it in items]
    except KeyError as exc:
        raise VisualError(f"each timeline item needs 'start' and 'end' ({exc})") from exc
    except Exception as exc:
        raise VisualError(f"could not parse timeline dates/values: {exc}") from exc

    is_dates = isinstance(starts[0], (date, datetime))

    fig, ax = plt.subplots(figsize=(10, 0.4 + 0.55 * len(items)))
    theme.apply_matplotlib(fig, [ax])

    for i, (lbl, s, e, it) in enumerate(zip(labels, starts, ends, items)):
        y = len(items) - 1 - i
        width = e - s
        if is_dates:
            width = (e - s).days or 1
        color = _STATUS_COLORS.get(str(it.get("status", "")).lower(), theme.ACCENTS[i % len(theme.ACCENTS)])
        ax.barh(y, width, left=s, height=0.5, color=color, alpha=0.9)

    ax.set_yticks(range(len(items)))
    ax.set_yticklabels(list(reversed(labels)))
    if is_dates:
        fig.autofmt_xdate()
    if spec.get("title"):
        ax.set_title(str(spec["title"]), fontsize=13, pad=12)

    data, w, h = figure_to_png(fig)
    return RenderResult(
        image_bytes=data,
        mime="image/png",
        kind="timeline",
        title=spec.get("title"),
        width=w,
        height=h,
        source=str(spec),
    )


def _coerce(value: Any):
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (date, datetime)):
        return value
    return datetime.fromisoformat(str(value))
