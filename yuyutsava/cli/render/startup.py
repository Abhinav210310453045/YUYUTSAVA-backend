"""What the REPL prints before the first prompt.

The banner, the one-line status standing in for the silenced start-up logs,
and the langgraph-api version notice. Separated from the REPL because none of it is
the REPL: it is output with no state, which makes it testable without standing
up a session.
"""

from __future__ import annotations

import importlib.metadata
import json
import sys
import urllib.request
from pathlib import Path
from typing import Iterable

from packaging.version import InvalidVersion, Version

from yuyutsava.ansi import CYAN, DIM, RESET, YELLOW
from yuyutsava.llm import tracing as _tracing
from yuyutsava.storage.backend import StorageSettings
from yuyutsava.storage.sessions.config import SessionsSettings

BANNER_LINES = [
    "╦ ╦ ╦ ╦ ╦ ╦ ╦ ╦ ╔╦╗ ╔═╗ ╔═╗ ╦  ╦ ╔═╗",
    "╚╦╝ ║ ║ ╚╦╝ ║ ║  ║  ╚═╗ ╠═╣ ╚╗╔╝ ╠═╣",
    " ╩  ╚═╝  ╩  ╚═╝  ╩  ╚═╝ ╩ ╩  ╚╝  ╩ ╩",
]

LANGGRAPH_API = "langgraph-api"


def print_version_notice(*, full: bool = False) -> None:
    """Say once, above the banner, when langgraph-api has a newer release.

    langgraph_api has its own check, but it logs from a background thread that
    lands mid-chat (``AsyncSubagentHost.start`` turns it off with
    ``LANGGRAPH_NO_VERSION_CHECK``), and calling it directly means private API.
    This asks PyPI itself, with the same half-second timeout and the same
    end-of-life rule (:func:`langgraph_api_is_eol`). A pre-release install is
    not checked. Best-effort — never breaks start-up.

    ``full=True`` (debug plumbing) also shows the installed version.
    """
    try:
        current = Version(importlib.metadata.version(LANGGRAPH_API))
        url = f"https://pypi.org/pypi/{LANGGRAPH_API}/json"
        with urllib.request.urlopen(url, timeout=0.5) as resp:
            payload = json.load(resp)
        latest = Version(payload["info"]["version"])
        releases = payload.get("releases") or {}
    except Exception:  # noqa: BLE001 — not installed, offline, or an odd reply: no notice
        return
    if current.is_prerelease or latest <= current:
        return
    parts = [f"{LANGGRAPH_API} update available → {latest}"]
    if full:
        parts[0] = f"{LANGGRAPH_API} {current} → {latest} available"
    if langgraph_api_is_eol(current, latest, releases):
        parts.append("current version is EOL")
    parts.append(f"pip install -U {LANGGRAPH_API}")
    print(f"  {DIM}{YELLOW}⚠ {' · '.join(parts)}{RESET}", file=sys.stderr)


def langgraph_api_is_eol(current: Version, latest: Version, releases: Iterable[str]) -> bool:
    """langgraph-api's published support rule, applied to an installed version.

    End of life is two or more minor versions behind on the latest major, or
    an older major, except that while a new major is still at X.0 the last
    minor of the previous major is supported.
    """
    if current.major == latest.major:
        return current.minor <= latest.minor - 2
    if current.major == latest.major - 1 and latest.minor == 0:
        previous = []
        for raw in releases:
            try:
                version = Version(raw)
            except InvalidVersion:
                continue
            if version.major == current.major and not version.is_prerelease:
                previous.append(version)
        return not previous or current.minor != max(previous).minor
    return current.major < latest.major


def print_banner(
    *,
    session_id: str,
    workspace: Path,
    resuming: bool,
    status: str | None = None,
) -> None:
    print(file=sys.stderr)
    for line in BANNER_LINES:
        print(f"{CYAN}{line}{RESET}", file=sys.stderr)
    verb = "resumed session" if resuming else "session"
    print(file=sys.stderr)
    print(f"  {DIM}{verb}:{RESET}  {session_id}", file=sys.stderr)
    print(f"  {DIM}workspace:{RESET} {workspace}", file=sys.stderr)
    if status:
        print(f"  {DIM}{status}{RESET}", file=sys.stderr)
    print(
        f"  {DIM}type /help, /quit, or press Ctrl+D to exit{RESET}",
        file=sys.stderr,
    )
    print(file=sys.stderr)


def startup_status_line(sessions_settings: SessionsSettings) -> str:
    """One dim line summarizing what the silenced startup logs used to say.

    Replaces the ``pg pool: open`` / ``pg migrations: at vN`` / ``Langfuse not
    active`` INFO lines with a compact ``storage · tracing · langgraph-api``
    summary. Best-effort — every probe degrades to a placeholder.
    """
    try:
        tracing_on = _tracing.is_configured() and _tracing.langfuse_reachable()
    except Exception:
        tracing_on = False
    try:
        lg_version = importlib.metadata.version(LANGGRAPH_API)
    except importlib.metadata.PackageNotFoundError:
        lg_version = "?"
    # The EFFECTIVE backend, not SessionsSettings.backend. That field reads
    # YUYUTSAVA_SESSIONS_BACKEND, which only selects the checkpointer's own
    # default and is almost never set — so the banner announced "sqlite" while
    # every store and the checkpointer were on Postgres, and a whole session's
    # analysis started from the wrong assumption about where its data was.
    try:
        storage = (
            "postgres" if StorageSettings.from_env().is_postgres() else sessions_settings.backend
        )
    except Exception:
        storage = sessions_settings.backend
    return (
        f"storage: {storage} · "
        f"tracing: {'on' if tracing_on else 'off'} · "
        f"langgraph-api {lg_version}"
    )


__all__ = [
    "BANNER_LINES",
    "LANGGRAPH_API",
    "langgraph_api_is_eol",
    "print_banner",
    "print_version_notice",
    "startup_status_line",
]
