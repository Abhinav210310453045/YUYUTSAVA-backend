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

from collections.abc import Sequence
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

# Row priorities, lowest number kept longest. A short terminal loses detail
# from the bottom up; it never loses the window occupancy at the top.
_P_ESSENTIAL = 0
_P_SEGMENTS = 1
_P_LAST_CALL = 2
_P_SESSION = 3
_P_STATUS = 4
_P_NOTICE = 5
_P_DETAIL = 6
_P_FOOTER = 7


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
    height: int | None = None,
    status: str = "",
    tool: str = "",
    notices: Sequence[str] = (),
) -> list[tuple[str, str]]:
    """The live context column, as ``(style, text)`` fragments with newlines.

    *status* is the spinner-equivalent line (``Running tr_run_python…``) and
    *tool* the tool in flight; both come from the renderer, which owns that
    state — the meter has no idea a tool is running. *notices* are transient
    lines (a provider retry, an unpriced model) that belong here rather than
    printed over the transcript or the prompt.

    Every row is padded to exactly *width*, because a short row lets the
    transcript beside it show through and a long one paints over it.

    When *height* is given the column is made to **fit**: rows carry a
    priority and the least important are dropped until it does. Letting
    prompt_toolkit clip instead truncated the bottom of the panel — which is
    where the session totals live — and a number cut in half is worse than a
    number that stepped aside.
    """
    w = max(MIN_PANEL_WIDTH, width)
    rows: list[tuple[int, list[tuple[str, str]]]] = []

    def row(prio: int, text: str = "", style: str = "class:ctx.dim") -> None:
        rows.append((prio, [(style, text.ljust(w)[:w])]))

    def raw(prio: int, frags: list[tuple[str, str]]) -> None:
        used = sum(len(t) for _s, t in frags)
        pad = max(0, w - used)
        rows.append((prio, [*frags, ("", " " * pad)] if pad else frags))

    row(_P_ESSENTIAL, " CONTEXT", "class:ctx.title")

    if snap is None:
        row(_P_ESSENTIAL)
        row(_P_ESSENTIAL, " no model call yet", "class:ctx.dim")
        row(_P_DETAIL, " numbers appear once", "class:ctx.dim")
        row(_P_DETAIL, " the agent runs", "class:ctx.dim")
        return _fit(rows, height)

    row(_P_ESSENTIAL, f" {snap.model or 'model unknown'}", "class:ctx.accent")

    inner = w - 2
    bar_w = max(1, w - 7)
    filled, empty = bar(snap.used_fraction, bar_w)
    tail = f" {fmt_pct(snap.used_fraction):>4}"
    raw(_P_ESSENTIAL, [
        ("", " "),
        ("class:ctx.bar.fill", "▓" * filled),
        ("class:ctx.bar.empty", "░" * empty),
        ("class:ctx.value", tail),
    ])
    row(_P_ESSENTIAL, " " + _row(
        f"{fmt_tokens(snap.used_tokens)} / {fmt_tokens(snap.max_input_tokens)}",
        "", inner,
    ), "class:ctx.dim")
    if snap.compact_trigger_tokens and snap.used_tokens >= snap.compact_trigger_tokens:
        row(_P_ESSENTIAL, " compaction imminent", "class:ctx.warn")

    row(_P_SEGMENTS)
    for _key, label, tokens in snap.segments():
        row(_P_SEGMENTS, " " + _row(label, fmt_tokens(tokens), inner, approx=True))
    row(_P_SEGMENTS, " " + _row("free", fmt_tokens(snap.free_tokens), inner, approx=True))

    row(_P_LAST_CALL)
    row(_P_LAST_CALL, f" LAST CALL  #{snap.call_no}", "class:ctx.title")
    cache = f"  ↺{fmt_pct(snap.cache_hit_fraction)}" if snap.cache_read_tokens else ""
    row(_P_LAST_CALL, " " + _row("in", fmt_tokens(snap.input_tokens) + cache, inner),
        "class:ctx.value")
    row(_P_LAST_CALL, " " + _row("out", fmt_tokens(snap.output_tokens), inner),
        "class:ctx.value")
    row(_P_LAST_CALL, " " + _row("cost", fmt_cost(snap.est_cost_usd, snap.priced), inner))

    row(_P_SESSION)
    row(_P_SESSION, " SESSION", "class:ctx.title")
    row(_P_SESSION, " " + _row("calls", str(snap.calls), inner))
    row(_P_SESSION, " " + _row("in", fmt_tokens(snap.session_input_tokens), inner))
    row(_P_SESSION, " " + _row("out", fmt_tokens(snap.session_output_tokens), inner))
    if snap.session_cache_read_tokens:
        row(_P_DETAIL, " " + _row("cached",
                                  fmt_tokens(snap.session_cache_read_tokens), inner))
    row(_P_SESSION, " " + _row("cost", fmt_cost(snap.session_cost_usd, snap.priced), inner))
    row(_P_DETAIL, " " + _row("compacted", str(snap.compactions), inner))
    row(_P_DETAIL, " " + _row("offloaded", str(snap.offloads), inner))
    if snap.offloaded_digests:
        row(_P_DETAIL, " " + _row("digests in ctx", str(snap.offloaded_digests), inner))

    if status or tool:
        row(_P_STATUS)
        row(_P_STATUS, f" ● {tool or status}"[:w], "class:ctx.accent")

    # Transient notices, newest last. The user asked for these here rather than
    # over the transcript; they are also the only place a provider retry or an
    # unpriced model gets said now that the log no longer paints on screen.
    for text in notices:
        row(_P_NOTICE)
        for line in _wrap_plain(f" {text}", w):
            row(_P_NOTICE, line, "class:ctx.warn")

    row(_P_FOOTER)
    row(_P_FOOTER,
        f" {_APPROX} estimated" + ("" if snap.calibrated else ", uncalibrated"),
        "class:ctx.dim")
    return _fit(rows, height)


def _wrap_plain(text: str, width: int) -> list[str]:
    """Word-wrap plain text to the panel width, padding each row.

    Word-aware because notices are sentences, not data: breaking
    "no price entry" across two rows mid-word makes a 34-column column
    genuinely hard to read. Over-long single words still hard-break.
    """
    import textwrap

    if not text:
        return [""]
    rows = textwrap.wrap(
        text, width=width, break_long_words=True, break_on_hyphens=False,
    ) or [""]
    return [r.ljust(width)[:width] for r in rows]


def _fit(
    rows: list[tuple[int, list[tuple[str, str]]]], height: int | None
) -> list[tuple[str, str]]:
    """Drop the least important rows until the column fits, then flatten."""
    if height is not None and height > 0 and len(rows) > height:
        # Trim from the least important priority upward, and within a priority
        # from the bottom, so a section shortens rather than losing its header.
        for prio in sorted({p for p, _ in rows}, reverse=True):
            while len(rows) > height:
                victim = next(
                    (i for i in range(len(rows) - 1, -1, -1) if rows[i][0] == prio),
                    None,
                )
                if victim is None:
                    break
                rows.pop(victim)
            if len(rows) <= height:
                break
        rows = rows[:height]
    out: list[tuple[str, str]] = []
    for _prio, frags in rows:
        out.extend(frags)
        out.append(("", "\n"))
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
