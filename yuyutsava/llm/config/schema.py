"""The shape of ``models.toml``, as pydantic models.

Structure only: each table and key, its type, and which login methods each type
of provider accepts. Whether a reference points at something that exists — a
task's model, a model's provider — is checked by :mod:`.loader` afterwards, so
every broken reference in a file is reported together rather than one per run.

A setting that holds a secret takes a *reference* (``secret:``, ``env:`` or
``file:``), never the value (:mod:`.secrets`). Each login class lists those
settings in ``secret_fields``.

Unknown keys are errors, not ignored: a misspelt ``projct`` would otherwise be
dropped and the real setting silently fall back to nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, ClassVar, Literal, Union, get_args

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
)
from pydantic_core import PydanticCustomError

from yuyutsava.llm.config.secrets import NAME_PATTERN, SecretRefError, parse_secret_ref

#: A name the file defines and refers to: a provider, a model alias or a secret.
Name = Annotated[str, StringConstraints(pattern=NAME_PATTERN)]

#: Where each OpenAI-compatible provider type lives unless its connection says otherwise.
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"
OLLAMA_BASE_URL = "http://localhost:11434"

#: Thinking tokens a thinking model may spend before it answers, unless its alias
#: sets ``thinking.budget``; on top of the answer's own share.
DEFAULT_THINKING_BUDGET = 8192


def _reference_only(value: str) -> str:
    try:
        parse_secret_ref(value)
    except SecretRefError as exc:
        raise PydanticCustomError("secret_reference", exc.problem, {"fix": exc.fix}) from exc
    return value


#: A setting that holds a secret: a reference to it, never the value.
SecretReference = Annotated[str, AfterValidator(_reference_only)]


class _Table(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Login(_Table):
    #: The settings of this login that hold secret references.
    secret_fields: ClassVar[tuple[str, ...]] = ()


# ─── logins ───────────────────────────────────────────────────────────────


class AdcLogin(_Login):
    """Google Application Default Credentials (``gcloud auth application-default login``)."""

    method: Literal["adc"]


class ServiceAccountLogin(_Login):
    """A Google service-account key file."""

    method: Literal["service_account"]
    key_file: str = Field(min_length=1)


class AccessTokenLogin(_Login):
    """A Google OAuth access token, refreshed by whoever supplies it."""

    method: Literal["access_token"]
    token: SecretReference
    secret_fields: ClassVar[tuple[str, ...]] = ("token",)


class ApiKeyLogin(_Login):
    """An API key, sent as the provider expects."""

    method: Literal["api_key"]
    key: SecretReference
    secret_fields: ClassVar[tuple[str, ...]] = ("key",)


class BearerTokenLogin(_Login):
    """A token sent as ``Authorization: Bearer`` (a personal access token, a proxy's key)."""

    method: Literal["bearer_token"]
    token: SecretReference
    secret_fields: ClassVar[tuple[str, ...]] = ("token",)


class NoLogin(_Login):
    """No credentials — a local server."""

    method: Literal["none"]


class AwsDefaultChainLogin(_Login):
    """Whatever the AWS SDK finds itself: environment, ``~/.aws``, an instance role."""

    method: Literal["aws_default_chain"]


class AwsProfileLogin(_Login):
    """A named profile from ``~/.aws/config`` (SSO, assumed role, keys)."""

    method: Literal["aws_profile"]
    profile: str = Field(min_length=1)


class AwsKeysLogin(_Login):
    """An access key pair, with a session token for temporary credentials."""

    method: Literal["aws_keys"]
    access_key_id: SecretReference
    secret_access_key: SecretReference
    session_token: SecretReference | None = None
    secret_fields: ClassVar[tuple[str, ...]] = (
        "access_key_id",
        "secret_access_key",
        "session_token",
    )


class BedrockApiKeyLogin(_Login):
    """A Bedrock API key, sent as a bearer token."""

    method: Literal["bedrock_api_key"]
    key: SecretReference
    secret_fields: ClassVar[tuple[str, ...]] = ("key",)


def _one_of(*logins: type[_Login]) -> object:
    """A login that is any of *logins*, chosen by its ``method``."""
    return Annotated[Union[logins], Field(discriminator="method")]


VertexLogin = _one_of(AdcLogin, ServiceAccountLogin, AccessTokenLogin)
BedrockLogin = _one_of(AwsDefaultChainLogin, AwsProfileLogin, AwsKeysLogin, BedrockApiKeyLogin)
OpenAICompatibleLogin = _one_of(ApiKeyLogin, BearerTokenLogin, NoLogin)
OllamaLogin = _one_of(NoLogin, BearerTokenLogin)


# ─── providers ────────────────────────────────────────────────────────────


class VertexConnection(_Table):
    """Google Vertex AI: Gemini, Claude and open models in one Google Cloud project."""

    type: Literal["vertex"]
    project: str = Field(min_length=1)
    location: str = Field(min_length=1)
    auth: VertexLogin


class GoogleConnection(_Table):
    """Gemini through Google AI Studio."""

    type: Literal["google"]
    auth: ApiKeyLogin


class BedrockConnection(_Table):
    """AWS Bedrock: every model family Bedrock serves, through the Converse API."""

    type: Literal["bedrock"]
    region: str = Field(min_length=1)
    auth: BedrockLogin


class AnthropicConnection(_Table):
    """Claude through Anthropic's own API."""

    type: Literal["anthropic"]
    auth: ApiKeyLogin


class OpenAIConnection(_Table):
    """OpenAI's API."""

    type: Literal["openai"]
    auth: ApiKeyLogin
    base_url: str = OPENAI_BASE_URL


class OpenRouterConnection(_Table):
    """OpenRouter: hundreds of models behind one key."""

    type: Literal["openrouter"]
    auth: ApiKeyLogin
    base_url: str = OPENROUTER_BASE_URL
    #: Attribution OpenRouter shows on its leaderboards; both optional.
    http_referer: str | None = None
    app_title: str | None = None


class GroqConnection(_Table):
    """Groq's API."""

    type: Literal["groq"]
    auth: ApiKeyLogin
    base_url: str = GROQ_BASE_URL


class MistralConnection(_Table):
    """Mistral's API."""

    type: Literal["mistral"]
    auth: ApiKeyLogin


class CohereConnection(_Table):
    """Cohere's API."""

    type: Literal["cohere"]
    auth: ApiKeyLogin


class AzureConnection(_Table):
    """Azure OpenAI: a model's ``id`` is the name of its deployment."""

    type: Literal["azure"]
    endpoint: str = Field(min_length=1)
    api_version: str = "2024-10-21"
    auth: ApiKeyLogin


class OpenAICompatibleConnection(_Table):
    """Any server that speaks OpenAI's chat API (xAI, DeepSeek, Together, a proxy, …)."""

    type: Literal["openai_compatible"]
    base_url: str = Field(min_length=1)
    auth: OpenAICompatibleLogin


class OllamaConnection(_Table):
    """An Ollama server, local by default."""

    type: Literal["ollama"]
    base_url: str = OLLAMA_BASE_URL
    auth: OllamaLogin = NoLogin(method="none")


#: Every provider type, in the order people are shown them. One source: the
#: union below, the messages and the JSON Schema are all derived from it.
CONNECTIONS: tuple[type[_Table], ...] = (
    VertexConnection,
    GoogleConnection,
    BedrockConnection,
    AnthropicConnection,
    OpenAIConnection,
    OpenRouterConnection,
    GroqConnection,
    MistralConnection,
    CohereConnection,
    AzureConnection,
    OpenAICompatibleConnection,
    OllamaConnection,
)

Connection = Annotated[Union[CONNECTIONS], Field(discriminator="type")]


def connection_type(cls: type[_Table]) -> str:
    """The ``type = "…"`` value that selects *cls*."""
    return get_args(cls.model_fields["type"].annotation)[0]


def login_methods(cls: type[_Table]) -> tuple[str, ...]:
    """The login ``method`` values a provider type accepts, in declared order."""
    annotation = cls.model_fields["auth"].annotation
    variants = get_args(annotation) or (annotation,)
    return tuple(get_args(variant.model_fields["method"].annotation)[0] for variant in variants)


def connection_types() -> tuple[str, ...]:
    """Every ``type`` a provider may have."""
    return tuple(connection_type(cls) for cls in CONNECTIONS)


# ─── models, tasks, the file ──────────────────────────────────────────────


class Thinking(_Table):
    """How many tokens a thinking model may spend before it answers."""

    budget: NonNegativeInt


class Price(_Table):
    """US dollars per million tokens, when the provider's own price is not known or not wanted."""

    input: NonNegativeFloat
    output: NonNegativeFloat


class ModelEntry(_Table):
    """A model under a short name, with anything that should differ from the provider's defaults."""

    provider: Name
    id: str = Field(min_length=1)
    #: Vertex only: which family serves ``id``. Inferred when left out.
    family: Literal["gemini", "anthropic", "maas"] | None = None
    #: Vertex only: a location for this model instead of the provider's.
    location: str | None = None
    #: Bedrock only: a region for this model instead of the provider's.
    region: str | None = None
    #: Bedrock and Azure only: the model behind ``id`` when ``id`` names an
    #: application inference profile (Bedrock) or a deployment (Azure) — what
    #: prices and limits are looked up by.
    base_model: str | None = None
    context_window: PositiveInt | None = None
    max_output: PositiveInt | None = None
    thinking: Thinking | None = None
    price: Price | None = None


class Defaults(_Table):
    model: str = Field(min_length=1)


class Security(_Table):
    #: Ask the OS to confirm the owner is present before a secret is shown or changed.
    require_user_presence: bool = False


class ModelsConfig(_Table):
    """``models.toml`` as a whole."""

    version: Literal[1]
    defaults: Defaults
    tasks: dict[str, str] = Field(default_factory=dict)
    providers: dict[Name, Connection] = Field(min_length=1)
    models: dict[Name, ModelEntry] = Field(default_factory=dict)
    security: Security = Field(default_factory=Security)


# ─── reading references ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelRef:
    """A reference to a model: an alias, or a provider name and a model id."""

    #: The provider for ``provider/model-id``; ``None`` when the reference is an alias.
    provider: str | None
    #: The model id, or the alias.
    name: str


def parse_model_ref(text: str) -> ModelRef:
    """Split *text* on its first ``/``: ``openrouter/anthropic/claude`` is OpenRouter's model."""
    provider, sep, rest = text.partition("/")
    return ModelRef(provider, rest) if sep else ModelRef(None, text)


def secret_references(config: ModelsConfig) -> Iterator[tuple[str, str]]:
    """``(setting path, reference)`` for every secret the config refers to."""
    for name, connection in config.providers.items():
        login = connection.auth
        for field in login.secret_fields:
            value = getattr(login, field)
            if value:
                yield f"providers.{name}.auth.{field}", value


__all__ = [
    "CONNECTIONS",
    "Connection",
    "ModelEntry",
    "ModelRef",
    "ModelsConfig",
    "connection_type",
    "connection_types",
    "login_methods",
    "parse_model_ref",
    "secret_references",
]
