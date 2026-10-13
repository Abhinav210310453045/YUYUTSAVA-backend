"""Writing settings into a ``.env`` file — the other place they can live.

A person chooses where their settings are kept: ``settings.toml``
(:mod:`yuyutsava.config.settings_file`) or a ``.env`` file, which is what
YUYUTSAVA has always read. Both are first-class, and ``yuyutsava config move``
carries values between them.

Writing one is not a matter of rewriting the file. A ``.env`` is something a
person edits: it has their comments in it, their ordering, their other
variables, and it may hold keys. So a change here keeps every line it does not
touch exactly as it was, changes a variable in place where it already appears,
appends the rest under a named heading, and writes the result atomically with
owner-only permissions — the same treatment
:func:`yuyutsava.llm.config.leftovers.remove_leftovers` gives the same files.
"""

from __future__ import annotations

import io
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv.parser import parse_stream

from yuyutsava.platform.files import replace_private_text
from yuyutsava.storage.paths import state_dir

#: The heading new variables are appended under, so a person can see where
#: they came from when they open the file later.
ADDED_HEADING = "# Added by `yuyutsava setup`"


@dataclass(frozen=True)
class EnvFile:
    """One ``.env`` file a person can choose to keep their settings in."""

    path: Path
    #: How to describe it in a question, e.g. "this project's .env".
    description: str
    exists: bool


def home_env_path() -> Path:
    """``~/.yuyutsava/.env`` — the one the desktop app writes."""
    return state_dir() / ".env"


def project_env_path(start: Path | None = None) -> Path:
    """The checkout's ``.env``, whether or not it is there yet."""
    root = start or Path(__file__).resolve().parents[2]
    return root / ".env"


def choices() -> list[EnvFile]:
    """The ``.env`` files a person may pick, each named in full."""
    home, project = home_env_path(), project_env_path()
    found = [
        EnvFile(home, "shared by every YUYUTSAVA on this computer", home.is_file()),
        EnvFile(project, "only this checkout", project.is_file()),
    ]
    return [
        file for index, file in enumerate(found) if file.path not in {f.path for f in found[:index]}
    ]


def read_env_file(path: Path) -> dict[str, str]:
    """Every variable the file sets; nothing at all when there is no file."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    values = {}
    for binding in parse_stream(io.StringIO(text)):
        if binding.key:
            values[binding.key] = binding.value or ""
    return values


def write_env_values(
    path: Path,
    changes: Mapping[str, str],
    *,
    environ: dict[str, str] | None = None,
) -> list[str]:
    """Set *changes* in *path*; the names written. An empty value removes one.

    Every line the change does not touch — comments, blank lines, other
    variables — is kept byte for byte. A variable already in the file is
    changed where it stands, so a person's ordering survives; the rest are
    appended under :data:`ADDED_HEADING`.
    """
    target = path.resolve() if path.exists() else path  # a symlinked .env stays a symlink
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    remaining = dict(changes)
    kept = []
    for binding in parse_stream(io.StringIO(text)):
        key = binding.key
        if key is None or key not in remaining:
            kept.append(binding.original.string)
            continue
        value = remaining.pop(key)
        if value != "":
            kept.append(f"{key}={_quoted(value)}\n")
    body = "".join(kept)
    added = {key: value for key, value in remaining.items() if value != ""}
    if added:
        if body and not body.endswith("\n"):
            body += "\n"
        if ADDED_HEADING not in body:
            body += f"\n{ADDED_HEADING}\n"
        body += "".join(f"{key}={_quoted(value)}\n" for key, value in added.items())
    body = _without_an_orphan_heading(body)
    target.parent.mkdir(parents=True, exist_ok=True)
    if body != text:
        replace_private_text(target, body)
    written = [key for key, value in changes.items() if value != ""]
    live = os.environ if environ is None else environ
    for key, value in changes.items():
        if value == "":
            live.pop(key, None)
        else:
            live[key] = value
    return written


def _quoted(value: str) -> str:
    """A value as a ``.env`` line writes it: quoted only where it has to be."""
    if value == "" or any(c in value for c in " \t\"'#$`\\") or value != value.strip():
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _without_an_orphan_heading(text: str) -> str:
    """Drop our own heading once the last variable under it has been removed.

    Leaving it behind would litter a person's file with a heading for a section
    that no longer has anything in it — and it would still be there years later,
    because nothing else would ever clean it up.
    """
    if ADDED_HEADING not in text:
        return text
    lines = text.splitlines(keepends=True)
    at = max(i for i, line in enumerate(lines) if line.strip() == ADDED_HEADING)
    if any(line.strip() and not line.lstrip().startswith("#") for line in lines[at + 1 :]):
        return text
    while at and not lines[at - 1].strip():
        at -= 1
    return "".join(lines[:at])
