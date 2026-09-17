"""The ``image`` stream event, built from a ``vis_*`` tool result.

Every tool in :mod:`yuyutsava.visuals.tools` returns the same JSON envelope, and
a rendered visual has to reach the UI as a typed frame or it arrives as a blob
of JSON in the tool result. The envelope is this package's shape, so the
extractor that reads it lives here; the graph driver only applies
:data:`IMAGE_EVENT`.
"""

from __future__ import annotations

import json

from yuyutsava.ports.tool_events import ToolEventRule


def image_event_from_result(body: str) -> dict | None:
    """Extract an ``image`` event payload from a ``vis_*`` tool result, or None.

    ``vis_*`` tools return JSON like ``{"status":"ok","visual_id":...,"url":...}``.
    A rendered visual becomes a typed ``image`` frame so the UI can show it inline
    (and any SSE consumer gets structured image data, not opaque text).
    """
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("status") != "ok" or not data.get("visual_id"):
        return None
    return {
        "visual_id": data["visual_id"],
        "url": data.get("url"),
        "kind": data.get("kind"),
        "title": data.get("title"),
        "mime": data.get("mime", "image/png"),
    }


#: The whole ``vis_*`` family shares the envelope above, so the prefix is the
#: match: a new renderer tool needs no edit here and none in the driver.
IMAGE_EVENT = ToolEventRule(kind="image", extract=image_event_from_result, prefixes=("vis_",))
