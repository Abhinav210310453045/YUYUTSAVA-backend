"""General (non-card) artifact store for inline chat & voice artifacts.

The TODO board can pin rich pluggable artifacts (JSX sandbox, audio, HTML,
diagram, text) to a card. This package lets the master/tinker produce the SAME
block types for an inline chat/voice reply — not bound to any card — reusing the
one artifact-block registry in :mod:`yuyutsava.todoboard.artifacts`. Each
artifact is a directory under ``blobs/artifacts/<id>/`` holding the produced
file plus a ``meta.json`` sidecar (the record — no SQLite table). Served at
``/artifacts/{id}`` and rendered by the frontend's shared block registry.
"""

from yuyutsava.artifacts.store import (
    ArtifactError,
    ArtifactRecordV1,
    CONTENT_KINDS,
    artifacts_root,
    create_from_content,
    generate,
    attach_file,
    load_record,
)

__all__ = [
    "ArtifactError",
    "ArtifactRecordV1",
    "CONTENT_KINDS",
    "artifacts_root",
    "create_from_content",
    "generate",
    "attach_file",
    "load_record",
]
