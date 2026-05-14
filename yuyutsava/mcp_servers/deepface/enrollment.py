"""Identity enrollment: image path(s) → embedding(s) → store rows.

We use one DeepFace model name across detect / enroll / identify so that
embeddings stored at enrollment time are comparable to query embeddings at
match time. Mixing models is meaningless (different vector spaces); the store
also filters by model on lookup.
"""

from __future__ import annotations

import logging

from yuyutsava.mcp_servers.deepface.store import EmbeddingStore

logger = logging.getLogger("yuyutsava.mcp_servers.deepface.enrollment")

DEFAULT_MODEL = "Facenet512"
DEFAULT_DETECTOR = "opencv"


def enroll(
    store: EmbeddingStore,
    identity: str,
    image_paths: list[str],
    *,
    model_name: str = DEFAULT_MODEL,
    detector_backend: str = DEFAULT_DETECTOR,
) -> dict:
    """Embed each image and insert into *store* under *identity*.

    Returns a small report dict the MCP tool can serialise back to the caller.
    Per-image errors are collected, not raised — partial success is preferred
    over total failure when the user supplied several samples.
    """
    if not identity.strip():
        raise ValueError("identity must be a non-empty string")
    if not image_paths:
        raise ValueError("at least one image_path is required")

    DeepFace = _import_deepface()
    added: list[int] = []
    errors: list[dict] = []

    for path in image_paths:
        try:
            reps = DeepFace.represent(
                img_path=path,
                model_name=model_name,
                detector_backend=detector_backend,
                enforce_detection=False,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append({"image": path, "error": str(exc)})
            continue

        # ``represent`` returns a list (one entry per face).
        if not reps:
            errors.append({"image": path, "error": "no face found"})
            continue

        # Use the first face only; multi-face enrollment per sample is
        # ambiguous (which face is the identity?).
        emb = reps[0].get("embedding") if isinstance(reps[0], dict) else None
        if not emb:
            errors.append({"image": path, "error": "embedding missing in deepface result"})
            continue

        rid = store.add(identity, [float(x) for x in emb], model=model_name)
        added.append(rid)

    return {
        "identity": identity,
        "model": model_name,
        "added": len(added),
        "row_ids": added,
        "errors": errors,
    }


def _import_deepface():
    try:
        from deepface import DeepFace  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "deepface is not installed. Install with `uv add 'yuyutsava[deepface]'`."
        ) from exc
    return DeepFace
