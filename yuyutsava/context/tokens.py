"""One approximate token counter, shared by everything that reports a number.

Before this there were three estimators and no two agreed:

* the compactor counted through langchain's ``count_tokens_approximately``,
  tuned per model type, and that count is what decides when history is
  destroyed;
* :mod:`yuyutsava.context.prompt_inspector` used ``chars // 4``;
* nothing at all reported the live window occupancy to a user.

A context meter that disagreed with the compaction trigger would be worse than
no meter — "7 % used" next to a compaction is not a readable instrument. So
every number a user sees comes from here, and here defers to the same langchain
function and the same per-model tuning the compactor gets, mirroring
``langchain.agents.middleware.summarization._get_approximate_token_counter``.

These are estimates. ``usage_metadata`` from a completed call is the only
measurement, and the meter calibrates against it rather than replacing it —
see :class:`yuyutsava.context.meter.ContextMeterPolicy`.
"""

from __future__ import annotations

from typing import Any

#: langchain's default. One token ≈ 4 characters of common English text.
DEFAULT_CHARS_PER_TOKEN = 4.0

#: Anthropic tokenizes denser than the generic default; 3.3 is the figure
#: langchain established against Claude's token-counting API, and using
#: anything else here would put our number and the compaction trigger on
#: different scales for Anthropic models.
ANTHROPIC_CHARS_PER_TOKEN = 3.3


def chars_per_token_for(model: Any | None = None) -> float:
    """Characters per token for *model*, matching the compactor's choice."""
    llm_type = getattr(model, "_llm_type", "") if model is not None else ""
    if isinstance(llm_type, str) and llm_type.startswith("anthropic-chat"):
        return ANTHROPIC_CHARS_PER_TOKEN
    return DEFAULT_CHARS_PER_TOKEN


def approx_tokens(messages: Any, *, model: Any | None = None) -> int:
    """Approximate tokens for a message list, the way the compactor counts.

    Includes langchain's per-message overhead (role, name, tool-call ids, a
    fixed penalty per image), which a bare character count misses. Returns 0
    rather than raising — a telemetry number is never worth a failed turn.
    """
    try:
        from langchain_core.messages.utils import count_tokens_approximately

        return int(
            count_tokens_approximately(
                messages or [],
                chars_per_token=chars_per_token_for(model),
                use_usage_metadata_scaling=True,
            )
        )
    except Exception:  # noqa: BLE001 — estimation must never fail a caller
        return 0


def approx_tokens_text(text: str, *, model: Any | None = None) -> int:
    """Approximate tokens for a bare string — a prompt block, a tool schema.

    No per-message overhead: the caller is measuring a *slice* of one message,
    and the slices have to sum to the whole.
    """
    if not text:
        return 0
    return max(1, round(len(text) / chars_per_token_for(model)))


def approx_tokens_chars(chars: int, *, model: Any | None = None) -> int:
    """Same as :func:`approx_tokens_text` for an already-measured length."""
    if chars <= 0:
        return 0
    return max(1, round(chars / chars_per_token_for(model)))


__all__ = [
    "ANTHROPIC_CHARS_PER_TOKEN",
    "DEFAULT_CHARS_PER_TOKEN",
    "approx_tokens",
    "approx_tokens_chars",
    "approx_tokens_text",
    "chars_per_token_for",
]
