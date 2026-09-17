"""The ``artifact`` stream event, built from an ``artifact_create`` result.

:mod:`yuyutsava.artifacts.tools` returns a JSON record; a turn shows that
artifact inline in the same reply, which means the record has to become a typed
frame. The mapping lives here, beside the tool that writes the record, rather
than in the graph driver: the driver applies :data:`ARTIFACT_EVENT` along with
the other packages' rules and never reads this JSON itself.

The daemon replays chat history through the same extractor
(:mod:`yuyutsava.daemon.web.routers.sessions`), so a reloaded conversation
carries the artifacts the live turn showed.
"""

from __future__ import annotations

import json

from yuyutsava.ports.tool_events import ToolEventRule


def artifact_event_from_result(body: str) -> dict | None:
    """Extract an ``artifact`` event payload from an ``artifact_create`` result.

    Turns the tool's record JSON into a typed ``artifact`` frame so the UI can
    render the pluggable block inline (SandboxBlock/AudioBlock/TextBlock/…) and
    open it big — the non-visual twin of
    :func:`yuyutsava.visuals.events.image_event_from_result`.
    """
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("status") != "ok" or not data.get("artifact_id"):
        return None
    return {
        # attachment_id is the key the frontend block components read; alias it.
        "artifact_id": data["artifact_id"],
        "attachment_id": data["artifact_id"],
        "url": data.get("url"),
        "kind": data.get("kind"),
        "mime": data.get("mime"),
        "title": data.get("title"),
    }


#: Matched by exact name, not by prefix: an unrelated tool whose result happens
#: to carry an ``artifact_id`` is not this package's record to read.
ARTIFACT_EVENT = ToolEventRule(
    kind="artifact",
    extract=artifact_event_from_result,
    names=("artifact_create", "artifact_show"),
)
