"""The words a ``models.toml`` problem is explained with.

Every problem the loader reports ends in a *fix*: the line to write, ready to copy.
The examples here are those lines. ``test/llm/config/test_messages.py`` checks
that every provider type and every login method has one, so a new provider
cannot arrive without its explanation.
"""

from __future__ import annotations

#: What each provider ``type`` is called when talking to a person.
TYPE_LABELS: dict[str, str] = {
    "vertex": "Google Vertex AI",
    "google": "Google AI Studio",
    "bedrock": "AWS Bedrock",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "groq": "Groq",
    "mistral": "Mistral",
    "cohere": "Cohere",
    "azure": "Azure OpenAI",
    "openai_compatible": "an OpenAI-compatible server",
    "ollama": "Ollama",
}

#: A complete ``auth = …`` line for each login method. ``{secret}`` becomes a
#: secret name for the provider in question (see :func:`login_example`), so the
#: line is valid exactly as shown.
LOGIN_EXAMPLES: dict[str, str] = {
    "adc": 'auth = { method = "adc" }   # after `gcloud auth application-default login`',
    "service_account": 'auth = { method = "service_account", key_file = "~/keys/service-account.json" }',
    "access_token": 'auth = { method = "access_token", token = "env:GOOGLE_ACCESS_TOKEN" }',
    "api_key": 'auth = { method = "api_key", key = "secret:{secret}" }',
    "bearer_token": 'auth = { method = "bearer_token", token = "secret:{secret}" }',
    "none": 'auth = { method = "none" }',
    "aws_default_chain": 'auth = { method = "aws_default_chain" }   # environment, ~/.aws or an instance role',
    "aws_profile": 'auth = { method = "aws_profile", profile = "default" }',
    "aws_keys": (
        'auth = { method = "aws_keys", access_key_id = "secret:aws-key-id", '
        'secret_access_key = "secret:aws-secret-key" }'
    ),
    "bedrock_api_key": 'auth = { method = "bedrock_api_key", key = "secret:bedrock" }',
}

#: An example line for the settings people most often get wrong or leave out.
SETTING_EXAMPLES: dict[str, str] = {
    "version": "version = 1",
    "type": 'type = "vertex"',
    "project": 'project = "my-gcp-project"',
    "location": 'location = "global"',
    "region": 'region = "us-east-1"',
    "endpoint": 'endpoint = "https://<resource>.openai.azure.com"',
    "base_url": 'base_url = "https://api.example.com/v1"',
    "api_version": 'api_version = "2024-10-21"',
    "key_file": 'key_file = "~/keys/service-account.json"',
    "profile": 'profile = "default"',
    "provider": 'provider = "<a name from [providers]>"',
    "id": 'id = "<the model id the provider uses>"',
    "model": 'model = "<an alias from [models], or provider/model-id>"',
    "defaults": '[defaults]\nmodel = "<an alias from [models], or provider/model-id>"',
    "providers": '[providers.<name>]\ntype = "<provider type>"\nauth = { … }',
    "budget": "thinking = { budget = 8192 }",
    "input": "price = { input = 0.30, output = 2.50 }   # US dollars per million tokens",
    "output": "price = { input = 0.30, output = 2.50 }   # US dollars per million tokens",
    "context_window": "context_window = 1000000",
    "max_output": "max_output = 8192",
    "family": 'family = "anthropic"   # Vertex only: gemini, anthropic or maas',
    "require_user_presence": "require_user_presence = true",
}

#: Where the whole file is described, for problems no single example settles.
REFERENCE = "docs/design/model-config.md §1"


def login_example(method: str, provider_type: str | None) -> str:
    """The ``auth = …`` line for *method*, its secret named after *provider_type*."""
    return LOGIN_EXAMPLES[method].replace("{secret}", provider_type or "my-key")


def type_label(type_name: str) -> str:
    """How a provider type is named in a message; the raw value if it is unknown."""
    return TYPE_LABELS.get(type_name, type_name)


__all__ = [
    "LOGIN_EXAMPLES",
    "REFERENCE",
    "SETTING_EXAMPLES",
    "TYPE_LABELS",
    "login_example",
    "type_label",
]
