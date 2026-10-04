"""Assistant turns another model wrote, in the form every provider accepts.

A conversation can move between models — ``/model``, the app's model chip, an
edit to ``models.toml`` — so its history holds assistant turns written by a
model other than the one about to read them.

## The rule

An assistant message goes to **the model that wrote it unchanged**, so its
thought signatures and thinking blocks survive. To **any other model** it goes
**portable**: its text and its ``tool_calls``, nothing else. Only what is sent
changes; the checkpoint keeps every message as it was written, so a conversation
that switches back finds its own turns intact.

## Why only that form

Measured locally across five producers and nine request builders (design
§2a, pinned by ``test/framework_contract/test_cross_provider_history_contract.py``):

* raw history fails — Anthropic's turns raise in the Gemini and Bedrock
  builders, a Gemini thinking block reaches Anthropic without the signature it
  requires, and signatures leak into text parts elsewhere;
* LangChain's standard ``content_blocks`` still raise on two builders;
* text plus ``tool_calls`` builds cleanly for all 45 pairs, with no foreign field
  in any payload.

Nothing the reader needs is lost. A turn is pinned to one model (the switch
applies from the next turn), so the active tool loop is always the reader's own
and stays raw; earlier turns' thinking is optional for every provider.

## Who wrote a turn

:data:`STAMP` in ``response_metadata`` — the reference
(``<provider>/<model id>``) of the model that answered, set by the model switch
on every answer it hands back. The SDKs' own tags cannot say it: Vertex, Bedrock
and OpenAI's parser set none, and ``"openai"`` cannot tell Groq from OpenRouter
from Vertex MaaS. A turn without a stamp was written before stamping existed, by
a model nobody can name, so it is treated as foreign.
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage

#: The ``response_metadata`` key naming the model that wrote a turn.
STAMP = "yuyutsava_model"


def stamped(message: AIMessage, reference: str) -> AIMessage:
    """*message* recording that the model *reference* wrote it."""
    metadata = {**message.response_metadata, STAMP: reference}
    return message.model_copy(update={"response_metadata": metadata})


def writer_of(message: AIMessage) -> str | None:
    """The reference of the model that wrote *message*, or ``None`` when unknown."""
    found = message.response_metadata.get(STAMP)
    return found if isinstance(found, str) and found else None


def portable(message: AIMessage) -> AIMessage:
    """*message*'s text and tool calls — the form every provider's builder accepts."""
    if isinstance(message.content, str):
        text = message.content
    else:
        text = "".join(
            block if isinstance(block, str) else str(block.get("text", ""))
            for block in message.content
            if isinstance(block, str) or block.get("type") == "text"
        )
    return AIMessage(
        content=text,
        tool_calls=message.tool_calls,
        id=message.id,
        name=message.name,
    )


def for_reader(messages: Sequence[BaseMessage], reference: str) -> list[BaseMessage]:
    """*messages* as the model *reference* should receive them.

    Its own assistant turns pass unchanged; every other assistant turn is
    :func:`portable`. Human, system and tool messages carry nothing
    provider-specific and pass as they are.
    """
    return [
        portable(message)
        if isinstance(message, AIMessage) and writer_of(message) != reference
        else message
        for message in messages
    ]


__all__ = ["STAMP", "for_reader", "portable", "stamped", "writer_of"]
