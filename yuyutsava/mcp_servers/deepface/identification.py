"""Identify a face by embedding it and finding the closest enrolled identity.

The threshold below is a calibration knob: cosine similarity above it counts
as a match; otherwise the result is "unknown". 0.4 is a conservative default
for Facenet512 — callers can override per-call.
"""

from __future__ import annotations

import logging

from yuyutsava.mcp_servers.deepface.enrollment import DEFAULT_DETECTOR, DEFAULT_MODEL
from yuyutsava.mcp_servers.deepface.store import EmbeddingStore, Match

logger = logging.getLogger("yuyutsava.mcp_servers.deepface.identification")

DEFAULT_THRESHOLD = 0.40


def identify(
    store: EmbeddingStore,
    image_path: str,
    *,
    model_name: str = DEFAULT_MODEL,
    detector_backend: str = DEFAULT_DETECTOR,
    threshold: float = DEFAULT_THRESHOLD,
) -> dict:
    """Identify the most-prominent face in *image_path*.

    Returns ``{"identity": str|None, "similarity": float, "threshold": float,
    "model": str}``. ``identity`` is ``None`` when the best match's similarity
    is below the threshold or no face is found.
    """
    DeepFace = _import_deepface()
    try:
        reps = DeepFace.represent(
            img_path=image_path,
            model_name=model_name,
            detector_backend=detector_backend,
            enforce_detection=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"deepface.represent failed: {exc}") from exc

    if not reps:
        return {"identity": None, "similarity": 0.0, "threshold": threshold, "model": model_name, "reason": "no face found"}

    emb = reps[0].get("embedding") if isinstance(reps[0], dict) else None
    if not emb:
        return {"identity": None, "similarity": 0.0, "threshold": threshold, "model": model_name, "reason": "embedding missing"}

    match: Match | None = store.best_match([float(x) for x in emb], model=model_name)
    if match is None:
        return {"identity": None, "similarity": 0.0, "threshold": threshold, "model": model_name, "reason": "no enrolled identities"}

    if match.similarity < threshold:
        return {
            "identity": None,
            "similarity": match.similarity,
            "threshold": threshold,
            "model": model_name,
            "closest": match.identity,
            "reason": "below threshold",
        }

    return {
        "identity": match.identity,
        "similarity": match.similarity,
        "threshold": threshold,
        "model": model_name,
    }


def _import_deepface():
    try:
        from deepface import DeepFace  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "deepface is not installed. Install with `uv add 'yuyutsava[deepface]'`."
        ) from exc
    return DeepFace
