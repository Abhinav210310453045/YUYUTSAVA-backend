"""TODO board tools: attaching and generating a card's artifacts.

Split out of the 266-line ``make_todo_tools`` (`R-RDB-02`); the factory in
``__init__`` composes the families.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool

from yuyutsava.todoboard.models import (
    ATTACHMENT_KINDS,
)
from yuyutsava.todoboard.tools.support import (
    BoardContext,
    safe,
)

logger = logging.getLogger("yuyutsava.todoboard.tools.artifacts")


def make_artifacts_tools(ctx: BoardContext) -> list[BaseTool]:
    """Build the artifacts tools bound to one exchange."""
    ex, author = ctx.exchange, ctx.author

    @tool
    @safe
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
            card_id,
            kind,
            path=path,
            url=url,
            title=title,
            mime=mime,
            actor=author,
        )
        return f"attached {att.kind} {att.attachment_id} to {card_id}"

    @tool
    @safe
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
        att = await ex.generate_artifact(card_id, block, spec, title=title, actor=author)
        return (
            f"generated {block} artifact {att.attachment_id} ({att.mime}) "
            f"on {card_id}: {Path(att.path).name if att.path else ''}"
        )

    return [
        todo_attach_artifact,
        todo_generate_artifact,
    ]
