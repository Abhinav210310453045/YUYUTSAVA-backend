"""What the environment still says about models once ``models.toml`` decides them.

Before ``models.toml`` exists, a process picks its models from ``LLM_PROVIDER``
and that provider's variables (:mod:`yuyutsava.llm.settings`). Once it exists
they are not read — the file decides. Left in a ``.env`` they mislead: someone
changes ``VERTEX_MODEL`` and nothing happens. So every surface names the ones
still set and offers to remove them (design §7, M7). Two kinds are left over:

* **settings** — ``LLM_PROVIDER``, a provider's model, project, location and
  the like, with or without a task's prefix (``TRIAGE_LLM_PROVIDER``);
* **keys** whose value ``secrets.toml`` now keeps and that ``models.toml`` does
  not read through ``env:`` — a wizard copied them in, leaving the original.

Never offered: a key ``models.toml`` reads (``env:GROQ_API_KEY``), a key another
part of YUYUTSAVA reads, and variables other programs share
(:data:`~yuyutsava.llm.settings.SHARED_VARIABLES`).
"""

from __future__ import annotations

import io
import os
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, find_dotenv
from dotenv.parser import parse_stream

from yuyutsava.llm.config.loader import ModelsConfigError, load_text
from yuyutsava.llm.config.schema import ModelsConfig, secret_references
from yuyutsava.llm.config.secrets import read_secrets_file
from yuyutsava.llm.settings import KEY_VARIABLES, SETTING_VARIABLES
from yuyutsava.llm.tasks import TASKS, env_role
from yuyutsava.platform.files import replace_private_text

#: Keys read outside the models, by what. Never offered for removal.
#: (``voice/backends/stt.py`` reads ``GROQ_API_KEY`` for Groq Whisper.)
READ_ELSEWHERE: Mapping[str, str] = {"GROQ_API_KEY": "voice speech-to-text (Groq Whisper)"}

#: ``PYTHON_DOTENV_DISABLED`` values that turn python-dotenv's loading off (its own set).
_DISABLED = frozenset({"1", "true", "t", "yes", "y"})

#: The ``<ROLE>_`` prefixes a task's variables may carry: ``TRIAGE_``, ``TIER_LIGHT_``, …
_PREFIXES: tuple[str, ...] = tuple(
    sorted({f"{role.upper()}_" for t in TASKS if (role := env_role(t.name)) is not None})
)


@dataclass(frozen=True)
class Leftover:
    """One variable ``models.toml`` made unnecessary."""

    #: The variable, as set: ``LLM_PROVIDER``, ``TRIAGE_GROQ_MODEL``.
    name: str
    #: ``"setting"`` or ``"key"``.
    kind: str
    #: The ``.env`` files that set it. Empty: it comes from elsewhere — the
    #: shell, a profile — and only its owner can unset it there.
    files: tuple[Path, ...]

    @property
    def why(self) -> str:
        """Why it can go, in a phrase."""
        if self.kind == "key":
            return "secrets.toml keeps this key now"
        return "models.toml decides this now"


def env_files(home: Path) -> tuple[Path, ...]:
    """The ``.env`` files YUYUTSAVA loads, in order: the project's, then ``<home>/.env``.

    The project's is found the way the CLI and daemon load it (python-dotenv,
    upwards from the installed package). Only files that exist; none when
    ``PYTHON_DOTENV_DISABLED`` turns python-dotenv's loading off.
    """
    if os.environ.get("PYTHON_DOTENV_DISABLED", "").casefold() in _DISABLED:
        return ()
    found = find_dotenv(usecwd=False)
    candidates = [Path(found)] if found else []
    candidates.append(home / ".env")
    seen: list[Path] = []
    for path in candidates:
        if path.is_file() and path.resolve() not in {p.resolve() for p in seen}:
            seen.append(path)
    return tuple(seen)


def find_leftovers(
    config: ModelsConfig,
    *,
    secrets: Mapping[str, str],
    environ: Mapping[str, str],
    files: Sequence[Path],
) -> list[Leftover]:
    """The variables set in *environ* or written in *files* that *config* makes unnecessary.

    *secrets* is ``secrets.toml``'s content. In name order.
    """
    written = {path: _values(path) for path in files}
    values: dict[str, str] = {}
    for found in written.values():
        values.update(found)
    values.update({k: v for k, v in environ.items() if v.strip()})
    kept = {value.strip() for value in secrets.values() if value.strip()}
    read = {ref.removeprefix("env:") for _, ref in secret_references(config)}
    leftovers = []
    for name, value in sorted(values.items()):
        base = _base_name(name)
        if base in SETTING_VARIABLES:
            kind = "setting"
        elif (
            base in KEY_VARIABLES
            and name not in read
            and name not in READ_ELSEWHERE
            and value.strip() in kept
        ):
            kind = "key"
        else:
            continue
        sources = tuple(path for path, found in written.items() if name in found)
        leftovers.append(Leftover(name, kind, sources))
    return leftovers


def leftovers_on_disk(
    models_path: Path, secrets_path: Path, *, environ: Mapping[str, str] | None = None
) -> list[Leftover]:
    """:func:`find_leftovers` for the files as they are now; none without a usable ``models.toml``.

    The ``.env`` files are :func:`env_files` of the directory ``models.toml`` is in.
    Reads files: off the event loop.
    """
    try:
        config = load_text(models_path.read_text(encoding="utf-8"), source=str(models_path))
    except (OSError, ModelsConfigError):
        return []
    return find_leftovers(
        config,
        secrets=read_secrets_file(secrets_path),
        environ=os.environ if environ is None else environ,
        files=env_files(models_path.parent),
    )


def say_leftovers(leftovers: Sequence[Leftover]) -> str:
    """One line naming *leftovers* and where they are, for a warning."""
    names = ", ".join(leftover.name for leftover in leftovers)
    files = sorted({str(path) for leftover in leftovers for path in leftover.files})
    where = f" (in {', '.join(files)})" if files else ""
    return f"{names}{where} — not read: models.toml decides the models now"


def remove_leftovers(
    leftovers: Iterable[Leftover], *, environ: MutableMapping[str, str] | None = None
) -> list[Path]:
    """Each leftover's lines removed from its ``.env`` files; the files changed.

    Every other line — comments, blank lines, other variables — is kept as it
    was, and the file stays readable by its owner alone (it may hold keys). A
    variable removed from a file leaves *environ* (default ``os.environ``) too,
    since it was loaded from there; one set elsewhere is left for its owner.
    """
    environ = os.environ if environ is None else environ
    by_file: dict[Path, set[str]] = {}
    for leftover in leftovers:
        for path in leftover.files:
            by_file.setdefault(path, set()).add(leftover.name)
    changed = []
    for path, names in by_file.items():
        target = path.resolve()  # a symlinked .env stays a symlink
        text = target.read_text(encoding="utf-8")
        kept = "".join(
            binding.original.string
            for binding in parse_stream(io.StringIO(text))
            if binding.key not in names
        )
        if kept != text:
            replace_private_text(target, kept)
            changed.append(path)
        for name in names:
            environ.pop(name, None)
    return changed


def _base_name(name: str) -> str:
    """*name* without a task's prefix: ``TRIAGE_GROQ_MODEL`` → ``GROQ_MODEL``."""
    if name in SETTING_VARIABLES or name in KEY_VARIABLES:
        return name
    for prefix in _PREFIXES:
        if name.startswith(prefix):
            return name.removeprefix(prefix)
    return name


def _values(path: Path) -> dict[str, str]:
    try:
        return {k: v for k, v in dotenv_values(path).items() if v is not None and v.strip()}
    except OSError:
        return {}


__all__ = [
    "READ_ELSEWHERE",
    "Leftover",
    "env_files",
    "find_leftovers",
    "leftovers_on_disk",
    "remove_leftovers",
    "say_leftovers",
]
