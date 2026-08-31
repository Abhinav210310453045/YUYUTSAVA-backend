"""A single discoverable resource (a tool, a skill, …).

The discovery layer speaks only ``CatalogEntry``. Each provider (tools, skills,
future resource types) turns its own objects into entries; the shared search
tool and renderers never need to know what the underlying resource actually is.

``load_detail`` is a thunk so the *full* payload — a tool's JSON schema, a
skill's SKILL.md body — is materialised only when an entry is actually expanded,
never just to list it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class CatalogEntry:
    id: str                       # unique resource name (tool name / skill name)
    group: str                    # namespace bucket for the catalog (e.g. "tr", "ws", "skill")
    blurb: str                    # ≤1-line description for the always-visible catalog
    match_text: str               # text ranked against for keyword search (id + blurb + …)
    load_detail: Callable[[], str]  # lazily returns the full schema/body on expand
