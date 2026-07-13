"""JSX sandbox artifact block (backend half).

Phase-7 proof of the pluggability contract (docs/TODO_BOARD_PLAN.md §8): one
module plus one ``register_block`` entry in ``artifacts.py``. Rows ride the
closed V1 ``artifact`` kind refined by the ``text/jsx`` mime (HTML artifacts
already flow as ``file``/``text/html`` through the text block's mimes — the
frontend decides how to render them). All rendering happens in the frontend
twin (``components/todos/artifactBlocks/SandboxBlock.jsx``) inside a hardened
sandboxed iframe; the backend only needs the mime to survive upload/attach
round-trips.
"""

from __future__ import annotations

import mimetypes

from yuyutsava.todoboard.artifacts import ArtifactBlock, _file_validator

# Python's mimetypes has no .jsx mapping on any version — same gap (and same
# fix) as .md in artifacts.py. Agent-side attaches and the upload endpoint's
# filename-based inference both rely on it.
mimetypes.add_type("text/jsx", ".jsx")

JSX_BLOCK = ArtifactBlock(
    name="jsx", kind="artifact",  # closed V1 vocabulary: JSX rides "artifact" by mime
    validate=_file_validator("artifact"),
    mimes=("text/jsx",),
    upload_mimes=("text/jsx",),
)

__all__ = ["JSX_BLOCK"]
