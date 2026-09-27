"""How big the prompt's parts are, in characters.

Two jobs that both answer "how much of the window is this?": measuring the tool
schemas a bundle carries, and attributing a system prompt's characters to the
block that put them there. Characters, not tokens — turning them into tokens is
:mod:`~yuyutsava.context.meter.calibration`'s job, because only it knows what
the provider has said about this model.

The registry handles are weak on purpose: a closed bundle must be collectable,
and the alternative is a registry — and every tool it holds — pinned for the
life of the process by a telemetry dict.
"""

from __future__ import annotations

import logging
import weakref
from typing import Any

from yuyutsava.context.injector import MEMORY_BLOCK_PREFIX
from yuyutsava.context.meter.bus import reset_counters
from yuyutsava.memory.agent_memory import INDEX_BLOCK_HEADER
from yuyutsava.retrieval.injector import last_block_chars
from yuyutsava.skills.injector import SKILLS_BLOCK_PREFIX

logger = logging.getLogger("yuyutsava.context.meter")


#: Weak refs to the live tool registries, by role. Weak so a bundle that is
#: closed can be collected — the alternative is a registry (and every tool it
#: holds) pinned for the life of the process by a telemetry dict.
_registries: dict[str, weakref.ReferenceType] = {}


#: name -> schema chars, resolved on first sight and reused. Tool names are
#: globally unique by the prefix convention, so one cache serves every role.
_schema_chars: dict[str, int] = {}


_catalog_chars: dict[str, int] = {}


def note_tool_registry(role: str, registry: Any) -> None:
    """Keep a weak handle on a role's tool registry, for late-seen tool names.

    Weak on purpose: holding it strongly would pin every registry — and every
    tool in it — for the life of the process. It survives the builder's local
    because the ``tool_search`` tool bound to the graph captures it
    (``ToolRegistry.to_catalog`` closes over ``self`` in each entry's
    ``load_detail``). If that ever stops being true the fallback resolves to
    nothing and unmeasured tools contribute 0 rather than raising.

    Prefer :func:`note_bound_tools`, which measures up front and makes this a
    fallback rather than the primary path.
    """
    try:
        _registries[role] = weakref.ref(registry)
    except TypeError:  # not weak-referenceable (a test double)
        pass


def note_bound_tools(role: str, registry: Any, tools: Any) -> None:
    """Measure, once at build time, the schemas of the tools bound to a graph.

    Resolving sizes lazily from the registry misses the most important tool of
    all: ``tool_search`` is *created* by the registry and prepended to the
    bound list, never registered in it, so a registry lookup priced our single
    always-visible tool at zero — and under lazy discovery that is most of what
    the model is actually sent. Measuring the bound list directly is both exact
    and cheaper, since it happens once per bundle instead of per lookup.
    """
    note_tool_registry(role, registry)
    for tool in tools or ():
        name = getattr(tool, "name", "")
        if not name or name in _schema_chars:
            continue
        try:
            _schema_chars[name] = len(registry.schema_block([tool]))
        except Exception:  # noqa: BLE001 — sizing is best-effort
            _schema_chars[name] = 0
    if role not in _catalog_chars:
        try:
            _catalog_chars[role] = len(registry.catalog_block() or "")
        except Exception:  # noqa: BLE001 — the meter is an estimate; it must never break a turn
            logger.debug("context meter: sizing the tool catalog failed", exc_info=True)
            _catalog_chars[role] = 0


def _registry_for(role: str) -> Any | None:
    """This role's registry, else any live one.

    The builders name their registry with an *agent* name ("cli") while a
    policy carries a *role* ("cli", "orchestrator", "tinker"), and the two do
    not always coincide. Tool names are globally unique by the prefix
    convention, so any registry that holds the tool answers correctly — and
    falling back beats a "tool schemas 0" row that looks like a finding.
    """
    ref = _registries.get(role)
    registry = ref() if ref is not None else None
    if registry is not None:
        return registry
    for ref in list(_registries.values()):
        registry = ref()
        if registry is not None:
            return registry
    return None


def tool_schema_chars(role: str, names: tuple[str, ...]) -> int:
    """Chars of JSON schema for the tools actually bound to a call.

    Only the bound names are measured — with lazy discovery that is a handful
    out of dozens, and that gap is exactly what the "tool schemas" row exists
    to show.
    """
    registry = _registry_for(role)
    total = 0
    for name in names:
        cached = _schema_chars.get(name)
        if cached is None:
            cached = 0
            if registry is not None:
                try:
                    tool = next((t for t in registry.all_tools() if t.name == name), None)
                    if tool is not None:
                        cached = len(registry.schema_block([tool]))
                except Exception:  # noqa: BLE001 — sizing is best-effort
                    cached = 0
            _schema_chars[name] = cached
        total += cached
    return total


def catalog_chars(role: str) -> int:
    """Chars of the tier-0 ``name: blurb`` catalog carried in the prompt."""
    cached = _catalog_chars.get(role)
    if cached is not None:
        return cached
    registry = _registry_for(role)
    size = 0
    if registry is not None:
        try:
            size = len(registry.catalog_block() or "")
        except Exception:  # noqa: BLE001 — the meter is an estimate; it must never break a turn
            logger.debug("context meter: sizing the tool catalog failed", exc_info=True)
            size = 0
    _catalog_chars[role] = size
    return size


def reset_sizing_caches() -> None:
    """Drop registry refs and size caches. Tests only."""
    _registries.clear()
    _schema_chars.clear()
    _catalog_chars.clear()
    reset_counters()


# ----------------------------------------------------------------------
# Segment attribution
# ----------------------------------------------------------------------


def markers() -> tuple[tuple[str, str], ...]:
    """``(segment, marker)`` for the blocks we can find inside a system prompt.

    Imported from the modules that own the strings, never re-spelled here — a
    second copy would drift and silently mis-attribute prompt bytes.
    """
    return (
        ("memory", INDEX_BLOCK_HEADER.strip()),
        ("memory", MEMORY_BLOCK_PREFIX),
        ("skills", SKILLS_BLOCK_PREFIX),
    )


def split_system_chars(text: str) -> dict[str, int]:
    """Attribute one system-prompt block's chars to segments.

    Each known block runs from its marker to the next marker (or the end), and
    everything before the first marker is the prompt proper. A block the
    markers do not cover stays in ``system`` — under-attributing to the
    catch-all is honest, inventing a row is not.
    """
    out = {"system": 0, "memory": 0, "skills": 0}
    if not text:
        return out
    hits: list[tuple[int, str]] = []
    for segment, marker in markers():
        if not marker:
            continue
        idx = text.find(marker)
        if idx >= 0:
            hits.append((idx, segment))
    hits.sort()
    if not hits:
        out["system"] = len(text)
        return out
    out["system"] = hits[0][0]
    for i, (start, segment) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        out[segment] = out.get(segment, 0) + (end - start)
    return out


def injected_block_chars(thread_id: str) -> dict[str, int]:
    """Sizes of the blocks the retrieval injectors last rendered on *thread_id*."""
    return last_block_chars(thread_id)


__all__ = [
    "catalog_chars",
    "injected_block_chars",
    "markers",
    "note_bound_tools",
    "note_tool_registry",
    "reset_sizing_caches",
    "split_system_chars",
    "tool_schema_chars",
]
