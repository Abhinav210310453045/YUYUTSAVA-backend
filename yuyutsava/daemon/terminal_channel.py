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

from yuyutsava.daemon.channels import (
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
    name = "terminal"

    def __init__(self, *, verbose: bool = False) -> None:
        self._verbose = verbose

    async def post_event(self, ev: ChannelEvent) -> None:
        match ev.payload:
            case TimelinePayload(line=line) if line:
                print(f"\033[36m• {line}\033[0m", file=sys.stderr, flush=True)
            case ToolCallPayload(name=name, args=args):
                if self._verbose:
                    args_str = ""
                    if args:
                        args_str = " " + json.dumps(dict(args), ensure_ascii=False)[:160]
                    print(f"\033[33m🔧 {name}{args_str}\033[0m", file=sys.stderr, flush=True)
                else:
                    print(f"\033[33m🔧 {name}\033[0m", file=sys.stderr, flush=True)
            case ToolResultPayload(name=name, preview=preview) if self._verbose:
                short = preview[:300].replace("\n", " ") if preview else "(empty)"
                print(f"\033[32m  ↳ [{name}] {short}\033[0m", file=sys.stderr, flush=True)
            case TokenPayload(text=text) if self._verbose and text:
                print(text, end="", flush=True, file=sys.stderr)
            case LogPayload(text=text) if self._verbose:
                print(text, file=sys.stderr, flush=True)
            case _:
                pass

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
        # An ask is a permission prompt when its options include the
        # approve/reject pair (plus optional session/project scopes); anything
        # else (free-text / custom options) is a user_question passed through.
        from yuyutsava.consent import decision_token, is_permission_ask

        options = a.options or []
        is_permission = is_permission_ask(options)

        print(f"\n\033[33m{_SEP}\033[0m", file=sys.stderr)
        print(f"\033[33m🛑 {a.title}\033[0m", file=sys.stderr)
        print(f"\033[33m{_SEP}\033[0m", file=sys.stderr)
        print(f"  {a.body}", file=sys.stderr)
        if is_permission:
            scope = " / [s]ession / [p]roject" if ("session" in options or "project" in options) else ""
            print(f"  \033[2m[y]es / [n]o{scope}\033[0m", file=sys.stderr)
        elif options:
            print(f"  Options: {' | '.join(options)}", file=sys.stderr)
        print(f"\033[33m{_SEP}\033[0m", file=sys.stderr)

        prompt = "  Allow? [y/N]: " if is_permission else "  Response: "
        answer = await asyncio.to_thread(input, prompt)
        if is_permission:
            return decision_token(answer) or "reject"
        return (answer or "").strip() or "reject"
