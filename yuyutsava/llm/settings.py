"""How to reach each LLM provider: settings loaded from the environment.

Supported LLM providers (set ``LLM_PROVIDER``):

OpenAI-compatible (handled by ``ChatOpenAI`` + ``base_url``):

- Groq:              https://console.groq.com/docs/overview
- OpenRouter:        https://openrouter.ai/docs/quickstart
- Ollama:            https://ollama.com/  (local, no API key required)
- OpenAI:            https://platform.openai.com/  (``openai``)
- OpenAI-compatible: any host (xAI, DeepSeek, Together, Fireworks, Perplexity,
                     Cerebras, DeepInfra, …) via ``openai_compatible`` + base_url

Native provider SDKs (lazy-imported; install the matching extra):

- Anthropic:    https://console.anthropic.com/                  (``anthropic``)
- Google Gemini:https://ai.google.dev/                          (``google``/``gemini``)
- Vertex AI:    https://cloud.google.com/vertex-ai              (``vertex``)
- AWS Bedrock:  https://aws.amazon.com/bedrock/                 (``bedrock``/``aws``)
- Azure OpenAI: https://learn.microsoft.com/azure/ai-services/  (``azure``/``azure_openai``)
- Mistral:      https://docs.mistral.ai/                        (``mistral``)
- Cohere:       https://docs.cohere.com/                        (``cohere``)

Role-prefixed overrides
-----------------------
``llm_settings_from_env(role)`` lets a daemon role (e.g. ``triage``,
``orchestrator``, ``file_organizer``) pick a different provider/model from the
``main`` one without changing the CLI's behaviour. Each lookup tries
``<ROLE>_<NAME>`` first and falls back to ``<NAME>``::

    LLM_PROVIDER=anthropic ANTHROPIC_MODEL=claude-haiku-4-5-...
    TRIAGE_LLM_PROVIDER=ollama OLLAMA_MODEL=llama3.2:3b

Calls to ``llm_settings_from_env()`` (no role) are byte-equivalent to today.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Protocol

from yuyutsava.config.env import env, env_int

GROQ_OPENAI_BASE_URL = "https://api.groq.com/openai/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OLLAMA_BASE_URL = "http://localhost:11434/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"

# OpenAI-compatible long-tail providers reachable through the generic
# ``openai_compatible`` entry — the user just points the base_url at one of these
# (or any other OpenAI-compatible host) and sets the model:
#   xAI/Grok    https://api.x.ai/v1
#   DeepSeek    https://api.deepseek.com/v1
#   Together    https://api.together.xyz/v1
#   Fireworks   https://api.fireworks.ai/inference/v1
#   Perplexity  https://api.perplexity.ai


# ---------------------------------------------------------------------------
# LLM provider settings
# ---------------------------------------------------------------------------


class LlmSettings(Protocol):
    """Structural type for ``ChatOpenAI``-compatible provider configs."""

    @property
    def api_key(self) -> str: ...
    @property
    def base_url(self) -> str: ...
    @property
    def model(self) -> str: ...


@dataclass(frozen=True)
class GroqSettings:
    #: Input window the context budget steers under — Groq-hosted open models (Llama, Qwen, …) take 128k.
    max_input_tokens: ClassVar[int] = 128_000

    api_key: str
    base_url: str = GROQ_OPENAI_BASE_URL
    model: str = "llama-3.3-70b-versatile"

    @classmethod
    def from_env(cls, role: str | None = None) -> GroqSettings:
        key = env("GROQ_API_KEY", role)
        if not key:
            raise RuntimeError(
                "Set GROQ_API_KEY in the environment (or .env loaded by the CLI). "
                "See https://console.groq.com/docs/overview"
            )
        base = env("GROQ_BASE_URL", role, GROQ_OPENAI_BASE_URL)
        model = env("GROQ_MODEL", role, "llama-3.3-70b-versatile")
        return cls(api_key=key, base_url=base, model=model)


@dataclass(frozen=True)
class OpenRouterSettings:
    """OpenRouter unified API (OpenAI-compatible chat completions)."""

    #: Input window the context budget steers under — varies by routed model; 128k is the common window.
    max_input_tokens: ClassVar[int] = 128_000

    api_key: str
    base_url: str = OPENROUTER_BASE_URL
    model: str = "openai/gpt-4o-mini"
    default_headers: dict[str, str] | None = None

    @classmethod
    def from_env(cls, role: str | None = None) -> OpenRouterSettings:
        key = env("OPENROUTER_API_KEY", role)
        if not key:
            raise RuntimeError(
                "Set OPENROUTER_API_KEY in the environment (or .env loaded by the CLI). "
                "See https://openrouter.ai/docs/quickstart"
            )
        base = env("OPENROUTER_BASE_URL", role, OPENROUTER_BASE_URL)
        model = env("OPENROUTER_MODEL", role, "openai/gpt-4o-mini")
        headers: dict[str, str] = {}
        referer = env("OPENROUTER_HTTP_REFERER", role)
        title = env("OPENROUTER_APP_TITLE", role)
        if referer:
            headers["HTTP-Referer"] = referer
        if title:
            headers["X-OpenRouter-Title"] = title
        dh = headers if headers else None
        return cls(api_key=key, base_url=base, model=model, default_headers=dh)


@dataclass(frozen=True)
class AnthropicSettings:
    """Anthropic API provider (enables prompt caching via AnthropicPromptCachingMiddleware)."""

    #: Input window the context budget steers under — Claude models take 200k.
    max_input_tokens: ClassVar[int] = 200_000

    api_key: str
    base_url: str = "https://api.anthropic.com"
    model: str = "claude-haiku-4-5-20251001"

    @classmethod
    def from_env(cls, role: str | None = None) -> AnthropicSettings:
        key = env("ANTHROPIC_API_KEY", role)
        if not key:
            raise RuntimeError(
                "Set ANTHROPIC_API_KEY in the environment. See https://console.anthropic.com/"
            )
        model = env("ANTHROPIC_MODEL", role, "claude-haiku-4-5-20251001")
        return cls(api_key=key, model=model)


@dataclass(frozen=True)
class OllamaSettings:
    """Ollama local inference server (OpenAI-compatible chat completions).

    Ollama exposes an OpenAI-compatible API at ``http://localhost:11434/v1``.
    No API key is required; the string ``"ollama"`` is sent as a placeholder
    because ``ChatOpenAI`` requires a non-empty value.

    See https://ollama.com/
    """

    #: Input window the context budget steers under — local models: a small window unless the server is configured larger.
    max_input_tokens: ClassVar[int] = 8_192

    api_key: str = "ollama"
    base_url: str = OLLAMA_BASE_URL
    model: str = "gemma4:e2b"

    @classmethod
    def from_env(cls, role: str | None = None) -> OllamaSettings:
        base = env("OLLAMA_HOST", role, OLLAMA_BASE_URL)
        base = base.rstrip("/")
        if not base.endswith("/v1"):
            base = base + "/v1"
        model = env("OLLAMA_MODEL", role, "gemma4:e2b")
        return cls(base_url=base, model=model)


@dataclass(frozen=True)
class OpenAISettings:
    """Native OpenAI API (OpenAI-compatible chat completions).

    Handled by the default ``ChatOpenAI`` path in ``yuyutsava.llm`` — no
    dedicated factory branch needed. See https://platform.openai.com/
    """

    #: Input window the context budget steers under — the GPT-4o family's window.
    max_input_tokens: ClassVar[int] = 128_000

    api_key: str
    base_url: str = OPENAI_BASE_URL
    model: str = "gpt-4o-mini"

    @classmethod
    def from_env(cls, role: str | None = None) -> OpenAISettings:
        key = env("OPENAI_API_KEY", role)
        if not key:
            raise RuntimeError(
                "Set OPENAI_API_KEY in the environment (or .env loaded by the CLI). "
                "See https://platform.openai.com/api-keys"
            )
        base = env("OPENAI_BASE_URL", role, OPENAI_BASE_URL)
        model = env("OPENAI_MODEL", role, "gpt-4o-mini")
        return cls(api_key=key, base_url=base, model=model)


@dataclass(frozen=True)
class OpenAICompatibleSettings:
    """Generic OpenAI-compatible endpoint (xAI, DeepSeek, Together, Fireworks,
    Perplexity, Cerebras, DeepInfra, …).

    Point ``OPENAI_COMPATIBLE_BASE_URL`` at the provider and set the model. Handled
    by the default ``ChatOpenAI`` path — no dedicated factory branch needed.
    """

    #: Input window the context budget steers under — an unknown host: the common window.
    max_input_tokens: ClassVar[int] = 128_000

    api_key: str
    base_url: str
    model: str
    default_headers: dict[str, str] | None = None

    @classmethod
    def from_env(cls, role: str | None = None) -> OpenAICompatibleSettings:
        base = env("OPENAI_COMPATIBLE_BASE_URL", role)
        if not base:
            raise RuntimeError(
                "Set OPENAI_COMPATIBLE_BASE_URL for LLM_PROVIDER=openai_compatible "
                "(e.g. https://api.x.ai/v1, https://api.deepseek.com/v1, "
                "https://api.together.xyz/v1)."
            )
        key = env("OPENAI_COMPATIBLE_API_KEY", role)
        if not key:
            raise RuntimeError("Set OPENAI_COMPATIBLE_API_KEY for LLM_PROVIDER=openai_compatible.")
        model = env("OPENAI_COMPATIBLE_MODEL", role)
        if not model:
            raise RuntimeError("Set OPENAI_COMPATIBLE_MODEL for LLM_PROVIDER=openai_compatible.")
        return cls(api_key=key, base_url=base.rstrip("/"), model=model)


@dataclass(frozen=True)
class GoogleSettings:
    """Google Gemini via the AI Studio (Developer) API — ``ChatGoogleGenerativeAI``.

    Needs ``langchain-google-genai`` (``pip install 'yuyutsava[google]'``).
    See https://ai.google.dev/
    """

    #: Input window the context budget steers under — Gemini takes ~1M.
    max_input_tokens: ClassVar[int] = 1_000_000

    api_key: str
    model: str = "gemini-2.5-flash"

    @property
    def base_url(self) -> str:  # structural LlmSettings conformance (n/a here)
        return ""

    @classmethod
    def from_env(cls, role: str | None = None) -> GoogleSettings:
        key = env("GOOGLE_API_KEY", role) or env("GEMINI_API_KEY", role)
        if not key:
            raise RuntimeError(
                "Set GOOGLE_API_KEY (or GEMINI_API_KEY) in the environment. "
                "See https://ai.google.dev/gemini-api/docs/api-key"
            )
        model = env("GOOGLE_MODEL", role, "gemini-2.5-flash")
        return cls(api_key=key, model=model)


@dataclass(frozen=True)
class VertexSettings:
    """Google Gemini on Vertex AI — ``ChatVertexAI``.

    Auth is Google Application Default Credentials (``gcloud auth
    application-default login``); no API key. Needs ``langchain-google-vertexai``
    (``pip install 'yuyutsava[vertex]'``). See https://cloud.google.com/vertex-ai
    """

    #: Input window the context budget steers under — Gemini on Vertex takes ~1M.
    max_input_tokens: ClassVar[int] = 1_000_000

    project: str
    location: str = "us-central1"
    model: str = "gemini-2.5-flash"
    # Retries for transient Vertex errors (429 RESOURCE_EXHAUSTED, 503, …).
    # Applies to both the SDK's own tenacity wrapper (non-streaming) and the
    # first-chunk retry quirk that covers the streaming path the SDK misses.
    max_retries: int = 6

    @property
    def api_key(self) -> str:  # ADC auth — no key
        return ""

    @property
    def base_url(self) -> str:  # structural LlmSettings conformance (n/a here)
        return ""

    @classmethod
    def from_env(cls, role: str | None = None) -> VertexSettings:
        project = env("VERTEX_PROJECT", role) or env("GOOGLE_CLOUD_PROJECT", role)
        if not project:
            raise RuntimeError(
                "Set VERTEX_PROJECT (or GOOGLE_CLOUD_PROJECT) for LLM_PROVIDER=vertex, "
                "and run `gcloud auth application-default login`."
            )
        location = env("VERTEX_LOCATION", role, "us-central1")
        model = env("VERTEX_MODEL", role, "gemini-2.5-flash")
        max_retries = max(0, env_int("VERTEX_MAX_RETRIES", role, 6))
        return cls(project=project, location=location, model=model, max_retries=max_retries)


@dataclass(frozen=True)
class BedrockSettings:
    """AWS Bedrock via the Converse API — ``ChatBedrockConverse``.

    Auth is the standard boto3 credential chain (env vars, shared config, IAM
    role). Needs ``langchain-aws`` (``pip install 'yuyutsava[bedrock]'``).
    See https://aws.amazon.com/bedrock/
    """

    #: Input window the context budget steers under — Claude on Bedrock takes 200k.
    max_input_tokens: ClassVar[int] = 200_000

    region: str
    model: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

    @property
    def api_key(self) -> str:  # boto3 credential chain — no key here
        return ""

    @property
    def base_url(self) -> str:  # structural LlmSettings conformance (n/a here)
        return ""

    @classmethod
    def from_env(cls, role: str | None = None) -> BedrockSettings:
        region = env("BEDROCK_REGION", role) or env("AWS_REGION", role, "us-east-1")
        model = env("BEDROCK_MODEL", role, "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
        return cls(region=region, model=model)


@dataclass(frozen=True)
class AzureOpenAISettings:
    """Azure OpenAI Service — ``AzureChatOpenAI`` (from the installed
    ``langchain-openai``; no extra needed).

    See https://learn.microsoft.com/azure/ai-services/openai/
    """

    #: Input window the context budget steers under — the GPT-4o family's window.
    max_input_tokens: ClassVar[int] = 128_000

    api_key: str
    azure_endpoint: str
    azure_deployment: str
    api_version: str = "2024-10-21"
    model: str = ""  # deployment name is authoritative; model is informational

    @property
    def base_url(self) -> str:  # structural LlmSettings conformance
        return self.azure_endpoint

    @classmethod
    def from_env(cls, role: str | None = None) -> AzureOpenAISettings:
        key = env("AZURE_OPENAI_API_KEY", role)
        if not key:
            raise RuntimeError(
                "Set AZURE_OPENAI_API_KEY for LLM_PROVIDER=azure. "
                "See https://learn.microsoft.com/azure/ai-services/openai/"
            )
        endpoint = env("AZURE_OPENAI_ENDPOINT", role)
        if not endpoint:
            raise RuntimeError(
                "Set AZURE_OPENAI_ENDPOINT (e.g. https://<resource>.openai.azure.com) "
                "for LLM_PROVIDER=azure."
            )
        deployment = env("AZURE_OPENAI_DEPLOYMENT", role)
        if not deployment:
            raise RuntimeError(
                "Set AZURE_OPENAI_DEPLOYMENT (your model deployment name) for LLM_PROVIDER=azure."
            )
        api_version = env("AZURE_OPENAI_API_VERSION", role, "2024-10-21")
        model = env("AZURE_OPENAI_MODEL", role, deployment)
        return cls(
            api_key=key,
            azure_endpoint=endpoint,
            azure_deployment=deployment,
            api_version=api_version,
            model=model,
        )


@dataclass(frozen=True)
class MistralSettings:
    """Mistral AI — ``ChatMistralAI``.

    Needs ``langchain-mistralai`` (``pip install 'yuyutsava[mistral]'``).
    See https://docs.mistral.ai/
    """

    #: Input window the context budget steers under — Mistral Large takes 128k.
    max_input_tokens: ClassVar[int] = 128_000

    api_key: str
    model: str = "mistral-large-latest"

    @property
    def base_url(self) -> str:  # structural LlmSettings conformance (n/a here)
        return ""

    @classmethod
    def from_env(cls, role: str | None = None) -> MistralSettings:
        key = env("MISTRAL_API_KEY", role)
        if not key:
            raise RuntimeError(
                "Set MISTRAL_API_KEY in the environment. See https://console.mistral.ai/api-keys/"
            )
        model = env("MISTRAL_MODEL", role, "mistral-large-latest")
        return cls(api_key=key, model=model)


@dataclass(frozen=True)
class CohereSettings:
    """Cohere — ``ChatCohere``.

    Needs ``langchain-cohere`` (``pip install 'yuyutsava[cohere]'``).
    See https://docs.cohere.com/
    """

    #: Input window the context budget steers under — Command R takes 128k.
    max_input_tokens: ClassVar[int] = 128_000

    api_key: str
    model: str = "command-r-plus"

    @property
    def base_url(self) -> str:  # structural LlmSettings conformance (n/a here)
        return ""

    @classmethod
    def from_env(cls, role: str | None = None) -> CohereSettings:
        key = env("COHERE_API_KEY", role)
        if not key:
            raise RuntimeError(
                "Set COHERE_API_KEY in the environment. See https://dashboard.cohere.com/api-keys"
            )
        model = env("COHERE_MODEL", role, "command-r-plus")
        return cls(api_key=key, model=model)


#: Every accepted ``LLM_PROVIDER`` value and the settings class it selects.
#: Adding a provider is one row here — not a branch in a chain plus a name in an
#: error message that the chain could drift away from. The first alias of each
#: class is its canonical name, which is what an error lists.
SETTINGS_BY_PROVIDER: dict[str, type] = {
    # OpenAI-compatible providers (the ChatOpenAI path)
    "groq": GroqSettings,
    "openrouter": OpenRouterSettings,
    "ollama": OllamaSettings,
    "openai": OpenAISettings,
    "openai_compatible": OpenAICompatibleSettings,
    "custom": OpenAICompatibleSettings,
    # Native-SDK providers (a dedicated factory, lazily imported)
    "anthropic": AnthropicSettings,
    "google": GoogleSettings,
    "gemini": GoogleSettings,
    "vertex": VertexSettings,
    "bedrock": BedrockSettings,
    "aws": BedrockSettings,
    "azure": AzureOpenAISettings,
    "azure_openai": AzureOpenAISettings,
    "mistral": MistralSettings,
    "cohere": CohereSettings,
}


def _canonical_names() -> list[str]:
    """One name per provider, in table order: the first alias of each class."""
    seen: set[type] = set()
    names: list[str] = []
    for name, cls in SETTINGS_BY_PROVIDER.items():
        if cls not in seen:
            seen.add(cls)
            names.append(name)
    return names


def llm_settings_from_env(role: str | None = None) -> LlmSettings:
    """Pick provider via ``<ROLE>_LLM_PROVIDER`` (falls back to ``LLM_PROVIDER``).

    ``role`` may be ``None`` (no prefix — current CLI behaviour), ``"main"``
    (alias for None), or a custom role name like ``"triage"``,
    ``"orchestrator"``, ``"file_organizer"``. Roles let the daemon run a small
    cheap model for triage and a stronger one for the orchestrator.
    """
    effective_role = None if role in (None, "main") else role
    provider = env("LLM_PROVIDER", effective_role, "groq").lower()
    cls = SETTINGS_BY_PROVIDER.get(provider)
    if cls is None:
        raise RuntimeError(
            f"Unknown LLM_PROVIDER={provider!r}; use one of: {', '.join(_canonical_names())}."
        )
    return cls.from_env(effective_role)
