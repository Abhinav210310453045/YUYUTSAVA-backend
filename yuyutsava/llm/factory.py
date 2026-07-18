"""``chat_model()`` — the one seam every model in the system is built through.

All 12 construction sites (CLI, tinker, orchestrator, triage, subagents,
compaction, ``model_router`` tiers) call this. That is what makes it the right
place to apply provider quirks: a middleware would miss every caller that has no
agent loop — ``TriageAgent`` invokes ``model.with_structured_output(...)``
directly, and the compaction model is invoked *inside* the summarization
middleware.

The signature is deliberately unchanged from the pre-refactor version, so no
caller had to move.
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from yuyutsava.core.config import LlmSettings
from yuyutsava.llm.providers import provider_for


def model_name_of(model: BaseChatModel | object) -> str:
    """Best-effort model identifier for logging / usage rows.

    ``ChatOpenAI`` exposes ``model_name``, ``ChatAnthropic``/``ChatVertexAI``/
    ``ChatGoogleGenerativeAI``/``ChatMistralAI``/``ChatCohere`` expose ``model``,
    ``ChatBedrockConverse`` exposes ``model_id``; fakes and stubs typically expose
    none → "".

    This dispatches on the *model object*, not on settings, so it cannot go
    through :func:`provider_for` — callers hold a model long after its settings
    are gone. A per-provider accessor on :class:`~yuyutsava.llm.base.Provider` is
    the natural home once something needs it to be exact rather than best-effort.
    """
    for attr in ("model_name", "model", "model_id"):
        v = getattr(model, attr, None)
        if isinstance(v, str) and v:
            return v
    return ""


def chat_model(
    settings: LlmSettings,
    *,
    temperature: float = 0.1,
    disable_reasoning: bool = False,
) -> BaseChatModel:
    """Return a chat model for tool calling, built by *settings*' provider.

    ``disable_reasoning`` turns off a thinking model's internal reasoning; only
    the OpenAI-compatible provider implements it (see
    ``providers/openai_compat.py``), the rest ignore it.
    """
    return provider_for(settings).build(
        settings, temperature=temperature, disable_reasoning=disable_reasoning
    )


__all__ = ["chat_model", "model_name_of"]
