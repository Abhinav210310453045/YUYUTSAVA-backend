"""On-disk store for general (non-card) artifacts.

One directory per artifact under ``blobs/artifacts/<id>/`` holding the produced
file and a ``meta.json`` sidecar that IS the record — no DB table, so no
migration. Generation and validation reuse the pluggable block registry in
:mod:`yuyutsava.todoboard.artifacts` (the same generators the TODO board uses),
so a chat artifact and a card artifact of the same kind are byte-identical.

Every function here is synchronous and does blocking file IO; callers on the
event loop wrap them in ``asyncio.to_thread`` (mirroring how the exchange runs
validators and how ``todo_generate_artifact`` runs generators off-loop).
"""

from __future__ import annotations

import json
import mimetypes
import shutil
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from yuyutsava.storage.paths import blobs_dir

mimetypes.add_type("text/markdown", ".md")
mimetypes.add_type("text/jsx", ".jsx")


class ArtifactError(Exception):
    """A bad spec/kind or a failed generation — surfaced to the tool caller."""


# Content-kind → (storage kind, mime, file extension). The storage kind rides
# the same closed vocabulary the frontend block registry keys on: html/jsx land
# as ``artifact`` so SandboxBlock renders them live; the rest as ``file`` so
# TextBlock shows a source preview. Keep in sync with the frontend resolveBlock
# matchers (components/todos/artifactBlocks/*).
CONTENT_KINDS: dict[str, tuple[str, str, str]] = {
    "html": ("artifact", "text/html", ".html"),
    "jsx": ("artifact", "text/jsx", ".jsx"),
    "markdown": ("file", "text/markdown", ".md"),
    "text": ("file", "text/plain", ".txt"),
    "code": ("file", "text/plain", ".txt"),
    "csv": ("file", "text/csv", ".csv"),
    "json": ("file", "application/json", ".json"),
}


class ArtifactRecordV1(BaseModel):
    """One general artifact — the meta.json sidecar and the wire/record shape.

    Mirrors :class:`~yuyutsava.todoboard.models.TodoAttachmentV1` minus the
    card binding so the same frontend blocks render it. ``attachment_id`` is an
    alias of ``artifact_id`` in the serialized form the frontend consumes, but
    the record itself keeps the artifact-native name.
    """

    schema_version: Literal[1] = 1
    artifact_id: str
    kind: str
    path: str | None = None
    url: str | None = None
    mime: str | None = None
    title: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    thread_id: str | None = None
    created_ts: float


def artifacts_root() -> Path:
    return blobs_dir() / "artifacts"


def _artifact_dir(artifact_id: str) -> Path:
    return artifacts_root() / artifact_id


def _url(artifact_id: str) -> str:
    return f"/artifacts/{artifact_id}"


def _new_id() -> str:
    from ulid import ULID

    return str(ULID())


def _kind_for_mime(mime: str | None) -> str:
    m = mime or ""
    if m.startswith("image/"):
        return "image"
    if m.startswith("video/"):
        return "video"
    if m in ("text/html", "text/jsx"):
        return "artifact"
    return "file"


def _record(
    artifact_id: str,
    kind: str,
    path: Path,
    mime: str | None,
    title: str | None,
    meta: dict[str, Any],
    thread_id: str | None,
) -> ArtifactRecordV1:
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    rec = ArtifactRecordV1(
        artifact_id=artifact_id,
        kind=kind,
        path=str(path),
        url=_url(artifact_id),
        mime=mime,
        title=title,
        meta={**meta, **({"size": size} if size is not None else {})},
        thread_id=thread_id,
        created_ts=time.time(),
    )
    (_artifact_dir(artifact_id) / "meta.json").write_text(
        rec.model_dump_json(), encoding="utf-8"
    )
    return rec


def create_from_content(
    content_kind: str,
    content: str,
    *,
    title: str | None = None,
    thread_id: str | None = None,
) -> ArtifactRecordV1:
    """Write a content artifact (html/jsx/markdown/text/code/csv/json) to disk."""
    spec = CONTENT_KINDS.get(content_kind)
    if spec is None:
        raise ArtifactError(
            f"unknown content kind {content_kind!r} "
            f"(one of: {', '.join(sorted(CONTENT_KINDS))})"
        )
    if not content:
        raise ArtifactError(f"{content_kind} artifacts need non-empty content")
    kind, mime, ext = spec
    artifact_id = _new_id()
    d = _artifact_dir(artifact_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"artifact{ext}"
    path.write_text(content, encoding="utf-8")
    return _record(
        artifact_id, kind, path, mime, title,
        {"source": "content", "content_kind": content_kind}, thread_id,
    )


def generate(
    block_name: str,
    spec: dict[str, Any] | None,
    *,
    title: str | None = None,
    thread_id: str | None = None,
) -> ArtifactRecordV1:
    """Run a generative block (e.g. ``audio``) into a fresh artifact dir."""
    from yuyutsava.todoboard.artifacts import blocks

    gen = next((b for b in blocks() if b.name == block_name), None)
    if gen is None or gen.generate is None:
        names = ", ".join(sorted(b.name for b in blocks() if b.generate))
        raise ArtifactError(
            f"no generative block named {block_name!r} (available: {names})"
        )
    artifact_id = _new_id()
    d = _artifact_dir(artifact_id)
    d.mkdir(parents=True, exist_ok=True)
    path, mime = gen.generate(dict(spec or {}), d)
    return _record(
        artifact_id, gen.kind, Path(path), mime, title,
        {"source": "generate", "block": block_name}, thread_id,
    )


def attach_file(
    src: str | Path,
    *,
    kind: str | None = None,
    mime: str | None = None,
    title: str | None = None,
    meta: dict[str, Any] | None = None,
    thread_id: str | None = None,
) -> ArtifactRecordV1:
    """Copy an already-produced file into the store (used by delegation, where a
    subagent wrote the file into its own workspace)."""
    src = Path(src)
    if not src.is_file():
        raise ArtifactError(f"artifact source file not found: {src}")
    mime = mime or mimetypes.guess_type(str(src))[0]
    kind = kind or _kind_for_mime(mime)
    artifact_id = _new_id()
    d = _artifact_dir(artifact_id)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / src.name
    shutil.copy2(src, dest)
    return _record(
        artifact_id, kind, dest, mime, title,
        {"source": "attach", **(meta or {})}, thread_id,
    )


def load_record(artifact_id: str) -> ArtifactRecordV1 | None:
    """Read an artifact's sidecar record, or None if it doesn't exist."""
    # Guard against path traversal: ids are ULIDs (Crockford base32).
    if not artifact_id or not artifact_id.replace("-", "").isalnum():
        return None
    meta = _artifact_dir(artifact_id) / "meta.json"
    if not meta.is_file():
        return None
    try:
        return ArtifactRecordV1.model_validate_json(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_records(limit: int = 200) -> list[ArtifactRecordV1]:
    """Every general artifact, newest first — one meta.json per subdir.

    The sidecars ARE the index (no DB table), so this scans the store root and
    reads each record. Cheap for the handful an interactive session produces;
    the gallery caps it at ``limit``. Blocking IO — call via ``asyncio.to_thread``.
    """
    root = artifacts_root()
    if not root.is_dir():
        return []
    recs = [
        rec
        for d in root.iterdir()
        if d.is_dir() and (rec := load_record(d.name)) is not None
    ]
    recs.sort(key=lambda r: r.created_ts, reverse=True)
    return recs[:limit]
