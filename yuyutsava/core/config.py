"""
Load LLM and Docker settings from environment.

Supported LLM providers (set ``LLM_PROVIDER``):

- Groq:        https://console.groq.com/docs/overview
- OpenRouter:  https://openrouter.ai/docs/quickstart
- Anthropic:   https://console.anthropic.com/
- Ollama:      https://ollama.com/  (local, no API key required)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol


GROQ_OPENAI_BASE_URL = "https://api.groq.com/openai/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OLLAMA_BASE_URL = "http://localhost:11434/v1"


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
    api_key: str
    base_url: str = GROQ_OPENAI_BASE_URL
    model: str = "llama-3.3-70b-versatile"

    @classmethod
    def from_env(cls) -> GroqSettings:
        key = os.environ.get("GROQ_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "Set GROQ_API_KEY in the environment (or .env loaded by the CLI). "
                "See https://console.groq.com/docs/overview"
            )
        base = os.environ.get("GROQ_BASE_URL", GROQ_OPENAI_BASE_URL).strip() or GROQ_OPENAI_BASE_URL
        default_model = "llama-3.3-70b-versatile"
        model = os.environ.get("GROQ_MODEL", default_model).strip() or default_model
        return cls(api_key=key, base_url=base, model=model)


@dataclass(frozen=True)
class OpenRouterSettings:
    """OpenRouter unified API (OpenAI-compatible chat completions)."""

    api_key: str
    base_url: str = OPENROUTER_BASE_URL
    model: str = "openai/gpt-4o-mini"
    default_headers: dict[str, str] | None = None

    @classmethod
    def from_env(cls) -> OpenRouterSettings:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "Set OPENROUTER_API_KEY in the environment (or .env loaded by the CLI). "
                "See https://openrouter.ai/docs/quickstart"
            )
        base = os.environ.get("OPENROUTER_BASE_URL", OPENROUTER_BASE_URL).strip() or OPENROUTER_BASE_URL
        default_model = "openai/gpt-4o-mini"
        model = os.environ.get("OPENROUTER_MODEL", default_model).strip() or default_model
        headers: dict[str, str] = {}
        referer = os.environ.get("OPENROUTER_HTTP_REFERER", "").strip()
        title = os.environ.get("OPENROUTER_APP_TITLE", "").strip()
        if referer:
            headers["HTTP-Referer"] = referer
        if title:
            headers["X-OpenRouter-Title"] = title
        dh = headers if headers else None
        return cls(api_key=key, base_url=base, model=model, default_headers=dh)


@dataclass(frozen=True)
class DockerSettings:
    """Docker sandbox configuration — belongs to the agent layer, not the CLI.

    Any invocation path (CLI, REST API, task runner) can call ``DockerSettings.from_env()``
    to get a fully configured instance without touching CLI code.  The CLI then applies
    flag overrides on top using ``dataclasses.replace()``.
    """

    image: str = "deepagent-sandbox:local"
    network: Literal["bridge", "none"] = "bridge"
    memory: str = "512m"
    cpus: str = "1.0"
    pids_limit: int = 100
    export_dir: Path | None = None

    @classmethod
    def from_env(cls) -> DockerSettings:
        """Build a ``DockerSettings`` instance from environment variables."""
        image = os.environ.get("YUYUTSAVA_DOCKER_IMAGE", "").strip() or "deepagent-sandbox:local"

        network_raw = os.environ.get("YUYUTSAVA_DOCKER_NETWORK", "bridge").strip().lower()
        network: Literal["bridge", "none"] = network_raw if network_raw in ("bridge", "none") else "bridge"  # type: ignore[assignment]

        memory = os.environ.get("YUYUTSAVA_DOCKER_MEMORY", "").strip() or "512m"
        cpus = os.environ.get("YUYUTSAVA_DOCKER_CPUS", "").strip() or "1.0"

        raw_pids = os.environ.get("YUYUTSAVA_DOCKER_PIDS_LIMIT", "").strip()
        try:
            pids_limit = int(raw_pids) if raw_pids else 100
        except ValueError:
            pids_limit = 100

        raw_export = os.environ.get("YUYUTSAVA_DOCKER_EXPORT_DIR", "").strip()
        export_dir = Path(raw_export) if raw_export else None

        return cls(
            image=image,
            network=network,
            memory=memory,
            cpus=cpus,
            pids_limit=pids_limit,
            export_dir=export_dir,
        )


@dataclass(frozen=True)
class LocalSettings:
    """Local (non-Docker) sandbox and output directory configuration."""

    sandbox_dir: Path | None = None
    output_dir: Path | None = None

    @classmethod
    def from_env(cls) -> LocalSettings:
        raw_sandbox = os.environ.get("YUYUTSAVA_SANDBOX_DIR", "").strip()
        raw_output  = os.environ.get("YUYUTSAVA_OUTPUT_DIR",  "").strip()
        return cls(
            sandbox_dir=Path(raw_sandbox) if raw_sandbox else None,
            output_dir=Path(raw_output)   if raw_output  else None,
        )


@dataclass(frozen=True)
class AnthropicSettings:
    """Anthropic API provider (enables prompt caching via AnthropicPromptCachingMiddleware)."""

    api_key: str
    base_url: str = "https://api.anthropic.com"
    model: str = "claude-haiku-4-5-20251001"

    @classmethod
    def from_env(cls) -> AnthropicSettings:
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "Set ANTHROPIC_API_KEY in the environment. "
                "See https://console.anthropic.com/"
            )
        default_model = "claude-haiku-4-5-20251001"
        model = os.environ.get("ANTHROPIC_MODEL", default_model).strip() or default_model
        return cls(api_key=key, model=model)


@dataclass(frozen=True)
class OllamaSettings:
    """Ollama local inference server (OpenAI-compatible chat completions).

    Ollama exposes an OpenAI-compatible API at ``http://localhost:11434/v1``.
    No API key is required; the string ``"ollama"`` is sent as a placeholder
    because ``ChatOpenAI`` requires a non-empty value.

    See https://ollama.com/
    """

    api_key: str = "ollama"
    base_url: str = OLLAMA_BASE_URL
    model: str = "gemma4:e2b"

    @classmethod
    def from_env(cls) -> OllamaSettings:
        base = os.environ.get("OLLAMA_HOST", OLLAMA_BASE_URL).strip() or OLLAMA_BASE_URL
        base = base.rstrip("/")
        if not base.endswith("/v1"):
            base = base + "/v1"
        default_model = "gemma4:e2b"
        model = os.environ.get("OLLAMA_MODEL", default_model).strip() or default_model
        return cls(base_url=base, model=model)


def llm_settings_from_env() -> LlmSettings:
    """Pick provider via ``LLM_PROVIDER`` (``groq``, ``openrouter``, ``anthropic``, or ``ollama``; default ``groq``)."""
    provider = os.environ.get("LLM_PROVIDER", "groq").strip().lower()
    if provider == "openrouter":
        return OpenRouterSettings.from_env()
    if provider == "groq":
        return GroqSettings.from_env()
    if provider == "anthropic":
        return AnthropicSettings.from_env()
    if provider == "ollama":
        return OllamaSettings.from_env()
    raise RuntimeError(
        f"Unknown LLM_PROVIDER={provider!r}; use 'groq', 'openrouter', 'anthropic', or 'ollama'."
    )
