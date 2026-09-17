"""Turning a tool's result into a typed stream event, without the driver knowing the tools.

The graph driver sees a ``ToolMessage`` and has to decide whether it also
carries something the UI can render — a rendered chart, a created artifact. It
used to decide by name: ``vis_*`` meant an image, ``artifact_create`` meant an
artifact, and both payload shapes were written out in the driver. Every new
renderable tool family meant editing the driver.

A :class:`ToolEventRule` moves that knowledge to the package that owns the
result: :mod:`yuyutsava.visuals.events` knows what a ``vis_*`` result looks
like, :mod:`yuyutsava.artifacts.events` knows its own record. The driver holds a
:class:`ToolEventRegistry` of their rules and asks it.

The rules are passed in, never registered into a module-level list: a registry
that fills itself on import breaks the moment nobody imports the filling module,
and the failure is a silently missing event rather than an ImportError.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

#: Parses one tool's result body into an event payload, or ``None`` when the
#: result is not renderable (an error JSON, a plain string, a truncated body).
ToolEventExtractor = Callable[[str], "dict | None"]


@dataclass(frozen=True)
class ToolEventRule:
    """One tool family's mapping from a result body to a stream event.

    *names* matches a tool exactly, *prefixes* matches a family (``vis_``).
    A rule with neither never matches, which keeps a half-written rule quiet
    instead of claiming every tool.
    """

    kind: str  # the StreamEvent kind this produces ("image", "artifact", …)
    extract: ToolEventExtractor
    names: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()

    def matches(self, tool_name: str) -> bool:
        return tool_name in self.names or tool_name.startswith(self.prefixes)


@dataclass(frozen=True)
class ToolEventRegistry:
    """The rules a driver applies, in order; the first match wins."""

    rules: tuple[ToolEventRule, ...] = ()

    def event_for(self, tool_name: str, body: str) -> tuple[str, dict] | None:
        """``(kind, payload)`` for *body*, or ``None`` when nothing renders it.

        A rule that matches the name but cannot parse the body yields ``None``
        rather than falling through to the next rule: the owning package said
        this result is not renderable, and a later rule guessing at the same
        body would be reading someone else's JSON.
        """
        for rule in self.rules:
            if not rule.matches(tool_name):
                continue
            payload = rule.extract(body)
            return None if payload is None else (rule.kind, payload)
        return None
