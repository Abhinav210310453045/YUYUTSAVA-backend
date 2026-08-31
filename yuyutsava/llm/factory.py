"""``chat_model()`` — the one seam every model in the system is built through.

All 12 construction sites (CLI, tinker, orchestrator, triage, subagents,
compaction, ``model_router`` tiers) call this. That is what makes it the right
place to apply provider quirks: a middleware would miss every caller that has no
agent loop — ``TriageAgent`` invokes ``model.with_structured_output(...)``
directly, and the compaction model is invoked *inside* the summarization
middleware.

The signature is deliberately unchanged from the pre-refactor version, so no
caller had to move.

Being the one seam is also what makes :func:`model_handle` possible: every model
in the system passes through here, so recording what the provider knew at build
time covers all of them. See :mod:`yuyutsava.llm.handle`.
"""

from __future__ import annotations

import weakref
from typing import NamedTuple

from langchain_core.language_models import BaseChatModel

from yuyutsava.core.config import LlmSettings
from yuyutsava.llm.handle import Capability, ModelHandle
from yuyutsava.llm.providers import provider_for


class _Identity(NamedTuple):
    """What the provider knew, *without* a reference back to the model.

    Storing a whole :class:`ModelHandle` here instead would keep every model
    alive forever: the registry would hold the handle, the handle holds
    ``.model``, and the finalizer that evicts the entry only runs once the model
    is collected. A daemon that builds a fresh model per task would grow without
    bound. Caught by ``TheRegistryDoesNotLeak`` — 25 models built, 25 still
    registered after ``del`` and a full ``gc.collect()``.
    """

    name: str
    provider: str
    capabilities: frozenset[Capability]


# What the provider knew about each model this system built, keyed by object
# IDENTITY.
#
# Not a ``WeakKeyDictionary``: chat models are pydantic objects and unhashable
# (measured — ``hash(ChatOpenAI(...))`` raises), so they cannot be dict keys at
# all. Keying on ``id`` also states the intent exactly — this belongs to one
# built instance, not to anything that compares equal to it.
_IDENTITIES: dict[int, _Identity] = {}


def _remember(model: BaseChatModel, identity: _Identity) -> None:
    """Record *identity* for *model*, evicting when the model is collected."""
    key = id(model)
    try:
        weakref.finalize(model, _IDENTITIES.pop, key, None)
    except TypeError:
        # Not weak-referenceable — a stub or a fake. Registering it would leak,
        # and model_name_of still has the attribute probe for exactly this case.
        return
    _IDENTITIES[key] = identity


def handle_for(model: BaseChatModel | object) -> ModelHandle | None:
    """The handle for *model*, or ``None`` if this system did not build it.

    Rebuilt on each call from the stored identity — see :class:`_Identity` for
    why the handle itself is not what gets stored.
    """
    identity = _IDENTITIES.get(id(model))
    if identity is None:
        return None
    return ModelHandle(
        model=model,  # type: ignore[arg-type]
        name=identity.name,
        provider=identity.provider,
        capabilities=identity.capabilities,
    )


def model_name_of(model: BaseChatModel | object) -> str:
    """The model identifier, for logging and usage rows.

    For any model this system built, the answer comes from the provider that
    built it — exact, and available even when the SDK exposes no usable
    attribute. ``AzureChatOpenAI`` is the case that matters: it is constructed
    from ``azure_deployment`` and leaves ``model_name`` at ``None``, so the
    attribute probe below returns ``""`` and every Azure usage row recorded a
    blank model.

    The probe survives as a **fallback for models we did not build** — test
    fakes, doubles, and anything constructed outside :func:`chat_model`.
    ``ChatOpenAI`` exposes ``model_name``, ``ChatAnthropic``/``ChatVertexAI``/
    ``ChatGoogleGenerativeAI``/``ChatMistralAI``/``ChatCohere`` expose ``model``,
    ``ChatBedrockConverse`` exposes ``model_id``; anything else → ``""``.
    """
    identity = _IDENTITIES.get(id(model))
    if identity is not None:
        return identity.name
    for attr in ("model_name", "model", "model_id"):
        v = getattr(model, attr, None)
        if isinstance(v, str) and v:
            return v
    return ""


def model_handle(
    settings: LlmSettings,
    *,
    temperature: float = 0.1,
    disable_reasoning: bool = False,
) -> ModelHandle:
    """Build a chat model together with what its provider knows about it.

    The honest path for new code: it carries the model's identity and its
    provider's capabilities instead of leaving callers to interrogate the SDK
    object. :func:`chat_model` is this function with the metadata discarded.
    """
    provider = provider_for(settings)
    model = provider.build(
        settings, temperature=temperature, disable_reasoning=disable_reasoning
    )
    identity = _Identity(
        name=provider.model_name(settings),
        provider=provider.provider_name(settings),
        capabilities=provider.capabilities,
    )
    _remember(model, identity)
    return ModelHandle(
        model=model,
        name=identity.name,
        provider=identity.provider,
        capabilities=identity.capabilities,
    )


def chat_model(
    settings: LlmSettings,
    *,
    temperature: float = 0.1,
    disable_reasoning: bool = False,
) -> BaseChatModel:
    """Return a chat model for tool calling, built by *settings*' provider.

    ``disable_reasoning`` turns off a thinking model's internal reasoning; only
    the OpenAI-compatible provider implements it (see
    ``providers/openai_compat.py``), the rest ignore it — ask
    :attr:`Capability.REASONING_TOGGLE` on the handle if that matters.
    """
    return model_handle(
        settings, temperature=temperature, disable_reasoning=disable_reasoning
    ).model


def supports(model: BaseChatModel | object, capability: Capability) -> bool:
    """Whether *model*'s provider supports *capability*.

    ``False`` for a model this system did not build — an unknown provider makes
    no promises, which is the safe reading for every capability declared so far.
    """
    handle = handle_for(model)
    return handle is not None and handle.has(capability)


__all__ = [
    "chat_model",
    "handle_for",
    "model_handle",
    "model_name_of",
    "supports",
]


def _registered_count() -> int:
    """Live entry count — for the leak test in ``test/llm/test_model_handle.py``."""
    return len(_IDENTITIES)
