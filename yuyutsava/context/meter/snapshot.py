"""One reading of a context window, and the order its segments are reported in.

A frozen record with no behaviour beyond arithmetic on its own fields: it is
what the bus carries, what ``/context`` prints and what the app receives as a
frame, so it must stay cheap to construct and safe to hand to anyone.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

#: Order the segments are reported in — largest structural pieces first, so a
#: reader scans "what is this window made of" top to bottom.
SEGMENT_LABELS: tuple[tuple[str, str], ...] = (
    ("system", "system prompt"),
    ("tools", "tool schemas"),
    ("memory", "memory"),
    ("skills", "skills"),
    ("messages", "messages"),
)


@dataclass(frozen=True)
class ContextSnapshot:
    """One reading of a conversation's context window and spend."""

    role: str = "agent"
    thread_id: str = ""
    model: str = ""

    # The window the compactor steers under.
    max_input_tokens: int = 0
    compact_trigger_tokens: int = 0

    # Calibrated estimates of what occupies it.
    system_tokens: int = 0
    tools_tokens: int = 0
    memory_tokens: int = 0
    skills_tokens: int = 0
    messages_tokens: int = 0

    # Shape of the message list.
    message_count: int = 0
    tool_count: int = 0
    offloaded_digests: int = 0

    #: Whether the segment estimates have been corrected against a real call.
    calibrated: bool = False
    #: Whether the headline total rests on a provider measurement rather than
    #: being a free-floating character estimate.
    anchored: bool = False
    #: Whether the headline is the provider's own arithmetic end to end — true
    #: on the publish right after a call, when nothing has been appended since
    #: and so nothing is estimated. Renderers drop the ``≈`` from the total.
    window_measured: bool = False

    # The last completed call — provider-reported, never estimated.
    call_no: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    est_cost_usd: float = 0.0
    #: False when the model has no entry in the price table, so every surface
    #: can say "unpriced" instead of showing a confident $0.00.
    priced: bool = False

    # This conversation so far.
    calls: int = 0
    session_input_tokens: int = 0
    session_output_tokens: int = 0
    session_cache_read_tokens: int = 0
    session_cost_usd: float = 0.0
    compactions: int = 0
    offloads: int = 0
    started_at: float = 0.0

    ts: float = field(default_factory=time.time)

    # -- derived ------------------------------------------------------------

    @property
    def used_tokens(self) -> int:
        return (
            self.system_tokens
            + self.tools_tokens
            + self.memory_tokens
            + self.skills_tokens
            + self.messages_tokens
        )

    @property
    def free_tokens(self) -> int:
        return max(0, self.max_input_tokens - self.used_tokens)

    @property
    def used_fraction(self) -> float:
        if self.max_input_tokens <= 0:
            return 0.0
        return min(1.0, self.used_tokens / self.max_input_tokens)

    @property
    def cache_hit_fraction(self) -> float:
        """Share of the last call's input the provider served from cache."""
        if self.input_tokens <= 0:
            return 0.0
        return min(1.0, self.cache_read_tokens / self.input_tokens)

    def segments(self) -> list[tuple[str, str, int]]:
        """``(key, label, tokens)`` in report order, zero rows included.

        Zeros are kept on purpose: "skills 0" is the finding that 31 indexed
        skills went unused, and a row that vanishes cannot say that.
        """
        return [
            (key, label, int(getattr(self, f"{key}_tokens", 0))) for key, label in SEGMENT_LABELS
        ]

    def as_dict(self) -> dict[str, Any]:
        """Wire shape for the app. Flat where the UI wants numbers, nested
        where it wants a group, and segments pre-ordered so both fronts show
        the same rows in the same order."""
        return {
            "role": self.role,
            "thread_id": self.thread_id,
            "model": self.model,
            "max_input_tokens": self.max_input_tokens,
            "compact_trigger_tokens": self.compact_trigger_tokens,
            "used_tokens": self.used_tokens,
            "free_tokens": self.free_tokens,
            "used_fraction": round(self.used_fraction, 6),
            "calibrated": self.calibrated,
            "anchored": self.anchored,
            "window_measured": self.window_measured,
            "segments": [{"key": k, "label": lbl, "tokens": n} for k, lbl, n in self.segments()],
            "message_count": self.message_count,
            "tool_count": self.tool_count,
            "offloaded_digests": self.offloaded_digests,
            "call": {
                "n": self.call_no,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_creation_tokens": self.cache_creation_tokens,
                "cache_hit_fraction": round(self.cache_hit_fraction, 6),
                "est_cost_usd": self.est_cost_usd,
                "priced": self.priced,
            },
            "session": {
                "calls": self.calls,
                "input_tokens": self.session_input_tokens,
                "output_tokens": self.session_output_tokens,
                "cache_read_tokens": self.session_cache_read_tokens,
                "est_cost_usd": self.session_cost_usd,
                "compactions": self.compactions,
                "offloads": self.offloads,
                "started_at": self.started_at,
            },
            "ts": self.ts,
        }


__all__ = ["SEGMENT_LABELS", "ContextSnapshot"]
