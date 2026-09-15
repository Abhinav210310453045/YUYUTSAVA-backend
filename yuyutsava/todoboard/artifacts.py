"""The TODO board's built-in artifact blocks.

Importing this module registers every built-in block, in the order the
registry depends on (see below). The registry itself lives in
:mod:`yuyutsava.todoboard.block_registry`; its API is re-exported here, which
is where the exchange, the upload router and the artifacts store import it
from, so a lookup always sees the built-in blocks.

The diagram block delegates to the existing ``yuyutsava.visuals`` renderers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ulid import ULID

from yuyutsava.todoboard.block_audio import AUDIO_BLOCK
from yuyutsava.todoboard.block_journey import JOURNEY_BLOCK
from yuyutsava.todoboard.block_jsx import JSX_BLOCK
from yuyutsava.todoboard.block_registry import (
    ArtifactBlock,
    blocks,
    file_validator,
    kind_for_upload,
    register_block,
    resolve_block,
    upload_mime_allowed,
)
from yuyutsava.todoboard.exchange import TodoValidationError
from yuyutsava.visuals.render import render

# ── validators ──────────────────────────────────────────────────────────


def _link_validator(
    *, path: str | None = None, url: str | None = None, mime: str | None = None
) -> str | None:
    if not url:
        raise TodoValidationError("link attachments require a url")
    if not str(url).startswith(("http://", "https://")):
        raise TodoValidationError("link url must be http(s)")
    return mime


# ── generators ──────────────────────────────────────────────────────────


def _generate_visual(spec: dict[str, Any], out_dir: Path) -> tuple[Path, str]:
    """Render a visual spec (``{"kind": "diagram"|"chart"|..., ...}``) into
    *out_dir* via the shared visuals dispatcher, which loads matplotlib and
    friends only when a visual is actually rendered."""

    vis_kind = str(spec.get("kind", "diagram"))
    result = render(vis_kind, {k: v for k, v in spec.items() if k != "kind"})
    ext = ".svg" if "svg" in result.mime else ".png"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{vis_kind}_{ULID()}{ext}"
    path.write_bytes(result.image_bytes)
    return path, result.mime


# ── v1 blocks ───────────────────────────────────────────────────────────
# Registration order matters twice: mime-specific blocks of a kind must come
# before that kind's catch-all (resolve_block), and kind_for_upload takes the
# first claimant.

_TEXT_MIMES = (
    "text/plain",
    "text/markdown",
    "text/html",
    "text/csv",
    "application/json",
)

register_block(
    ArtifactBlock(
        name="text",
        kind="file",
        validate=file_validator("file"),
        mimes=_TEXT_MIMES,
        upload_mimes=_TEXT_MIMES,
    )
)
register_block(
    ArtifactBlock(
        name="image",
        kind="image",
        validate=file_validator("image", family="image"),
        mimes=("image/*",),
        upload_mimes=("image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"),
    )
)
register_block(
    ArtifactBlock(
        name="video",
        kind="video",
        validate=file_validator("video", family="video"),
        mimes=("video/*",),
        upload_mimes=("video/mp4", "video/webm", "video/quicktime"),
    )
)
register_block(
    ArtifactBlock(
        name="link",
        kind="link",
        validate=_link_validator,
    )
)
register_block(
    ArtifactBlock(
        name="diagram",
        kind="diagram",
        validate=file_validator("diagram", family="image"),
        mimes=("image/*",),
        generate=_generate_visual,
    )
)
register_block(
    ArtifactBlock(
        name="artifact",
        kind="artifact",
        validate=file_validator("artifact"),
    )
)
register_block(
    ArtifactBlock(
        name="file",
        kind="file",  # non-text files: the generic download-tile kind
        validate=file_validator("file"),
        upload_mimes=("application/pdf", "application/zip"),
    )
)

# ── blocks in their own modules ─────────────────────────────────────────
# Each module's block is registered by one line here — the pluggability
# contract in action (the frontend twin is one entry in
# components/todos/artifactBlocks/index.js). Registered after the v1 blocks:
# they refine a kind by mime, so resolve_block's mime-specific pass finds
# them regardless of order, and their mimes are unclaimed by earlier blocks.
register_block(AUDIO_BLOCK)
register_block(JSX_BLOCK)
register_block(JOURNEY_BLOCK)


__all__ = [
    "ArtifactBlock",
    "register_block",
    "blocks",
    "resolve_block",
    "upload_mime_allowed",
    "kind_for_upload",
]
