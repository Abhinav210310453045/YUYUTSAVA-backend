"""What the REPL prints before the first prompt.

The banner, the one-line status standing in for the silenced start-up logs,
and the langgraph-api version notice. Separated from the REPL because none of it is
the REPL: it is output with no state, which makes it testable without standing
up a session.
"""

from __future__ import annotations

import datetime as _dt
import getpass
import importlib.metadata
import json
import logging
import shutil
import socket
import sys
import urllib.request
from pathlib import Path
from typing import Iterable

from packaging.version import InvalidVersion, Version

from yuyutsava.ansi import AGENT, BOLD, CHROME, RESET, WARN
from yuyutsava.llm import tracing as _tracing
from yuyutsava.storage.backend import StorageSettings
from yuyutsava.storage.sessions.config import SessionsSettings

logger = logging.getLogger("yuyutsava.cli.render.startup")

#: The full wordmark: six rows, 78 columns. What a new session opens on when
#: the pane is wide enough to centre it.
WORDMARK = [
    "██╗   ██╗██╗   ██╗██╗   ██╗██╗   ██╗████████╗███████╗ █████╗ ██╗   ██╗ █████╗ ",
    "╚██╗ ██╔╝██║   ██║╚██╗ ██╔╝██║   ██║╚══██╔══╝██╔════╝██╔══██╗██║   ██║██╔══██╗",
    " ╚████╔╝ ██║   ██║ ╚████╔╝ ██║   ██║   ██║   ███████╗███████║██║   ██║███████║",
    "  ╚██╔╝  ██║   ██║  ╚██╔╝  ██║   ██║   ██║   ╚════██║██╔══██║╚██╗ ██╔╝██╔══██║",
    "   ██║   ╚██████╔╝   ██║   ╚██████╔╝   ██║   ███████║██║  ██║ ╚████╔╝ ██║  ██║",
    "   ╚═╝    ╚═════╝    ╚═╝    ╚═════╝    ╚═╝   ╚══════╝╚═╝  ╚═╝  ╚═══╝  ╚═╝  ╚═╝",
]

#: The narrow fallback: three rows, 36 columns.
BANNER_LINES = [
    "╦ ╦ ╦ ╦ ╦ ╦ ╦ ╦ ╔╦╗ ╔═╗ ╔═╗ ╦  ╦ ╔═╗",
    "╚╦╝ ║ ║ ╚╦╝ ║ ║  ║  ╚═╗ ╠═╣ ╚╗╔╝ ╠═╣",
    " ╩  ╚═╝  ╩  ╚═╝  ╩  ╚═╝ ╩ ╩  ╚╝  ╩ ╩",
]

#: Narrower still: the name, and nothing drawn.
WORDMARK_TEXT = "Y U Y U T S A V A"

TAGLINE = "a g e n t   w o r k b e n c h"

LANGGRAPH_API = "langgraph-api"

#: Left margin for everything under the wordmark, when nothing is centred.
_GUTTER = 2

#: The REPL's closing line. A one-shot run has no prompt to type at, so it
#: passes ``hint=None`` and gets its resume command instead.
REPL_HINT = "/help for commands   ·   Ctrl+D to exit"


def version_notice(*, full: bool = False) -> str:
    """One line when langgraph-api has a newer release, or ``""``.

    langgraph_api has its own check, but it logs from a background thread that
    lands mid-chat (``AsyncSubagentHost.start`` turns it off with
    ``LANGGRAPH_NO_VERSION_CHECK``), and calling it directly means private API.
    This asks PyPI itself, with the same half-second timeout and the same
    end-of-life rule (:func:`langgraph_api_is_eol`). A pre-release install is
    not checked. Best-effort — never breaks start-up.

    Returns the text rather than printing it, for two reasons: the banner
    decides where a notice belongs (below the wordmark, not above it), and the
    caller can run this off the start-up path — it makes a network call, and
    half a second of nothing before the first pixel is half a second spent on
    the one screen the program is judged by.

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
        logger.debug("version check skipped", exc_info=True)
        return ""
    if current.is_prerelease or latest <= current:
        return ""
    parts = [f"{LANGGRAPH_API} {latest} available"]
    if full:
        parts[0] = f"{LANGGRAPH_API} {current} → {latest} available"
    if langgraph_api_is_eol(current, latest, releases):
        parts.append(f"{current} is end of life")
    parts.append(f"pip install -U {LANGGRAPH_API}")
    return " · ".join(parts)


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


def _cols() -> int:
    """The terminal's width, or a sane 80 when it will not say."""
    try:
        return shutil.get_terminal_size(fallback=(80, 24)).columns
    except OSError:
        return 80


def wordmark(width: int) -> tuple[list[str], int]:
    """The largest wordmark that fits *width*, and its left margin.

    Three sizes, same ladder the dashboard uses for the split view: draw the
    thing that fits rather than the thing that was asked for. The margin
    centres the art, because a wordmark pushed against column zero reads as
    output and a centred one reads as a title screen.
    """
    for art in (WORDMARK, BANNER_LINES, [WORDMARK_TEXT]):
        if len(art[0]) + 2 <= width:
            return art, max(_GUTTER, (width - len(art[0])) // 2)
    return [WORDMARK_TEXT], _GUTTER


def identity_line() -> str:
    """``user@host · Mon 29 Sep 2026 · 14:32``.

    A session scrolled back to a week later should say when it started, and a
    screenshot of one should say whose machine it was. Both are best-effort:
    a container with no passwd entry still gets a banner.
    """
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 — no passwd entry (a container): skip the name
        user = "?"
    host = socket.gethostname().split(".")[0] or "?"
    now = _dt.datetime.now().astimezone()
    return f"{user}@{host}   ·   {now:%a %d %b %Y}   ·   {now:%H:%M}"


def print_banner(
    *,
    session_id: str,
    workspace: Path,
    resuming: bool,
    status: str | None = None,
    notice: str | None = None,
    resume_cmd: str | None = None,
    hint: str | None = REPL_HINT,
    width: int | None = None,
) -> None:
    """The screen a session opens on.

    Centred wordmark, who and when, then the machine facts on labelled rows
    that line up. *notice* (the langgraph-api line) goes below them rather than
    above the art, where it used to be the first thing anyone saw.
    """
    width = _cols() if width is None else width
    art, margin = wordmark(width)
    pad = " " * margin
    gutter = " " * _GUTTER

    out = [""]
    out += [f"{pad}{AGENT}{BOLD}{line}{RESET}" for line in art]
    if art is WORDMARK:
        tag_pad = " " * max(_GUTTER, (width - len(TAGLINE)) // 2)
        out += ["", f"{tag_pad}{CHROME}{TAGLINE}{RESET}"]
    out += ["", f"{gutter}{identity_line()}", ""]

    rows = [
        ("workspace", str(workspace)),
        ("session" if not resuming else "resumed", session_id),
    ]
    if status:
        rows.append(("runtime", status))
    if resume_cmd:
        rows.append(("resume", resume_cmd))
    label_width = max(len(label) for label, _ in rows)
    out += [f"{gutter}{CHROME}{label:<{label_width}}{RESET}   {value}" for label, value in rows]

    if notice:
        out += ["", f"{gutter}{WARN}⚠ {notice}{RESET}"]
    if hint:
        out += ["", f"{gutter}{CHROME}{hint}{RESET}"]
    out += [""]

    print("\n".join(out), file=sys.stderr)


def startup_status_line(sessions_settings: SessionsSettings) -> str:
    """One dim line summarizing what the silenced startup logs used to say.

    Replaces the ``pg pool: open`` / ``pg migrations: at vN`` / ``Langfuse not
    active`` INFO lines with a compact ``storage · tracing · langgraph-api``
    summary. Best-effort — every probe degrades to a placeholder.
    """
    try:
        tracing_on = _tracing.is_configured() and _tracing.langfuse_reachable()
    except Exception:  # noqa: BLE001 — a status-line probe: any failure shows tracing as off
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
    except RuntimeError:
        storage = sessions_settings.backend
    # No inline labels: the banner prints this beside a ``runtime`` label of
    # its own, and "runtime   storage: postgres" says storage twice.
    return f"{storage} · tracing {'on' if tracing_on else 'off'} · langgraph-api {lg_version}"


__all__ = [
    "BANNER_LINES",
    "LANGGRAPH_API",
    "TAGLINE",
    "WORDMARK",
    "WORDMARK_TEXT",
    "REPL_HINT",
    "identity_line",
    "langgraph_api_is_eol",
    "print_banner",
    "startup_status_line",
    "version_notice",
    "wordmark",
]
