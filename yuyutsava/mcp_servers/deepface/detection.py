"""Face detection wrapper around the ``deepface`` package.

We import ``deepface`` lazily so the MCP server's module-import phase stays
cheap and so a missing dependency surfaces as a clean tool-call error rather
than a spawn crash.

The default detector backend is ``opencv`` because it has no extra native
deps; users can override per-call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("yuyutsava.mcp_servers.deepface.detection")

DEFAULT_DETECTOR = "opencv"


@dataclass(frozen=True)
class FaceBox:
    x: int
    y: int
    w: int
    h: int
    confidence: float = 0.0
    landmarks: dict[str, Any] = field(default_factory=dict)


def detect(image_path: str, *, detector_backend: str = DEFAULT_DETECTOR) -> list[FaceBox]:
    """Return bounding boxes for every face in *image_path*.

    Raises :class:`RuntimeError` with a useful message if the ``deepface``
    package is not installed; the MCP server turns this into a tool error.
    """
    DeepFace = _import_deepface()
    try:
        results = DeepFace.extract_faces(
            img_path=image_path,
            detector_backend=detector_backend,
            enforce_detection=False,
            align=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"deepface.extract_faces failed: {exc}") from exc

    boxes: list[FaceBox] = []
    for r in results or []:
        area = (r or {}).get("facial_area") or {}
        conf = float(r.get("confidence", 0.0) or 0.0)
        try:
            boxes.append(
                FaceBox(
                    x=int(area.get("x", 0)),
                    y=int(area.get("y", 0)),
                    w=int(area.get("w", 0)),
                    h=int(area.get("h", 0)),
                    confidence=conf,
                )
            )
        except (TypeError, ValueError):
            continue
    return boxes


def _import_deepface():
    try:
        from deepface import DeepFace  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "deepface is not installed. Install with `uv add 'yuyutsava[deepface]'` "
            "or `pip install deepface`."
        ) from exc
    return DeepFace
