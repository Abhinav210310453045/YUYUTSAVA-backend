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

import asyncio
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
    return (
        f"- {pin}{c.card_id} [{c.status}] {c.title}{tags} "
        f"({c.note_count} note(s), {c.attachment_count} attachment(s))"
    )


def _render_card(c: TodoCardV1) -> str:
    lines = [
        f"{'📌 ' if c.pinned else ''}{c.title}",
        f"id: {c.card_id} | status: {c.status}"
        + (f" | tags: {', '.join(c.tags)}" if c.tags else ""),
    ]
    if c.workspace_path:
        lines.append(f"workspace: {c.workspace_path}")
    if c.notes:
        lines.append("notes:")
        lines.extend(f"  [{n.author}] ({n.note_id}) {n.body}" for n in c.notes)
    if c.attachments:
        lines.append("attachments:")
        lines.extend(
            f"  [{a.kind}] ({a.attachment_id}) {a.title or ''} {a.path or a.url or ''}".rstrip()
            for a in c.attachments
        )
    if not c.notes and not c.attachments:
        lines.append("(no notes or attachments yet)")
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
        """Read one TODO card in full: title, status, tags, all notes and
        attachments, and the card's workspace directory path."""
        return _render_card(await ex.get_card(card_id))

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
            card_id, title=title, status=status, pinned=pinned, tags=tags
        )
        return f"updated {card.card_id}: {card.title!r} [{card.status}]"

    @tool
    @_safe
    async def todo_set_status(card_id: str, status: str) -> str:
        """Move a TODO card to a new status: inbox, active, done, or archived."""
        card = await ex.update_card(card_id, status=status)
        return f"{card.card_id} is now [{card.status}]"

    @tool
    @_safe
    async def todo_add_note(card_id: str, body: str) -> str:
        """Append a note to a TODO card — an idea, finding, decision, or next
        step worth keeping on the card."""
        note = await ex.add_note(card_id, body, author=author)
        return f"added note {note.note_id} to {card_id}"

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
            card_id, kind, path=path, url=url, title=title, mime=mime
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
        "diagram"|"chart"|..., ...}``). ``spec`` is a JSON object (an
        object-encoded string is accepted too). The file is written into the
        card's workspace directory, so it survives with the card.
        """
        # The dispatcher is block-agnostic: everything block-specific lives
        # in the registry, so new generative blocks need no edits here.
        from yuyutsava.todoboard.artifacts import blocks

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

        gen = next((b for b in blocks() if b.name == block), None)
        if gen is None or gen.generate is None:
            names = ", ".join(sorted(b.name for b in blocks() if b.generate))
            return (
                f"todo-board error [TodoValidationError]: no generative "
                f"artifact block named {block!r} (available: {names})"
            )
        card = await ex.get_card(card_id)
        from yuyutsava.storage.paths import blobs_dir

        out_dir = (
            Path(card.workspace_path) if card.workspace_path
            else blobs_dir() / "todoboard" / card_id
        )
        # Generators do blocking work (TTS/renderers) — off the event loop,
        # like the exchange runs validators.
        try:
            path, mime = await asyncio.to_thread(gen.generate, dict(spec or {}), out_dir)
        except TodoError:
            raise  # _safe renders these
        except Exception as exc:  # a broken generator must not crash the loop
            return f"todo-board error [TodoAttachmentError]: {block} generation failed: {exc}"
        att = await ex.attach(
            card_id, gen.kind, path=str(path), mime=mime, title=title,
            meta={"source": "generate", "block": block, "author": author},
        )
        return (
            f"generated {block} artifact {att.attachment_id} ({att.mime}) "
            f"on {card_id}: {path.name}"
        )

    return [
        *capture_tools,
        todo_update,
        todo_set_status,
        todo_add_note,
        todo_attach_artifact,
        todo_generate_artifact,
    ]


__all__ = ["make_todo_tools", "ToolScope", "CARD_STATUSES"]
