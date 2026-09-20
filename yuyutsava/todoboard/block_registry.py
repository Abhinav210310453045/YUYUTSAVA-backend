"""Pluggable artifact-block registry for TODO-card attachments.

An :class:`ArtifactBlock` bundles everything the board needs to accept one
family of attachments: the storage ``kind`` its rows carry, the mimes it
claims (for validation and for the upload endpoint's allowlist), a validator,
and an optional ``generate(spec, out_dir)`` that produces the file itself.

Dispatch is loose-coupled: ``TodoExchange.attach()`` and the upload endpoint
resolve a block by ``(kind, mime)`` and never special-case any kind — adding
a block is one :func:`register_block` call plus one frontend module, with zero
edits to exchange/store/router. The storage ``kind`` column keeps the closed V1
vocabulary (:data:`~yuyutsava.todoboard.models.ATTACHMENT_KINDS` — it is part
of the versioned schema and the DB CHECK constraint); blocks refine behavior
*within* those kinds by mime, riding on ``artifact``/``file`` as umbrellas.

The built-in blocks are registered by :mod:`yuyutsava.todoboard.artifacts`,
which re-exports this API. Block modules import from here, so they never
import a module that is still registering them.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from yuyutsava.todoboard.errors import TodoAttachmentError, TodoValidationError

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

    name: str  # unique block id (registry key)
    kind: str  # storage kind its rows carry
    validate: Validator
    mimes: tuple[str, ...] = ()  # mimes this block claims; "image/*" =
    # prefix wildcard; () = any mime of its kind
    upload_mimes: tuple[str, ...] = ()  # subset accepted by the multipart upload
    # endpoint; () = not user-uploadable
    generate: Generator | None = None
    needs_context: bool = False  # generate() wants the hydrated card +
    # timeline injected into its spec (the
    # dispatcher collects them ON the event
    # loop — generators run in a thread and
    # must never touch the loop-bound store)
    singleton: bool = False  # at most one generated attachment of
    # this block per card — regeneration
    # updates the existing row in place
    # instead of attaching a duplicate


def _mime_matches(mime: str | None, patterns: tuple[str, ...]) -> bool:
    if not mime:
        return False
    return any(mime == p or (p.endswith("/*") and mime.startswith(p[:-1])) for p in patterns)


def _existing_file(path: str | None, kind: str) -> Path:
    if not path:
        raise TodoValidationError(f"{kind} attachments require a file path")
    p = Path(path)
    if not p.is_file():
        raise TodoAttachmentError(f"attachment file not found: {path}")
    return p


def file_validator(kind: str, family: str | None = None) -> Validator:
    """Path must be an existing file; mime is inferred from the suffix when
    the caller didn't send one; ``family`` pins the mime's major type."""

    def validate(
        *, path: str | None = None, url: str | None = None, mime: str | None = None
    ) -> str | None:
        p = _existing_file(path, kind)
        mime = mime or mimetypes.guess_type(str(p))[0]
        if family and not (mime or "").startswith(family + "/"):
            raise TodoValidationError(
                f"{kind} attachments must carry a {family}/* mime (got {mime!r})"
            )
        return mime

    return validate


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


__all__ = [
    "ArtifactBlock",
    "Generator",
    "Validator",
    "blocks",
    "file_validator",
    "kind_for_upload",
    "register_block",
    "resolve_block",
    "upload_mime_allowed",
]
