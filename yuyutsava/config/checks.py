"""Does this setting actually work? One probe per answer, each free to run.

The wizard, ``yuyutsava config check`` and the Settings UI all ask the same
question of a setting — *will this work?* — and all need the same answer back:
what is wrong, and what to do about it. That is already the shape
:class:`yuyutsava.llm.discovery.Discovery` uses for a model provider's login,
so :class:`Check` is the same four fields. A probe never raises and never
prints: a boundary decides how to say it.

Every probe here is free and sends nothing anywhere a person would be billed
for, with one exception that is asked about first: :func:`check_tavily` and
:func:`check_exa` can spend one search credit to prove a key really works, and
only do so when ``live=True``. The wizard says so before it offers.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

import httpx

#: How long any one probe may take. Long enough for a cold container, short
#: enough that a wizard with five of them does not feel hung.
TIMEOUT = 8.0

#: A Mermaid diagram small enough to render in a blink, used to prove Kroki's
#: companion container is up: Kroki itself answers /health without it and then
#: 503s every mermaid request, which is the failure people actually hit.
_TINY_MERMAID = "graph LR\n  a-->b\n"


@dataclass(frozen=True)
class Check:
    """What a probe found: the same shape a provider's login check returns."""

    #: What was probed, as a person would name it.
    name: str
    #: Why it did not work. Empty means it did.
    problem: str = ""
    #: What to do about :attr:`problem`.
    fix: str = ""
    #: Something worth knowing that is not a failure.
    note: str = ""

    @property
    def ok(self) -> bool:
        """Whether it worked."""
        return not self.problem


def _ran(*command: str, timeout: float = TIMEOUT) -> tuple[int, str]:
    """Run *command* for its exit status and output; ``(127, reason)`` if it cannot run."""
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, f"{command[0]} is not installed, or is not on PATH"
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return done.returncode, (done.stdout or done.stderr).strip()


def check_docker() -> Check:
    """Whether the Docker daemon is running and will answer."""
    name = "Docker"
    if not shutil.which("docker"):
        return Check(
            name,
            problem="Docker is not installed, or is not on PATH",
            fix="Install Docker Desktop from https://docs.docker.com/get-docker/, "
            "then start YUYUTSAVA again.",
        )
    code, output = _ran("docker", "version", "--format", "{{.Server.Version}}")
    if code == 0 and output:
        return Check(name, note=f"Docker Engine {output}")
    return Check(
        name,
        problem="Docker is installed but its engine is not responding",
        fix="Start Docker Desktop and wait for it to say it is running, then try again.",
    )


def check_postgres(dsn: str = "") -> Check:
    """Whether the configured Postgres answers a query, not just a connection."""
    from yuyutsava.storage.backend import StorageSettings  # noqa: PLC0415 — psycopg is heavy
    from yuyutsava.storage.pg.pool import describe_dsn  # noqa: PLC0415 — psycopg is heavy

    settings = StorageSettings.from_env()
    target = dsn or settings.pg_dsn
    where = describe_dsn(target)
    name = f"Postgres at {where}"
    try:
        import psycopg  # noqa: PLC0415 — only needed on this path
    except ImportError:
        return Check(
            name,
            problem="the psycopg driver is not installed",
            fix="Reinstall with `python scripts/install.py`, which includes it.",
        )
    try:
        with psycopg.connect(target, connect_timeout=int(TIMEOUT)) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
    except Exception as exc:  # noqa: BLE001 — a boundary: psycopg raises many types
        reason = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return Check(
            name,
            problem=f"it is not answering ({reason.rsplit(': ', 1)[-1]})",
            fix="Start it with `docker compose up -d postgres` in the YUYUTSAVA checkout "
            "(the container is called yuyutsava-db), or switch to SQLite with "
            "`yuyutsava config set YUYUTSAVA_STORAGE_BACKEND sqlite`.",
        )
    return Check(name)


def check_kroki(url: str = "") -> Check:
    """Whether Kroki answers *and* can render Mermaid, which needs its companion."""
    base = (url or os.environ.get("YUYUTSAVA_KROKI_URL", "") or "http://localhost:8000").rstrip("/")
    name = f"Kroki at {base}"
    try:
        answer = httpx.post(f"{base}/mermaid/svg", content=_TINY_MERMAID.encode(), timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        return Check(
            name,
            problem=f"it is not reachable ({type(exc).__name__})",
            fix="Start it with `docker compose -f docker-compose.kroki.yml up -d` in the "
            "YUYUTSAVA checkout, or point YUYUTSAVA_KROKI_URL at a Kroki you already run.",
        )
    if answer.status_code == 200:
        return Check(name)
    if answer.status_code == 503:
        return Check(
            name,
            problem="Kroki is up but cannot render Mermaid (503)",
            fix="Its companion container is missing. `docker compose -f "
            "docker-compose.kroki.yml up -d` starts both kroki and mermaid; "
            "the kroki container alone renders Graphviz and PlantUML but not Mermaid.",
        )
    return Check(
        name,
        problem=f"it answered {answer.status_code}",
        fix="Check that YUYUTSAVA_KROKI_URL names a Kroki server.",
    )


def check_langfuse(host: str = "") -> Check:
    """Whether the Langfuse web container is up."""
    base = (host or os.environ.get("LANGFUSE_HOST", "") or "http://localhost:3000").rstrip("/")
    name = f"Langfuse at {base}"
    try:
        answer = httpx.get(f"{base}/api/public/health", timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        return Check(
            name,
            problem=f"it is not reachable ({type(exc).__name__})",
            fix="Start it with `docker compose --profile langfuse up -d` in the YUYUTSAVA "
            "checkout. It takes host port 3000, which most frontend dev servers also "
            "want — remap it in docker-compose.yml if something else needs 3000.",
        )
    if answer.status_code < 400:
        return Check(name)
    return Check(
        name,
        problem=f"it answered {answer.status_code}",
        fix="Give it a moment to finish starting, then check it again.",
    )


def _key_shaped(name: str, value: str, prefix: str, where: str) -> Check:
    """A key's shape only — no call, so nothing is spent."""
    if not value.strip():
        return Check(name, problem="no key is set", fix=f"Get one at {where}.")
    if prefix and not value.startswith(prefix):
        return Check(
            name,
            problem=f"that key does not start with {prefix}, as this provider's keys do",
            fix=f"Check you pasted the whole key from {where}.",
            note="Only the shape was checked; nothing was sent.",
        )
    return Check(name, note="Only the shape was checked; nothing was sent.")


def check_tavily(key: str = "", *, live: bool = False) -> Check:
    """Tavily's key. With *live*, one real search — which spends one credit."""
    value = key or os.environ.get("TAVILY_API_KEY", "")
    name = "Tavily"
    if not live:
        return _key_shaped(name, value, "tvly-", "https://tavily.com")
    if not value.strip():
        return Check(name, problem="no key is set", fix="Get one at https://tavily.com.")
    try:
        answer = httpx.post(
            "https://api.tavily.com/search",
            json={"api_key": value, "query": "yuyutsava setup check", "max_results": 1},
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return Check(
            name,
            problem=f"Tavily could not be reached ({type(exc).__name__})",
            fix="Check this computer's connection, then try again.",
        )
    if answer.status_code == 200:
        return Check(name, note="One search was spent proving the key works.")
    return Check(
        name,
        problem=f"Tavily refused the key ({answer.status_code})",
        fix="Copy the key again from https://app.tavily.com.",
    )


def check_exa(key: str = "", *, live: bool = False) -> Check:
    """Exa's key. With *live*, one real search — which spends one credit."""
    value = key or os.environ.get("EXA_API_KEY", "")
    name = "Exa"
    if not live:
        return _key_shaped(name, value, "", "https://exa.ai")
    if not value.strip():
        return Check(name, problem="no key is set", fix="Get one at https://exa.ai.")
    try:
        answer = httpx.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": value},
            json={"query": "yuyutsava setup check", "numResults": 1},
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return Check(
            name,
            problem=f"Exa could not be reached ({type(exc).__name__})",
            fix="Check this computer's connection, then try again.",
        )
    if answer.status_code == 200:
        return Check(name, note="One search was spent proving the key works.")
    return Check(
        name,
        problem=f"Exa refused the key ({answer.status_code})",
        fix="Copy the key again from https://dashboard.exa.ai.",
    )


#: Every probe by the name a :class:`~yuyutsava.config.catalog.ConfigVar` gives.
PROBES: dict[str, Callable[..., Check]] = {
    "docker": check_docker,
    "postgres": check_postgres,
    "kroki": check_kroki,
    "langfuse": check_langfuse,
    "tavily": check_tavily,
    "exa": check_exa,
}

#: The probes that can spend money or a credit, and so are never run unasked.
COSTS_A_CREDIT = frozenset({"tavily", "exa"})


def run(name: str, **options: object) -> Check:
    """Run the probe *name*; an unknown name is a problem, not an exception."""
    probe = PROBES.get(name)
    if probe is None:
        return Check(name, problem=f"there is no check called {name}")
    try:
        return probe(**options)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 — a boundary: a probe must never raise at a caller
        return Check(
            name,
            problem=f"the check itself failed ({type(exc).__name__}: {exc})",
            fix="This is a bug in YUYUTSAVA, not in your setup — the setting may still be fine.",
        )
