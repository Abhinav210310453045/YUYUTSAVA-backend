"""Data-chart renderer (matplotlib + seaborn).

``render_chart(spec)`` takes a JSON-serializable spec so it behaves identically
whether the spec arrives from an LLM tool call, a REST body, or an SSE request.

Spec shape::

    {
      "chart_type": "bar" | "line" | "pie" | "scatter" | "histogram" | "heatmap",
      "title": str | None,
      "x_label": str | None,
      "y_label": str | None,
      # For bar/line/scatter:
      "labels": [str, ...],                 # x categories / x values
      "series": [{"name": str, "data": [float, ...]}, ...],
      # For pie:
      "values": [float, ...], "labels": [str, ...],
      # For histogram:
      "values": [float, ...], "bins": int,
      # For heatmap:
      "matrix": [[float, ...], ...], "row_labels": [...], "col_labels": [...],
    }
"""

from __future__ import annotations

from typing import Any

from . import theme
from ._mpl import figure_to_png, plt
from .types import RenderResult, VisualError

_CHART_TYPES = {"bar", "barh", "line", "pie", "scatter", "histogram", "heatmap"}


def render_chart(spec: dict[str, Any]) -> RenderResult:
    chart_type = str(spec.get("chart_type", "")).lower().strip()
    if chart_type not in _CHART_TYPES:
        raise VisualError(
            f"unknown chart_type {chart_type!r}; expected one of {sorted(_CHART_TYPES)}"
        )

    fig, ax = plt.subplots(figsize=(8, 5))
    theme.apply_matplotlib(fig, [ax])

    try:
        if chart_type == "bar":
            _bar(ax, spec)
        elif chart_type == "barh":
            _barh(ax, spec)
        elif chart_type == "line":
            _line(ax, spec)
        elif chart_type == "scatter":
            _scatter(ax, spec)
        elif chart_type == "pie":
            _pie(ax, spec)
        elif chart_type == "histogram":
            _histogram(ax, spec)
        elif chart_type == "heatmap":
            _heatmap(fig, ax, spec)
    except VisualError:
        plt.close(fig)
        raise
    except Exception as exc:  # malformed numeric data, mismatched lengths, ...
        plt.close(fig)
        raise VisualError(f"could not render {chart_type} chart: {exc}") from exc

    if spec.get("title"):
        ax.set_title(str(spec["title"]), fontsize=13, pad=12)
    if spec.get("x_label"):
        ax.set_xlabel(str(spec["x_label"]))
    if spec.get("y_label"):
        ax.set_ylabel(str(spec["y_label"]))

    data, w, h = figure_to_png(fig)
    return RenderResult(
        image_bytes=data,
        mime="image/png",
        kind="chart",
        title=spec.get("title"),
        width=w,
        height=h,
        source=str(spec),
    )


def _series(spec: dict[str, Any]) -> list[dict[str, Any]]:
    series = spec.get("series")
    if not series:
        raise VisualError("chart requires a non-empty 'series' list")
    return series


def _bar(ax, spec) -> None:
    labels = [str(x) for x in spec.get("labels", [])]
    series = _series(spec)
    n = len(series)
    import numpy as np  # local: numpy ships with matplotlib

    idx = np.arange(len(labels) or len(series[0]["data"]))
    width = 0.8 / max(n, 1)
    for i, s in enumerate(series):
        color = theme.ACCENTS[i % len(theme.ACCENTS)]
        ax.bar(idx + i * width, s["data"], width, label=s.get("name", f"series {i+1}"), color=color)
    ax.set_xticks(idx + width * (n - 1) / 2)
    if labels:
        # Rotate + right-align when labels are many or long so they don't overlap
        # (the exact pain the user hit — no need to hand-roll a custom chart).
        crowded = len(labels) > 6 or any(len(l) > 8 for l in labels)
        if crowded:
            ax.set_xticklabels(labels, rotation=40, ha="right", fontsize=8)
        else:
            ax.set_xticklabels(labels, rotation=0)
    if n > 1:
        _legend(ax)


def _barh(ax, spec) -> None:
    """Horizontal bars — best when category names are long (they read left-to-right)."""
    labels = [str(x) for x in spec.get("labels", [])]
    series = _series(spec)
    n = len(series)
    import numpy as np

    idx = np.arange(len(labels) or len(series[0]["data"]))
    height = 0.8 / max(n, 1)
    for i, s in enumerate(series):
        color = theme.ACCENTS[i % len(theme.ACCENTS)]
        ax.barh(idx + i * height, s["data"], height, label=s.get("name", f"series {i+1}"), color=color)
    ax.set_yticks(idx + height * (n - 1) / 2)
    if labels:
        ax.set_yticklabels(labels, fontsize=9)
    ax.invert_yaxis()  # first item on top, natural reading order
    if n > 1:
        _legend(ax)


def _line(ax, spec) -> None:
    labels = spec.get("labels")
    for i, s in enumerate(_series(spec)):
        color = theme.ACCENTS[i % len(theme.ACCENTS)]
        xs = labels if labels else range(len(s["data"]))
        ax.plot(xs, s["data"], marker="o", color=color, label=s.get("name", f"series {i+1}"))
    _legend(ax)


def _scatter(ax, spec) -> None:
    for i, s in enumerate(_series(spec)):
        color = theme.ACCENTS[i % len(theme.ACCENTS)]
        xs = s.get("x") or range(len(s["data"]))
        ax.scatter(xs, s["data"], color=color, label=s.get("name", f"series {i+1}"), alpha=0.8)
    _legend(ax)


def _pie(ax, spec) -> None:
    values = spec.get("values")
    labels = [str(x) for x in spec.get("labels", [])]
    if not values:
        raise VisualError("pie chart requires 'values'")
    colors = [theme.ACCENTS[i % len(theme.ACCENTS)] for i in range(len(values))]
    ax.pie(values, labels=labels or None, colors=colors, autopct="%1.1f%%",
           textprops={"color": theme.TEXT})
    ax.set_aspect("equal")
    ax.grid(False)


def _histogram(ax, spec) -> None:
    values = spec.get("values")
    if not values:
        raise VisualError("histogram requires 'values'")
    ax.hist(values, bins=int(spec.get("bins", 20)), color=theme.ACCENTS[0], alpha=0.85)


def _heatmap(fig, ax, spec) -> None:
    matrix = spec.get("matrix")
    if not matrix:
        raise VisualError("heatmap requires a 'matrix'")
    import seaborn as sns

    sns.heatmap(
        matrix,
        ax=ax,
        xticklabels=spec.get("col_labels", "auto"),
        yticklabels=spec.get("row_labels", "auto"),
        cmap="mako",
        annot=bool(spec.get("annotate", False)),
        cbar=True,
    )
    ax.grid(False)


def _legend(ax) -> None:
    leg = ax.legend(facecolor=theme.BG_CARD, edgecolor=theme.GRID, labelcolor=theme.TEXT)
    if leg:
        leg.get_frame().set_alpha(0.9)
