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
SecretReference = Annotated[
    str,
    AfterValidator(_reference_only),
    Field(
        description="A reference, never the value: secret:<name> (secrets.toml), "
        "env:VARIABLE or file:<path>."
    ),
]


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
    key_file: str = Field(min_length=1, description="Path to the service account's JSON key file.")


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
    profile: str = Field(min_length=1, description="A profile name from ~/.aws/config.")


class AwsKeysLogin(_Login):
    """An access key pair, with a session token for temporary credentials."""

    method: Literal["aws_keys"]
    access_key_id: SecretReference
    secret_access_key: SecretReference
    session_token: SecretReference | None = Field(
        None, description="For temporary credentials only: their session token, as a reference."
    )
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
    project: str = Field(min_length=1, description="The Google Cloud project id.")
    location: str = Field(
        min_length=1,
        description="Where requests are served: global (the most models) or a region such as "
        "us-central1. A model alias may override it.",
    )
    auth: VertexLogin


class GoogleConnection(_Table):
    """Gemini through Google AI Studio."""

    type: Literal["google"]
    auth: ApiKeyLogin


class BedrockConnection(_Table):
    """AWS Bedrock: every model family Bedrock serves, through the Converse API."""

    type: Literal["bedrock"]
    region: str = Field(
        min_length=1, description="The AWS region, e.g. us-east-1. A model alias may override it."
    )
    auth: BedrockLogin


class AnthropicConnection(_Table):
    """Claude through Anthropic's own API."""

    type: Literal["anthropic"]
    auth: ApiKeyLogin


class OpenAIConnection(_Table):
    """OpenAI's API."""

    type: Literal["openai"]
    auth: ApiKeyLogin
    base_url: str = Field(OPENAI_BASE_URL, description="Where the API is served.")


class OpenRouterConnection(_Table):
    """OpenRouter: hundreds of models behind one key."""

    type: Literal["openrouter"]
    auth: ApiKeyLogin
    base_url: str = Field(OPENROUTER_BASE_URL, description="Where the API is served.")
    http_referer: str | None = Field(
        None, description="Optional: the site OpenRouter credits on its leaderboards."
    )
    app_title: str | None = Field(
        None, description="Optional: the app name OpenRouter shows on its leaderboards."
    )


class GroqConnection(_Table):
    """Groq's API."""

    type: Literal["groq"]
    auth: ApiKeyLogin
    base_url: str = Field(GROQ_BASE_URL, description="Where the API is served.")


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
    endpoint: str = Field(
        min_length=1, description="The resource's endpoint: https://<resource>.openai.azure.com"
    )
    api_version: str = Field("2024-10-21", description="The Azure OpenAI API version.")
    auth: ApiKeyLogin


class OpenAICompatibleConnection(_Table):
    """Any server that speaks OpenAI's chat API (xAI, DeepSeek, Together, a proxy, …)."""

    type: Literal["openai_compatible"]
    base_url: str = Field(
        min_length=1, description="The server's OpenAI API address, e.g. https://api.x.ai/v1"
    )
    auth: OpenAICompatibleLogin


class OllamaConnection(_Table):
    """An Ollama server, local by default."""

    type: Literal["ollama"]
    base_url: str = Field(OLLAMA_BASE_URL, description="Where the Ollama server listens.")
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

    budget: NonNegativeInt = Field(
        description="Tokens it may think for, on top of the answer's own; 0 turns thinking off."
    )


class Price(_Table):
    """US dollars per million tokens, when the provider's own price is not known or not wanted."""

    input: NonNegativeFloat = Field(description="US dollars per million tokens sent to it.")
    output: NonNegativeFloat = Field(description="US dollars per million tokens it answers with.")


class ModelEntry(_Table):
    """A model under a short name, with anything that should differ from the provider's defaults."""

    provider: Name = Field(description="The [providers.<name>] that serves it.")
    id: str = Field(min_length=1, description="The model id, as the provider names it.")
    family: Literal["gemini", "anthropic", "maas"] | None = Field(
        None,
        description="Vertex only: which family serves id. Inferred when left out: claude-* is "
        "anthropic, <publisher>/<name>-maas is maas, anything else gemini.",
    )
    location: str | None = Field(
        None, description="Vertex only: a location for this model instead of the provider's."
    )
    region: str | None = Field(
        None, description="Bedrock only: a region for this model instead of the provider's."
    )
    base_model: str | None = Field(
        None,
        description="Bedrock and Azure only: the model behind id when id names an application "
        "inference profile (Bedrock) or a deployment (Azure) — what prices and limits are "
        "looked up by.",
    )
    context_window: PositiveInt | None = Field(
        None, description="The input window in tokens, when the known one is wrong or unknown."
    )
    max_output: PositiveInt | None = Field(None, description="The most tokens one answer may have.")
    thinking: Thinking | None = Field(
        None, description="For a thinking model: how many tokens it may think before answering."
    )
    price: Price | None = Field(
        None, description="Its price, when the provider's own is not known or not wanted."
    )


class Defaults(_Table):
    """The model YUYUTSAVA uses unless told otherwise."""

    model: str = Field(
        min_length=1,
        description="The model every task uses unless [tasks] gives it one: an alias or "
        "<provider>/<model id>.",
    )


class Security(_Table):
    """How YUYUTSAVA guards changes to your keys."""

    require_user_presence: bool = Field(
        False,
        description="Have the OS confirm it is you (Touch ID or your password, Windows Hello, "
        "polkit) before YUYUTSAVA stores or removes a key, or turns this off.",
    )


class ModelsConfig(_Table):
    """``models.toml`` as a whole."""

    version: Literal[1] = Field(description="The file's format: 1.")
    defaults: Defaults
    tasks: dict[str, str] = Field(
        default_factory=dict,
        description="A model per task: chat, orchestrator, triage, subagent, subagent.<name>, "
        "compaction, tier_light, tier_standard, tier_heavy. A task left out uses the default.",
    )
    providers: dict[Name, Connection] = Field(
        min_length=1, description="Where models are served, and how to log in there."
    )
    models: dict[Name, ModelEntry] = Field(
        default_factory=dict, description="Short names for models, with their own settings."
    )
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
