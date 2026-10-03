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
