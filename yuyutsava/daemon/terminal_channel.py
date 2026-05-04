"""
Terminal fallback channel.

Mirrors the existing ``astream_agent`` stderr UX so users running the daemon
in the foreground without a browser can still see what's happening and
respond to proposals/asks via stdin. Web is the primary channel; terminal
exists for headed-but-no-browser scenarios and as a debug surface.
"""

from __future__ import annotations

import asyncio
import sys
import time

from yuyutsava.daemon.channels import (
    AskPrompt, ChannelEvent, ProposalDecision, UserChannel,
)
from yuyutsava.events.store import Proposal

_SEP = "━" * 60


class TerminalChannel(UserChannel):
    name = "terminal"

    def __init__(self, *, verbose: bool = False) -> None:
        self._verbose = verbose

    async def post_event(self, ev: ChannelEvent) -> None:
        # Keep terminal noise low: only show timeline + tool calls + asks.
        if ev.kind == "timeline":
            line = ev.data.get("line", "")
            if line:
                print(f"\033[36m• {line}\033[0m", file=sys.stderr, flush=True)
        elif ev.kind == "tool_call":
            name = ev.data.get("name", "?")
            print(f"\033[33m🔧 {name}\033[0m", file=sys.stderr, flush=True)
        elif ev.kind == "log" and self._verbose:
            print(ev.data.get("text", ""), file=sys.stderr, flush=True)

    async def post_proposal(self, p: Proposal) -> ProposalDecision:
        print(f"\n\033[35m{_SEP}\033[0m", file=sys.stderr)
        print(f"\033[35m🟣  PROPOSAL  {p.topic}\033[0m", file=sys.stderr)
        print(f"\033[35m{_SEP}\033[0m", file=sys.stderr)
        print(f"  Event   : {p.summary}", file=sys.stderr)
        print(f"  Proposed: {p.proposed}", file=sys.stderr)
        print(f"  Subagent: {p.subagent}   urgency={p.urgency}", file=sys.stderr)
        ttl = max(0, int(p.expires_ts - time.time()))
        print(f"  Expires : in {ttl}s", file=sys.stderr)
        print(f"\033[35m{_SEP}\033[0m", file=sys.stderr)
        print("  [a]pprove  [r]emember-approve  [s]kip  [k]eep-skip  [m]odify", file=sys.stderr)
        try:
            answer = await asyncio.wait_for(
                asyncio.to_thread(input, "  Decision: "),
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
            edit = await asyncio.to_thread(input, "  New instruction: ")
            return ProposalDecision(decision="modify", edited_instruction=edit.strip() or None)
        return ProposalDecision(decision="skip")

    async def post_ask(self, a: AskPrompt) -> str:
        print(f"\n\033[33m{_SEP}\033[0m", file=sys.stderr)
        print(f"\033[33m🛑 {a.title}\033[0m", file=sys.stderr)
        print(f"\033[33m{_SEP}\033[0m", file=sys.stderr)
        print(f"  {a.body}", file=sys.stderr)
        if a.options:
            print(f"  Options: {' | '.join(a.options)}", file=sys.stderr)
        print(f"\033[33m{_SEP}\033[0m", file=sys.stderr)
        answer = await asyncio.to_thread(input, "  Response: ")
        return (answer or "").strip() or "reject"
