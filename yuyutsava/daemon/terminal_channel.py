"""
Terminal fallback channel.

Mirrors the existing ``astream_agent`` stderr UX so users running the daemon
in the foreground without a browser can still see what's happening and
respond to proposals/asks via stdin. Web is the primary channel; terminal
exists for headed-but-no-browser scenarios and as a debug surface.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Callable
from typing import TextIO

from yuyutsava.ansi import CYAN, DIM, GREEN, MAGENTA, RESET, YELLOW
from yuyutsava.consent import decision_token, is_permission_ask
from yuyutsava.ports.channels import (
    AskPrompt,
    ChannelEvent,
    LogPayload,
    ProposalDecision,
    TimelinePayload,
    TokenPayload,
    ToolCallPayload,
    ToolResultPayload,
    UserChannel,
)
from yuyutsava.storage.events import Proposal

_SEP = "━" * 60


class TerminalChannel(UserChannel):
    """Renders to a text stream and reads answers line by line.

    ``out`` defaults to stderr (stdout stays free for anything piped) and
    ``ask`` to :func:`input`, which runs off the loop in a thread; both are
    parameters so the channel can be driven without a terminal.
    """

    name = "terminal"

    def __init__(
        self,
        *,
        verbose: bool = False,
        out: TextIO | None = None,
        ask: Callable[[str], str] | None = None,
    ) -> None:
        self._verbose = verbose
        self._out = out
        self._ask = ask

    async def _read(self, prompt: str) -> str:
        # Off the loop: input() blocks. Resolved per call, like ``out``.
        return await asyncio.to_thread(self._ask or input, prompt)

    def _line(self, text: str = "", *, end: str = "\n") -> None:
        # Resolved per call: the dashboard and tests swap sys.stderr.
        out = self._out if self._out is not None else sys.stderr
        out.write(text + end)
        out.flush()

    async def post_event(self, ev: ChannelEvent) -> None:
        match ev.payload:
            case TimelinePayload(line=line) if line:
                self._line(f"{CYAN}• {line}{RESET}")
            case ToolCallPayload(name=name, args=args):
                if self._verbose:
                    args_str = ""
                    if args:
                        args_str = " " + json.dumps(dict(args), ensure_ascii=False)[:160]
                    self._line(f"{YELLOW}🔧 {name}{args_str}{RESET}")
                else:
                    self._line(f"{YELLOW}🔧 {name}{RESET}")
            case ToolResultPayload(name=name, preview=preview) if self._verbose:
                short = preview[:300].replace("\n", " ") if preview else "(empty)"
                self._line(f"{GREEN}  ↳ [{name}] {short}{RESET}")
            case TokenPayload(text=text) if self._verbose and text:
                self._line(text, end="")
            case LogPayload(text=text) if self._verbose:
                self._line(text)
            case _:
                pass

    async def post_proposal(self, p: Proposal) -> ProposalDecision:
        self._line(f"\n{MAGENTA}{_SEP}{RESET}")
        self._line(f"{MAGENTA}🟣  PROPOSAL  {p.topic}{RESET}")
        self._line(f"{MAGENTA}{_SEP}{RESET}")
        self._line(f"  Event   : {p.summary}")
        self._line(f"  Proposed: {p.proposed}")
        self._line(f"  Subagent: {p.subagent}   urgency={p.urgency}")
        ttl = max(0, int(p.expires_ts - time.time()))
        self._line(f"  Expires : in {ttl}s")
        self._line(f"{MAGENTA}{_SEP}{RESET}")
        self._line("  [a]pprove  [r]emember-approve  [s]kip  [k]eep-skip  [m]odify")
        try:
            answer = await asyncio.wait_for(
                self._read("  Decision: "),
                timeout=max(1.0, p.expires_ts - time.time()),
            )
        except asyncio.TimeoutError:
            return ProposalDecision(decision="expired")
        ans = (answer or "").strip().lower()
        if ans == "a":
            return ProposalDecision(decision="approve")
        if ans == "r":
            return ProposalDecision(decision="approve_remember")
        if ans == "k":
            return ProposalDecision(decision="skip_remember")
        if ans == "m":
            edit = await self._read("  New instruction: ")
            return ProposalDecision(decision="modify", edited_instruction=edit.strip() or None)
        return ProposalDecision(decision="skip")

    async def post_ask(self, a: AskPrompt) -> str:
        # An ask is a permission prompt when its options include the
        # approve/reject pair (plus optional session/project scopes); anything
        # else (free-text / custom options) is a user_question passed through.
        options = a.options or []
        is_permission = is_permission_ask(options)

        self._line(f"\n{YELLOW}{_SEP}{RESET}")
        self._line(f"{YELLOW}🛑 {a.title}{RESET}")
        self._line(f"{YELLOW}{_SEP}{RESET}")
        self._line(f"  {a.body}")
        if is_permission:
            scope = (
                " / [s]ession / [p]roject" if ("session" in options or "project" in options) else ""
            )
            self._line(f"  {DIM}[y]es / [n]o{scope}{RESET}")
        elif options:
            self._line(f"  Options: {' | '.join(options)}")
        self._line(f"{YELLOW}{_SEP}{RESET}")

        prompt = "  Allow? [y/N]: " if is_permission else "  Response: "
        answer = await self._read(prompt)
        if is_permission:
            return decision_token(answer) or "reject"
        return (answer or "").strip() or "reject"
