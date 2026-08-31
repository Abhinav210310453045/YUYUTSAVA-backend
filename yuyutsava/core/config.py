"""
Load LLM, Docker, events, and daemon settings from environment / config files.

Supported LLM providers (set ``LLM_PROVIDER``):

OpenAI-compatible (handled by ``ChatOpenAI`` + ``base_url``):

- Groq:              https://console.groq.com/docs/overview
- OpenRouter:        https://openrouter.ai/docs/quickstart
- Ollama:            https://ollama.com/  (local, no API key required)
- OpenAI:            https://platform.openai.com/  (``openai``)
- OpenAI-compatible: any host (xAI, DeepSeek, Together, Fireworks, Perplexity,
                     Cerebras, DeepInfra, …) via ``openai_compatible`` + base_url

Native provider SDKs (lazy-imported; install the matching extra):

- Anthropic:    https://console.anthropic.com/                  (``anthropic``)
- Google Gemini:https://ai.google.dev/                          (``google``/``gemini``)
- Vertex AI:    https://cloud.google.com/vertex-ai              (``vertex``)
- AWS Bedrock:  https://aws.amazon.com/bedrock/                 (``bedrock``/``aws``)
- Azure OpenAI: https://learn.microsoft.com/azure/ai-services/  (``azure``/``azure_openai``)
- Mistral:      https://docs.mistral.ai/                        (``mistral``)
- Cohere:       https://docs.cohere.com/                        (``cohere``)

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

from yuyutsava.storage.paths import events_config_path


GROQ_OPENAI_BASE_URL = "https://api.groq.com/openai/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OLLAMA_BASE_URL = "http://localhost:11434/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"

# OpenAI-compatible long-tail providers reachable through the generic
# ``openai_compatible`` entry — the user just points the base_url at one of these
# (or any other OpenAI-compatible host) and sets the model:
#   xAI/Grok    https://api.x.ai/v1
#   DeepSeek    https://api.deepseek.com/v1
#   Together    https://api.together.xyz/v1
#   Fireworks   https://api.fireworks.ai/inference/v1
#   Perplexity  https://api.perplexity.ai


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


@dataclass(frozen=True)
class OpenAISettings:
    """Native OpenAI API (OpenAI-compatible chat completions).

    Handled by the default ``ChatOpenAI`` path in ``yuyutsava.llm`` — no
    dedicated factory branch needed. See https://platform.openai.com/
    """

    api_key: str
    base_url: str = OPENAI_BASE_URL
    model: str = "gpt-4o-mini"

    @classmethod
    def from_env(cls, role: str | None = None) -> OpenAISettings:
        key = _env("OPENAI_API_KEY", role)
        if not key:
            raise RuntimeError(
                "Set OPENAI_API_KEY in the environment (or .env loaded by the CLI). "
                "See https://platform.openai.com/api-keys"
            )
        base = _env("OPENAI_BASE_URL", role, OPENAI_BASE_URL)
        model = _env("OPENAI_MODEL", role, "gpt-4o-mini")
        return cls(api_key=key, base_url=base, model=model)


@dataclass(frozen=True)
class OpenAICompatibleSettings:
    """Generic OpenAI-compatible endpoint (xAI, DeepSeek, Together, Fireworks,
    Perplexity, Cerebras, DeepInfra, …).

    Point ``OPENAI_COMPATIBLE_BASE_URL`` at the provider and set the model. Handled
    by the default ``ChatOpenAI`` path — no dedicated factory branch needed.
    """

    api_key: str
    base_url: str
    model: str
    default_headers: dict[str, str] | None = None

    @classmethod
    def from_env(cls, role: str | None = None) -> OpenAICompatibleSettings:
        base = _env("OPENAI_COMPATIBLE_BASE_URL", role)
        if not base:
            raise RuntimeError(
                "Set OPENAI_COMPATIBLE_BASE_URL for LLM_PROVIDER=openai_compatible "
                "(e.g. https://api.x.ai/v1, https://api.deepseek.com/v1, "
                "https://api.together.xyz/v1)."
            )
        key = _env("OPENAI_COMPATIBLE_API_KEY", role)
        if not key:
            raise RuntimeError(
                "Set OPENAI_COMPATIBLE_API_KEY for LLM_PROVIDER=openai_compatible."
            )
        model = _env("OPENAI_COMPATIBLE_MODEL", role)
        if not model:
            raise RuntimeError(
                "Set OPENAI_COMPATIBLE_MODEL for LLM_PROVIDER=openai_compatible."
            )
        return cls(api_key=key, base_url=base.rstrip("/"), model=model)


@dataclass(frozen=True)
class GoogleSettings:
    """Google Gemini via the AI Studio (Developer) API — ``ChatGoogleGenerativeAI``.

    Needs ``langchain-google-genai`` (``pip install 'yuyutsava[google]'``).
    See https://ai.google.dev/
    """

    api_key: str
    model: str = "gemini-2.5-flash"

    @property
    def base_url(self) -> str:  # structural LlmSettings conformance (n/a here)
        return ""

    @classmethod
    def from_env(cls, role: str | None = None) -> GoogleSettings:
        key = _env("GOOGLE_API_KEY", role) or _env("GEMINI_API_KEY", role)
        if not key:
            raise RuntimeError(
                "Set GOOGLE_API_KEY (or GEMINI_API_KEY) in the environment. "
                "See https://ai.google.dev/gemini-api/docs/api-key"
            )
        model = _env("GOOGLE_MODEL", role, "gemini-2.5-flash")
        return cls(api_key=key, model=model)


@dataclass(frozen=True)
class VertexSettings:
    """Google Gemini on Vertex AI — ``ChatVertexAI``.

    Auth is Google Application Default Credentials (``gcloud auth
    application-default login``); no API key. Needs ``langchain-google-vertexai``
    (``pip install 'yuyutsava[vertex]'``). See https://cloud.google.com/vertex-ai
    """

    project: str
    location: str = "us-central1"
    model: str = "gemini-2.5-flash"

    @property
    def api_key(self) -> str:  # ADC auth — no key
        return ""

    @property
    def base_url(self) -> str:  # structural LlmSettings conformance (n/a here)
        return ""

    @classmethod
    def from_env(cls, role: str | None = None) -> VertexSettings:
        project = _env("VERTEX_PROJECT", role) or _env("GOOGLE_CLOUD_PROJECT", role)
        if not project:
            raise RuntimeError(
                "Set VERTEX_PROJECT (or GOOGLE_CLOUD_PROJECT) for LLM_PROVIDER=vertex, "
                "and run `gcloud auth application-default login`."
            )
        location = _env("VERTEX_LOCATION", role, "us-central1")
        model = _env("VERTEX_MODEL", role, "gemini-2.5-flash")
        return cls(project=project, location=location, model=model)


@dataclass(frozen=True)
class BedrockSettings:
    """AWS Bedrock via the Converse API — ``ChatBedrockConverse``.

    Auth is the standard boto3 credential chain (env vars, shared config, IAM
    role). Needs ``langchain-aws`` (``pip install 'yuyutsava[bedrock]'``).
    See https://aws.amazon.com/bedrock/
    """

    region: str
    model: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

    @property
    def api_key(self) -> str:  # boto3 credential chain — no key here
        return ""

    @property
    def base_url(self) -> str:  # structural LlmSettings conformance (n/a here)
        return ""

    @classmethod
    def from_env(cls, role: str | None = None) -> BedrockSettings:
        region = _env("BEDROCK_REGION", role) or _env("AWS_REGION", role, "us-east-1")
        model = _env(
            "BEDROCK_MODEL", role, "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        )
        return cls(region=region, model=model)


@dataclass(frozen=True)
class AzureOpenAISettings:
    """Azure OpenAI Service — ``AzureChatOpenAI`` (from the installed
    ``langchain-openai``; no extra needed).

    See https://learn.microsoft.com/azure/ai-services/openai/
    """

    api_key: str
    azure_endpoint: str
    azure_deployment: str
    api_version: str = "2024-10-21"
    model: str = ""  # deployment name is authoritative; model is informational

    @property
    def base_url(self) -> str:  # structural LlmSettings conformance
        return self.azure_endpoint

    @classmethod
    def from_env(cls, role: str | None = None) -> AzureOpenAISettings:
        key = _env("AZURE_OPENAI_API_KEY", role)
        if not key:
            raise RuntimeError(
                "Set AZURE_OPENAI_API_KEY for LLM_PROVIDER=azure. "
                "See https://learn.microsoft.com/azure/ai-services/openai/"
            )
        endpoint = _env("AZURE_OPENAI_ENDPOINT", role)
        if not endpoint:
            raise RuntimeError(
                "Set AZURE_OPENAI_ENDPOINT (e.g. https://<resource>.openai.azure.com) "
                "for LLM_PROVIDER=azure."
            )
        deployment = _env("AZURE_OPENAI_DEPLOYMENT", role)
        if not deployment:
            raise RuntimeError(
                "Set AZURE_OPENAI_DEPLOYMENT (your model deployment name) for "
                "LLM_PROVIDER=azure."
            )
        api_version = _env("AZURE_OPENAI_API_VERSION", role, "2024-10-21")
        model = _env("AZURE_OPENAI_MODEL", role, deployment)
        return cls(
            api_key=key,
            azure_endpoint=endpoint,
            azure_deployment=deployment,
            api_version=api_version,
            model=model,
        )


@dataclass(frozen=True)
class MistralSettings:
    """Mistral AI — ``ChatMistralAI``.

    Needs ``langchain-mistralai`` (``pip install 'yuyutsava[mistral]'``).
    See https://docs.mistral.ai/
    """

    api_key: str
    model: str = "mistral-large-latest"

    @property
    def base_url(self) -> str:  # structural LlmSettings conformance (n/a here)
        return ""

    @classmethod
    def from_env(cls, role: str | None = None) -> MistralSettings:
        key = _env("MISTRAL_API_KEY", role)
        if not key:
            raise RuntimeError(
                "Set MISTRAL_API_KEY in the environment. "
                "See https://console.mistral.ai/api-keys/"
            )
        model = _env("MISTRAL_MODEL", role, "mistral-large-latest")
        return cls(api_key=key, model=model)


@dataclass(frozen=True)
class CohereSettings:
    """Cohere — ``ChatCohere``.

    Needs ``langchain-cohere`` (``pip install 'yuyutsava[cohere]'``).
    See https://docs.cohere.com/
    """

    api_key: str
    model: str = "command-r-plus"

    @property
    def base_url(self) -> str:  # structural LlmSettings conformance (n/a here)
        return ""

    @classmethod
    def from_env(cls, role: str | None = None) -> CohereSettings:
        key = _env("COHERE_API_KEY", role)
        if not key:
            raise RuntimeError(
                "Set COHERE_API_KEY in the environment. "
                "See https://dashboard.cohere.com/api-keys"
            )
        model = _env("COHERE_MODEL", role, "command-r-plus")
        return cls(api_key=key, model=model)


def llm_settings_from_env(role: str | None = None) -> LlmSettings:
    """Pick provider via ``<ROLE>_LLM_PROVIDER`` (falls back to ``LLM_PROVIDER``).

    ``role`` may be ``None`` (no prefix — current CLI behaviour), ``"main"``
    (alias for None), or a custom role name like ``"triage"``,
    ``"orchestrator"``, ``"file_organizer"``. Roles let the daemon run a small
    cheap model for triage and a stronger one for the orchestrator.
    """
    effective_role = None if role in (None, "main") else role
    provider = _env("LLM_PROVIDER", effective_role, "groq").lower()
    # OpenAI-compatible providers (default ChatOpenAI path)
    if provider == "groq":
        return GroqSettings.from_env(effective_role)
    if provider == "openrouter":
        return OpenRouterSettings.from_env(effective_role)
    if provider == "ollama":
        return OllamaSettings.from_env(effective_role)
    if provider == "openai":
        return OpenAISettings.from_env(effective_role)
    if provider in ("openai_compatible", "custom"):
        return OpenAICompatibleSettings.from_env(effective_role)
    # Native-SDK providers (dedicated factory branch, lazy-imported package)
    if provider == "anthropic":
        return AnthropicSettings.from_env(effective_role)
    if provider in ("google", "gemini"):
        return GoogleSettings.from_env(effective_role)
    if provider == "vertex":
        return VertexSettings.from_env(effective_role)
    if provider in ("bedrock", "aws"):
        return BedrockSettings.from_env(effective_role)
    if provider in ("azure", "azure_openai"):
        return AzureOpenAISettings.from_env(effective_role)
    if provider == "mistral":
        return MistralSettings.from_env(effective_role)
    if provider == "cohere":
        return CohereSettings.from_env(effective_role)
    raise RuntimeError(
        f"Unknown LLM_PROVIDER={provider!r}; use one of: groq, openrouter, ollama, "
        "openai, openai_compatible, anthropic, google, vertex, bedrock, azure, "
        "mistral, cohere."
    )


# ---------------------------------------------------------------------------
# Path functions moved to ``yuyutsava.storage.paths`` (Step 1 of restructure).
# Importers should switch to ``from yuyutsava.storage.paths import …``.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Size limits + timing — consolidated from scattered module-level constants.
# Tune here; callers import LIMITS / TIMING and read the typed fields.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LimitsConfig:
    """Size caps applied to LLM-bound content and on-disk payloads."""

    # Absolute ceiling: tool results larger than this are never passed to the
    # LLM as-is. Catches binary blobs read as text and pathological stdout.
    max_tool_result_chars: int = 100_000

    # Softer cap used when constructing SuppressedContentNotice payloads for
    # sandbox stdout overflow.
    max_stdout_chars: int = 40_000

    # User preferences block injected into the orchestrator system prompt.
    # Roughly 500 tokens at 4 chars/token.
    max_prefs_chars: int = 2_000

    # Relevant-memory block injected into the orchestrator system prompt
    # (see yuyutsava.context.injector.MemoryInjector). Same budget rationale
    # as the prefs block.
    max_memory_chars: int = 2_000

    # Skill index XML rendered into the orchestrator system prompt.
    max_skill_index_chars: int = 8_000

    # Per-skill description cap inside the index.
    max_skill_desc_chars: int = 512

    # Docker sandbox: per-command stdout/stderr cap before SuppressedContentNotice.
    docker_max_output_bytes: int = 100_000


@dataclass(frozen=True)
class TimingConfig:
    """Default timeouts and busy-waits used across the runtime."""

    # Default `PRAGMA busy_timeout` for every async-sqlite store.
    sqlite_busy_timeout_ms: int = 5_000

    # Default seconds before a task_runner tr_execute_in_sandbox call gives up.
    tool_default_timeout_sec: int = 120

    # Default seconds before a deepagents bash tool call gives up.
    bash_default_timeout_sec: int = 120


LIMITS = LimitsConfig()
TIMING = TimingConfig()


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
        path = path or events_config_path()
        if not path.exists():
            return cls.default()
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
        return cls(sources=sources) if sources else cls.default()

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
                    "coalesce_window_ms": 750,
                },
            ),
        })

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"sources": {}}
        for name, src in self.sources.items():
            body: dict[str, Any] = {"enabled": src.enabled}
            body.update(src.params)
            out["sources"][name] = body
        return out

    def to_file(self, path: Path | None = None) -> Path:
        """Persist this config to events_config.json atomically."""
        path = path or events_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True))
        tmp.replace(path)
        return path

    def with_fs_root_added(self, root: str | Path) -> EventsConfig:
        """Return a new EventsConfig with ``root`` added to the fs source's roots."""
        root_str = str(Path(str(root)).expanduser())
        fs = self.sources.get("fs")
        if fs is None:
            new_fs = SourceConfig(
                name="fs",
                enabled=True,
                params={
                    "roots": [root_str],
                    "ignore": ["*.tmp", ".DS_Store", "*.crdownload", "*.part"],
                    "coalesce_window_ms": 750,
                },
            )
        else:
            roots = list(fs.params.get("roots") or [])
            if root_str not in roots:
                roots.append(root_str)
            params = {**fs.params, "roots": roots}
            new_fs = SourceConfig(name=fs.name, enabled=fs.enabled, params=params)
        return EventsConfig(sources={**self.sources, "fs": new_fs})

    def with_fs_root_removed(self, root: str | Path) -> EventsConfig:
        root_str = str(Path(str(root)).expanduser())
        fs = self.sources.get("fs")
        if fs is None:
            return self
        roots = [r for r in (fs.params.get("roots") or []) if r != root_str]
        params = {**fs.params, "roots": roots}
        new_fs = SourceConfig(name=fs.name, enabled=fs.enabled, params=params)
        return EventsConfig(sources={**self.sources, "fs": new_fs})


# ---------------------------------------------------------------------------
# Daemon config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DaemonConfig:
    """Daemon-wide settings: web server, consent expiry, token budgets."""

    web_host: str = "127.0.0.1"
    web_port: int = 7654
    web_open_browser: bool = False
    proposal_expiry_sec: int = 300
    orchestrator_token_budget: int = 60000
    subagent_token_budget: int = 60000
    headless: bool = False  # --no-ui semantics
    heartbeat_sec: int = 30  # idle sleep between event bursts; 0 = no sleep

    @classmethod
    def from_env(cls) -> DaemonConfig:
        port_raw = os.environ.get("YUYUTSAVA_DAEMON_PORT", "").strip()
        try:
            port = int(port_raw) if port_raw else 7654
        except ValueError:
            port = 7654

        host = os.environ.get("YUYUTSAVA_DAEMON_HOST", "127.0.0.1").strip() or "127.0.0.1"

        open_browser_raw = os.environ.get("YUYUTSAVA_DAEMON_OPEN_BROWSER", "").strip().lower()
        open_browser = open_browser_raw in ("1", "true", "yes")

        expiry_raw = os.environ.get("YUYUTSAVA_PROPOSAL_EXPIRY_SEC", "").strip()
        try:
            expiry = int(expiry_raw) if expiry_raw else 300
        except ValueError:
            expiry = 300

        # Fallbacks match the dataclass defaults (they used to disagree:
        # 8000/30000 here vs 60000 on the class — from_env silently shrank
        # every budget).
        orch_raw = os.environ.get("YUYUTSAVA_ORCHESTRATOR_TOKEN_BUDGET", "").strip()
        try:
            orch_budget = int(orch_raw) if orch_raw else 60000
        except ValueError:
            orch_budget = 60000

        sub_raw = os.environ.get("YUYUTSAVA_SUBAGENT_TOKEN_BUDGET", "").strip()
        try:
            sub_budget = int(sub_raw) if sub_raw else 60000
        except ValueError:
            sub_budget = 60000

        heartbeat_raw = os.environ.get("YUYUTSAVA_HEARTBEAT_SEC", "").strip()
        try:
            heartbeat = int(heartbeat_raw) if heartbeat_raw else 30
        except ValueError:
            heartbeat = 30

        return cls(
            web_host=host,
            web_port=port,
            web_open_browser=open_browser,
            proposal_expiry_sec=expiry,
            orchestrator_token_budget=orch_budget,
            subagent_token_budget=sub_budget,
            heartbeat_sec=heartbeat,
        )


# ---------------------------------------------------------------------------
# Search provider config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchConfig:
    """API keys for external web search providers (Tavily, Exa).

    Missing keys are not an error — the corresponding ws_* tools are simply
    absent from make_search_tools() output. No provider configured = no tools.
    """

    tavily_api_key: str = ""
    exa_api_key: str = ""

    @classmethod
    def from_env(cls) -> SearchConfig:
        """Build from env; missing keys leave the provider unavailable."""
        return cls(
            tavily_api_key=_env("TAVILY_API_KEY"),
            exa_api_key=_env("EXA_API_KEY"),
        )

    def is_available(self) -> dict[str, bool]:
        """Return which providers have a configured API key."""
        return {
            "tavily": bool(self.tavily_api_key),
            "exa": bool(self.exa_api_key),
        }
