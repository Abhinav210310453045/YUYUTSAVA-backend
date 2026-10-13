"""Starting the containers a setting needs, and waiting until they really work.

The failure this exists for: Postgres is configured, Docker is not running, so
a chat dies on ``Postgres at 127.0.0.1:5435/yuyutsava is unavailable``. Starting
Docker is not enough either — the container still has to be up. Nothing in the
codebase could start one, so every path ended in a command a person had to find
and run themselves.

Three things make that safe to automate:

**Permission for each step.** Starting an application and starting containers
are separate questions, asked separately. Whoever asked gets told the exact
command first, and a no is answered with that command and the reason, not with
a dead end.

**Healthy, not started.** ``docker compose up -d`` returns as soon as the
container exists, which is well before Postgres accepts a connection. So a
start is not finished until the service's own probe in
:mod:`yuyutsava.config.checks` says it works — the same probe the wizard and
``yuyutsava config check`` use, so "started" and "works" cannot disagree.

**The repo's own compose files.** The service names, ports and profiles here
are the ones in ``docker-compose.yml`` and ``docker-compose.kroki.yml``; they
are not restated, they are referenced.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from yuyutsava.config import checks
from yuyutsava.config.checks import Check
from yuyutsava.platform.docker_app import launch_command, launch_docker_desktop

#: The checkout, where the compose files are.
REPO = Path(__file__).resolve().parents[2]

#: How long to wait for a service to become usable. A first start pulls images.
READY_TIMEOUT = 180.0

#: How long ``docker compose up`` itself may take, pulls included.
START_TIMEOUT = 600.0

#: How long to wait for the engine after Docker Desktop is asked to start.
ENGINE_TIMEOUT = 120.0


@dataclass(frozen=True)
class Service:
    """One thing YUYUTSAVA can start for you, as the compose files define it."""

    name: str
    #: The compose file, relative to the checkout; empty means the default one.
    compose_file: str
    #: What ``docker compose up -d`` is told to start.
    services: tuple[str, ...]
    #: The compose profile it lives behind, if any.
    profile: str = ""
    #: The probe that says whether it actually works.
    check: str = ""
    #: What it is for, in one line, for someone deciding whether to start it.
    purpose: str = ""

    @property
    def up_command(self) -> list[str]:
        """The exact command that starts it."""
        command = ["docker", "compose"]
        if self.compose_file:
            command += ["-f", self.compose_file]
        if self.profile:
            command += ["--profile", self.profile]
        return [*command, "up", "-d", *self.services]


#: Everything the setup can start, named as the compose files name it.
SERVICES: dict[str, Service] = {
    "postgres": Service(
        "postgres",
        "",
        ("postgres",),
        check="postgres",
        purpose="keeps your history and memory durably, and enables semantic memory "
        "(container yuyutsava-db, 127.0.0.1:5435)",
    ),
    "kroki": Service(
        "kroki",
        "docker-compose.kroki.yml",
        ("kroki", "mermaid"),
        check="kroki",
        purpose="renders Mermaid, PlantUML and D2 diagrams (port 8000; the mermaid "
        "container is started too, without which Mermaid alone returns 503)",
    ),
    "langfuse": Service(
        "langfuse",
        "",
        (),
        profile="langfuse",
        check="langfuse",
        purpose="records every model call so a run can be read back later "
        "(ClickHouse, Redis and MinIO beside Postgres; its web container takes port 3000)",
    ),
}


def _quoted(command: list[str]) -> str:
    """A command as a person would type it."""
    return " ".join(part if " " not in part else f'"{part}"' for part in command)


class DockerContainers:
    """Starts the containers a setting needs, with permission, and waits for them.

    *progress* is called with a line whenever there is something to say, so a
    wizard can show the wait without this module knowing what a terminal is.
    """

    def __init__(
        self,
        progress: Callable[[str], None] | None = None,
        *,
        repo: Path | None = None,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._say = progress or (lambda _line: None)
        self._repo = repo or REPO
        self._now = now
        self._sleep = sleep

    # --- the engine --------------------------------------------------------

    def engine(self) -> Check:
        """Whether the container engine is running."""
        return checks.check_docker()

    def engine_launch_command(self) -> str:
        """The command that would start Docker Desktop, or empty where there is none."""
        command = launch_command()
        return _quoted(command) if command else ""

    def launch_engine(self) -> Check:
        """Start Docker Desktop and wait for its engine to answer."""
        if not launch_docker_desktop():
            return Check(
                "Docker",
                problem="Docker Desktop could not be started from here",
                fix="Start Docker Desktop yourself, then try again.",
            )
        deadline = self._now() + ENGINE_TIMEOUT
        while self._now() < deadline:
            found = self.engine()
            if found.ok:
                return found
            self._say("  waiting for Docker to start …")
            self._sleep(3.0)
        return Check(
            "Docker",
            problem=f"Docker did not start within {ENGINE_TIMEOUT:.0f} seconds",
            fix="Open Docker Desktop and wait for it to say it is running, then try again.",
        )

    # --- the services ------------------------------------------------------

    def command(self, service: str) -> str:
        """The exact command that starts *service*, for someone who says no."""
        found = SERVICES.get(service)
        return _quoted(found.up_command) if found else "docker compose up -d"

    def purpose(self, service: str) -> str:
        """What *service* is for, for someone deciding whether to start it."""
        found = SERVICES.get(service)
        return found.purpose if found else ""

    def start(self, service: str) -> Check:
        """Start *service* and wait until its own probe says it works."""
        found = SERVICES.get(service)
        if found is None:
            return Check(service, problem=f"there is nothing called {service} to start")
        engine = self.engine()
        if not engine.ok:
            return engine
        self._say(f"  {_quoted(found.up_command)}")
        try:
            done = subprocess.run(
                found.up_command,
                cwd=self._repo,
                capture_output=True,
                text=True,
                timeout=START_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Check(
                service,
                problem=f"it did not start within {START_TIMEOUT / 60:.0f} minutes",
                fix=f"Run it yourself to watch it: {_quoted(found.up_command)}",
            )
        except OSError as exc:
            return Check(
                service, problem=str(exc), fix=f"Run {_quoted(found.up_command)} yourself."
            )
        if done.returncode != 0:
            return Check(
                service,
                problem=_last_line(done.stderr or done.stdout)
                or f"docker exited {done.returncode}",
                fix=f"Run it yourself to see the whole story: {_quoted(found.up_command)}",
            )
        return self.wait_until_ready(found)

    def wait_until_ready(self, service: Service) -> Check:
        """Poll *service*'s own probe until it works, or give up saying so."""
        if not service.check:
            return Check(service.name, note="started")
        deadline = self._now() + READY_TIMEOUT
        last = checks.run(service.check)
        while self._now() < deadline:
            if last.ok:
                return Check(service.name, note=f"{last.name} answered")
            self._say(f"  waiting for {service.name} to be ready …")
            self._sleep(3.0)
            last = checks.run(service.check)
        return Check(
            service.name,
            problem=f"it started but was not ready within {READY_TIMEOUT / 60:.0f} minutes "
            f"({last.problem})",
            fix=last.fix or f"Check it with: docker compose logs {service.name}",
        )


def _last_line(text: str) -> str:
    """The last line that says something, which is where docker puts the reason."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""
