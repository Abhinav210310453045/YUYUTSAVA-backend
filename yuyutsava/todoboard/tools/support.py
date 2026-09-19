"""Shared plumbing for the TODO board tool families."""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from yuyutsava.todoboard.exchange import TodoError, TodoExchange
from yuyutsava.todoboard.models import TodoCardSummaryV1, TodoCardV1

logger = logging.getLogger("yuyutsava.todoboard.tools.support")

#: ``capture`` records a card and finds it again; ``full`` also works one.
ToolScope = Literal["capture", "full"]


@dataclass(frozen=True)
class BoardContext:
    """What every todo tool needs: the board, and who is writing.

    ``author`` labels notes written through these tools — ``master`` for the
    orchestrator and chat deepagent, ``tinker`` for the TinkerAgent — so a
    card's history says which agent did what.
    """

    exchange: TodoExchange
    author: str


def safe(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    """Turn TodoError into a structured error string the model can act on."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return await fn(*args, **kwargs)
        except TodoError as exc:
            return f"todo-board error [{type(exc).__name__}]: {exc}"

    return wrapper


def render_summary(c: TodoCardSummaryV1) -> str:
    pin = "📌 " if c.pinned else ""
    tags = f" #{' #'.join(c.tags)}" if c.tags else ""
    objectives = (
        f", {c.objective_done_count}/{c.objective_count} objectives" if c.objective_count else ""
    )
    return (
        f"- {pin}{c.card_id} [{c.status}] {c.title}{tags} "
        f"({c.note_count} note(s), {c.attachment_count} attachment(s){objectives})"
    )


def humanize_event(e) -> str:
    p = e.payload or {}
    title = p.get("title") or p.get("attachment_id") or ""
    body = {
        "card_status": f"card {p.get('from')} → {p.get('to')}",
        "objective_created": f"objective added: {title} [{p.get('phase')}]",
        "objective_phase": f"{title}: {p.get('from')} → {p.get('to')}"
        + (f" ({p['reason']})" if p.get("reason") else ""),
        "objective_updated": f"{title}: {', '.join(p.get('fields', []))} changed",
        "objective_deleted": f"objective removed: {title}",
        "note_assigned": f"note {p.get('note_id')} → objective",
        "artifact_attached": f"attached {p.get('kind')}: {title}",
        "journey_generated": "journey document generated",
    }.get(e.kind, e.kind)
    return f"  [{e.actor}] {body}"


def render_card(c: TodoCardV1, events: list | None = None) -> str:
    lines = [
        f"{'📌 ' if c.pinned else ''}{c.title}",
        f"id: {c.card_id} | status: {c.status}"
        + (f" | tags: {', '.join(c.tags)}" if c.tags else ""),
    ]
    if c.workspace_path:
        lines.append(f"workspace: {c.workspace_path}")
    if c.objectives:
        lines.append("objectives (think flow):")
        for o in c.objectives:
            lines.append(f"  [{o.phase}] ({o.objective_id}) #{o.order_idx} {o.title}")
            if o.reason:
                lines.append(f"    ↳ reason: {o.reason}")
            if o.outcome:
                lines.append(f"    ↳ outcome: {o.outcome}")
    if c.notes:
        lines.append("notes:")
        lines.extend(
            f"  [{n.author}{'→' + n.objective_id if n.objective_id else ''}] ({n.note_id}) {n.body}"
            for n in c.notes
        )
    if c.attachments:
        lines.append("attachments:")
        lines.extend(
            f"  [{a.kind}] ({a.attachment_id}) {a.title or ''} {a.path or a.url or ''}".rstrip()
            for a in c.attachments
        )
    if not c.objectives and not c.notes and not c.attachments:
        lines.append("(no objectives, notes, or attachments yet)")
    if events:
        lines.append("recent activity:")
        lines.extend(humanize_event(e) for e in events)
    return "\n".join(lines)
