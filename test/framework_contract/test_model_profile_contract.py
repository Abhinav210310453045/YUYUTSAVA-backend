"""The SDKs still publish a model profile for the model ids users will configure.

The catalog takes a model's context window, output limit and tool support from
LangChain's model profile when the user has not overridden them and discovery
has not answered (docs/architecture/decisions/ADR-005-models-config.md §4;
design §2, ``llm/catalog.py``). That replaces the per-provider
``max_input_tokens`` guesses, so a profile that silently disappears would put a
128k default under a 1M Gemini, or a 1M default under a 200k Claude.

Pinned for the families whose profile the catalog reads:

- **Anthropic:** alias and dated ids.
- **Gemini through genai:** AI Studio and Vertex.
- **OpenAI.**
- **Mistral.**
- **Bedrock:** ids with an inference-profile prefix.

``ChatAnthropicVertex`` and OpenAI-compatible hosts other than OpenAI publish no
profile. The catalog fills those from discovery or from the Anthropic profile,
so they are not pinned here. Nothing is sent.
"""

from __future__ import annotations

import os
import unittest
from collections.abc import Callable

import pytest


def _anthropic(model: str):
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(api_key="unused", model=model)


def _genai(model: str, *, vertex: bool = False):
    from langchain_google_genai import ChatGoogleGenerativeAI

    if not vertex:
        return ChatGoogleGenerativeAI(api_key="unused", model=model)
    from google.auth.credentials import AnonymousCredentials

    return ChatGoogleGenerativeAI(
        vertexai=True,
        project="p",
        location="us-central1",
        credentials=AnonymousCredentials(),
        model=model,
    )


def _openai(model: str):
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(api_key="unused", model=model)


def _mistral(model: str):
    from langchain_mistralai import ChatMistralAI

    return ChatMistralAI(api_key="unused", model=model)


def _bedrock(model: str):
    import boto3
    from botocore.credentials import CredentialResolver
    from botocore.session import Session
    from langchain_aws import ChatBedrockConverse

    session = Session()
    for variable in ("config_file", "credentials_file"):
        session.set_config_variable(variable, os.devnull)
    session.register_component("credential_provider", CredentialResolver(providers=[]))
    client = boto3.Session(botocore_session=session).client(
        "bedrock-runtime", region_name="us-east-1"
    )
    return ChatBedrockConverse(
        model=model, region_name="us-east-1", client=client, bedrock_client=object()
    )


#: (label, builder, the window it must report)
CASES: list[tuple[str, Callable[[], object], int]] = [
    ("anthropic alias", lambda: _anthropic("claude-sonnet-4-5"), 200_000),
    ("anthropic dated", lambda: _anthropic("claude-sonnet-4-5-20250929"), 200_000),
    ("gemini on AI Studio", lambda: _genai("gemini-2.5-flash"), 1_048_576),
    ("gemini on Vertex", lambda: _genai("gemini-2.5-flash", vertex=True), 1_048_576),
    ("openai", lambda: _openai("gpt-4o-mini"), 128_000),
    ("mistral", lambda: _mistral("mistral-large-latest"), 262_144),
    (
        "bedrock us. profile",
        lambda: _bedrock("us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
        200_000,
    ),
    (
        "bedrock global. profile",
        lambda: _bedrock("global.anthropic.claude-sonnet-4-5-20250929-v1:0"),
        200_000,
    ),
]


def profile_gaps(cases: list[tuple[str, Callable[[], object], int]]) -> tuple[list[str], int]:
    """``(cases whose profile no longer reports the window, cases checked)``."""
    gaps: list[str] = []
    checked = 0
    for label, build, window in cases:
        try:
            profile = build().profile or {}
        except ImportError:
            continue
        checked += 1
        if profile.get("max_input_tokens") != window or not profile.get("tool_calling"):
            gaps.append(
                f"{label}: {profile.get('max_input_tokens')!r} tokens, tools {profile.get('tool_calling')!r}"
            )
    return gaps, checked


def test_the_profiled_families_report_their_window() -> None:
    gaps, checked = profile_gaps(CASES)
    if not checked:
        pytest.skip("no chat SDK installed")
    assert not gaps, (
        "a model profile the catalog reads changed; llm/catalog.py falls back to a provider "
        "default for these:\n  " + "\n  ".join(gaps)
    )


class TheCheckFails(unittest.TestCase):
    """Negative control: a model with no profile is reported."""

    def test_an_unprofiled_model_is_named(self) -> None:
        cases = [("an OpenRouter-style id", lambda: _openai("someone/not-a-model"), 128_000)]
        gaps, checked = profile_gaps(cases)
        self.assertEqual((checked, len(gaps)), (1, 1))


if __name__ == "__main__":
    unittest.main()
