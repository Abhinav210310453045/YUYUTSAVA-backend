"""Which zone a path the agent named appears to be in, for the display chip.

Deliberately **not** :func:`yuyutsava.agents.task_runner.zones.classify_zone`,
which is the policy classifier: that one canonicalizes the path, needs a
sandbox root, and has a ``SYSTEM_CRITICAL`` verdict because a wrong answer
there decides whether an operation runs. This one only decides whether to draw
``[workspace]`` next to a tool call, so it reads the path as the agent wrote it
and says nothing at all when it cannot tell.

The consequence worth knowing: the chip describes the literal argument, not the
resolved target, so a path that escapes via ``..`` can be chipped as the zone
it starts in. The policy still refuses it — the chip is a label, never a gate.
"""

from __future__ import annotations

import os
from pathlib import Path

from yuyutsava.platform import host_profile


def display_zone(path: str, workspace: Path | None) -> str:
    """``"workspace"``, ``"sandbox"``, ``"external"``, or ``""`` for no chip."""
    if workspace is not None and path.startswith(str(workspace)):
        return "workspace"
    if path.startswith(host_profile().temp_zone_prefixes()):
        return "sandbox"
    if os.path.isabs(path):
        return "external"
    return ""


def path_argument(args: object) -> str | None:
    """The path a ``tr_*`` call is about, if its arguments name one.

    Both renderers pick the same argument out of the same keys, in the same
    order, so the two chips agree about what they are describing.
    """
    if not isinstance(args, dict):
        return None
    for key in ("path", "file_path", "target", "directory"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    paths = args.get("paths")
    if isinstance(paths, list) and paths:
        first = paths[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    return None


__all__ = ["display_zone", "path_argument"]
