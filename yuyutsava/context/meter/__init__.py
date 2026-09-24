"""Context meter — what is in the window right now, and what the last call cost.

Before this existed, a question like "12,285 -> 72,848 input tokens, ~93 %
cache-read, 0 compactions" could only be answered by querying Postgres after the
session was over. From inside a session none of it was visible, so a context
window filling up looked exactly like a context window that was fine.

This is the one measurement source both fronts read, and the package is laid
out in the order a reading is built:

* :mod:`snapshot` — the record that is published.
* :mod:`bus` — where it is published, and the compaction/offload counters.
* :mod:`sizing` — how many *characters* each part of the prompt is.
* :mod:`calibration` — how those characters become tokens a provider would
  agree with, anchored on what it actually reported.
* :mod:`policy` — the one thing that runs per call and ties them together.

Last-call and session figures are always raw provider numbers. Renderers must
keep the distinction visible; ``/context`` marks estimates with ``≈``.
"""

from __future__ import annotations

from yuyutsava.context.meter.bus import (
    MeterBus,
    bus,
    counters,
    note_compaction,
    note_offload,
)
from yuyutsava.context.meter.calibration import reset_thread_state
from yuyutsava.context.meter.policy import ContextMeterPolicy
from yuyutsava.context.meter.sizing import (
    note_bound_tools,
    note_tool_registry,
    reset_sizing_caches,
    split_system_chars,
    tool_schema_chars,
)
from yuyutsava.context.meter.snapshot import SEGMENT_LABELS, ContextSnapshot

__all__ = [
    "SEGMENT_LABELS",
    "ContextMeterPolicy",
    "ContextSnapshot",
    "MeterBus",
    "bus",
    "counters",
    "note_bound_tools",
    "note_compaction",
    "note_offload",
    "note_tool_registry",
    "reset_sizing_caches",
    "reset_thread_state",
    "split_system_chars",
    "tool_schema_chars",
]
