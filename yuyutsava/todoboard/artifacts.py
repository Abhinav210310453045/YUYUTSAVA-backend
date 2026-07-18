"""Pluggable artifact-block registry for TODO-card attachments.

An :class:`ArtifactBlock` bundles everything the board needs to accept one
family of attachments: the storage ``kind`` its rows carry, the mimes it
claims (for validation and for the upload endpoint's allowlist), a validator,
and an optional ``generate(spec, out_dir)`` that produces the file itself
(the diagram block delegates to the existing ``yuyutsava.visuals`` renderers).

Dispatch is loose-coupled: ``TodoExchange.attach()`` and the upload endpoint
resolve a block by ``(kind, mime)`` and never special-case any kind — adding
a block later (Phase 7: JSX sandbox, audio) is one :func:`register_block`
call plus one frontend module, with zero edits to exchange/store/router.
The storage ``kind`` column keeps the closed V1 vocabulary
(:data:`~yuyutsava.todoboard.models.ATTACHMENT_KINDS` — it is part of the
versioned schema and the DB CHECK constraint); new blocks refine behavior
*within* those kinds by mime, riding on ``artifact``/``file`` as umbrellas.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from yuyutsava.todoboard.exchange import TodoAttachmentError, TodoValidationError

# Python < 3.13 doesn't map .md — agent-side attaches (no browser mime) need
# the inference so the frontend text block can claim markdown files.
mimetypes.add_type("text/markdown", ".md")
mimetypes.add_type("text/markdown", ".markdown")

# A validator receives the attach() inputs and returns the (possibly inferred)
# mime to store. It runs off the event loop (the exchange wraps it in
# asyncio.to_thread) so it may touch the filesystem.
Validator = Callable[..., "str | None"]

# generate(spec, out_dir) -> (path, mime): render an artifact file into the
# card workspace so the caller can attach() it. Optional per block.
Generator = Callable[[dict[str, Any], Path], "tuple[Path, str]"]


@dataclass(frozen=True)
class ArtifactBlock:
    """One attachment family the board knows how to validate/serve/render."""

    name: str                          # unique block id (registry key)
    kind: str                          # storage kind its rows carry
    validate: Validator
    mimes: tuple[str, ...] = ()        # mimes this block claims; "image/*" =
                                       # prefix wildcard; () = any mime of its kind
    upload_mimes: tuple[str, ...] = () # subset accepted by the multipart upload
                                       # endpoint; () = not user-uploadable
    generate: Generator | None = None
    needs_context: bool = False        # generate() wants the hydrated card +
                                       # timeline injected into its spec (the
                                       # dispatcher collects them ON the event
                                       # loop — generators run in a thread and
                                       # must never touch the loop-bound store)
    singleton: bool = False            # at most one generated attachment of
                                       # this block per card — regeneration
                                       # updates the existing row in place
                                       # instead of attaching a duplicate


def _mime_matches(mime: str | None, patterns: tuple[str, ...]) -> bool:
    if not mime:
        return False
    return any(
        mime == p or (p.endswith("/*") and mime.startswith(p[:-1]))
        for p in patterns
    )


# ── validators ──────────────────────────────────────────────────────────

def _existing_file(path: str | None, kind: str) -> Path:
    if not path:
        raise TodoValidationError(f"{kind} attachments require a file path")
    p = Path(path)
    if not p.is_file():
        raise TodoAttachmentError(f"attachment file not found: {path}")
    return p


def _file_validator(kind: str, family: str | None = None) -> Validator:
    """Path must be an existing file; mime is inferred from the suffix when
    the caller didn't send one; ``family`` pins the mime's major type."""

    def validate(*, path: str | None = None, url: str | None = None,
                 mime: str | None = None) -> str | None:
        p = _existing_file(path, kind)
        mime = mime or mimetypes.guess_type(str(p))[0]
        if family and not (mime or "").startswith(family + "/"):
            raise TodoValidationError(
                f"{kind} attachments must carry a {family}/* mime (got {mime!r})"
            )
        return mime

    return validate


def _link_validator(*, path: str | None = None, url: str | None = None,
                    mime: str | None = None) -> str | None:
    if not url:
        raise TodoValidationError("link attachments require a url")
    if not str(url).startswith(("http://", "https://")):
        raise TodoValidationError("link url must be http(s)")
    return mime


# ── generators ──────────────────────────────────────────────────────────

def _generate_visual(spec: dict[str, Any], out_dir: Path) -> tuple[Path, str]:
    """Render a visual spec (``{"kind": "diagram"|"chart"|..., ...}``) into
    *out_dir* via the shared visuals dispatcher. Lazy import — the renderers
    pull matplotlib and friends only when actually used."""
    from ulid import ULID

    from yuyutsava.visuals.render import render

    vis_kind = str(spec.get("kind", "diagram"))
    result = render(vis_kind, {k: v for k, v in spec.items() if k != "kind"})
    ext = ".svg" if "svg" in result.mime else ".png"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{vis_kind}_{ULID()}{ext}"
    path.write_bytes(result.image_bytes)
    return path, result.mime


# ── registry ────────────────────────────────────────────────────────────

_REGISTRY: dict[str, ArtifactBlock] = {}


def register_block(block: ArtifactBlock) -> None:
    _REGISTRY[block.name] = block


def blocks() -> tuple[ArtifactBlock, ...]:
    return tuple(_REGISTRY.values())


def resolve_block(kind: str, mime: str | None = None) -> ArtifactBlock:
    """Pick the block handling *(kind, mime)*: mime-specific blocks win over a
    kind's catch-all; an unregistered kind is a validation error."""
    of_kind = [b for b in _REGISTRY.values() if b.kind == kind]
    if not of_kind:
        raise TodoValidationError(f"no artifact block registered for kind {kind!r}")
    for b in of_kind:
        if b.mimes and _mime_matches(mime, b.mimes):
            return b
    for b in of_kind:
        if not b.mimes:
            return b
    return of_kind[0]  # mime unknown/absent — the validator will infer or reject


def upload_mime_allowed(mime: str | None) -> bool:
    """The multipart endpoint's allowlist — the union of every block's
    ``upload_mimes``."""
    return any(_mime_matches(mime, b.upload_mimes) for b in _REGISTRY.values())


def kind_for_upload(mime: str | None) -> str | None:
    """Storage kind for an uploaded file, from the first block claiming its
    mime (registration order breaks ties)."""
    for b in _REGISTRY.values():
        if _mime_matches(mime, b.upload_mimes):
            return b.kind
    return None


# ── v1 blocks ───────────────────────────────────────────────────────────
# Registration order matters twice: mime-specific blocks of a kind must come
# before that kind's catch-all (resolve_block), and kind_for_upload takes the
# first claimant.

_TEXT_MIMES = (
    "text/plain", "text/markdown", "text/html", "text/csv", "application/json",
)

register_block(ArtifactBlock(
    name="text", kind="file",
    validate=_file_validator("file"),
    mimes=_TEXT_MIMES, upload_mimes=_TEXT_MIMES,
))
register_block(ArtifactBlock(
    name="image", kind="image",
    validate=_file_validator("image", family="image"),
    mimes=("image/*",),
    upload_mimes=("image/png", "image/jpeg", "image/gif", "image/webp",
                  "image/svg+xml"),
))
register_block(ArtifactBlock(
    name="video", kind="video",
    validate=_file_validator("video", family="video"),
    mimes=("video/*",),
    upload_mimes=("video/mp4", "video/webm", "video/quicktime"),
))
register_block(ArtifactBlock(
    name="link", kind="link",
    validate=_link_validator,
))
register_block(ArtifactBlock(
    name="diagram", kind="diagram",
    validate=_file_validator("diagram", family="image"),
    mimes=("image/*",),
    generate=_generate_visual,
))
register_block(ArtifactBlock(
    name="artifact", kind="artifact",
    validate=_file_validator("artifact"),
))
register_block(ArtifactBlock(
    name="file", kind="file",  # non-text files: the generic download-tile kind
    validate=_file_validator("file"),
    upload_mimes=("application/pdf", "application/zip"),
))

# ── Phase-7 blocks ──────────────────────────────────────────────────────
# Each lives in its own module; these lines are its entire registration —
# the pluggability contract in action (the frontend twin is one entry in
# components/todos/artifactBlocks/index.js). Registered after the v1 blocks:
# both refine a kind by mime, so resolve_block's mime-specific pass finds
# them regardless of order, and their mimes are unclaimed by earlier blocks.
from yuyutsava.todoboard.block_audio import AUDIO_BLOCK  # noqa: E402
from yuyutsava.todoboard.block_jsx import JSX_BLOCK  # noqa: E402
from yuyutsava.todoboard.block_journey import JOURNEY_BLOCK  # noqa: E402

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
