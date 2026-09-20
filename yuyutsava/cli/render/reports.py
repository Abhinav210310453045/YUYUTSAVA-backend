"""What the REPL prints in answer to a command.

`/help`, `/usage`, `/skills`, `/context` and the permission card. None of it is
REPL logic — each is a function of what it is handed — so it lives beside the
renderers rather than inside the loop.

Everything writes to stderr, so a piped transcript keeps the conversation and
leaves the reports out of it.
"""

from __future__ import annotations

import sys
from typing import Any, Sequence

from yuyutsava.ansi import CYAN, DIM, RESET
from yuyutsava.cli.render.context_panel import context_report, fmt_cost, fmt_pct, fmt_tokens
from yuyutsava.cli.usage import format_usage, session_usage
from yuyutsava.context.meter import bus
from yuyutsava.skills.injector import last_recalled
from yuyutsava.skills.registry import SkillRegistry


def print_help(commands: "Sequence[Any]") -> None:
    """The command list, grouped, generated from the registry.

    Takes the registry rather than importing it: the dispatcher owns it,
    and importing it here would point this module back at the REPL.
    """
    print(file=sys.stderr)
    print(f"{CYAN}Commands:{RESET}", file=sys.stderr)
    width = max(len(c.signature) for c in commands)
    group = commands[0].group
    for cmd in commands:
        if cmd.group != group:
            print(file=sys.stderr)
            group = cmd.group
        print(f"  {DIM}{cmd.signature:<{width}}{RESET}  {cmd.help}", file=sys.stderr)
    print(file=sys.stderr)
    print(
        f"{DIM}Ctrl+C cancels the current turn but keeps the session open.{RESET}",
        file=sys.stderr,
    )
    print(
        f"{DIM}In the split view: PgUp/PgDn or the wheel scrolls, End follows "
        f"the output again, Ctrl+G hides the context panel, Ctrl+L clears the "
        f"transcript. Typing during a turn queues your message; Ctrl+S "
        f"interrupts the turn and sends it now. Start with --classic for the "
        f"single-pane view.{RESET}",
        file=sys.stderr,
    )
    print(file=sys.stderr)


def print_kv(label: str, value: str) -> None:
    print(f"  {DIM}{label:<10}{RESET} {value}", file=sys.stderr)


def print_permission_payload(payload: dict[str, Any]) -> None:
    """Render the typed interrupt body so the user knows what's being asked.

    Mirrors the field layout used by ``prompt_permission`` (non-chat CLI)
    and the Electron card builder in ``daemon/orchestrator_loop`` so the
    three surfaces stay consistent.
    """
    itype = payload.get("type", "")

    if itype == "task_runner_permission":
        op = str(payload.get("operation") or "?").upper()
        paths = payload.get("paths") or []
        path_str = ", ".join(paths) if isinstance(paths, list) else str(paths)
        zone = str(payload.get("zone") or "?").upper()
        reason = payload.get("reason") or ""
        risk = payload.get("risk_level") or ""
        agent = payload.get("requesting_agent") or ""
        parent = payload.get("parent_agent") or ""

        print_kv("Operation", op)
        if path_str:
            print_kv("Path(s)", path_str)
        print_kv("Zone", zone)
        if agent:
            line = agent + (f"  (parent: {parent})" if parent else "")
            print_kv("Agent", line)
        if reason:
            print_kv("Reason", reason)
        if risk:
            print_kv("Risk", str(risk).upper())
        return

    if itype == "permission_request":
        command = payload.get("command") or ""
        reason = payload.get("reason") or ""
        if command:
            print_kv("Command", command)
        if reason:
            print_kv("Reason", reason)
        return

    # Unknown / loose payload — best-effort body so something is visible.
    body = payload.get("body") or payload.get("command") or payload.get("reason") or ""
    if body:
        print(f"  {body}", file=sys.stderr)


async def print_usage_summary(store: Any, thread_id: str, since: float) -> None:
    """``/usage``: session totals, broken down by model."""
    rows = await session_usage(store, thread_id, since)
    if not rows:
        print(
            f"  {DIM}no recorded model calls yet in this session{RESET}",
            file=sys.stderr,
        )
        return
    print(f"  {DIM}session: {format_usage(rows)}{RESET}", file=sys.stderr)
    by_model: dict[str, list[Any]] = {}
    for r in rows:
        by_model.setdefault(r.model or "?", []).append(r)
    if len(by_model) > 1:
        for model, subset in sorted(
            by_model.items(), key=lambda kv: -sum(r.input_tokens for r in kv[1])
        ):
            print(f"    {DIM}{model}: {format_usage(subset)}{RESET}", file=sys.stderr)
    peak = max(r.input_tokens for r in rows)
    print(
        f"    {DIM}largest single request: {peak:,} input tokens{RESET}",
        file=sys.stderr,
    )


def print_skills(layout: Any) -> None:
    """``/skills``: what was recalled last turn, and what is available at all.

    Retrieval happens inside the graph, so until now there was no way to tell
    whether a skill reached the prompt — which matters, because a skill the
    agent never sees is indistinguishable from one that does not exist.
    """

    recalled = last_recalled()
    if recalled:
        print(f"  {CYAN}recalled last turn:{RESET} {', '.join(recalled)}", file=sys.stderr)
    else:
        print(
            f"  {DIM}nothing recalled yet this session (the block is built per "
            f"turn from the message text){RESET}",
            file=sys.stderr,
        )
    try:
        registry = (
            SkillRegistry(workspace_dir=layout.skills) if layout is not None else SkillRegistry()
        )
        skills = registry.scan(agent="cli")
    except Exception:  # noqa: BLE001 — a listing must never break the REPL
        return
    if not skills:
        return
    print(f"  {DIM}available ({len(skills)}):{RESET}", file=sys.stderr)
    for s in sorted(skills, key=lambda s: (s.scope, s.name)):
        mark = "*" if s.name in recalled else " "
        print(f"   {mark} {DIM}{s.scope:9}{RESET} {s.name}", file=sys.stderr)
    print(
        f"  {DIM}the agent can also search these itself with sk_search_skill{RESET}",
        file=sys.stderr,
    )


def print_context(thread_id: str, console: Any = None) -> None:
    """``/context``: what is in the window, and what it has cost.

    Reads the latest snapshot the context meter published for this thread. The
    dashboard shows a condensed version of the same numbers continuously; this
    is the view with room to say which of them are estimates and why.
    """

    snap = bus().latest(thread_id)
    if console is not None:
        try:
            console.print(context_report(snap))
            return
        except Exception:  # noqa: BLE001 — fall through to the plain summary
            pass
    if snap is None:
        print(
            f"  {DIM}no model call has completed yet — context figures appear "
            f"after the first one{RESET}",
            file=sys.stderr,
        )
        return

    print(
        f"  {DIM}window:{RESET} {snap.used_tokens:,} / "
        f"{snap.max_input_tokens:,} ({fmt_pct(snap.used_fraction)})",
        file=sys.stderr,
    )
    for _key, label, tokens in snap.segments():
        print(f"    {DIM}{label:<16}{RESET} ≈{tokens:,}", file=sys.stderr)
    print(
        f"  {DIM}last call:{RESET} in {snap.input_tokens:,} · "
        f"out {snap.output_tokens:,} · "
        f"{fmt_cost(snap.est_cost_usd, snap.priced)}",
        file=sys.stderr,
    )
    print(
        f"  {DIM}session:{RESET} {snap.calls} calls · "
        f"in {fmt_tokens(snap.session_input_tokens)} · "
        f"{snap.compactions} compactions · {snap.offloads} offloaded",
        file=sys.stderr,
    )


__all__ = [
    "print_context",
    "print_help",
    "print_kv",
    "print_permission_payload",
    "print_skills",
    "print_usage_summary",
]
