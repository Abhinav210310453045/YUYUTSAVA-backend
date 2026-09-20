"""Showing the user a question from an agent, and reading their answer.

Two surfaces ask: the REPL's bridge, for a background subagent that has paused,
and `yuyutsava attach`, for a question coming off a running daemon's stream.
They had a copy each, and the copies had drifted — one showed the ask id and
marked background tasks, the other showed neither, for the same question about
the same operation.

That matters more than duplication usually does, because this is the prompt a
person approves an action at. Two renderings of one question mean the answer
depends on which surface happened to ask.

Everything here writes to stderr, so a piped transcript carries the
conversation and not the prompts.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from yuyutsava.ansi import CYAN, DIM, RESET
from yuyutsava.cli.line_reader import read_line
from yuyutsava.consent import decision_token, is_permission_ask

#: Suffix an agent path carries when the asker is a background task.
BACKGROUND_SUFFIX = "#bg"


@dataclass(frozen=True)
class AskView:
    """One question, in the terms a terminal can show it."""

    title: str
    body: str = ""
    options: list[str] = field(default_factory=list)
    agent_path: str = ""
    ask_id: str = ""

    @property
    def is_permission(self) -> bool:
        return is_permission_ask(self.options)

    @property
    def is_background(self) -> bool:
        return self.agent_path.endswith(BACKGROUND_SUFFIX)


def permission_keys(options: list[str]) -> str:
    """The answer keys a permission ask offers, e.g. ``[y]es / [n]o``.

    Scope keys appear only when the daemon offered them — a low- or
    medium-risk operation. Offering ``[p]roject`` for an ask that cannot store
    a project grant would teach a key that silently does nothing.
    """
    scope = " / [s]ession / [p]roject" if ("session" in options or "project" in options) else ""
    return f"[y]es / [n]o{scope}"


def render_ask(view: AskView) -> None:
    """Print the question and what may be answered."""
    tag = " [background]" if view.is_background else ""
    ident = f"  {DIM}(ask={view.ask_id[:8]}){RESET}" if view.ask_id else ""
    print(f"\n{CYAN}▣ {view.title}{tag}{RESET}{ident}", file=sys.stderr)
    if view.agent_path:
        print(f"  {DIM}from: {view.agent_path}{RESET}", file=sys.stderr)
    if view.body:
        print(f"  {view.body}", file=sys.stderr)
    if view.is_permission:
        print(f"  {DIM}{permission_keys(view.options)}{RESET}", file=sys.stderr)
    elif view.options:
        print(f"  options: {' / '.join(view.options)}", file=sys.stderr)
    sys.stderr.flush()


async def prompt_for_ask(view: AskView) -> str:
    """Show the question, read one line, and turn it into an answer.

    Read through the line-reader seam rather than ``input()``: the split-view
    dashboard owns stdin for the whole session, so a background task's approval
    was otherwise unanswerable — the prompt appeared and every key was
    swallowed.

    Anything that is not an answer is a refusal. A question that cannot be
    answered — EOF, a closed surface — is never consent.
    """
    render_ask(view)
    prompt = "approve/reject> " if view.is_permission else "> "
    line = await read_line(prompt)
    if line is None:
        return "reject"
    if view.is_permission:
        return decision_token(line) or "reject"
    return line or "reject"


__all__ = [
    "BACKGROUND_SUFFIX",
    "AskView",
    "permission_keys",
    "prompt_for_ask",
    "render_ask",
]
