"""The ``artifact_create`` tool — the master/tinker's inline-artifact maker.

Produces a rich artifact (interactive HTML/JSX, a Markdown/text/CSV/JSON
document, a code snippet, or a spoken audio note) into the general artifact
store and returns a JSON record. The streaming layer turns that record into an
``artifact`` StreamEvent so it renders inline in the SAME chat/voice reply
(see :func:`yuyutsava.core.streaming._artifact_event_from_result`), and the
frontend's shared block registry renders/opens it — no card required.

Charts, data plots, and diagrams already have first-class tools (``vis_*``);
this tool is for the interactive/document/audio artifacts those don't cover.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from langchain_core.tools import BaseTool, tool

from yuyutsava.artifacts import store

logger = logging.getLogger("yuyutsava.artifacts.tools")

# Generative (non-content) kinds → the registry block that makes them.
_GENERATIVE = {"audio": "audio"}


def _thread_id() -> str | None:
    try:
        from yuyutsava.context.artifacts import thread_id_from_runtime

        return thread_id_from_runtime()
    except Exception:  # noqa: BLE001 — best-effort tagging, never fail the tool
        return None


def make_artifact_tools() -> list[BaseTool]:
    """Build the artifact-creation tool family (currently just artifact_create).

    Safe to call unconditionally, like ``make_visual_tools`` — it needs no
    daemon-injected state (the store is a filesystem path).
    """

    @tool
    async def artifact_create(
        kind: str,
        content: str | None = None,
        spec: dict[str, Any] | str | None = None,
        title: str | None = None,
    ) -> str:
        """Create a rich artifact and show it inline in this reply.

        Use this to hand the user something interactive or substantial that
        plain text can't carry — it appears as a card in the chat bubble and
        opens to a big view. For data charts, diagrams, tables, code images,
        and math, use the ``vis_*`` tools instead.

        kind:
          "html"     — a self-contained HTML document/app (runs live, sandboxed;
                       must be fully self-contained: inline CSS/JS, no network)
          "jsx"      — a React component in JSX (export default a component;
                       transpiled and run live against the app's React)
          "markdown" — a formatted Markdown document
          "text"     — a plain-text document
          "code"     — a code snippet (shown as a scrollable source block)
          "csv"      — CSV data
          "json"     — a JSON document
          "audio"    — a spoken voice note (TTS); pass spec={"text": "words"}

        content: the artifact body for every kind EXCEPT audio.
        spec:    JSON object for generative kinds (audio: {"text": ...}). A
                 JSON-encoded string is accepted too.
        title:   short label shown on the artifact card (optional).

        Returns JSON: {"status":"ok", artifact_id, kind, mime, title, path, url}
        on success, or {"status":"error", "error": ...}.
        """
        tid = _thread_id()
        try:
            if kind in _GENERATIVE:
                if isinstance(spec, str):
                    try:
                        spec = json.loads(spec)
                    except json.JSONDecodeError:
                        return json.dumps({
                            "status": "error",
                            "error": 'spec must be a JSON object, e.g. {"text": "words to speak"}',
                        })
                if spec is not None and not isinstance(spec, dict):
                    return json.dumps({
                        "status": "error",
                        "error": "spec must be a JSON object, not a list/scalar",
                    })
                rec = await asyncio.to_thread(
                    store.generate, _GENERATIVE[kind], dict(spec or {}),
                    title=title, thread_id=tid,
                )
            elif kind in store.CONTENT_KINDS:
                if not content:
                    return json.dumps({
                        "status": "error",
                        "error": f"{kind} artifacts need the `content` argument",
                    })
                rec = await asyncio.to_thread(
                    store.create_from_content, kind, content,
                    title=title, thread_id=tid,
                )
            else:
                kinds = ", ".join(sorted({*store.CONTENT_KINDS, *_GENERATIVE}))
                return json.dumps({
                    "status": "error",
                    "error": f"unknown artifact kind {kind!r} (one of: {kinds})",
                })
        except store.ArtifactError as exc:
            return json.dumps({"status": "error", "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 — a broken generator must not crash the turn
            logger.exception("artifact_create failed for kind=%s", kind)
            return json.dumps({"status": "error", "error": f"artifact creation failed: {exc}"})

        return _ok(rec)

    @tool
    async def artifact_show(artifact_id: str) -> str:
        """Re-show an EXISTING artifact inline — WITHOUT recreating it.

        Use this to surface an artifact that already exists (e.g. one a
        background subagent you delegated to created and reported by id): it
        re-embeds the saved artifact in your reply instantly, openable big.
        Returns the usual {"status":"ok", artifact_id, kind, mime, title,
        path, url}; an unknown id → error.
        """
        rec = await asyncio.to_thread(store.load_record, artifact_id)
        if rec is None:
            return json.dumps({
                "status": "error",
                "error": f"no artifact with id {artifact_id!r}",
            })
        return _ok(rec)

    return [artifact_create, artifact_show]


def _ok(rec: store.ArtifactRecordV1) -> str:
    return json.dumps({
        "status": "ok",
        "artifact_id": rec.artifact_id,
        "kind": rec.kind,
        "mime": rec.mime,
        "title": rec.title,
        "path": rec.path,
        "url": rec.url,
    })
