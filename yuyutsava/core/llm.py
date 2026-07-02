"""
Build a LangChain chat model for the configured provider.

Supported providers (set LLM_PROVIDER env var):

OpenAI-compatible (default ``ChatOpenAI`` path, no dedicated branch):
- groq              → ChatOpenAI pointed at Groq
- openrouter        → ChatOpenAI pointed at OpenRouter
- ollama            → ChatOpenAI pointed at local Ollama (http://localhost:11434/v1)
- openai            → ChatOpenAI pointed at OpenAI
- openai_compatible → ChatOpenAI pointed at any OpenAI-compatible host
                      (xAI, DeepSeek, Together, Fireworks, Perplexity, …)

Native provider SDKs (lazy-imported; install the matching extra):
- anthropic → ChatAnthropic (prompt caching via AnthropicPromptCachingMiddleware)
- google    → ChatGoogleGenerativeAI          [yuyutsava[google]]
- vertex    → ChatVertexAI                     [yuyutsava[vertex]]
- bedrock   → ChatBedrockConverse              [yuyutsava[bedrock]]
- azure     → AzureChatOpenAI                  (langchain-openai, already installed)
- mistral   → ChatMistralAI                    [yuyutsava[mistral]]
- cohere    → ChatCohere                       [yuyutsava[cohere]]
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from yuyutsava.core.config import (
    AnthropicSettings,
    AzureOpenAISettings,
    BedrockSettings,
    CohereSettings,
    GoogleSettings,
    LlmSettings,
    MistralSettings,
    VertexSettings,
)


def model_name_of(model: BaseChatModel | object) -> str:
    """Best-effort model identifier for logging / usage rows.

    ``ChatOpenAI`` exposes ``model_name``, ``ChatAnthropic``/``ChatVertexAI``/
    ``ChatGoogleGenerativeAI``/``ChatMistralAI``/``ChatCohere`` expose ``model``,
    ``ChatBedrockConverse`` exposes ``model_id``; fakes and stubs typically expose
    none → "".
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
    """Return a chat model for tool calling. Uses ChatAnthropic for AnthropicSettings, ChatOpenAI otherwise.

    ``disable_reasoning`` turns off a thinking/reasoning model's internal
    reasoning on the OpenAI-compatible (OpenRouter) path. Thinking models such as
    ``google/gemini-2.5-flash`` otherwise spend the completion-token budget on
    reasoning tokens, which can truncate small structured outputs (e.g. the
    triage decision JSON). No-op on the Anthropic path.
    """
    if isinstance(settings, AnthropicSettings):
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as exc:
            raise RuntimeError(
                "langchain-anthropic is required for LLM_PROVIDER=anthropic. "
                "Run: pip install langchain-anthropic"
            ) from exc
        return ChatAnthropic(
            api_key=SecretStr(settings.api_key),
            model_name=settings.model,
            temperature=temperature,
            max_tokens_to_sample=4096
        )

    if isinstance(settings, GoogleSettings):
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError as exc:
            raise RuntimeError(
                "langchain-google-genai is required for LLM_PROVIDER=google. "
                "Run: pip install 'yuyutsava[google]'"
            ) from exc
        return ChatGoogleGenerativeAI(
            api_key=SecretStr(settings.api_key),
            model=settings.model,
            temperature=temperature,
            max_output_tokens=4096,
        )

    if isinstance(settings, VertexSettings):
        try:
            from langchain_google_vertexai import ChatVertexAI
        except ImportError as exc:
            raise RuntimeError(
                "langchain-google-vertexai is required for LLM_PROVIDER=vertex. "
                "Run: pip install 'yuyutsava[vertex]'"
            ) from exc
        return ChatVertexAI(
            model=settings.model,
            project=settings.project,
            location=settings.location,
            temperature=temperature,
            max_output_tokens=4096,
        )

    if isinstance(settings, BedrockSettings):
        try:
            from langchain_aws import ChatBedrockConverse
        except ImportError as exc:
            raise RuntimeError(
                "langchain-aws is required for LLM_PROVIDER=bedrock. "
                "Run: pip install 'yuyutsava[bedrock]'"
            ) from exc
        return ChatBedrockConverse(
            model=settings.model,
            region_name=settings.region,
            temperature=temperature,
            max_tokens=4096,
        )

    if isinstance(settings, AzureOpenAISettings):
        # AzureChatOpenAI ships with langchain-openai (already a core dep).
        from langchain_openai import AzureChatOpenAI

        return AzureChatOpenAI(
            api_key=SecretStr(settings.api_key),
            azure_endpoint=settings.azure_endpoint,
            azure_deployment=settings.azure_deployment,
            api_version=settings.api_version,
            temperature=temperature,
            max_tokens=4096,
        )

    if isinstance(settings, MistralSettings):
        try:
            from langchain_mistralai import ChatMistralAI
        except ImportError as exc:
            raise RuntimeError(
                "langchain-mistralai is required for LLM_PROVIDER=mistral. "
                "Run: pip install 'yuyutsava[mistral]'"
            ) from exc
        return ChatMistralAI(
            api_key=SecretStr(settings.api_key),
            model_name=settings.model,
            temperature=temperature,
            max_tokens=4096,
        )

    if isinstance(settings, CohereSettings):
        try:
            from langchain_cohere import ChatCohere
        except ImportError as exc:
            raise RuntimeError(
                "langchain-cohere is required for LLM_PROVIDER=cohere. "
                "Run: pip install 'yuyutsava[cohere]'"
            ) from exc
        return ChatCohere(
            cohere_api_key=SecretStr(settings.api_key),
            model=settings.model,
            temperature=temperature,
            max_tokens=4096,
        )

    headers = getattr(settings, "default_headers", None)
    # OpenRouter's unified reasoning control; ``enabled: false`` disables a
    # thinking model's reasoning so the full token budget goes to the answer.
    extra_body = {"reasoning": {"enabled": False}} if disable_reasoning else None
    return ChatOpenAI(
        api_key=SecretStr(settings.api_key),
        base_url=settings.base_url,
        model=settings.model,
        temperature=temperature,
        max_completion_tokens=4096,
        **({"default_headers": headers} if headers else {}),
        **({"extra_body": extra_body} if extra_body else {}),
    )
