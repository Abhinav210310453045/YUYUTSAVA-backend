"""DeepFace MCP server entrypoint.

Run with::

    python -m yuyutsava.mcp_servers.deepface.server

Spawned by :class:`yuyutsava.mcp.loader.MCPClientManager` when configured in
``~/.yuyutsava/mcp_config.json``::

    "deepface": {
      "command": "python",
      "args": ["-m", "yuyutsava.mcp_servers.deepface.server"]
    }

Exposed tools (registered with FastMCP):

- ``detect_faces(image_path, detector_backend?)`` → list of bounding boxes
- ``enroll(identity, image_paths, model_name?, detector_backend?)`` → report
- ``identify(image_path, model_name?, detector_backend?, threshold?)`` → match
- ``list_identities()`` → enrolled names + sample counts
- ``delete_identity(identity)`` → number of rows removed

The ``deepface`` Python package is imported lazily inside detection /
enrollment / identification, so this server can boot and respond to
``list_identities`` / ``delete_identity`` even without it installed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from yuyutsava.mcp_servers.deepface import detection, enrollment, identification
from yuyutsava.mcp_servers.deepface.store import EmbeddingStore

logger = logging.getLogger("yuyutsava.mcp_servers.deepface.server")


def _store_path() -> Path:
    raw = os.environ.get("YUYUTSAVA_HOME", "").strip()
    base = Path(raw).expanduser() if raw else Path.home() / ".yuyutsava"
    return base / "deepface" / "db.sqlite"


_store = EmbeddingStore(_store_path())
mcp = FastMCP("yuyutsava-deepface")


@mcp.tool()
def detect_faces(image_path: str, detector_backend: str = detection.DEFAULT_DETECTOR) -> list[dict]:
    """Return bounding boxes for every face found in the image at *image_path*."""
    boxes = detection.detect(image_path, detector_backend=detector_backend)
    return [
        {"x": b.x, "y": b.y, "w": b.w, "h": b.h, "confidence": b.confidence}
        for b in boxes
    ]


@mcp.tool()
def enroll(
    identity: str,
    image_paths: list[str],
    model_name: str = enrollment.DEFAULT_MODEL,
    detector_backend: str = enrollment.DEFAULT_DETECTOR,
) -> dict:
    """Add *identity* to the store using one or more reference images."""
    return enrollment.enroll(
        _store, identity, image_paths,
        model_name=model_name, detector_backend=detector_backend,
    )


@mcp.tool()
def identify(
    image_path: str,
    model_name: str = enrollment.DEFAULT_MODEL,
    detector_backend: str = enrollment.DEFAULT_DETECTOR,
    threshold: float = identification.DEFAULT_THRESHOLD,
) -> dict:
    """Identify the most-prominent face in *image_path* against enrolled identities."""
    return identification.identify(
        _store, image_path,
        model_name=model_name, detector_backend=detector_backend, threshold=threshold,
    )


@mcp.tool()
def list_identities() -> list[dict]:
    """Enrolled identities and their sample counts."""
    return [{"identity": name, "samples": n} for name, n in _store.list_identities()]


@mcp.tool()
def delete_identity(identity: str) -> dict:
    """Remove every embedding stored under *identity*."""
    removed = _store.delete_identity(identity)
    return {"identity": identity, "removed": removed}


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    mcp.run()


if __name__ == "__main__":
    main()
