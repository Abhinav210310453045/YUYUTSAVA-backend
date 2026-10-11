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
    "vertex": "Gemini, Claude and open models through your Google Cloud project",
    "google": "Gemini with a free key: the quickest way to start with Google",
    "bedrock": "the models on AWS Bedrock, through your AWS account",
    "anthropic": "Claude, with an Anthropic API key",
    "openai": "GPT models, with an OpenAI API key",
    "openrouter": "hundreds of models from many makers, with one OpenRouter key",
    "groq": "fast open models, with a Groq key (there is a free tier)",
    "mistral": "Mistral's models, with a Mistral key",
    "cohere": "Cohere's Command models, with a Cohere key",
    "azure": "OpenAI models you have deployed in Microsoft Azure",
    "openai_compatible": "any other server that works like OpenAI's API (xAI, DeepSeek, Together, …)",
    "ollama": "models that run on your own computer: free, private, no key",
}

#: How each login method is offered, and what to do before choosing it.
LOGINS: dict[str, str] = {
    "adc": "my Google account on this computer (run `gcloud auth application-default login` first)",
    "service_account": "a service-account key file (.json) from Google Cloud",
    "access_token": "an access token I paste (lasts about an hour — for trying things out)",
    "api_key": "an API key I paste",
    "bearer_token": "a token I paste",
    "none": "no login needed",
    "aws_default_chain": "whatever AWS login this computer already has",
    "aws_profile": "a named AWS profile from ~/.aws/config",
    "aws_keys": "an AWS access key and secret key",
    "bedrock_api_key": "a Bedrock API key",
}

#: How a setting is asked for, and what it starts as.
SETTINGS: dict[str, tuple[str, str]] = {
    "project": (
        "Your Google Cloud project ID — it is at the top of console.cloud.google.com, "
        "e.g. my-project-123",
        "",
    ),
    "location": (
        "Where Google should run the models — global offers the most (Enter keeps it)",
        "global",
    ),
    "region": ("Your AWS region, e.g. us-east-1 (Enter keeps it)", "us-east-1"),
    "endpoint": (
        "Your Azure endpoint — under Keys and Endpoint in the Azure portal, "
        "e.g. https://my-resource.openai.azure.com",
        "",
    ),
    "base_url": ("The server's address, e.g. https://api.x.ai/v1", ""),
    "key_file": ("Where the key file is, e.g. ~/keys/my-project.json", ""),
    "profile": ("The AWS profile's name (Enter keeps it)", "default"),
}

#: What each secret a login takes is called, for the question that asks for it.
SECRETS: dict[str, str] = {
    "key": "API key",
    "token": "token",
    "access_key_id": "AWS access key ID",
    "secret_access_key": "AWS secret access key",
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
        LoginForm(
            method, LOGINS.get(method, method), _login_fields(variants[method], type_label(kind))
        )
        for method in login_methods(cls)
    )
    return ProviderForm(kind, type_label(kind), INTRODUCTIONS.get(kind, ""), settings, logins)


def _login_fields(login_cls: type, label: str) -> tuple[Field, ...]:
    fields = []
    for name, info in login_cls.model_fields.items():
        if name == "method":
            continue
        if name in login_cls.secret_fields:
            if info.is_required():  # an optional secret (a session token) is not asked for
                fields.append(Field(name, _secret_question(name, label), secret=True))
        else:
            fields.append(_field(name))
    return tuple(fields)


def _secret_question(name: str, label: str) -> str:
    """ "Your Groq API key", "Your AWS secret access key": the secret, and whose it is."""
    what = SECRETS.get(name, name.replace("_", " "))
    if what.startswith("AWS"):
        return f"Your {what}"
    owner = label.removeprefix("an ").removeprefix("a ")  # "an OpenAI-compatible server"
    return f"Your {owner} {what}"


def _field(name: str) -> Field:
    question, default = SETTINGS.get(name, (name.replace("_", " "), ""))
    return Field(name, question, default)


__all__ = [
    "INTRODUCTIONS",
    "LOGINS",
    "SECRETS",
    "SETTINGS",
    "Field",
    "LoginForm",
    "ProviderForm",
    "provider_form",
    "provider_forms",
    "secret_name",
]
