"""Render an ``artifact`` StreamEvent into the terminal transcript.

The desktop app shows artifacts as cards that open to a big view. In the
terminal there is no card, and until now the renderer printed a single grey
line — so a table the agent built as an HTML artifact (because a saved memory
says the user prefers HTML tables) simply never appeared, and the user asked
where it went.

**This module is display-only, and that is load-bearing.** ``artifact_create``
returns only ``{artifact_id, kind, mime, title, path, url}``; the body is on
disk and never in the tool result. Reading it here therefore costs zero
context. Nothing in this file touches a ``StreamEvent`` payload, a message, or
graph state: feeding a freshly created artifact back into the prompt would
duplicate what the agent just wrote, on every later call, for no benefit —
the agent already knows what it made.

Renderable kinds go inline inside a bounded box; html/jsx/audio cannot be
drawn in a terminal, so those print their path with an ``open`` hint.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# One artifact's inline budget. Beyond this the box shows a head and points at
# the file: a 200k-char document should not scroll the transcript away.
INLINE_CHARS = 4_000

# kind/content-kind → (pygments lexer, needs_syntax_highlighting)
_LEXERS: dict[str, str] = {
    "json": "json",
    "csv": "text",
    "code": "text",
    "jsx": "jsx",
    "html": "html",
}

# Rendered inline; everything else is announced with its path.
_INLINE_KINDS = frozenset({"markdown", "text", "code", "csv", "json"})


def artifact_summary(data: dict) -> tuple[str, str]:
    """``(title, kind)`` for a one-line announcement. Never raises."""
    title = str(data.get("title") or data.get("artifact_id") or "artifact")
    kind = str(data.get("content_kind") or data.get("kind") or "")
    return title, kind


def load_artifact(data: dict) -> dict[str, Any] | None:
    """Resolve an ``artifact`` event to ``{title, kind, path, text}``.

    ``kind`` is the *content* kind (markdown/text/code/csv/json/html/jsx/audio)
    when the record knows it, which is finer-grained than the event's
    registry-block kind (html and jsx both arrive as ``"artifact"``). Returns
    ``None`` when the record or file cannot be read — the caller then falls
    back to the one-line form.
    """
    artifact_id = str(data.get("artifact_id") or data.get("attachment_id") or "")
    if not artifact_id:
        return None
    try:
        from yuyutsava.artifacts import store

        rec = store.load_record(artifact_id)
    except Exception:  # noqa: BLE001 — rendering must never break a turn
        return None
    if rec is None:
        return None

    meta = rec.meta if isinstance(rec.meta, dict) else {}
    kind = str(meta.get("content_kind") or rec.kind or "")
    path = Path(rec.path) if rec.path else None
    out: dict[str, Any] = {
        "artifact_id": artifact_id,
        "title": rec.title or artifact_id,
        "kind": kind,
        "mime": rec.mime or "",
        "path": path,
        "text": "",
        "clipped": False,
    }
    if kind in _INLINE_KINDS and path is not None:
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return out
        if kind == "json":
            # Pretty-print so a one-line dump is actually readable.
            try:
                body = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
            except ValueError:
                pass
        if len(body) > INLINE_CHARS:
            out["text"] = body[:INLINE_CHARS]
            out["clipped"] = True
        else:
            out["text"] = body
    return out


def artifact_renderable(info: dict[str, Any]) -> Any:
    """A bounded, titled Rich panel for one artifact.

    Imported lazily by the rich renderer only — the plain renderer must not
    pull Rich in.
    """
    from rich.console import Group
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.text import Text

    kind = info["kind"]
    body: Any
    if not info["text"]:
        hint = (
            f"cannot be displayed in a terminal — open it with:\n  open {info['path']}"
            if info["path"] else "no readable content"
        )
        body = Text(hint, style="chrome")
    elif kind == "markdown":
        body = Markdown(info["text"])
    elif kind in _LEXERS:
        body = Syntax(
            info["text"], _LEXERS.get(kind, "text"),
            theme="ansi_dark", word_wrap=True, background_color="default",
        )
    else:
        body = Text(info["text"])

    parts: list[Any] = [body]
    if info["clipped"]:
        parts.append(
            Text(
                f"… clipped at {INLINE_CHARS:,} chars — full file: {info['path']}",
                style="chrome",
            )
        )

    subtitle = f"{kind or info['mime']} · {info['artifact_id']}"
    return Panel(
        Group(*parts),
        title=f"◨ {info['title']}",
        subtitle=subtitle,
        title_align="left",
        subtitle_align="right",
        border_style="accent",
        padding=(1, 2),
    )


__all__ = [
    "INLINE_CHARS",
    "artifact_renderable",
    "artifact_summary",
    "load_artifact",
]
