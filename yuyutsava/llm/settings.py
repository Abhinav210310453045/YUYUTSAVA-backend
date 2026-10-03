"""The models config as the environment describes it.

``~/.yuyutsava/models.toml`` is where model configuration lives
(docs/architecture/decisions/ADR-005-models-config.md). Until the environment
cutover (design §7, M7), processes still choose their model from ``LLM_PROVIDER``
and the variables of that provider, and this module turns those into a
:class:`~yuyutsava.llm.target.ModelTarget` — so every model is built by the same
providers, whichever way it was configured.

Each provider type's variables, their defaults and the message for a missing
one are exactly those it has always had. A key is never copied out of the
environment into the target's connection: the connection holds an ``env:``
reference, resolved like any other.

Role-prefixed overrides
-----------------------
``llm_settings_from_env(role)`` lets a daemon role (``triage``, ``orchestrator``,
``subagent``, ``compaction``, ``tier_light``, …) use a different provider or
model from the main one. Each lookup tries ``<ROLE>_<NAME>`` first and falls back
to ``<NAME>``::

    LLM_PROVIDER=anthropic ANTHROPIC_MODEL=claude-haiku-4-5-...
    TRIAGE_LLM_PROVIDER=ollama OLLAMA_MODEL=llama3.2:3b
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import TypeAdapter

from yuyutsava.config.env import env, env_int
from yuyutsava.llm.config.schema import (
    DEFAULT_THINKING_BUDGET,
    GROQ_BASE_URL,
    OPENAI_BASE_URL,
    OPENROUTER_BASE_URL,
    Connection,
    ModelEntry,
)
from yuyutsava.llm.config.secrets import SecretResolver, parse_secret_ref
from yuyutsava.llm.target import ModelTarget

#: The ``LLM_PROVIDER`` value of each provider type, in the order an error lists them.
PROVIDER_TYPES: tuple[str, ...] = (
    "groq",
    "openrouter",
    "ollama",
    "openai",
    "openai_compatible",
    "anthropic",
    "google",
    "vertex",
    "bedrock",
    "azure",
    "mistral",
    "cohere",
)

#: Other ``LLM_PROVIDER`` spellings accepted for a type.
_ALIASES = {
    "custom": "openai_compatible",
    "gemini": "google",
    "aws": "bedrock",
    "azure_openai": "azure",
}

_OLLAMA_ENV_DEFAULT = "http://localhost:11434/v1"

#: One provider type, read from the environment: ``(connection, model id, alias settings)``.
_Described = tuple[dict[str, Any], str, dict[str, Any] | None]

_CONNECTION = TypeAdapter(Connection)


def provider_type_of(name: str) -> str | None:
    """The provider type an ``LLM_PROVIDER`` value selects, or ``None`` if none does."""
    name = name.strip().lower()
    if name in PROVIDER_TYPES:
        return name
    return _ALIASES.get(name)


def llm_settings_from_env(role: str | None = None) -> ModelTarget:
    """The model ``<ROLE>_LLM_PROVIDER`` (falls back to ``LLM_PROVIDER``) selects.

    ``role`` may be ``None`` or ``"main"`` (no prefix — the CLI), or a role name.
    Raises :class:`RuntimeError` naming the variable to set when a required one
    is missing, or the accepted values when ``LLM_PROVIDER`` names no provider.
    """
    effective = None if role in (None, "main") else role
    requested = env("LLM_PROVIDER", effective, "groq").lower()
    provider_type = provider_type_of(requested)
    if provider_type is None:
        raise RuntimeError(
            f"Unknown LLM_PROVIDER={requested!r}; use one of: {', '.join(PROVIDER_TYPES)}."
        )
    connection, model, entry = _READERS[provider_type](effective)
    return _target(provider_type, connection, model, entry)


def _target(
    provider_type: str, connection: dict[str, Any], model: str, entry: dict[str, Any] | None
) -> ModelTarget:
    parsed = _CONNECTION.validate_python(connection)
    resolver = SecretResolver({})
    secrets = {
        value: resolver.resolve(parse_secret_ref(value))
        for value in parsed.auth.model_dump().values()
        if isinstance(value, str) and value.startswith("env:")
    }
    alias = ModelEntry(provider=provider_type, id=model, **entry) if entry else None
    return ModelTarget(
        provider=provider_type, connection=parsed, model=model, entry=alias, secrets=secrets
    )


def _key(name: str, role: str | None, message: str) -> str:
    """An ``env:`` reference to *name*, the role's own variable first; *message* if neither is set."""
    if role and env(f"{role.upper()}_{name}"):
        return f"env:{role.upper()}_{name}"
    if env(name):
        return f"env:{name}"
    raise RuntimeError(message)


def _api_key(name: str, role: str | None, message: str) -> dict[str, str]:
    return {"method": "api_key", "key": _key(name, role, message)}


def _thinking(name: str, role: str | None) -> dict[str, Any]:
    return {"thinking": {"budget": max(0, env_int(name, role, DEFAULT_THINKING_BUDGET))}}


# ─── one reader per provider type ─────────────────────────────────────────


def _groq(role: str | None) -> _Described:
    auth = _api_key(
        "GROQ_API_KEY",
        role,
        "Set GROQ_API_KEY in the environment (or .env loaded by the CLI). "
        "See https://console.groq.com/docs/overview",
    )
    connection = {
        "type": "groq",
        "auth": auth,
        "base_url": env("GROQ_BASE_URL", role, GROQ_BASE_URL),
    }
    return connection, env("GROQ_MODEL", role, "llama-3.3-70b-versatile"), None


def _openrouter(role: str | None) -> _Described:
    auth = _api_key(
        "OPENROUTER_API_KEY",
        role,
        "Set OPENROUTER_API_KEY in the environment (or .env loaded by the CLI). "
        "See https://openrouter.ai/docs/quickstart",
    )
    connection = {
        "type": "openrouter",
        "auth": auth,
        "base_url": env("OPENROUTER_BASE_URL", role, OPENROUTER_BASE_URL),
        "http_referer": env("OPENROUTER_HTTP_REFERER", role) or None,
        "app_title": env("OPENROUTER_APP_TITLE", role) or None,
    }
    return connection, env("OPENROUTER_MODEL", role, "openai/gpt-4o-mini"), None


def _anthropic(role: str | None) -> _Described:
    auth = _api_key(
        "ANTHROPIC_API_KEY",
        role,
        "Set ANTHROPIC_API_KEY in the environment. See https://console.anthropic.com/",
    )
    return (
        {"type": "anthropic", "auth": auth},
        env("ANTHROPIC_MODEL", role, "claude-haiku-4-5-20251001"),
        None,
    )


def _ollama(role: str | None) -> _Described:
    connection = {"type": "ollama", "base_url": env("OLLAMA_HOST", role, _OLLAMA_ENV_DEFAULT)}
    return connection, env("OLLAMA_MODEL", role, "gemma4:e2b"), None


def _openai(role: str | None) -> _Described:
    auth = _api_key(
        "OPENAI_API_KEY",
        role,
        "Set OPENAI_API_KEY in the environment (or .env loaded by the CLI). "
        "See https://platform.openai.com/api-keys",
    )
    connection = {
        "type": "openai",
        "auth": auth,
        "base_url": env("OPENAI_BASE_URL", role, OPENAI_BASE_URL),
    }
    return connection, env("OPENAI_MODEL", role, "gpt-4o-mini"), None


def _openai_compatible(role: str | None) -> _Described:
    base = env("OPENAI_COMPATIBLE_BASE_URL", role)
    if not base:
        raise RuntimeError(
            "Set OPENAI_COMPATIBLE_BASE_URL for LLM_PROVIDER=openai_compatible "
            "(e.g. https://api.x.ai/v1, https://api.deepseek.com/v1, "
            "https://api.together.xyz/v1)."
        )
    auth = _api_key(
        "OPENAI_COMPATIBLE_API_KEY",
        role,
        "Set OPENAI_COMPATIBLE_API_KEY for LLM_PROVIDER=openai_compatible.",
    )
    model = env("OPENAI_COMPATIBLE_MODEL", role)
    if not model:
        raise RuntimeError("Set OPENAI_COMPATIBLE_MODEL for LLM_PROVIDER=openai_compatible.")
    return {"type": "openai_compatible", "auth": auth, "base_url": base.rstrip("/")}, model, None


def _google(role: str | None) -> _Described:
    message = (
        "Set GOOGLE_API_KEY (or GEMINI_API_KEY) in the environment. "
        "See https://ai.google.dev/gemini-api/docs/api-key"
    )
    try:
        key = _key("GOOGLE_API_KEY", role, message)
    except RuntimeError:
        key = _key("GEMINI_API_KEY", role, message)
    connection = {"type": "google", "auth": {"method": "api_key", "key": key}}
    return (
        connection,
        env("GOOGLE_MODEL", role, "gemini-2.5-flash"),
        _thinking("GOOGLE_THINKING_BUDGET", role),
    )


def _vertex(role: str | None) -> _Described:
    project = env("VERTEX_PROJECT", role) or env("GOOGLE_CLOUD_PROJECT", role)
    if not project:
        raise RuntimeError(
            "Set VERTEX_PROJECT (or GOOGLE_CLOUD_PROJECT) for LLM_PROVIDER=vertex, "
            "and run `gcloud auth application-default login`."
        )
    connection = {
        "type": "vertex",
        "project": project,
        "location": env("VERTEX_LOCATION", role, "us-central1"),
        "auth": {"method": "adc"},
    }
    return (
        connection,
        env("VERTEX_MODEL", role, "gemini-2.5-flash"),
        _thinking("VERTEX_THINKING_BUDGET", role),
    )


def _bedrock(role: str | None) -> _Described:
    region = env("BEDROCK_REGION", role) or env("AWS_REGION", role, "us-east-1")
    connection = {"type": "bedrock", "region": region, "auth": {"method": "aws_default_chain"}}
    return (
        connection,
        env("BEDROCK_MODEL", role, "us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
        None,
    )


def _azure(role: str | None) -> _Described:
    auth = _api_key(
        "AZURE_OPENAI_API_KEY",
        role,
        "Set AZURE_OPENAI_API_KEY for LLM_PROVIDER=azure. "
        "See https://learn.microsoft.com/azure/ai-services/openai/",
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
    connection = {
        "type": "azure",
        "endpoint": endpoint,
        "api_version": env("AZURE_OPENAI_API_VERSION", role, "2024-10-21"),
        "auth": auth,
    }
    named = env("AZURE_OPENAI_MODEL", role, deployment)
    return connection, deployment, ({"base_model": named} if named != deployment else None)


def _mistral(role: str | None) -> _Described:
    auth = _api_key(
        "MISTRAL_API_KEY",
        role,
        "Set MISTRAL_API_KEY in the environment. See https://console.mistral.ai/api-keys/",
    )
    return (
        {"type": "mistral", "auth": auth},
        env("MISTRAL_MODEL", role, "mistral-large-latest"),
        None,
    )


def _cohere(role: str | None) -> _Described:
    auth = _api_key(
        "COHERE_API_KEY",
        role,
        "Set COHERE_API_KEY in the environment. See https://dashboard.cohere.com/api-keys",
    )
    return {"type": "cohere", "auth": auth}, env("COHERE_MODEL", role, "command-r-plus"), None


_READERS: dict[str, Callable[[str | None], _Described]] = {
    "groq": _groq,
    "openrouter": _openrouter,
    "ollama": _ollama,
    "openai": _openai,
    "openai_compatible": _openai_compatible,
    "anthropic": _anthropic,
    "google": _google,
    "vertex": _vertex,
    "bedrock": _bedrock,
    "azure": _azure,
    "mistral": _mistral,
    "cohere": _cohere,
}


__all__ = ["PROVIDER_TYPES", "llm_settings_from_env", "provider_type_of"]
