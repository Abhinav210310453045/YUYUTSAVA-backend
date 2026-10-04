"""How each provider is set up, said for a person — one source for every setup surface.

The CLI wizard (:mod:`yuyutsava.cli.setup_wizard`) and the app's wizard (through
``GET /models/catalog``) ask the same questions: for each provider type, what it
is, the settings it needs, and the ways it can log in with the fields each takes.
The *shape* comes from the schema (:mod:`.schema`), so a new provider type or
login method shows up here without a second list to keep in step; only the words
are written here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import get_args

from yuyutsava.llm.config.messages import type_label
from yuyutsava.llm.config.schema import CONNECTIONS, connection_type, login_methods

#: What each provider type is, and what it takes.
INTRODUCTIONS: dict[str, str] = {
    "vertex": "Gemini, Claude and open models in your Google Cloud project — needs a project",
    "google": "Gemini with a Google AI Studio key — the quickest Google start",
    "bedrock": "every model AWS Bedrock serves — needs an AWS login",
    "anthropic": "Claude with an Anthropic API key",
    "openai": "GPT models with an OpenAI API key",
    "openrouter": "hundreds of models behind one OpenRouter key",
    "groq": "fast open models with a Groq key (free tier)",
    "mistral": "Mistral models with a Mistral key",
    "cohere": "Command models with a Cohere key",
    "azure": "OpenAI models deployed in your Azure resource",
    "openai_compatible": "any server that speaks OpenAI's API (xAI, DeepSeek, a proxy, …)",
    "ollama": "models running on this machine — free, private, no key",
}

#: How each login method is offered, and what to do before choosing it.
LOGINS: dict[str, str] = {
    "adc": "my gcloud login — run `gcloud auth application-default login` first",
    "service_account": "a service-account key file (JSON)",
    "access_token": "an access token I paste (expires in about an hour)",
    "api_key": "an API key",
    "bearer_token": "a token sent as a bearer token",
    "none": "no login",
    "aws_default_chain": "whatever AWS finds by itself (environment, ~/.aws, an instance role)",
    "aws_profile": "a profile from ~/.aws/config",
    "aws_keys": "an access key pair",
    "bedrock_api_key": "a Bedrock API key",
}

#: How a setting is asked for, and what it starts as.
SETTINGS: dict[str, tuple[str, str]] = {
    "project": ("Google Cloud project id", ""),
    "location": ("location (global serves the most models)", "global"),
    "region": ("AWS region", "us-east-1"),
    "endpoint": ("Azure endpoint (https://<resource>.openai.azure.com)", ""),
    "base_url": ("server address", ""),
    "key_file": ("path to the key file", ""),
    "profile": ("profile name", "default"),
}


@dataclass(frozen=True)
class Field:
    """One thing to ask for."""

    name: str
    question: str
    default: str = ""
    #: Typed but never shown; kept in ``secrets.toml`` and referred to as ``secret:<name>``.
    secret: bool = False


@dataclass(frozen=True)
class LoginForm:
    method: str
    label: str
    fields: tuple[Field, ...]


@dataclass(frozen=True)
class ProviderForm:
    """What setting up one provider type asks."""

    type: str
    label: str
    introduction: str
    settings: tuple[Field, ...]
    logins: tuple[LoginForm, ...]


def secret_name(provider: str, field: str) -> str:
    """The ``secrets.toml`` name a provider's secret *field* is kept under."""
    return provider if field in ("key", "token") else f"{provider}-{field.replace('_', '-')}"


def provider_forms() -> tuple[ProviderForm, ...]:
    """Every provider type's form, in the order people are shown them."""
    return tuple(_form(cls) for cls in CONNECTIONS)


def provider_form(kind: str) -> ProviderForm:
    """The form for provider type *kind*; :class:`KeyError` when there is no such type."""
    for form in provider_forms():
        if form.type == kind:
            return form
    raise KeyError(kind)


def _form(cls: type) -> ProviderForm:
    kind = connection_type(cls)
    settings = tuple(
        _field(name)
        for name, info in cls.model_fields.items()
        if name not in ("type", "auth") and info.is_required()
    )
    annotation = cls.model_fields["auth"].annotation
    variants = {
        get_args(v.model_fields["method"].annotation)[0]: v
        for v in get_args(annotation) or (annotation,)
    }
    logins = tuple(
        LoginForm(method, LOGINS.get(method, method), _login_fields(variants[method]))
        for method in login_methods(cls)
    )
    return ProviderForm(kind, type_label(kind), INTRODUCTIONS.get(kind, ""), settings, logins)


def _login_fields(login_cls: type) -> tuple[Field, ...]:
    fields = []
    for name, info in login_cls.model_fields.items():
        if name == "method":
            continue
        if name in login_cls.secret_fields:
            if info.is_required():  # an optional secret (a session token) is not asked for
                fields.append(Field(name, name.replace("_", " "), secret=True))
        else:
            fields.append(_field(name))
    return tuple(fields)


def _field(name: str) -> Field:
    question, default = SETTINGS.get(name, (name.replace("_", " "), ""))
    return Field(name, question, default)


__all__ = [
    "INTRODUCTIONS",
    "LOGINS",
    "SETTINGS",
    "Field",
    "LoginForm",
    "ProviderForm",
    "provider_form",
    "provider_forms",
    "secret_name",
]
