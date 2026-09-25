"""Journey artifact block: the "journey of the plan" document for one card.

Same pluggability contract as block_audio/block_jsx — this module plus one
``register_block`` entry in ``artifacts.py``, zero edits to exchange/store/
router/tools. Rows ride the closed V1 ``artifact`` kind refined by the
``text/html`` mime, which the frontend's SandboxBlock already renders, so no
frontend twin is needed either.

The generator is a DETERMINISTIC compiler: it renders the hydrated card +
activity timeline (injected into ``spec`` by the exchange dispatcher because
``needs_context=True`` — generators run in a thread and must never call the
loop-bound store) into one self-contained HTML page. The "agent's thoughts"
half of the hybrid comes from the tinker's prompt convention: it writes a
note starting with ``## Reflection`` before generating, and this compiler
lifts those notes into a lead section.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any

from ulid import ULID

from yuyutsava.todoboard.block_registry import ArtifactBlock, file_validator
from yuyutsava.todoboard.errors import TodoValidationError

REFLECTION_MARKER = "## Reflection"

# Flow order for the objective sections — mainline first, off-ramps after,
# finished work last. Empty phases are omitted from the document.
_PHASE_ORDER = ("thinking", "planning", "doing", "blocked", "completed", "abandoned")

_PHASE_COLOR = {
    "thinking": "#b48cff",
    "planning": "#6fa8ff",
    "doing": "#ffc36f",
    "completed": "#5fe0a0",
    "blocked": "#ff7a7a",
    "abandoned": "#8a8f98",
}

_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; margin: 0; }
body { background: #0d1117; color: #d7dce2; padding: 32px 8vw 64px;
       font: 14px/1.6 "SF Mono", ui-monospace, Menlo, Consolas, monospace; }
h1 { font-size: 22px; color: #f2f5f8; margin-bottom: 4px; }
h2 { font-size: 13px; letter-spacing: 2px; text-transform: uppercase;
     color: #7ee0a3; margin: 36px 0 12px; border-bottom: 1px solid #232a33;
     padding-bottom: 6px; }
.meta { color: #8a939e; font-size: 12px; margin-bottom: 4px; }
.pill { display: inline-block; padding: 1px 10px; border-radius: 999px;
        font-size: 11px; border: 1px solid; margin-right: 6px; }
.tag { color: #8fb8ff; font-size: 12px; margin-right: 8px; }
.reflection { background: #131a23; border-left: 3px solid #7ee0a3;
              padding: 14px 18px; border-radius: 6px; margin-bottom: 10px;
              white-space: pre-wrap; }
.phase-h { font-size: 12px; letter-spacing: 1px; text-transform: uppercase;
           margin: 20px 0 8px; }
.obj { background: #11161d; border: 1px solid #232a33; border-radius: 8px;
       padding: 12px 16px; margin-bottom: 10px; }
.obj .t { color: #eef2f6; font-weight: 600; }
.why { color: #c8a25f; font-size: 12px; margin-top: 4px; }
.won { color: #5fe0a0; font-size: 12px; margin-top: 4px; }
.note { border-left: 2px solid #2c3540; padding: 6px 12px; margin: 8px 0 0 6px;
        white-space: pre-wrap; color: #b8c0c9; }
.note .a { font-size: 11px; color: #7a828c; }
.tl { list-style: none; padding-left: 0; }
.tl li { padding: 4px 0 4px 18px; position: relative; color: #aeb6bf; }
.tl li::before { content: ""; position: absolute; left: 4px; top: 11px;
                 width: 6px; height: 6px; border-radius: 50%; background: #3a4450; }
.tl .ts { color: #6b737d; font-size: 11px; margin-right: 8px; }
.gallery li { color: #aeb6bf; }
.footer { margin-top: 48px; color: #5d656f; font-size: 11px; }
"""


def _esc(v: Any) -> str:
    return html.escape(str(v if v is not None else ""))


def _when(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "?"


def _humanize(e: dict[str, Any]) -> str:
    p = e.get("payload") or {}
    title = p.get("title") or p.get("attachment_id") or ""
    kind = e.get("kind")
    if kind == "card_status":
        return f"card moved {p.get('from')} → {p.get('to')}"
    if kind == "objective_created":
        return f"objective added: “{title}” [{p.get('phase')}]"
    if kind == "objective_phase":
        line = f"“{title}”: {p.get('from')} → {p.get('to')}"
        return line + (f" — {p['reason']}" if p.get("reason") else "")
    if kind == "objective_updated":
        return f"“{title}”: {', '.join(p.get('fields', []))} changed"
    if kind == "objective_deleted":
        return f"objective removed: “{title}”"
    if kind == "note_assigned":
        return "a note was assigned to an objective"
    if kind == "artifact_attached":
        return f"attached {p.get('kind')}: {title or p.get('attachment_id', '')}"
    if kind == "journey_generated":
        return "journey document generated"
    return str(kind)


def _note_html(n: dict[str, Any]) -> str:
    when = _when(n.get("updated_ts"))
    return (
        f'<div class="note"><span class="a">[{_esc(n.get("author"))} · {when}]'
        f"</span><br>{_esc(n.get('body'))}</div>"
    )


def _header_html(card: dict[str, Any]) -> list[str]:
    status = card.get("status", "inbox")
    return [
        f"<h1>{_esc(card.get('title'))}</h1>",
        "<div class='meta'>"
        f"<span class='pill' style='color:#8fb8ff;border-color:#8fb8ff'>{_esc(status)}</span>"
        + "".join(f"<span class='tag'>#{_esc(t)}</span>" for t in card.get("tags") or [])
        + "</div>",
        f"<div class='meta'>created {_when(card.get('created_ts'))} · "
        f"last activity {_when(card.get('updated_ts'))} · "
        f"journey compiled {datetime.now().strftime('%Y-%m-%d %H:%M')}</div>",
    ]


def _reflections(notes: list[dict]) -> list[dict]:
    """The tinker's reflection notes — the hybrid's narrative half."""
    return [
        n
        for n in notes
        if n.get("author") == "tinker"
        and str(n.get("body", "")).lstrip().startswith(REFLECTION_MARKER)
    ]


def _reflection_html(reflections: list[dict]) -> list[str]:
    if not reflections:
        return []
    out = ["<h2>Reflection</h2>"]
    for n in reflections:
        body = str(n.get("body", "")).lstrip()[len(REFLECTION_MARKER) :].strip()
        out.append(
            f"<div class='reflection'><span class='a' style='color:#7a828c;"
            f"font-size:11px'>tinker · {_when(n.get('updated_ts'))}</span>\n"
            f"{_esc(body)}</div>"
        )
    return out


def _objective_html(o: dict, phase: str, color: str, notes: list[dict]) -> list[str]:
    out = [
        "<div class='obj'>",
        f"<span class='pill' style='color:{color};border-color:{color}'>"
        f"{phase}</span><span class='t'>{_esc(o.get('title'))}</span>",
    ]
    if o.get("reason"):
        out.append(f"<div class='why'>why: {_esc(o['reason'])}</div>")
    if o.get("outcome"):
        out.append(f"<div class='won'>outcome: {_esc(o['outcome'])}</div>")
    out.extend(_note_html(n) for n in notes)
    out.append("</div>")
    return out


def _objectives_html(objectives: list[dict], notes: list[dict]) -> list[str]:
    """Objectives grouped by phase, each with the notes assigned to it."""
    if not objectives:
        return []
    done = sum(1 for o in objectives if o.get("phase") == "completed")
    out = [f"<h2>Objectives — {done}/{len(objectives)} completed</h2>"]
    by_objective: dict[str, list[dict]] = {}
    for n in notes:
        if n.get("objective_id"):
            by_objective.setdefault(n["objective_id"], []).append(n)
    for phase in _PHASE_ORDER:
        in_phase = [o for o in objectives if o.get("phase") == phase]
        if not in_phase:
            continue
        color = _PHASE_COLOR.get(phase, "#8a8f98")
        out.append(f"<div class='phase-h' style='color:{color}'>{phase}</div>")
        for o in in_phase:
            out.extend(
                _objective_html(o, phase, color, by_objective.get(o.get("objective_id"), []))
            )
    return out


def _general_notes_html(notes: list[dict], reflections: list[dict]) -> list[str]:
    """Notes on no objective, reflections excluded — they are shown above."""
    shown = {id(n) for n in reflections}
    general = [n for n in notes if not n.get("objective_id") and id(n) not in shown]
    if not general:
        return []
    return ["<h2>General notes</h2>", *(_note_html(n) for n in general)]


def _timeline_html(events: list[dict]) -> list[str]:
    if not events:
        return []
    return [
        "<h2>Journey timeline</h2><ul class='tl'>",
        *(
            f"<li><span class='ts'>{_when(e.get('created_ts'))}</span>"
            f"[{_esc(e.get('actor'))}] {_esc(_humanize(e))}</li>"
            for e in events
        ),
        "</ul>",
    ]


def _gallery_html(attachments: list[dict]) -> list[str]:
    """The card's artifacts by name — the files live on the card."""
    listable = [a for a in attachments if (a.get("meta") or {}).get("block") != "journey"]
    if not listable:
        return []
    return [
        "<h2>Artifacts on this card</h2><ul class='gallery'>",
        *(
            f"<li>[{_esc(a.get('kind'))}] "
            f"{_esc(a.get('title') or Path(str(a.get('path') or a.get('url') or '')).name)}</li>"
            for a in listable
        ),
        "</ul>",
    ]


def _generate_journey(spec: dict[str, Any], out_dir: Path) -> tuple[Path, str]:
    """Compile the injected card + events into ``journey_<ULID>.html``."""
    card = spec.get("card")
    if not isinstance(card, dict):
        raise TodoValidationError(
            "journey generation needs the card context — call it via "
            "todo_generate_artifact / the generate endpoint, not with a raw spec"
        )
    events = [e for e in spec.get("events") or [] if isinstance(e, dict)]
    notes = card.get("notes") or []
    reflections = _reflections(notes)
    out: list[str] = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>Journey — {_esc(card.get('title'))}</title>",
        f"<style>{_CSS}</style></head><body>",
        *_header_html(card),
        *_reflection_html(reflections),
        *_objectives_html(card.get("objectives") or [], notes),
        *_general_notes_html(notes, reflections),
        *_timeline_html(events),
        *_gallery_html(card.get("attachments") or []),
        f"<div class='footer'>journey of {_esc(card.get('card_id'))} — "
        "compiled from the card's think flow by the TODO board</div></body></html>",
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"journey_{ULID()}.html"
    path.write_text("\n".join(out), encoding="utf-8")
    return path, "text/html"


JOURNEY_BLOCK = ArtifactBlock(
    name="journey",
    kind="artifact",  # closed V1 vocabulary: rides "artifact" by mime
    validate=file_validator("artifact"),
    mimes=("text/html",),
    generate=_generate_journey,
    needs_context=True,
    # One journey per card: regeneration refreshes the existing attachment
    # (the generator still writes a fresh file; the exchange swaps + unlinks).
    singleton=True,
)

__all__ = ["JOURNEY_BLOCK", "REFLECTION_MARKER"]
