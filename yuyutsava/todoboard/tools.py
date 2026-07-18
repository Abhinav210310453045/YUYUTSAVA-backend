"""todo_* tools: agent-facing access to the TODO board via the exchange.

Hidden behind the normal ``tool_search`` discovery flow (the ``todo_`` prefix
is in ``ToolFilterMiddleware._SUPPRESS_PREFIXES``) — their names stay visible
in the system-prompt catalog and a schema is pulled on demand, like mem_*/ws_*.

Two scopes:
  * ``capture`` — master/CLI subset (todo_add, todo_list, todo_get) so "assign
    this as a TODO" works from any chat/voice surface;
  * ``full``   — everything, for the TinkerAgent working ON a card.

Every tool catches :class:`TodoError` and returns a structured error string —
a board failure must never crash an agent loop.
"""

from __future__ import annotations

import functools
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from langchain_core.tools import BaseTool, tool

from yuyutsava.todoboard.exchange import (
    TodoError,
    TodoExchange,
    get_default_exchange,
)
from yuyutsava.todoboard.models import (
    ATTACHMENT_KINDS,
    CARD_STATUSES,
    TodoCardSummaryV1,
    TodoCardV1,
)

logger = logging.getLogger("yuyutsava.todoboard.tools")

ToolScope = Literal["capture", "full"]


def _safe(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    """Turn TodoError into a structured error string the model can act on."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return await fn(*args, **kwargs)
        except TodoError as exc:
            return f"todo-board error [{type(exc).__name__}]: {exc}"

    return wrapper


def _render_summary(c: TodoCardSummaryV1) -> str:
    pin = "📌 " if c.pinned else ""
    tags = f" #{' #'.join(c.tags)}" if c.tags else ""
    objectives = (
        f", {c.objective_done_count}/{c.objective_count} objectives"
        if c.objective_count else ""
    )
    return (
        f"- {pin}{c.card_id} [{c.status}] {c.title}{tags} "
        f"({c.note_count} note(s), {c.attachment_count} attachment(s){objectives})"
    )


def _humanize_event(e) -> str:
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


def _render_card(c: TodoCardV1, events: list | None = None) -> str:
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
            f"  [{n.author}{'→' + n.objective_id if n.objective_id else ''}] "
            f"({n.note_id}) {n.body}"
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
        lines.extend(_humanize_event(e) for e in events)
    return "\n".join(lines)


def make_todo_tools(
    exchange: TodoExchange | None = None,
    *,
    scope: ToolScope = "capture",
    author: str = "master",
) -> list[BaseTool]:
    """Build the todo_* family bound to one exchange.

    ``author`` labels notes written through these tools (``master`` for the
    orchestrator/CLI deepagent, ``tinker`` for the TinkerAgent).
    """
    ex = exchange or get_default_exchange()

    @tool
    @_safe
    async def todo_add(
        title: str,
        note: str | None = None,
        tags: list[str] | None = None,
        status: str = "inbox",
    ) -> str:
        """Add a card to the user's global TODO board.

        Use when the user asks to remember, plan, or track a task/idea
        ("add this as a TODO", "put it on my board"). ``title`` is a short
        renamable headline; put any detail/context into ``note``. ``status``
        is one of: inbox, active, done, archived (default inbox).
        """
        card = await ex.add_card(
            title, status=status, tags=tags, note=note, note_author=author
        )
        return f"created TODO card {card.card_id}: {card.title!r} [{card.status}]"

    @tool
    @_safe
    async def todo_list(
        status: str | None = None,
        tag: str | None = None,
        limit: int = 25,
    ) -> str:
        """List cards on the user's global TODO board (pinned first, then most
        recently updated).

        Filter with ``status`` (inbox/active/done/archived) and/or ``tag``.
        Returns one line per card with its id — use todo_get for details.
        """
        cards = await ex.query_board(status=status, tag=tag, limit=limit)
        if not cards:
            return "the TODO board has no matching cards"
        return "\n".join(_render_summary(c) for c in cards)

    @tool
    @_safe
    async def todo_get(card_id: str) -> str:
        """Read one TODO card in full: title, status, tags, objectives (its
        think flow), all notes and attachments, the card's workspace directory
        path, and its most recent activity."""
        card = await ex.get_card(card_id)
        events = await ex.list_events(card_id)
        return _render_card(card, events[-8:])

    @tool
    @_safe
    async def todo_recall(query: str, card_id: str | None = None, k: int = 6) -> str:
        """Semantically search the notes on the user's TODO board — decisions,
        ideas, findings written on cards. Use when the user refers to something
        that may live on the board ("what did we decide about X?", "the plan on
        my board"). Pass ``card_id`` to search one card only. Returns matching
        note excerpts labeled with their card id — read the full card with
        todo_get."""
        hits = await ex.search_notes(query, k=k, card_id=card_id)
        if not hits:
            return (
                "no board notes matched (semantic recall may be unavailable "
                "on this deployment — try todo_list / todo_get)"
            )
        lines = []
        for h in hits:
            card = h.payload.get("card_id", "?")
            text = h.text if len(h.text) <= 300 else h.text[:300] + " …"
            lines.append(f"- [{card}] {text}")
        return "\n".join(lines)

    capture_tools: list[BaseTool] = [todo_add, todo_list, todo_get, todo_recall]
    if scope == "capture":
        return capture_tools

    @tool
    @_safe
    async def todo_update(
        card_id: str,
        title: str | None = None,
        status: str | None = None,
        pinned: bool | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Update a TODO card's title, status, pinned flag, or tags. Only the
        fields you pass change; the rest stay as they are."""
        card = await ex.update_card(
            card_id, title=title, status=status, pinned=pinned, tags=tags,
            actor=author,
        )
        return f"updated {card.card_id}: {card.title!r} [{card.status}]"

    @tool
    @_safe
    async def todo_set_status(card_id: str, status: str) -> str:
        """Move a TODO card to a new status: inbox, active, done, or archived."""
        card = await ex.update_card(card_id, status=status, actor=author)
        return f"{card.card_id} is now [{card.status}]"

    @tool
    @_safe
    async def todo_add_note(
        card_id: str,
        body: str,
        objective_id: str | None = None,
        phase: str | None = None,
    ) -> str:
        """Append a note to a TODO card — an idea, finding, decision, or next
        step worth keeping on the card. Pass ``objective_id`` to attach the
        note to the objective it serves (and optionally ``phase`` for the
        think-flow context it was written in); omit both for a card-level
        general note."""
        note = await ex.add_note(
            card_id, body, author=author, objective_id=objective_id, phase=phase
        )
        return f"added note {note.note_id} to {card_id}"

    @tool
    @_safe
    async def todo_add_objective(
        card_id: str,
        title: str,
        phase: str = "thinking",
    ) -> str:
        """Add one small, independently-checkable objective to a card's think
        flow. Objectives are the card's structured decomposition (typically
        3-6); each moves through phases: thinking, planning, doing, completed,
        with blocked and abandoned as off-ramps."""
        obj = await ex.add_objective(card_id, title, phase=phase, actor=author)
        return f"added objective {obj.objective_id} [{obj.phase}] to {card_id}: {obj.title!r}"

    @tool
    @_safe
    async def todo_update_objective(
        objective_id: str,
        title: str | None = None,
        phase: str | None = None,
        order: int | None = None,
        reason: str | None = None,
        outcome: str | None = None,
    ) -> str:
        """Update an objective — move it through its think flow or refine it.

        ``phase`` is one of: thinking, planning, doing, completed, blocked,
        abandoned (any move is allowed). Give ``reason`` when moving to
        blocked/abandoned (why it stopped) and ``outcome`` when completing
        (what it produced) — the journey document weaves these in. ``order``
        repositions the objective in the card's list."""
        obj = await ex.update_objective(
            objective_id, title=title, phase=phase, order_idx=order,
            reason=reason, outcome=outcome, actor=author,
        )
        return f"objective {obj.objective_id} is now [{obj.phase}]: {obj.title!r}"

    @tool
    @_safe
    async def todo_assign_note(
        note_id: str,
        objective_id: str | None = None,
        phase: str | None = None,
    ) -> str:
        """Attach an existing note to an objective on the same card (or clear
        the assignment by passing no objective_id). Use this to convert older
        prose notes into the structured think flow."""
        note = await ex.assign_note(
            note_id, objective_id=objective_id, phase=phase, actor=author
        )
        target = f"objective {note.objective_id}" if note.objective_id else "card level (general)"
        return f"note {note.note_id} assigned to {target}"

    @tool
    @_safe
    async def todo_attach_artifact(
        card_id: str,
        kind: str,
        path: str | None = None,
        url: str | None = None,
        title: str | None = None,
        mime: str | None = None,
    ) -> str:
        """Attach something you produced (or found) to a TODO card.

        ``kind`` is one of: file, image, video, link, diagram, artifact.
        ``link`` needs ``url``; every other kind needs ``path`` to an existing
        file — write files into the card's workspace directory (see todo_get).
        """
        if kind not in ATTACHMENT_KINDS:
            return f"todo-board error [TodoValidationError]: kind must be one of {ATTACHMENT_KINDS}"
        att = await ex.attach(
            card_id, kind, path=path, url=url, title=title, mime=mime,
            actor=author,
        )
        return f"attached {att.kind} {att.attachment_id} to {card_id}"

    @tool
    @_safe
    async def todo_generate_artifact(
        card_id: str,
        block: str,
        spec: dict[str, Any] | str | None = None,
        title: str | None = None,
    ) -> str:
        """Generate an artifact on a TODO card and attach it, in one step.

        Dispatches to a generative artifact block by name: ``audio`` speaks
        ``spec={"text": ...}`` through the TTS pipeline into a WAV voice
        note; ``diagram`` renders a visuals spec (``spec={"kind":
        "diagram"|"chart"|..., ...}``); ``journey`` compiles the card's whole
        think flow — objectives, notes, activity timeline, artifacts — into a
        themed HTML "journey of the plan" document (no spec needed). ``spec``
        is a JSON object (an object-encoded string is accepted too). The file
        is written into the card's workspace directory, so it survives with
        the card.
        """
        if isinstance(spec, str):  # models often stringify nested objects
            try:
                spec = json.loads(spec)
            except json.JSONDecodeError:
                return (
                    "todo-board error [TodoValidationError]: spec must be a "
                    'JSON object, e.g. {"text": "words to speak"}'
                )
        if spec is not None and not isinstance(spec, dict):
            return (
                "todo-board error [TodoValidationError]: spec must be a "
                "JSON object, not a list/scalar"
            )
        att = await ex.generate_artifact(
            card_id, block, spec, title=title, actor=author
        )
        return (
            f"generated {block} artifact {att.attachment_id} ({att.mime}) "
            f"on {card_id}: {Path(att.path).name if att.path else ''}"
        )

    return [
        *capture_tools,
        todo_update,
        todo_set_status,
        todo_add_note,
        todo_add_objective,
        todo_update_objective,
        todo_assign_note,
        todo_attach_artifact,
        todo_generate_artifact,
    ]


__all__ = ["make_todo_tools", "ToolScope", "CARD_STATUSES"]
