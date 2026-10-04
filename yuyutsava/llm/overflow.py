"""Whether a provider refused a call because the prompt did not fit the model's window.

Compaction catches this to summarize and retry once
(:class:`~yuyutsava.context.compaction.YuyutsavaCompactionMiddleware`), so it
must recognise every provider's way of saying it — and nothing else.

Three SDKs raise LangChain's ``ContextOverflowError`` themselves: Anthropic
("prompt is too long"), Gemini through genai, and OpenAI-compatible hosts
(``context_length_exceeded``). The others pass the provider's own error through:

* Claude on Vertex — ``ChatAnthropicVertex`` calls Anthropic's Vertex client
  directly, so the same "prompt is too long" arrives unconverted;
* Bedrock Converse — "Input is too long for requested model";
* Mistral — "... too large for model with ... maximum context length".

A rate limit is not an overflow, however it is worded: "tokens per minute" is a
429 that waiting fixes, and compacting a conversation for it would throw away
history for nothing (rule 15's sibling, design §2).
"""

from __future__ import annotations

import re

from langchain_core.exceptions import ContextOverflowError

#: How the providers that do not raise ``ContextOverflowError`` say it.
_TOO_LONG = re.compile(
    r"prompt is too long"
    r"|input is too long"
    r"|context_length_exceeded"
    r"|maximum context length"
    r"|exceeds the maximum number of tokens"
    r"|exceeds the context window",
    re.IGNORECASE,
)

#: A limit on throughput, which retrying later fixes; never an overflow.
_RATE_LIMIT = re.compile(r"per minute|rate limit|too many requests|\b429\b", re.IGNORECASE)


def is_context_overflow(exc: BaseException) -> bool:
    """Whether *exc* says the prompt was longer than the model's input window."""
    if isinstance(exc, ContextOverflowError):
        return True
    text = str(exc)
    return bool(_TOO_LONG.search(text)) and not _RATE_LIMIT.search(text)


__all__ = ["is_context_overflow"]
