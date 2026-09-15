"""Where tools run: the Docker sandbox and local execution settings, from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from yuyutsava.config.env import env_int


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
        network: Literal["bridge", "none"] = (
            network_raw if network_raw in ("bridge", "none") else "bridge"
        )  # type: ignore[assignment]

        memory = os.environ.get("YUYUTSAVA_DOCKER_MEMORY", "").strip() or "512m"
        cpus = os.environ.get("YUYUTSAVA_DOCKER_CPUS", "").strip() or "1.0"

        pids_limit = env_int("YUYUTSAVA_DOCKER_PIDS_LIMIT", default=100)

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
        raw_output = os.environ.get("YUYUTSAVA_OUTPUT_DIR", "").strip()
        return cls(
            sandbox_dir=Path(raw_sandbox) if raw_sandbox else None,
            output_dir=Path(raw_output) if raw_output else None,
        )
