"""Daemon settings, from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass

from yuyutsava.config.env import env_flag, env_int

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
        port = env_int("YUYUTSAVA_DAEMON_PORT", default=7654)

        host = os.environ.get("YUYUTSAVA_DAEMON_HOST", "127.0.0.1").strip() or "127.0.0.1"

        open_browser = env_flag("YUYUTSAVA_DAEMON_OPEN_BROWSER", true=("1", "true", "yes"))

        expiry = env_int("YUYUTSAVA_PROPOSAL_EXPIRY_SEC", default=300)

        # Fallbacks match the dataclass defaults (they used to disagree:
        # 8000/30000 here vs 60000 on the class — from_env silently shrank
        # every budget).
        orch_budget = env_int("YUYUTSAVA_ORCHESTRATOR_TOKEN_BUDGET", default=60000)
        sub_budget = env_int("YUYUTSAVA_SUBAGENT_TOKEN_BUDGET", default=60000)

        heartbeat = env_int("YUYUTSAVA_HEARTBEAT_SEC", default=30)

        return cls(
            web_host=host,
            web_port=port,
            web_open_browser=open_browser,
            proposal_expiry_sec=expiry,
            orchestrator_token_budget=orch_budget,
            subagent_token_budget=sub_budget,
            heartbeat_sec=heartbeat,
        )
