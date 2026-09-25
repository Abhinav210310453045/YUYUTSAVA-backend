"""What the Telegram bot says: message text and keyboards, as pure functions.

Everything here is presentation — HTML-escaped text in Telegram's ``HTML``
parse mode, and the ``reply_markup`` dicts the Bot API takes. The plugin
(:mod:`~yuyutsava.channels.telegram.channel`) decides when to send them.

Callback data is ``p:<proposal_id>:<a|s|m>`` for a proposal's buttons and
``a:<ask_id>:<option index>`` for an ask's; the plugin parses it back.
"""

from __future__ import annotations

import html
from typing import Any

from yuyutsava.ports.channels import (
    AskPrompt,
    AsyncTaskCompletedPayload,
    LogPayload,
    TimelinePayload,
    ToolCallPayload,
    ToolResultPayload,
)
from yuyutsava.storage.events import Proposal

HELP_TEXT = (
    "Send any text to submit it as a task.\n"
    "/tasks — pending + running tasks\n"
    "/status — daemon health"
)

BOT_COMMANDS = [
    {"command": "tasks", "description": "Pending and running tasks"},
    {"command": "status", "description": "Daemon health"},
    {"command": "help", "description": "How to use this bot"},
]

FORCE_REPLY = {"force_reply": True}
MODIFY_PROMPT = "✏️ Reply to this message with the modified instruction."


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def event_line(p: Any) -> str:
    """One line for a channel event; empty means nothing worth sending."""
    if isinstance(p, LogPayload):
        return p.text.strip()
    if isinstance(p, TimelinePayload):
        return p.line.strip()
    if isinstance(p, ToolCallPayload):
        return f"⚙ {p.name}"
    if isinstance(p, ToolResultPayload):
        preview = (p.preview or "").strip().replace("\n", " ")[:120]
        return f"⚙ {p.name} → {preview}" if preview else ""
    if isinstance(p, AsyncTaskCompletedPayload):
        mark = "✅" if p.ok else "❌"
        return f"{mark} bg {p.agent_name}: {p.summary[:200]}"
    # Other async-task chatter: keep it terse.
    text = getattr(p, "text", "") or getattr(p, "title", "")
    return str(text).strip()[:200]


def batch_text(lines: list[str]) -> str:
    """A debounced batch as one message: de-duplicated, in order, within the limit."""
    return esc("\n".join(dict.fromkeys(lines)))[:4000]


def proposal_text(p: Proposal) -> str:
    return (
        f"<b>Proposal</b> — {esc(p.summary)}\n"
        f"{esc(p.proposed)}\n"
        f"<i>subagent: {esc(p.subagent)} · urgency {p.urgency}</i>"
    )


def proposal_keyboard(proposal_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"p:{proposal_id}:a"},
                {"text": "⏭ Skip", "callback_data": f"p:{proposal_id}:s"},
                {"text": "✏️ Modify…", "callback_data": f"p:{proposal_id}:m"},
            ]
        ]
    }


def decided_text(text: str, decision: str) -> str:
    """The proposal message once answered, with the outcome appended."""
    return f"{text}\n→ <b>{esc(decision)}</b>"


def ask_text(a: AskPrompt) -> str:
    text = f"<b>{esc(a.title)}</b>\n{esc(a.body)}"
    return text if a.options else text + "\n<i>Reply to this message with your answer.</i>"


def ask_keyboard(a: AskPrompt) -> dict[str, Any]:
    """Buttons for each option, or a force-reply for a free-text answer."""
    if not a.options:
        return FORCE_REPLY
    return {
        "inline_keyboard": [
            [{"text": opt, "callback_data": f"a:{a.ask_id}:{i}"}] for i, opt in enumerate(a.options)
        ]
    }


def tasks_text(pending: dict[str, Any]) -> str:
    """``/tasks``: queued and running tasks, and what is waiting on the user."""
    tasks = pending.get("tasks", [])
    if not tasks:
        return "No queued or running tasks."
    lines = []
    for t in tasks[:15]:
        instr = (t.get("instruction") or "").replace("\n", " ")[:80]
        lines.append(f"[{t.get('status')}] {t.get('task_id')} — {instr}")
    n_props = len(pending.get("pending_proposal_ids", []))
    n_asks = len(pending.get("pending_ask_ids", []))
    if n_props or n_asks:
        lines.append(f"(awaiting you: {n_props} proposal(s), {n_asks} ask(s))")
    return "\n".join(lines)
