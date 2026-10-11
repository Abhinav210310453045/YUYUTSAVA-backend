"""What the REPL prints before the first prompt.

The banner, the one-line status standing in for the silenced start-up logs,
and the langgraph-api version notice. Separated from the REPL because none of it is
the REPL: it is output with no state, which makes it testable without standing
up a session.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import getpass
import importlib.metadata
import json
import logging
import shutil
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Iterable

from packaging.version import InvalidVersion, Version

from yuyutsava.ansi import AGENT, BOLD, CHROME, KEY, RESET, VALUE, WARN
from yuyutsava.llm import tracing as _tracing
from yuyutsava.storage.backend import StorageSettings
from yuyutsava.storage.paths import state_dir
from yuyutsava.storage.sessions.config import SessionsSettings

logger = logging.getLogger("yuyutsava.cli.render.startup")

#: The full wordmark: six rows, 78 columns. What a new session opens on when
#: the pane is wide enough to centre it. One block rather than six quoted
#: strings: the rows only mean anything stacked, and a stray edit to one of
#: them is visible here and invisible in a list of literals.
WORDMARK = """\
██╗   ██╗██╗   ██╗██╗   ██╗██╗   ██╗████████╗███████╗ █████╗ ██╗   ██╗ █████╗ 
╚██╗ ██╔╝██║   ██║╚██╗ ██╔╝██║   ██║╚══██╔══╝██╔════╝██╔══██╗██║   ██║██╔══██╗
 ╚████╔╝ ██║   ██║ ╚████╔╝ ██║   ██║   ██║   ███████╗███████║██║   ██║███████║
  ╚██╔╝  ██║   ██║  ╚██╔╝  ██║   ██║   ██║   ╚════██║██╔══██║╚██╗ ██╔╝██╔══██║
   ██║   ╚██████╔╝   ██║   ╚██████╔╝   ██║   ███████║██║  ██║ ╚████╔╝ ██║  ██║
   ╚═╝    ╚═════╝    ╚═╝    ╚═════╝    ╚═╝   ╚══════╝╚═╝  ╚═╝  ╚═══╝  ╚═╝  ╚═╝\
""".splitlines()

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
REPL_HINT = "/help for commands  ·  Ctrl+D to exit"


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
        latest, releases = _latest_release(timeout=0.5)
    except Exception:  # noqa: BLE001 — not installed, offline, or an odd reply: no notice
        logger.debug("version check skipped", exc_info=True)
        return ""
    return _notice(current, latest, releases, full=full)


#: How long a version check's answer is used before it is asked again.
_VERSION_CHECK_EVERY_SECONDS = 24 * 3600


def cached_version_notice(*, full: bool = False, path: Path | None = None) -> str:
    """:func:`version_notice`, from the last check's answer — never from the network.

    The answer is kept in the state directory. When it is missing or more than
    a day old, it is asked for again on a background thread, for the next start:
    the banner never waits on PyPI, and a first start after a day shows the
    notice one start later. Best-effort, like the check itself.
    """
    path = path or state_dir() / ".langgraph_api_release.json"
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        checked_at = float(cached["checked_at"])
    except (OSError, ValueError, KeyError, TypeError):
        cached, checked_at = None, 0.0
    if time.time() - checked_at > _VERSION_CHECK_EVERY_SECONDS:
        threading.Thread(
            target=_remember_latest_release, args=(path,), name="version-check", daemon=True
        ).start()
    if cached is None:
        return ""
    try:
        current = Version(importlib.metadata.version(LANGGRAPH_API))
        latest = Version(cached["latest"])
    except Exception:  # noqa: BLE001 — not installed, or an odd file: no notice
        return ""
    return _notice(current, latest, cached.get("releases") or [], full=full)


def _latest_release(*, timeout: float) -> tuple[Version, list[str]]:
    """langgraph-api's newest release on PyPI, and every release it has published."""
    url = f"https://pypi.org/pypi/{LANGGRAPH_API}/json"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        payload = json.load(resp)
    return Version(payload["info"]["version"]), list(payload.get("releases") or {})


def _remember_latest_release(path: Path) -> None:
    try:
        latest, releases = _latest_release(timeout=5)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"checked_at": time.time(), "latest": str(latest), "releases": releases}),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 — offline or an odd reply: asked again next start
        logger.debug("version check skipped", exc_info=True)


def _notice(current: Version, latest: Version, releases: Iterable[str], *, full: bool) -> str:
    if current.is_prerelease or latest <= current:
        return ""
    parts = [f"{LANGGRAPH_API} {latest} available"]
    if full:
        parts[0] = f"{LANGGRAPH_API} {current} → {latest} available"
    if langgraph_api_is_eol(current, latest, releases):
        parts.append(f"{current} is end of life")
    # Not `pip install -U`: that moves one package off the lock, and a newer
    # langgraph-api needs langgraph-sdk and langgraph to move with it.
    parts.append("upgrade through uv.lock, see CONTRIBUTING.md")
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


def _row(label: str, value: str, width: int, *, style: str = KEY) -> str:
    """One ``label   value`` line: the label bold and coloured, the value not.

    The banner's job is to be read at a glance, and a dim grey label beside a
    plain white value gives the eye nothing to land on. The label carries the
    weight and the hue; the value carries a second hue of its own so it is
    legible without competing.
    """
    return f"{' ' * _GUTTER}{style}{BOLD}{label:<{width}}{RESET}   {VALUE}{value}{RESET}"


@dataclasses.dataclass(frozen=True)
class BannerFacts:
    """What the opening screen says about this session.

    A record rather than seven parameters: the banner is drawn from four call
    sites, each of which cares about a different subset, and a positional
    mistake among that many keywords is the kind that shows a workspace path
    where a session id belongs.
    """

    session_id: str
    workspace: Path
    resuming: bool = False
    #: ``storage · tracing · langgraph-api``, from :func:`startup_status_line`.
    status: str | None = None
    #: The langgraph-api line, drawn as a ``warning`` row among the facts.
    notice: str | None = None
    #: The command that resumes this session. The one-shot path offers it;
    #: the REPL is already inside the session and does not.
    resume_cmd: str | None = None
    #: The closing line. ``None`` for a run with no prompt to type at.
    hint: str | None = REPL_HINT


def print_banner(facts: BannerFacts, *, width: int | None = None) -> None:
    """The screen a session opens on.

    A centred wordmark, who and when, then every fact about the session as one
    labelled row. *notice* — the langgraph-api line — is a row like the others
    rather than a paragraph, so the block has one shape; its label keeps the
    warning colour, because a warning that looks exactly like a fact is not
    doing its job.
    """
    width = _cols() if width is None else width
    art, margin = wordmark(width)
    pad = " " * margin

    out = [""]
    out += [f"{pad}{AGENT}{BOLD}{line}{RESET}" for line in art]
    if art is WORDMARK:
        tag_pad = " " * max(_GUTTER, (width - len(TAGLINE)) // 2)
        out += ["", f"{tag_pad}{CHROME}{TAGLINE}{RESET}"]
    out += ["", f"{' ' * _GUTTER}{identity_line()}", ""]

    rows: list[tuple[str, str, str]] = [
        ("workspace", str(facts.workspace), KEY),
        ("resumed" if facts.resuming else "session", facts.session_id, KEY),
    ]
    if facts.status:
        rows.append(("runtime", facts.status, KEY))
    if facts.resume_cmd:
        rows.append(("resume", facts.resume_cmd, KEY))
    if facts.notice:
        rows.append(("warning", facts.notice, WARN))
    if facts.hint:
        rows.append(("help", facts.hint, KEY))

    label_width = max(len(label) for label, _value, _style in rows)
    out += [_row(label, value, label_width, style=style) for label, value, style in rows]
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
    "BannerFacts",
    "LANGGRAPH_API",
    "TAGLINE",
    "WORDMARK",
    "WORDMARK_TEXT",
    "REPL_HINT",
    "identity_line",
    "langgraph_api_is_eol",
    "print_banner",
    "startup_status_line",
    "cached_version_notice",
    "version_notice",
    "wordmark",
]
