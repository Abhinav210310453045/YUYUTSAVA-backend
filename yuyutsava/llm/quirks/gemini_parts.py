"""Gemini's zero-parts rejection — the quirk that wedges a thread permanently.

Shared by both Gemini-family providers (``providers/vertex.py`` and
``providers/google.py``); it is a property of the Gemini *wire format*, not of
either SDK.

## The failure

Gemini converts every LangChain message into a ``Content`` carrying a list of
``parts``, and rejects the WHOLE request with::

    400 Unable to submit request because it must include at least one parts
    field, which describes the prompt input

if *any* message renders to zero parts. The rule is the converter's own
(``langchain_google_vertexai.chat_models``, AI branch)::

    parts = []
    if message.content:                 # falsy content -> NO text parts
        parts = _convert_to_parts(message)
    for tc in message.tool_calls:       # tool calls add functionCall parts
        parts.append(Part(function_call=...))

so an assistant turn is empty **iff its content is falsy and it has no tool
calls**. A generation that produced no text and called nothing — cancelled,
barged-in, filtered — becomes ``AIMessage(content="")``. LangGraph commits it to
the thread checkpoint, and from then on every RESUMED turn replays it and gets a
400. The conversation is bricked: the user keeps typing and never gets a reply,
because the model node fails before it ever sees the new input. (The converter
already skips one flavour — empty content flagged
``response_metadata["is_blocked"]``; this closes the rest.)

## Why this is applied at the model, not as middleware

A middleware would miss the callers that have no agent loop: ``TriageAgent`` does
``model.with_structured_output(...).ainvoke()``, the compaction model is invoked
*inside* the summarization middleware, and ``model_router`` tier models are
invoked bare. The model boundary is the only layer every caller passes through.

The repair is read-side by necessity: LangGraph commits whatever the model node
returns, and checkpoints that are already poisoned cannot be rewritten. Dropping
the empty message on the way out both **self-heals already-wedged threads** and
makes the failure unreachable.

## Deliberately NOT touched

* ``ToolMessage``/``FunctionMessage`` — the converter always emits a
  ``functionResponse`` part for them whatever the content, so a blank tool result
  is not a hazard, and rewriting it would only distort what the model reads.
* Whitespace-only turns — truthy, therefore a real part, therefore not our
  business. Stripping would be over-repair.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Iterable

from langchain_core.messages import AIMessage, BaseMessage, FunctionMessage, ToolMessage

# Truthy, so it always converts to a part, and near-invisible to the model.
_PLACEHOLDER = " "


def renders_parts(message: BaseMessage) -> bool:
    """True when *message* converts to at least one Gemini part."""
    if isinstance(message, (ToolMessage, FunctionMessage)):
        # Always a functionResponse part, regardless of content.
        return True
    if message.content:
        return True
    if isinstance(message, AIMessage):
        # A tool-call-only turn is legitimate: empty text, but each tool call
        # becomes a functionCall part.
        if message.tool_calls or getattr(message, "invalid_tool_calls", None):
            return True
        if (message.additional_kwargs or {}).get("function_call"):
            return True
    return False


def _with_content(message: BaseMessage, text: str) -> BaseMessage:
    """A copy of *message* carrying *text*, preserving its class and every other
    field (``tool_call_id``, ``id``, ``additional_kwargs``, …)."""
    return message.model_copy(update={"content": text})


def normalize_messages(messages: Iterable[BaseMessage]) -> list[BaseMessage]:
    """Return *messages* with every zero-parts message dropped or repaired.

    An empty assistant turn is dropped — it carries no information, and having no
    tool calls (that is what made it empty) it leaves no ``ToolMessage`` orphaned
    behind it. Anything else empty keeps its slot with a placeholder so the turn
    structure survives. Consecutive same-role messages left behind by a drop are
    fine: the converter merges them into one ``Content``.

    Order is preserved, the input is not mutated, and the result is never empty —
    an all-empty input keeps its last message with a placeholder, so we never
    trade a 400 for a different 400.
    """
    messages = list(messages)
    out: list[BaseMessage] = []
    for message in messages:
        if renders_parts(message):
            out.append(message)
        elif isinstance(message, AIMessage):
            continue  # the poisoned-checkpoint case
        else:
            out.append(_with_content(message, _PLACEHOLDER))
    if not out and messages:
        out.append(_with_content(messages[-1], _PLACEHOLDER))
    return out


class _PartsSafeMixin:
    """Normalises messages on every generate/stream entrypoint.

    ``bind_tools`` and ``with_structured_output`` both return runnables that
    delegate to these same methods, so tool-bound and structured calls are covered
    without knowing about them.
    """

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
        return super()._generate(
            normalize_messages(messages), stop=stop, run_manager=run_manager, **kwargs
        )

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
        return await super()._agenerate(
            normalize_messages(messages), stop=stop, run_manager=run_manager, **kwargs
        )

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
        yield from super()._stream(
            normalize_messages(messages), stop=stop, run_manager=run_manager, **kwargs
        )

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
        async for chunk in super()._astream(
            normalize_messages(messages), stop=stop, run_manager=run_manager, **kwargs
        ):
            yield chunk


@lru_cache(maxsize=None)
def parts_safe(base: type) -> type:
    """``base`` re-based so no zero-parts message can reach the API.

    Cached, and that is the point: provider SDKs are lazily-imported optional
    extras, so this subclass cannot be declared at import time. Building it per
    call would mint a fresh class object every time — breaking ``isinstance``,
    pickling and identity. One cached class per base keeps that stable.
    """
    return type(f"PartsSafe{base.__name__}", (_PartsSafeMixin, base), {})


__all__ = ["normalize_messages", "parts_safe", "renders_parts"]
