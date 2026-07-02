"""Canonical configuration-variable catalog served to the Settings UI.

This is the **single source of truth** for which env variables the desktop
Settings form renders, so the form can never drift from the daemon again (the
old form hardcoded a stale subset). Each entry mirrors ``.env.example`` and the
defaults baked into the settings loaders (``core/config.py`` etc.).

``reload_class`` tells the UI what applying a change requires:

* ``hot``               — applied live, no restart (events/MCP config; not env).
* ``restart_resume``    — graceful restart; an in-flight task resumes from its
                          last LangGraph checkpoint. The default for env vars.
* ``restart_no_resume`` — graceful restart, but the change invalidates the
                          checkpoint store or the connection the UI talks to
                          (storage backend / DB paths / port / host), so an
                          in-flight task re-runs from scratch.

The endpoint returns metadata only — never secret *values*. The renderer
overlays the user's current values (read from ``~/.yuyutsava/.env``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# reload_class constants
HOT = "hot"
RESTART_RESUME = "restart_resume"
RESTART_NO_RESUME = "restart_no_resume"


@dataclass(frozen=True)
class ConfigVar:
    key: str
    label: str
    type: str = "text"  # text | number | password | select | toggle
    default: str = ""
    secret: bool = False
    reload_class: str = RESTART_RESUME
    options: list[str] = field(default_factory=list)
    placeholder: str = ""
    help: str = ""
    # When set, the UI only shows this field if settings[depends_key] == depends_value.
    depends_key: str = ""
    depends_value: str = ""


@dataclass(frozen=True)
class ConfigGroup:
    name: str
    vars: list[ConfigVar]


def _provider_dep(value: str) -> dict:
    return {"depends_key": "LLM_PROVIDER", "depends_value": value}


# Ordered groups. Keep the order user-friendly: most-touched first.
_GROUPS: list[ConfigGroup] = [
    ConfigGroup("Daemon", [
        ConfigVar("YUYUTSAVA_DAEMON_PORT", "Port", "number", default="7654",
                  reload_class=RESTART_NO_RESUME, placeholder="7654",
                  help="The app reconnects on the new port after restart."),
        ConfigVar("YUYUTSAVA_DAEMON_HOST", "Host", default="127.0.0.1",
                  reload_class=RESTART_NO_RESUME, placeholder="127.0.0.1"),
        ConfigVar("YUYUTSAVA_PROPOSAL_EXPIRY_SEC", "Proposal expiry (seconds)",
                  "number", default="300", placeholder="300"),
        ConfigVar("YUYUTSAVA_HEARTBEAT_SEC", "Heartbeat interval (seconds)",
                  "number", default="30", placeholder="30"),
        ConfigVar("YUYUTSAVA_ORCHESTRATOR_TOKEN_BUDGET", "Orchestrator token budget",
                  "number", default="60000", placeholder="60000"),
        ConfigVar("YUYUTSAVA_SUBAGENT_TOKEN_BUDGET", "Subagent token budget",
                  "number", default="60000", placeholder="60000"),
        ConfigVar("YUYUTSAVA_API_TOKEN", "API token", "password", secret=True,
                  help="Optional bearer token; required for non-loopback binds."),
        ConfigVar("YUYUTSAVA_OUTPUT_DIR", "Output directory"),
    ]),
    ConfigGroup("LLM Provider", [
        ConfigVar("LLM_PROVIDER", "Provider", "select", default="groq",
                  options=["groq", "openrouter", "anthropic", "ollama"]),
        ConfigVar("GROQ_API_KEY", "Groq API key", "password", secret=True,
                  placeholder="gsk_...", **_provider_dep("groq")),
        ConfigVar("GROQ_MODEL", "Groq model", default="llama-3.3-70b-versatile",
                  placeholder="llama-3.3-70b-versatile", **_provider_dep("groq")),
        ConfigVar("OPENROUTER_API_KEY", "OpenRouter API key", "password", secret=True,
                  placeholder="sk-or-...", **_provider_dep("openrouter")),
        ConfigVar("OPENROUTER_MODEL", "OpenRouter model", default="openai/gpt-4o-mini",
                  placeholder="openai/gpt-4o-mini", **_provider_dep("openrouter")),
        ConfigVar("ANTHROPIC_API_KEY", "Anthropic API key", "password", secret=True,
                  placeholder="sk-ant-...", **_provider_dep("anthropic")),
        ConfigVar("ANTHROPIC_MODEL", "Anthropic model",
                  default="claude-haiku-4-5-20251001",
                  placeholder="claude-haiku-4-5-20251001", **_provider_dep("anthropic")),
        ConfigVar("OLLAMA_HOST", "Ollama host", default="http://localhost:11434/v1",
                  placeholder="http://localhost:11434/v1", **_provider_dep("ollama")),
        ConfigVar("OLLAMA_MODEL", "Ollama model", default="llama3.2:3b",
                  placeholder="llama3.2:3b", **_provider_dep("ollama")),
    ]),
    ConfigGroup("Search", [
        ConfigVar("TAVILY_API_KEY", "Tavily API key", "password", secret=True,
                  placeholder="tvly-..."),
        ConfigVar("EXA_API_KEY", "Exa API key", "password", secret=True),
    ]),
    ConfigGroup("Execution & Docker", [
        ConfigVar("YUYUTSAVA_EXECUTION", "Execution mode", "select", default="local",
                  options=["local", "docker"]),
        ConfigVar("YUYUTSAVA_DOCKER_IMAGE", "Docker image",
                  default="deepagent-sandbox:local", placeholder="deepagent-sandbox:local"),
        ConfigVar("YUYUTSAVA_DOCKER_NETWORK", "Docker network", "select",
                  default="bridge", options=["bridge", "none"]),
        ConfigVar("YUYUTSAVA_DOCKER_MEMORY", "Memory limit", default="512m",
                  placeholder="512m"),
        ConfigVar("YUYUTSAVA_DOCKER_CPUS", "CPU limit", default="1.0", placeholder="1.0"),
        ConfigVar("YUYUTSAVA_DOCKER_PIDS_LIMIT", "PIDs limit", "number", default="100",
                  placeholder="100"),
        ConfigVar("YUYUTSAVA_DOCKER_EXPORT_DIR", "Export directory"),
    ]),
    ConfigGroup("Context", [
        ConfigVar("YUYUTSAVA_CONTEXT_MAX_INPUT_TOKENS", "Max input tokens", "number",
                  default="128000", placeholder="128000"),
        ConfigVar("YUYUTSAVA_CONTEXT_COMPACT_FRACTION", "Compact fraction",
                  default="0.7", placeholder="0.7"),
        ConfigVar("YUYUTSAVA_CONTEXT_KEEP_MESSAGES", "Keep recent messages", "number",
                  default="20", placeholder="20"),
        ConfigVar("YUYUTSAVA_CONTEXT_OFFLOAD_THRESHOLD_CHARS",
                  "Tool-result offload threshold (chars)", "number",
                  default="20000", placeholder="20000"),
        ConfigVar("YUYUTSAVA_CONTEXT_ALWAYS_OFFLOAD_PREFIXES",
                  "Always-offload tool prefixes", default="ws_", placeholder="ws_,db_",
                  help="Comma-separated tool-name prefixes whose results are always "
                       "offloaded regardless of size (the Context REPL). Default ws_ "
                       "covers web search."),
        ConfigVar("YUYUTSAVA_CONTEXT_SEMANTIC_RECALL", "Semantic recall (ctx_recall)",
                  "toggle", default="on",
                  help="Index offloaded artifacts into pgvector so agents can "
                       "ctx_recall relevant slices. Postgres-only; no-op on SQLite."),
        ConfigVar("YUYUTSAVA_CONTEXT_PIN_FIRST_MESSAGES", "Pin first messages", "number",
                  default="2", placeholder="2"),
        ConfigVar("YUYUTSAVA_CONTEXT_SUMMARIZER_INPUT_TOKENS", "Summarizer input tokens",
                  "number", default="12000", placeholder="12000"),
    ]),
    ConfigGroup("Memory & Embeddings", [
        ConfigVar("YUYUTSAVA_MEMORY_ENABLED", "Semantic memory", "toggle",
                  help="Defaults on when Postgres is live."),
        ConfigVar("YUYUTSAVA_MEMORY_TOP_K", "Memory recall top-K", "number",
                  default="5", placeholder="5"),
        ConfigVar("YUYUTSAVA_EMBED_MODEL", "Embedding model", default="nomic-embed-text",
                  placeholder="nomic-embed-text", help="Must output 768-dim vectors."),
        ConfigVar("EMBED_LLM_PROVIDER", "Embedder provider", default="ollama",
                  placeholder="ollama"),
        ConfigVar("EMBED_BASE_URL", "Embedder base URL",
                  default="http://localhost:11434/v1", placeholder="http://localhost:11434/v1"),
        ConfigVar("EMBED_API_KEY", "Embedder API key", "password", secret=True),
    ]),
    ConfigGroup("Model Routing", [
        ConfigVar("YUYUTSAVA_MODEL_ROUTING", "Complexity-based routing", "toggle",
                  default="0"),
        ConfigVar("YUYUTSAVA_ROUTING_THRESHOLDS", "Routing thresholds",
                  placeholder="comma-separated complexity cut points"),
        ConfigVar("TIER_LIGHT_LLM_PROVIDER", "Light-tier provider", "select",
                  options=["", "groq", "openrouter", "anthropic", "ollama"]),
        ConfigVar("TIER_LIGHT_OLLAMA_MODEL", "Light-tier Ollama model",
                  placeholder="llama3.2:3b"),
    ]),
    ConfigGroup("Storage", [
        ConfigVar("YUYUTSAVA_STORAGE_BACKEND", "Storage backend", "select",
                  default="sqlite", options=["sqlite", "postgres"],
                  reload_class=RESTART_NO_RESUME,
                  help="Switching backends moves the checkpoint store; an in-progress "
                       "task restarts from the beginning."),
        ConfigVar("YUYUTSAVA_PG_DSN", "Postgres DSN", "password", secret=True,
                  reload_class=RESTART_NO_RESUME,
                  placeholder="postgresql://yuyutsava:yuyutsava@127.0.0.1:5433/yuyutsava",
                  depends_key="YUYUTSAVA_STORAGE_BACKEND", depends_value="postgres"),
        ConfigVar("YUYUTSAVA_PG_POOL_MIN", "PG pool min", "number", default="1",
                  reload_class=RESTART_NO_RESUME, placeholder="1",
                  depends_key="YUYUTSAVA_STORAGE_BACKEND", depends_value="postgres"),
        ConfigVar("YUYUTSAVA_PG_POOL_MAX", "PG pool max", "number", default="10",
                  reload_class=RESTART_NO_RESUME, placeholder="10",
                  depends_key="YUYUTSAVA_STORAGE_BACKEND", depends_value="postgres"),
        ConfigVar("YUYUTSAVA_STORAGE_REQUIRE", "Require Postgres (no SQLite fallback)",
                  "toggle", default="0", reload_class=RESTART_NO_RESUME,
                  depends_key="YUYUTSAVA_STORAGE_BACKEND", depends_value="postgres"),
        ConfigVar("YUYUTSAVA_HOME", "Home directory", reload_class=RESTART_NO_RESUME,
                  placeholder="~/.yuyutsava"),
    ]),
    ConfigGroup("Resources", [
        ConfigVar("YUYUTSAVA_RES_CPU_HIGH_PCT", "CPU high watermark (%)", "number",
                  default="85.0", placeholder="85.0"),
        ConfigVar("YUYUTSAVA_RES_MEM_MIN_MB", "Min free memory (MB)", "number",
                  default="1024", placeholder="1024"),
        ConfigVar("YUYUTSAVA_RES_DISK_MIN_GB", "Min free disk (GB)", "number",
                  default="5.0", placeholder="5.0"),
        ConfigVar("YUYUTSAVA_RES_SAMPLE_SEC", "Sample interval (s)", "number",
                  default="5.0", placeholder="5.0"),
        ConfigVar("YUYUTSAVA_RES_DEFER_MAX_SEC", "Max deferral (s)", "number",
                  default="600.0", placeholder="600.0"),
        ConfigVar("YUYUTSAVA_RES_HEAVY_COMPLEXITY", "Heavy-task complexity", "number",
                  default="4", placeholder="4"),
        ConfigVar("YUYUTSAVA_RES_HEAVY_HINTS", "Heavy-task hint keywords",
                  placeholder="comma-separated keywords"),
    ]),
    ConfigGroup("Voice", [
        ConfigVar("STT_PROVIDER", "Speech-to-text", "select", default="faster_whisper",
                  options=["faster_whisper", "groq"]),
        ConfigVar("FASTER_WHISPER_MODEL", "faster-whisper model", default="base",
                  placeholder="base",
                  depends_key="STT_PROVIDER", depends_value="faster_whisper"),
        ConfigVar("GROQ_WHISPER_MODEL", "Groq Whisper model",
                  default="whisper-large-v3", placeholder="whisper-large-v3",
                  depends_key="STT_PROVIDER", depends_value="groq"),
        ConfigVar("YUYUTSAVA_STT_MIN_CONFIDENCE", "Min ASR confidence", "number",
                  default="0.35", placeholder="0.35",
                  help="Below this faster-whisper confidence (0–1) the user is "
                       "asked to repeat instead of running the agent on a garbled "
                       "transcript. Set 0 to disable. Ignored by Groq (no signal)."),
        ConfigVar("TTS_PROVIDER", "Text-to-speech", "select", default="piper",
                  options=["piper", "elevenlabs"]),
        ConfigVar("PIPER_MODEL", "Piper model",
                  depends_key="TTS_PROVIDER", depends_value="piper"),
        ConfigVar("ELEVENLABS_API_KEY", "ElevenLabs API key", "password", secret=True,
                  depends_key="TTS_PROVIDER", depends_value="elevenlabs"),
        ConfigVar("ELEVENLABS_VOICE_ID", "ElevenLabs voice id",
                  default="21m00Tcm4TlvDq8ikWAM",
                  depends_key="TTS_PROVIDER", depends_value="elevenlabs"),
        # Edited via the wake-word list editor, which pushes the new list to the
        # voice events-source params for hot-apply (no restart) — hence HOT.
        ConfigVar("WAKE_WORDS", "Wake words", default="hey_jarvis",
                  reload_class=HOT, placeholder="comma-separated"),
        ConfigVar("WAKE_THRESHOLD", "Wake threshold", default="0.5", placeholder="0.5"),
        ConfigVar("YUYUTSAVA_MIC_TUNE_LOG", "Mic tune score logging", "toggle",
                  default="0",
                  help="Log periodic wake-word peak scores to the voice log for "
                       "tuning the mic threshold. Off by default; these lines "
                       "flood the log when enabled."),
    ]),
    ConfigGroup("Notifications", [
        ConfigVar("YUYUTSAVA_TELEGRAM_BOT_TOKEN", "Telegram bot token", "password",
                  secret=True),
        ConfigVar("YUYUTSAVA_TELEGRAM_CHAT_IDS", "Telegram chat IDs",
                  placeholder="comma-separated chat ids"),
    ]),
    ConfigGroup("Observability", [
        ConfigVar("LANGFUSE_ENABLED", "Langfuse tracing", "toggle", default="1"),
        ConfigVar("LANGFUSE_PUBLIC_KEY", "Langfuse public key", "password", secret=True,
                  placeholder="pk-lf-..."),
        ConfigVar("LANGFUSE_SECRET_KEY", "Langfuse secret key", "password", secret=True,
                  placeholder="sk-lf-..."),
        ConfigVar("LANGFUSE_HOST", "Langfuse host", default="http://localhost:3000",
                  placeholder="http://localhost:3000"),
    ]),
]


def config_groups() -> list[ConfigGroup]:
    """The ordered config-variable catalog (metadata only, no values)."""
    return _GROUPS
