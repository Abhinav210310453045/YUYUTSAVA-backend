"""Model targets for tests, built the way the app builds them: through the models config.

A test that needs "some model" — usually to build an agent with ``chat_model``
patched out — takes one of these instead of hand-assembling a target, so it
exercises the same config path as the app and needs no real key.
"""

from __future__ import annotations

from yuyutsava.llm.config.loader import load_text
from yuyutsava.llm.config.secrets import Secret
from yuyutsava.llm.target import ModelTarget, target_for

_FAKE_KEY = "secret:fake"


def anthropic_target(model: str = "claude-haiku-4-5-20251001") -> ModelTarget:
    """An Anthropic model with a fake key."""
    config = load_text(
        "version = 1\n"
        f'[defaults]\nmodel = "anthropic/{model}"\n'
        "[providers.anthropic]\n"
        f'type = "anthropic"\nauth = {{ method = "api_key", key = "{_FAKE_KEY}" }}\n'
    )
    return target_for(
        config, f"anthropic/{model}", secrets={_FAKE_KEY: Secret("sk-fake")}, unavailable={}
    )


def gemini_targets() -> list[ModelTarget]:
    """Gemini on AI Studio and on Vertex (access-token login), both with fake secrets."""
    config = load_text(
        "version = 1\n"
        '[defaults]\nmodel = "google/gemini-3.5-flash"\n'
        "[providers.google]\n"
        f'type = "google"\nauth = {{ method = "api_key", key = "{_FAKE_KEY}" }}\n'
        "[providers.vertex]\n"
        'type = "vertex"\nproject = "p"\nlocation = "global"\n'
        f'auth = {{ method = "access_token", token = "{_FAKE_KEY}" }}\n'
    )
    secrets = {_FAKE_KEY: Secret("fake")}
    return [
        target_for(config, f"{name}/gemini-3.5-flash", secrets=secrets, unavailable={})
        for name in ("google", "vertex")
    ]


#: One provider of every type, as a models.toml would set them up. Vertex logs in
#: with an access token and Bedrock with keys, so nothing needs real credentials.
_EVERY_TYPE = load_text(
    """
version = 1
[defaults]
model = "groq"
[providers.groq]
type = "groq"
auth = { method = "api_key", key = "secret:k" }
[providers.openrouter]
type = "openrouter"
auth = { method = "api_key", key = "secret:k" }
[providers.ollama]
type = "ollama"
[providers.openai]
type = "openai"
auth = { method = "api_key", key = "secret:k" }
[providers.openai_compatible]
type = "openai_compatible"
base_url = "https://api.example.com/v1"
auth = { method = "api_key", key = "secret:k" }
[providers.anthropic]
type = "anthropic"
auth = { method = "api_key", key = "secret:k" }
[providers.google]
type = "google"
auth = { method = "api_key", key = "secret:k" }
[providers.vertex]
type = "vertex"
project = "p"
location = "us-central1"
auth = { method = "access_token", token = "secret:k" }
[providers.bedrock]
type = "bedrock"
region = "us-east-1"
auth = { method = "aws_keys", access_key_id = "secret:k", secret_access_key = "secret:k" }
[providers.mistral]
type = "mistral"
auth = { method = "api_key", key = "secret:k" }
[providers.cohere]
type = "cohere"
auth = { method = "api_key", key = "secret:k" }
[providers.azure]
type = "azure"
endpoint = "https://x.openai.azure.com"
auth = { method = "api_key", key = "secret:k" }
[models.groq]
provider = "groq"
id = "llama-3.3-70b-versatile"
[models.azure]
provider = "azure"
id = "dep"
base_model = "gpt-4o"
"""
)


def every_type_target(reference: str) -> ModelTarget:
    """*reference* in a config with one provider of every type, all with fake secrets."""
    return target_for(_EVERY_TYPE, reference, secrets={"secret:k": Secret("k")}, unavailable={})


def every_provider_type() -> dict[str, ModelTarget]:
    """One model of every provider type, keyed by the type."""
    return {
        "groq": every_type_target("groq"),
        "openrouter": every_type_target("openrouter/openai/gpt-4o-mini"),
        "ollama": every_type_target("ollama/gemma4:e2b"),
        "openai": every_type_target("openai/gpt-4o-mini"),
        "anthropic": every_type_target("anthropic/claude-haiku-4-5-20251001"),
        "google": every_type_target("google/gemini-2.5-flash"),
        "vertex": every_type_target("vertex/gemini-2.5-flash"),
        "bedrock": every_type_target("bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
        "mistral": every_type_target("mistral/mistral-large-latest"),
        "cohere": every_type_target("cohere/command-r-plus"),
        "azure": every_type_target("azure"),
    }
