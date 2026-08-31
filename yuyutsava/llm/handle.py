"""``ModelHandle`` — a chat model plus what this system actually knows about it.

Phase 4 step 4.1, [ADR-004](../../docs/architecture/review/adr/ADR-004-framework-boundary.md)
item 2 — *"the cheapest item in the entire review with a real payoff"*.

## The problem it solves

``BaseChatModel`` is the currency in 18 modules (`F-T02`), and it carries no
answer to the two questions this system keeps asking about a model:

**"What is it called?"** — asked by every usage row and the task registry.
Answered by :func:`~yuyutsava.llm.factory.model_name_of`, which probed
``model_name`` / ``model`` / ``model_id`` in turn and returned ``""`` when none
matched. That guess is not merely fragile in principle: it returns ``""`` for
**every Azure model**, because ``AzureChatOpenAI`` leaves ``model_name`` at
``None`` (the deployment is authoritative). Azure usage rows have been recording
a blank model name, and nothing failed — the exact silent-failure shape this
review exists to remove.

**"What can it do?"** — asked whenever behaviour is provider-conditional. Today
that is answered by knowing, out of band, which providers are special. The two
below are real and load-bearing, and neither was written down anywhere a caller
could read it.

## Why the provider is the right source

The provider knows both answers at build time, from settings, *before* an SDK
object exists to interrogate. Reading the name back off the constructed model is
strictly worse information: it survives only if the SDK keeps that attribute
name, and Azure shows it may not be there at all.

## Scope

Deliberately **not** a wrapper (see ADR-004, Alternative C). ``.model`` is the
real ``BaseChatModel`` and every existing caller keeps using it directly;
``ModelHandle`` adds the metadata alongside rather than mediating access. This
module imports no framework at runtime — the annotation is deferred — so domain
code can hold a handle without pulling LangChain in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # keeps this module framework-free at runtime
    from langchain_core.language_models import BaseChatModel


class Capability(str, Enum):
    """A property of a provider that changes how callers must treat its models.

    Only capabilities with a **real call site** are declared. An enum of
    plausible-sounding traits nobody branches on would be the "abstraction with
    one implementation" failure ADR-004 warns about; each member below names
    behaviour that already differs between providers today.
    """

    #: ``chat_model(..., disable_reasoning=True)`` is honoured. Only the
    #: OpenAI-compatible provider implements it (OpenRouter's unified
    #: ``reasoning.enabled`` flag); the other seven accept the argument and
    #: silently ignore it, because it is set per *call site* rather than per
    #: provider. Callers that need reasoning genuinely off — the triage agent,
    #: whose small structured output gets truncated by reasoning tokens — can
    #: now tell whether they got it.
    REASONING_TOGGLE = "reasoning_toggle"

    #: The model instance binds to the event loop that first drives it, so it
    #: must not be shared across loops: one instance per loop. Both Gemini SDKs
    #: cache an async client on first use (grpc.aio for Vertex, google-genai for
    #: the Gemini API). ``quirks/loop_affinity.py`` enforces this at runtime with
    #: a readable error; this declares it *before* the crash, so a builder
    #: deciding whether to construct its own model can ask instead of knowing.
    LOOP_AFFINE = "loop_affine"


@dataclass(frozen=True)
class ModelHandle:
    """A built chat model, its identity, and its provider's capabilities."""

    #: The chat model itself. Still the framework type — ADR-004 draws the
    #: boundary around what is ours (policy, HITL, agent identity), not around
    #: ``BaseChatModel``, which is a broad, stable interface every provider
    #: implements.
    model: BaseChatModel

    #: The model identifier, as the provider that built it names it. This is what
    #: usage rows and the task registry record.
    name: str

    #: Which provider built it — the host name where one exists (``groq``,
    #: ``ollama``) rather than the implementation class, since several hosts
    #: share ``OpenAICompatibleProvider``.
    provider: str

    capabilities: frozenset[Capability] = field(default_factory=frozenset)

    def has(self, capability: Capability) -> bool:
        """Whether this model's provider supports *capability*."""
        return capability in self.capabilities

    def __str__(self) -> str:  # logs and error messages
        return f"{self.provider}:{self.name}" if self.name else self.provider


__all__ = ["Capability", "ModelHandle"]
