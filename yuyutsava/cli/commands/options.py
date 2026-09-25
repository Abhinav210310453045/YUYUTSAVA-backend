"""What the two chat entry points are both given.

``run_chat`` (one shot) and ``run_chat_repl`` (interactive) take the same twelve
values and differ only at the edges — the one-shot needs the task and the Docker
paths to pull, the REPL needs its display flags. Those twelve were written out
in both signatures and built twice in ``cli.py`` from the same parsed arguments,
so a new one had to be threaded through four places to reach the agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from yuyutsava.config.execution import DockerSettings, ExecutionSettings, LocalSettings
from yuyutsava.config.search import SearchConfig
from yuyutsava.llm.settings import LlmSettings


@dataclass(frozen=True)
class ChatOptions:
    """Everything a chat needs before there is a conversation.

    Frozen because these are the invocation's decisions: a REPL that rewrote its
    own workspace or execution mode mid-session would leave the agent's bound
    tools pointing somewhere else.
    """

    workspace: Path
    settings: LlmSettings
    execution_mode: Literal["local", "docker"]
    docker_settings: DockerSettings
    local_settings: LocalSettings
    search_config: SearchConfig
    bash_timeout_sec: int
    recursion_limit: int
    permission_check: bool
    resume_id: str | None
    continue_latest: bool
    verbose: bool

    @property
    def execution(self) -> ExecutionSettings:
        """Where this chat's tools run."""
        return ExecutionSettings(
            mode=self.execution_mode, docker=self.docker_settings, local=self.local_settings
        )
