"""How much a Gemini model may write: its thinking budget, and the answer on top.

Gemini's thinking tokens count against ``max_output_tokens``. With a single cap
for both, a long think over a large context — a tool result arriving into a
50k-125k-token history — spent the whole cap, and the reply came back empty:
``finish_reason=MAX_TOKENS``, no text, no tool call. At our low temperature the
retry thought the same way, so a conversation stuck once it started. Thinking
therefore gets a budget of its own, and the cap is the answer's share plus it.

Shared by the Vertex and AI Studio providers, which speak the same model family.
"""

from __future__ import annotations

import re

from yuyutsava.llm.base import ANSWER_TOKENS

# Gemini 2.5 and later think; earlier models may reject a thinking config. Vertex
# model names can carry a resource prefix (publishers/google/models/…).
_THINKING_MODEL = re.compile(r"(?:^|/)gemini-(?:2\.5|[3-9])")


def gemini_output_kwargs(model: str, thinking_budget: int) -> dict[str, int]:
    """The SDK kwargs that bound one reply: ``max_output_tokens`` and, for a
    thinking model, ``thinking_budget``."""
    if not _THINKING_MODEL.search(model):
        return {"max_output_tokens": ANSWER_TOKENS}
    return {
        "max_output_tokens": ANSWER_TOKENS + thinking_budget,
        "thinking_budget": thinking_budget,
    }


__all__ = ["gemini_output_kwargs"]
