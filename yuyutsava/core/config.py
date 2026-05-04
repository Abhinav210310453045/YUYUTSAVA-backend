"""
Load LLM, Docker, events, and daemon settings from environment / config files.

Supported LLM providers (set ``LLM_PROVIDER``):

- Groq:        https://console.groq.com/docs/overview
- OpenRouter:  https://openrouter.ai/docs/quickstart
- Anthropic:   https://console.anthropic.com/
- Ollama:      https://ollama.com/  (local, no API key required)

Role-prefixed overrides
-----------------------
``llm_settings_from_env(role)`` lets a daemon role (e.g. ``triage``,
``orchestrator``, ``file_organizer``) pick a different provider/model from the
``main`` one without changing the CLI's behaviour. Each lookup tries
``<ROLE>_<NAME>`` first and falls back to ``<NAME>``::

    LLM_PROVIDER=anthropic ANTHROPIC_MODEL=claude-haiku-4-5-...
    TRIAGE_LLM_PROVIDER=ollama OLLAMA_MODEL=llama3.2:3b

Calls to ``llm_settings_from_env()`` (no role) are byte-equivalent to today.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol


GROQ_OPENAI_BASE_URL = "https://api.groq.com/openai/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OLLAMA_BASE_URL = "http://localhost:11434/v1"


# ---------------------------------------------------------------------------
# Env helper
# ---------------------------------------------------------------------------


def _env(name: str, role: str | None = None, default: str = "") -> str:
    """Read ``<ROLE>_<NAME>`` first, then ``<NAME>``, then ``default``.

    ``role=None`` means no prefix (current behaviour), preserving compatibility
    with all existing callsites.
    """
    if role:
        prefixed = f"{role.upper()}_{name}"
        v = os.environ.get(prefixed, "").strip()
        if v:
            return v
    return os.environ.get(name, "").strip() or default


# ---------------------------------------------------------------------------
# LLM provider settings
# ---------------------------------------------------------------------------


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
    def from_env(cls, role: str | None = None) -> GroqSettings:
        key = _env("GROQ_API_KEY", role)
        if not key:
            raise RuntimeError(
                "Set GROQ_API_KEY in the environment (or .env loaded by the CLI). "
                "See https://console.groq.com/docs/overview"
            )
        base = _env("GROQ_BASE_URL", role, GROQ_OPENAI_BASE_URL)
        model = _env("GROQ_MODEL", role, "llama-3.3-70b-versatile")
        return cls(api_key=key, base_url=base, model=model)


@dataclass(frozen=True)
class OpenRouterSettings:
    """OpenRouter unified API (OpenAI-compatible chat completions)."""

    api_key: str
    base_url: str = OPENROUTER_BASE_URL
    model: str = "openai/gpt-4o-mini"
    default_headers: dict[str, str] | None = None

    @classmethod
    def from_env(cls, role: str | None = None) -> OpenRouterSettings:
        key = _env("OPENROUTER_API_KEY", role)
        if not key:
            raise RuntimeError(
                "Set OPENROUTER_API_KEY in the environment (or .env loaded by the CLI). "
                "See https://openrouter.ai/docs/quickstart"
            )
        base = _env("OPENROUTER_BASE_URL", role, OPENROUTER_BASE_URL)
        model = _env("OPENROUTER_MODEL", role, "openai/gpt-4o-mini")
        headers: dict[str, str] = {}
        referer = _env("OPENROUTER_HTTP_REFERER", role)
        title = _env("OPENROUTER_APP_TITLE", role)
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
    def from_env(cls, role: str | None = None) -> AnthropicSettings:
        key = _env("ANTHROPIC_API_KEY", role)
        if not key:
            raise RuntimeError(
                "Set ANTHROPIC_API_KEY in the environment. "
                "See https://console.anthropic.com/"
            )
        model = _env("ANTHROPIC_MODEL", role, "claude-haiku-4-5-20251001")
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
    def from_env(cls, role: str | None = None) -> OllamaSettings:
        base = _env("OLLAMA_HOST", role, OLLAMA_BASE_URL)
        base = base.rstrip("/")
        if not base.endswith("/v1"):
            base = base + "/v1"
        model = _env("OLLAMA_MODEL", role, "gemma4:e2b")
        return cls(base_url=base, model=model)


def llm_settings_from_env(role: str | None = None) -> LlmSettings:
    """Pick provider via ``<ROLE>_LLM_PROVIDER`` (falls back to ``LLM_PROVIDER``).

    ``role`` may be ``None`` (no prefix — current CLI behaviour), ``"main"``
    (alias for None), or a custom role name like ``"triage"``,
    ``"orchestrator"``, ``"file_organizer"``. Roles let the daemon run a small
    cheap model for triage and a stronger one for the orchestrator.
    """
    effective_role = None if role in (None, "main") else role
    provider = _env("LLM_PROVIDER", effective_role, "groq").lower()
    if provider == "openrouter":
        return OpenRouterSettings.from_env(effective_role)
    if provider == "groq":
        return GroqSettings.from_env(effective_role)
    if provider == "anthropic":
        return AnthropicSettings.from_env(effective_role)
    if provider == "ollama":
        return OllamaSettings.from_env(effective_role)
    raise RuntimeError(
        f"Unknown LLM_PROVIDER={provider!r}; use 'groq', 'openrouter', 'anthropic', or 'ollama'."
    )


# ---------------------------------------------------------------------------
# Daemon home directory
# ---------------------------------------------------------------------------


def yuyutsava_home() -> Path:
    """Per-user state dir for the daemon (events, blobs, configs).

    Override with ``YUYUTSAVA_HOME``. Created on first access.
    """
    raw = os.environ.get("YUYUTSAVA_HOME", "").strip()
    p = Path(raw).expanduser() if raw else Path.home() / ".yuyutsava"
    p.mkdir(parents=True, exist_ok=True)
    (p / "blobs").mkdir(exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Events config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceConfig:
    """One entry from ``events_config.json`` ``sources`` map."""

    name: str
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EventsConfig:
    """Loaded ``events_config.json``."""

    sources: dict[str, SourceConfig]

    @classmethod
    def from_file(cls, path: Path | None = None) -> EventsConfig:
        path = path or (yuyutsava_home() / "events_config.json")
        if not path.exists():
            return cls(sources={})
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc
        sources_raw = raw.get("sources", {}) or {}
        sources: dict[str, SourceConfig] = {}
        for name, body in sources_raw.items():
            if not isinstance(body, dict):
                continue
            enabled = bool(body.get("enabled", True))
            params = {k: v for k, v in body.items() if k != "enabled"}
            sources[name] = SourceConfig(name=name, enabled=enabled, params=params)
        return cls(sources=sources)

    @classmethod
    def default(cls) -> EventsConfig:
        """Sensible default if no config file exists: watch ``~/Downloads``."""
        return cls(sources={
            "fs": SourceConfig(
                name="fs",
                enabled=True,
                params={
                    "roots": [str(Path.home() / "Downloads")],
                    "ignore": ["*.tmp", ".DS_Store", "*.crdownload", "*.part"],
                    "coalesce_window_ms": 2000,
                },
            ),
        })


# ---------------------------------------------------------------------------
# Daemon config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DaemonConfig:
    """Daemon-wide settings: web server, consent expiry, token budgets."""

    web_host: str = "127.0.0.1"
    web_port: int = 7654
    web_open_browser: bool = True
    proposal_expiry_sec: int = 300
    orchestrator_token_budget: int = 8000
    subagent_token_budget: int = 30000
    headless: bool = False  # --no-ui semantics

    @classmethod
    def from_env(cls) -> DaemonConfig:
        port_raw = os.environ.get("YUYUTSAVA_DAEMON_PORT", "").strip()
        try:
            port = int(port_raw) if port_raw else 7654
        except ValueError:
            port = 7654

        host = os.environ.get("YUYUTSAVA_DAEMON_HOST", "127.0.0.1").strip() or "127.0.0.1"

        open_browser_raw = os.environ.get("YUYUTSAVA_DAEMON_OPEN_BROWSER", "").strip().lower()
        open_browser = open_browser_raw not in ("0", "false", "no")

        expiry_raw = os.environ.get("YUYUTSAVA_PROPOSAL_EXPIRY_SEC", "").strip()
        try:
            expiry = int(expiry_raw) if expiry_raw else 300
        except ValueError:
            expiry = 300

        orch_raw = os.environ.get("YUYUTSAVA_ORCHESTRATOR_TOKEN_BUDGET", "").strip()
        try:
            orch_budget = int(orch_raw) if orch_raw else 8000
        except ValueError:
            orch_budget = 8000

        sub_raw = os.environ.get("YUYUTSAVA_SUBAGENT_TOKEN_BUDGET", "").strip()
        try:
            sub_budget = int(sub_raw) if sub_raw else 30000
        except ValueError:
            sub_budget = 30000

        return cls(
            web_host=host,
            web_port=port,
            web_open_browser=open_browser,
            proposal_expiry_sec=expiry,
            orchestrator_token_budget=orch_budget,
            subagent_token_budget=sub_budget,
        )
