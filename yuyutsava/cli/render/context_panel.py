"""Render a :class:`~yuyutsava.context.meter.ContextSnapshot` for the terminal.

Two views over one snapshot, kept together because they must agree:

* :func:`panel_fragments` — the narrow always-visible column in the chat
  dashboard, as prompt_toolkit formatted text;
* :func:`context_report` — the full ``/context`` breakdown, as a Rich
  renderable printed into the transcript.

Both are pure functions of a snapshot. Nothing here reads the meter, the store,
or the clock, so both can be tested by handing them a dataclass — and the panel
cannot become a second, disagreeing source of numbers.

## Estimated versus measured

Segment sizes are estimates (see the meter's module docstring). Every estimated
figure is prefixed ``≈`` and the panel says so in its footer until the meter has
calibrated against a real call. Last-call figures are the provider's own
numbers and carry no mark. An unpriced model reads ``unpriced``, never
``$0.00`` — "we have no price for this" is not "this was free".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    from rich.console import RenderableType

#: Narrowest useful panel. Below this the numbers wrap and stop being scannable.
MIN_PANEL_WIDTH = 24
MAX_PANEL_WIDTH = 40

#: Terminals narrower than this get the classic transcript instead of a split
#: view: a 34-column panel out of 80 leaves 45 for prose, which turns ordinary
#: markdown into a column of fragments.
MIN_DASHBOARD_COLS = 90

_APPROX = "≈"


def panel_width_for(cols: int) -> int:
    """Panel width for a terminal *cols* wide, clamped to a readable range."""
    return max(MIN_PANEL_WIDTH, min(MAX_PANEL_WIDTH, cols // 4))


def fmt_tokens(n: int | float) -> str:
    """``934`` · ``4.0k`` · ``72.8k`` · ``1.0M``.

    Token counts span four orders of magnitude in one session, and a panel 34
    columns wide cannot afford ``1,048,576``.
    """
    n = int(n or 0)
    if n < 0:
        n = 0
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        k = n / 1_000
        return f"{k:.0f}k" if k >= 100 else f"{k:.1f}k"
    m = n / 1_000_000
    return f"{m:.0f}M" if m >= 100 else f"{m:.1f}M"


def fmt_cost(usd: float, priced: bool) -> str:
    """``$0.0032``, or ``unpriced`` when the model has no price entry."""
    if not priced:
        return "unpriced"
    if usd <= 0:
        return "$0.0000"
    return f"${usd:,.4f}" if usd < 1 else f"${usd:,.2f}"


def fmt_pct(fraction: float) -> str:
    """``7%``, and ``<1%`` rather than ``0%`` for a small non-zero share."""
    pct = max(0.0, min(1.0, fraction)) * 100
    if 0 < pct < 1:
        return "<1%"
    return f"{pct:.0f}%"


def bar(fraction: float, width: int) -> tuple[int, int]:
    """``(filled, empty)`` cells for a *width*-wide meter.

    A non-zero fraction always shows at least one filled cell: rounding a real
    7 % down to an empty bar would say "nothing is used", which is the one
    thing the bar exists to deny.
    """
    width = max(1, width)
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    if fraction > 0 and filled == 0:
        filled = 1
    if fraction < 1.0 and filled == width:
        filled = width - 1
    return filled, width - filled


def _row(label: str, value: str, width: int, *, approx: bool = False) -> str:
    """``label ......... value`` padded to *width*, value right-aligned."""
    val = f"{_APPROX}{value}" if approx else value
    gap = width - len(label) - len(val)
    if gap < 1:
        # Trim the label, never the number — the number is the content.
        keep = max(1, width - len(val) - 1)
        label = label[:keep]
        gap = max(1, width - len(label) - len(val))
    return f"{label}{' ' * gap}{val}"


def panel_fragments(
    snap: Any | None,
    *,
    width: int,
    status: str = "",
    tool: str = "",
) -> list[tuple[str, str]]:
    """The live context column, as ``(style, text)`` fragments with newlines.

    *status* is the spinner-equivalent line (``Running tr_run_python…``) and
    *tool* the tool in flight; both come from the renderer, which owns that
    state — the meter has no idea a tool is running.
    """
    w = max(MIN_PANEL_WIDTH, width)
    out: list[tuple[str, str]] = []

    def line(text: str = "", style: str = "class:ctx.dim") -> None:
        out.append((style, text.ljust(w)[:w]))
        out.append(("", "\n"))

    line(" CONTEXT", "class:ctx.title")

    if snap is None:
        line()
        line(" no model call yet", "class:ctx.dim")
        line(" numbers appear once", "class:ctx.dim")
        line(" the agent runs", "class:ctx.dim")
        return out

    line(f" {snap.model or 'model unknown'}", "class:ctx.accent")

    # Occupancy bar. Built fragment by fragment (three styles on one row) and
    # padded by hand to exactly `w`: every line in this column must be the
    # panel's full width or the transcript beside it shows through.
    inner = w - 2
    bar_w = max(1, w - 7)
    filled, empty = bar(snap.used_fraction, bar_w)
    tail = f" {fmt_pct(snap.used_fraction):>4}"
    out.append(("", " "))
    out.append(("class:ctx.bar.fill", "▓" * filled))
    out.append(("class:ctx.bar.empty", "░" * empty))
    out.append(("class:ctx.value", tail))
    pad = w - 1 - bar_w - len(tail)
    if pad > 0:
        out.append(("", " " * pad))
    out.append(("", "\n"))
    line(
        " " + _row(
            f"{fmt_tokens(snap.used_tokens)} / {fmt_tokens(snap.max_input_tokens)}",
            "", inner,
        ),
        "class:ctx.dim",
    )
    if snap.compact_trigger_tokens and snap.used_tokens >= snap.compact_trigger_tokens:
        line(" compaction imminent", "class:ctx.warn")

    line()
    for _key, label, tokens in snap.segments():
        line(" " + _row(label, fmt_tokens(tokens), inner, approx=True))
    line(" " + _row("free", fmt_tokens(snap.free_tokens), inner, approx=True))

    # Last call — provider numbers, no approximation mark.
    line()
    line(f" LAST CALL  #{snap.call_no}", "class:ctx.title")
    cache = f"  ↺{fmt_pct(snap.cache_hit_fraction)}" if snap.cache_read_tokens else ""
    line(" " + _row("in", fmt_tokens(snap.input_tokens) + cache, inner),
         "class:ctx.value")
    line(" " + _row("out", fmt_tokens(snap.output_tokens), inner), "class:ctx.value")
    line(" " + _row("cost", fmt_cost(snap.est_cost_usd, snap.priced), inner))

    line()
    line(" SESSION", "class:ctx.title")
    line(" " + _row("calls", str(snap.calls), inner))
    line(" " + _row("in", fmt_tokens(snap.session_input_tokens), inner))
    line(" " + _row("out", fmt_tokens(snap.session_output_tokens), inner))
    if snap.session_cache_read_tokens:
        line(" " + _row("cached", fmt_tokens(snap.session_cache_read_tokens), inner))
    line(" " + _row("cost", fmt_cost(snap.session_cost_usd, snap.priced), inner))
    line(" " + _row("compacted", str(snap.compactions), inner))
    line(" " + _row("offloaded", str(snap.offloads), inner))
    if snap.offloaded_digests:
        line(" " + _row("digests in ctx", str(snap.offloaded_digests), inner))

    if status or tool:
        line()
        label = tool or status
        line(f" ● {label}"[:w], "class:ctx.accent")

    line()
    line(
        f" {_APPROX} estimated" + ("" if snap.calibrated else ", uncalibrated"),
        "class:ctx.dim",
    )
    return out


def context_report(snap: Any | None, *, width: int = 72) -> RenderableType:
    """The ``/context`` breakdown: the same numbers, room to explain them."""
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    if snap is None:
        return Text(
            "no model call has completed yet — context figures appear "
            "after the first one",
            style="chrome",
        )

    head = Table.grid(padding=(0, 2))
    head.add_column(style="chrome", justify="right")
    head.add_column()
    head.add_row("model", Text(snap.model or "unknown", style="accent"))
    head.add_row(
        "window",
        f"{snap.max_input_tokens:,} tokens"
        + (f"  ·  compacts at {snap.compact_trigger_tokens:,}"
           if snap.compact_trigger_tokens else ""),
    )
    filled, empty = bar(snap.used_fraction, 40)
    meter = Text()
    meter.append("▓" * filled, style="accent")
    meter.append("░" * empty, style="chrome")
    meter.append(
        f"  {fmt_pct(snap.used_fraction)}  "
        f"({snap.used_tokens:,} used · {snap.free_tokens:,} free)"
    )
    head.add_row("used", meter)

    seg = Table.grid(padding=(0, 2))
    seg.add_column(style="chrome", justify="right", width=16)
    seg.add_column(justify="right", width=12)
    seg.add_column(style="chrome", justify="right", width=6)
    seg.add_column()
    for _key, label, tokens in snap.segments():
        share = tokens / snap.max_input_tokens if snap.max_input_tokens else 0.0
        f2, e2 = bar(share, 20)
        seg.add_row(
            label,
            f"{_APPROX}{tokens:,}",
            fmt_pct(share),
            Text("▪" * f2, style="accent") + Text("·" * e2, style="chrome"),
        )
    seg.add_row(
        "free", f"{_APPROX}{snap.free_tokens:,}",
        fmt_pct(snap.free_tokens / snap.max_input_tokens
                if snap.max_input_tokens else 0.0), "",
    )

    calls = Table.grid(padding=(0, 2))
    calls.add_column(style="chrome", justify="right", width=16)
    calls.add_column()
    cache_note = (
        f"   cached {snap.cache_read_tokens:,} "
        f"({fmt_pct(snap.cache_hit_fraction)} of input)"
        if snap.cache_read_tokens else ""
    )
    calls.add_row(
        f"last call #{snap.call_no}",
        f"in {snap.input_tokens:,} · out {snap.output_tokens:,} · "
        f"{fmt_cost(snap.est_cost_usd, snap.priced)}{cache_note}",
    )
    calls.add_row(
        "session",
        f"{snap.calls} call{'s' if snap.calls != 1 else ''} · "
        f"in {snap.session_input_tokens:,} · out {snap.session_output_tokens:,} · "
        f"{fmt_cost(snap.session_cost_usd, snap.priced)}",
    )
    if snap.session_cache_read_tokens:
        calls.add_row("cached (session)", f"{snap.session_cache_read_tokens:,} tokens")
    calls.add_row(
        "context events",
        f"{snap.compactions} compaction{'s' if snap.compactions != 1 else ''} · "
        f"{snap.offloads} tool result{'s' if snap.offloads != 1 else ''} offloaded · "
        f"{snap.message_count} message{'s' if snap.message_count != 1 else ''} in state"
        + (f" ({snap.offloaded_digests} offloaded digest"
           f"{'s' if snap.offloaded_digests != 1 else ''})"
           if snap.offloaded_digests else ""),
    )

    notes = [
        f"{_APPROX} marks an estimate. Segment sizes cannot be measured — a "
        "provider reports one total for the prompt, not a figure per region.",
    ]
    if snap.calibrated:
        notes.append(
            "The estimate is calibrated against this conversation's reported "
            "input tokens, so the total tracks the real one closely."
        )
    else:
        notes.append(
            "No completed call has calibrated the estimate yet, so treat the "
            "segment figures as a rough split."
        )
    if not snap.priced:
        notes.append(
            f"{snap.model or 'This model'} has no entry in "
            "~/.yuyutsava/model_prices.json, so cost reads 'unpriced' rather "
            "than a misleading $0.00."
        )
    if snap.compactions:
        notes.append(
            "History has been compacted; evicted turns are still readable "
            "verbatim with ctx_history / ctx_history_grep."
        )

    return Panel(
        Group(head, Text(), seg, Text(), calls, Text(),
              Text("\n".join(notes), style="chrome")),
        title="◨ context",
        title_align="left",
        border_style="accent",
        padding=(1, 2),
        width=width,
    )


__all__ = [
    "MAX_PANEL_WIDTH",
    "MIN_DASHBOARD_COLS",
    "MIN_PANEL_WIDTH",
    "bar",
    "context_report",
    "fmt_cost",
    "fmt_pct",
    "fmt_tokens",
    "panel_fragments",
    "panel_width_for",
]
