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

Segment sizes are estimates (see the meter's module docstring) and every
estimated figure is prefixed ``≈``. The **total** is not an estimate once a call
has completed: it is anchored on the provider's reported input tokens, so it
carries the mark only while growth since that call is being estimated
(``window_measured``). That distinction is the whole point of the row — a panel
that printed ``23.5k / 1.0M`` four rows above ``in 27.6k``, for the same prompt,
is one number too many. Last-call figures are the provider's own and carry no
mark. An unpriced model reads ``unpriced``, never ``$0.00`` — "we have no price
for this" is not "this was free".
"""

from __future__ import annotations

import textwrap
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from yuyutsava.cli.render import pulse

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


def _fit_pair(label: str, value: str, width: int) -> tuple[str, int]:
    """*label* as it fits, and the gap that right-aligns *value* after it.

    The label is trimmed and never the number: a number cut in half is wrong
    where a name cut short is merely terse, and the number is what the row is
    for.
    """
    gap = width - len(label) - len(value)
    if gap < 1:
        keep = max(1, width - len(value) - 1)
        label = label[:keep]
        gap = max(1, width - len(label) - len(value))
    return label, gap


class _Column:
    """The panel's rows as they are built, each with a priority for fitting.

    Every row is padded to exactly the width, because a short row lets the
    transcript beside it show through and a long one paints over it.
    """

    def __init__(self, width: int) -> None:
        self.width = width
        self.inner = width - 2
        self.rows: list[tuple[int, list[tuple[str, str]]]] = []

    def row(self, prio: int, text: str = "", style: str = "class:ctx.dim") -> None:
        self.rows.append((prio, [(style, text.ljust(self.width)[: self.width])]))

    def raw(self, prio: int, frags: list[tuple[str, str]]) -> None:
        used = sum(len(t) for _s, t in frags)
        pad = max(0, self.width - used)
        self.rows.append((prio, [*frags, ("", " " * pad)] if pad else frags))

    def pair(
        self,
        prio: int,
        label: str,
        value: str,
        *,
        approx: bool = False,
        value_style: str = "class:ctx.value",
    ) -> None:
        """``Label            value``, the name and the number styled apart.

        One colour for both made the panel a block of grey in which nothing
        was findable. The name carries the banner's key colour and the number
        its value colour, so the column of numbers reads on its own.
        """
        val = f"{_APPROX}{value}" if approx else value
        label, gap = _fit_pair(label, val, self.inner)
        self.raw(
            prio,
            [("class:ctx.key", f" {label}"), ("", " " * gap), (value_style, val)],
        )


#: What each row is called on screen. The meter's own keys are terse because
#: they are also a wire format the app reads; these are the words a reader who
#: did not write the meter would use. ``in``/``out`` in particular said nothing
#: about which direction was which.
LABELS = {
    "input": "Sent",
    "output": "Received",
    "cost": "Cost",
    "calls": "Model calls",
    "cached": "Reused",
    "compacted": "Compactions",
    "offloaded": "Offloaded",
    "digests": "Digests kept",
    "free": "Free",
}


def _window_rows(col: _Column, snap: Any) -> bool:
    """Model, fill bar and used/max; returns whether the total is measured."""
    col.row(_P_ESSENTIAL, f" {snap.model or 'model unknown'}", "class:ctx.model")
    filled, empty = bar(snap.used_fraction, max(1, col.width - 7))
    col.raw(
        _P_ESSENTIAL,
        [
            ("", " "),
            ("class:ctx.bar.fill", "▓" * filled),
            ("class:ctx.bar.empty", "░" * empty),
            ("class:ctx.value", f" {fmt_pct(snap.used_fraction):>4}"),
        ],
    )
    # No mark when the total is the provider's own arithmetic: input tokens
    # for the prompt plus output tokens for the reply it produced.
    measured = bool(getattr(snap, "window_measured", False))
    mark = "" if measured else _APPROX
    used = f"{mark}{fmt_tokens(snap.used_tokens)}"
    col.row(
        _P_ESSENTIAL,
        f" {used} of {fmt_tokens(snap.max_input_tokens)} used",
        "class:ctx.dim",
    )
    if snap.compact_trigger_tokens and snap.used_tokens >= snap.compact_trigger_tokens:
        col.row(_P_ESSENTIAL, " compaction imminent", "class:ctx.warn")
    return measured


def _segment_rows(col: _Column, snap: Any, measured: bool) -> None:
    col.row(_P_SEGMENTS)
    for _key, label, tokens in snap.segments():
        col.pair(_P_SEGMENTS, label.capitalize(), fmt_tokens(tokens), approx=True)
    col.pair(_P_SEGMENTS, LABELS["free"], fmt_tokens(snap.free_tokens), approx=not measured)


def _last_call_rows(col: _Column, snap: Any) -> None:
    col.row(_P_LAST_CALL)
    col.row(_P_LAST_CALL, f" LAST CALL  #{snap.call_no}", "class:ctx.title")
    cache = f"  ↺{fmt_pct(snap.cache_hit_fraction)}" if snap.cache_read_tokens else ""
    col.pair(_P_LAST_CALL, LABELS["input"], fmt_tokens(snap.input_tokens) + cache)
    col.pair(_P_LAST_CALL, LABELS["output"], fmt_tokens(snap.output_tokens))
    col.pair(_P_LAST_CALL, LABELS["cost"], fmt_cost(snap.est_cost_usd, snap.priced))


def _session_rows(col: _Column, snap: Any) -> None:
    col.row(_P_SESSION)
    col.row(_P_SESSION, " SESSION", "class:ctx.title")
    col.pair(_P_SESSION, LABELS["calls"], str(snap.calls))
    col.pair(_P_SESSION, LABELS["input"], fmt_tokens(snap.session_input_tokens))
    col.pair(_P_SESSION, LABELS["output"], fmt_tokens(snap.session_output_tokens))
    if snap.session_cache_read_tokens:
        col.pair(_P_DETAIL, LABELS["cached"], fmt_tokens(snap.session_cache_read_tokens))
    col.pair(_P_SESSION, LABELS["cost"], fmt_cost(snap.session_cost_usd, snap.priced))
    col.pair(_P_DETAIL, LABELS["compacted"], str(snap.compactions))
    col.pair(_P_DETAIL, LABELS["offloaded"], str(snap.offloads))
    if snap.offloaded_digests:
        col.pair(_P_DETAIL, LABELS["digests"], str(snap.offloaded_digests))


def _footer_rows(col: _Column, snap: Any) -> None:
    col.row(_P_FOOTER)
    if getattr(snap, "anchored", False):
        foot = f"{_APPROX} rows estimated, total measured"
    elif snap.calibrated:
        foot = f"{_APPROX} estimated"
    else:
        foot = f"{_APPROX} estimated, uncalibrated"
    for line in _wrap_plain(f" {foot}", col.width):
        col.row(_P_FOOTER, line, "class:ctx.dim")


def _notice_rows(col: _Column, notices: Sequence[str]) -> None:
    """Transient notices, newest last.

    The user asked for these here rather than over the transcript; they are
    also the only place a provider retry, an unpriced model or an MCP server
    coming up gets said, since the log does not paint on screen.
    """
    for text in notices:
        col.row(_P_NOTICE)
        for line in _wrap_plain(f" {text}", col.width):
            col.row(_P_NOTICE, line, "class:ctx.warn")


def panel_fragments(
    snap: Any | None,
    *,
    width: int,
    height: int | None = None,
    status: str = "",
    tool: str = "",
    state: str = "",
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
    col = _Column(max(MIN_PANEL_WIDTH, width))
    col.row(_P_ESSENTIAL, " CONTEXT", "class:ctx.title")

    if snap is None:
        col.row(_P_ESSENTIAL)
        col.row(_P_ESSENTIAL, " no model call yet", "class:ctx.dim")
        col.row(_P_DETAIL, " numbers appear once", "class:ctx.dim")
        col.row(_P_DETAIL, " the agent runs", "class:ctx.dim")
        # Before the first message too: tools still starting say so here.
        _notice_rows(col, notices)
        return _fit(col.rows, height)

    measured = _window_rows(col, snap)
    _segment_rows(col, snap, measured)
    _last_call_rows(col, snap)
    _session_rows(col, snap)

    if status or tool:
        col.row(_P_STATUS)
        now = time.monotonic()
        mark = pulse.mark_at(now, state)
        col.raw(
            _P_STATUS,
            [
                (f"class:pulse.{pulse.hue_at(now)}", f" {mark} "),
                ("class:ctx.accent", (tool or status)[: max(0, col.width - 3)]),
            ],
        )

    _notice_rows(col, notices)
    _footer_rows(col, snap)
    return _fit(col.rows, height)


def _wrap_plain(text: str, width: int, *, indent: str = " ") -> list[str]:
    """Word-wrap plain text to the panel width, padding each row.

    Word-aware because notices are sentences, not data: breaking
    "no price entry" across two rows mid-word makes a 34-column column
    genuinely hard to read. Over-long single words still hard-break.

    Every row carries *indent*, the continuation rows included: a sentence
    whose second line starts at column zero reads as a new one, and in a
    column this narrow most notices have a second line.
    """

    if not text:
        return [""]
    rows = textwrap.wrap(
        text.strip(),
        width=width,
        initial_indent=indent,
        subsequent_indent=indent,
        break_long_words=True,
        break_on_hyphens=False,
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


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def _report_head(snap: Any) -> Table:
    """Model, window, how full it is, and what the total is based on."""
    head = Table.grid(padding=(0, 2))
    head.add_column(style="chrome", justify="right")
    head.add_column()
    head.add_row("model", Text(snap.model or "unknown", style="accent"))
    head.add_row(
        "window",
        f"{snap.max_input_tokens:,} tokens"
        + (
            f"  ·  compacts at {snap.compact_trigger_tokens:,}"
            if snap.compact_trigger_tokens
            else ""
        ),
    )
    filled, empty = bar(snap.used_fraction, 40)
    measured = bool(getattr(snap, "window_measured", False))
    meter = Text()
    meter.append("▓" * filled, style="accent")
    meter.append("░" * empty, style="chrome")
    meter.append(
        f"  {fmt_pct(snap.used_fraction)}  "
        f"({'' if measured else _APPROX}{snap.used_tokens:,} used · "
        f"{snap.free_tokens:,} free)"
    )
    head.add_row("used", meter)
    if measured:
        basis = "the provider's own count for the last prompt, plus the reply"
    elif getattr(snap, "anchored", False):
        basis = "the last measured prompt, plus an estimate of what has been appended since"
    else:
        basis = "a character estimate — no call has measured this window yet"
    head.add_row("basis", basis)
    return head


def _share(tokens: int, snap: Any) -> float:
    return tokens / snap.max_input_tokens if snap.max_input_tokens else 0.0


def _report_segments(snap: Any) -> Table:
    """Each segment's estimated size and share of the window, then free."""
    seg = Table.grid(padding=(0, 2))
    seg.add_column(style="chrome", justify="right", width=16)
    seg.add_column(justify="right", width=12)
    seg.add_column(style="chrome", justify="right", width=6)
    seg.add_column()
    for _key, label, tokens in snap.segments():
        share = _share(tokens, snap)
        f2, e2 = bar(share, 20)
        seg.add_row(
            label,
            f"{_APPROX}{tokens:,}",
            fmt_pct(share),
            Text("▪" * f2, style="accent") + Text("·" * e2, style="chrome"),
        )
    seg.add_row(
        "free", f"{_APPROX}{snap.free_tokens:,}", fmt_pct(_share(snap.free_tokens, snap)), ""
    )
    return seg


def _report_calls(snap: Any) -> Table:
    """The last call, the session, and what the context layer has done."""
    calls = Table.grid(padding=(0, 2))
    calls.add_column(style="chrome", justify="right", width=16)
    calls.add_column()
    cache_note = (
        f"   cached {snap.cache_read_tokens:,} ({fmt_pct(snap.cache_hit_fraction)} of input)"
        if snap.cache_read_tokens
        else ""
    )
    calls.add_row(
        f"last call #{snap.call_no}",
        f"in {snap.input_tokens:,} · out {snap.output_tokens:,} · "
        f"{fmt_cost(snap.est_cost_usd, snap.priced)}{cache_note}",
    )
    calls.add_row(
        "session",
        f"{_plural(snap.calls, 'call')} · "
        f"in {snap.session_input_tokens:,} · out {snap.session_output_tokens:,} · "
        f"{fmt_cost(snap.session_cost_usd, snap.priced)}",
    )
    if snap.session_cache_read_tokens:
        calls.add_row("cached (session)", f"{snap.session_cache_read_tokens:,} tokens")
    digests = (
        f" ({_plural(snap.offloaded_digests, 'offloaded digest')})"
        if snap.offloaded_digests
        else ""
    )
    calls.add_row(
        "context events",
        f"{_plural(snap.compactions, 'compaction')} · "
        f"{_plural(snap.offloads, 'tool result')} offloaded · "
        f"{_plural(snap.message_count, 'message')} in state{digests}",
    )
    return calls


def _report_notes(snap: Any) -> list[str]:
    """What the numbers mean, and which caveats apply to this snapshot."""
    notes = [
        f"{_APPROX} marks an estimate. Segment sizes cannot be measured — a "
        "provider reports one total for the prompt, not a figure per region — "
        "so the rows are a breakdown, scaled to sum to the total exactly.",
    ]
    if getattr(snap, "anchored", False):
        notes.append(
            "The total is not an estimate: it is anchored on the input tokens "
            "the provider reported for the last call, and only what has been "
            "appended since is estimated. It therefore cannot fall unless "
            "content actually left the window."
        )
    if snap.calibrated:
        notes.append(
            "Two corrections are fitted from this model's own reported "
            "tokens — one for the static prefix (prompt and tool schemas), one "
            "for the conversation. One combined factor averaged a JSON schema "
            "against prose and got both wrong."
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
    return notes


def context_report(snap: Any | None, *, width: int = 72) -> RenderableType:
    """The ``/context`` breakdown: the same numbers, room to explain them."""
    if snap is None:
        return Text(
            "no model call has completed yet — context figures appear after the first one",
            style="chrome",
        )
    return Panel(
        Group(
            _report_head(snap),
            Text(),
            _report_segments(snap),
            Text(),
            _report_calls(snap),
            Text(),
            Text("\n".join(_report_notes(snap)), style="chrome"),
        ),
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
