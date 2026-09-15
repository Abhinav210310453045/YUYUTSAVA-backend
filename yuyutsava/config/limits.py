"""Size limits and timing in one place.

Callers import ``LIMITS`` / ``TIMING`` and read the typed fields; tune them here.
"""

from __future__ import annotations

from dataclasses import dataclass


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
