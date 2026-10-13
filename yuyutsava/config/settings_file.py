"""``settings.toml`` — every setting that is not a model, kept as a file.

Two places can hold a setting, and a person chooses which:

* a ``.env`` file, which is what YUYUTSAVA has always read, and
* ``~/.yuyutsava/settings.toml``, written here: validated against
  :mod:`yuyutsava.config.catalog`, written whole or not at all, and keepable in
  named versions so more than one setup can live side by side.

Nothing reads this file directly. :func:`apply_settings` puts its values into
the environment at start-up, beside the ``load_dotenv()`` call that was already
there, so every reader goes on calling :func:`yuyutsava.config.env.env` and not
one of them had to change. That also fixes the order of precedence:

    a variable set in the real shell  >  settings.toml  >  .env  >  the default

The shell wins because a one-off override and a CI run must keep working.

The file's tables are the catalog's groups and its keys are the variable names,
so a value can move between here and a ``.env`` without being translated, and
the file reads next to ``.env.example``::

    version = 1

    [search]
    TAVILY_API_KEY = "tvly-..."

    [diagrams]
    YUYUTSAVA_KROKI_URL = "http://localhost:8000"

Writing follows ``llm/config/store.py``, for the same reasons: a lock so two
programs cannot interleave, the digest of what was read so a change made
elsewhere meanwhile is refused rather than lost, a parse before the swap so a
broken file is never left behind, ``tomlkit`` so comments written by hand
survive, and an atomic owner-only replace because the file holds keys.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

from yuyutsava.config.catalog import group_of, known_keys
from yuyutsava.platform import FileLock
from yuyutsava.platform.files import replace_private_text
from yuyutsava.storage.paths import state_dir

#: The digest of a file that is not there yet.
ABSENT = "absent"

#: The file format. Not a version a person keeps — see :meth:`SettingsStore.save_version`.
FORMAT = 1

FILE_HEADER = (
    "YUYUTSAVA settings: everything that is not a model (models live in models.toml).",
    "Edit by hand or with `yuyutsava config set <name> <value>`.",
    "A variable set in your shell still wins over this file; a .env file loses to it.",
    "Keep a copy to come back to with `yuyutsava config version save <name>`.",
)


class StaleWrite(RuntimeError):
    """``settings.toml`` changed between being read and being written."""


class SettingsError(ValueError):
    """``settings.toml`` does not load, or says something it may not say."""


@dataclass(frozen=True)
class Revision:
    """``settings.toml`` as read: its text and the digest a write must name."""

    text: str
    #: SHA-256 of :attr:`text`; :data:`ABSENT` when there is no file.
    digest: str


@dataclass(frozen=True)
class Settings:
    """What one ``settings.toml`` says."""

    values: dict[str, str] = field(default_factory=dict)
    #: Names the catalog does not know: kept as they are and reported, never
    #: dropped, because an unheard-of setting is likelier a newer build than a
    #: mistake.
    unknown: tuple[str, ...] = ()


def settings_path() -> Path:
    """``settings.toml``: every setting that is not a model."""
    return state_dir() / "settings.toml"


def versions_dir() -> Path:
    """Where kept versions of ``settings.toml`` live, one file each."""
    return state_dir() / "settings-versions"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse(text: str, *, source: str = "settings.toml") -> Settings:
    """What *text* says, or a :class:`SettingsError` saying where it is wrong."""
    try:
        document = tomlkit.parse(text)
    except Exception as exc:  # noqa: BLE001 — a boundary: tomlkit raises several types
        raise SettingsError(f"{source} is not valid TOML: {exc}") from exc
    known = known_keys()
    values: dict[str, str] = {}
    unknown: list[str] = []
    for table, body in document.items():
        if table == "version":
            continue
        if not isinstance(body, dict):
            raise SettingsError(
                f"{source}: {table} should be a group of settings, like [search], not a value"
            )
        for key, value in body.items():
            if isinstance(value, (dict, list)):
                raise SettingsError(f"{source}: {key} should be one value, not a list or a table")
            values[str(key)] = "" if value is None else str(value)
            if key not in known:
                unknown.append(str(key))
    return Settings(values, tuple(sorted(unknown)))


def read_settings(path: Path | None = None) -> Settings:
    """What ``settings.toml`` says; nothing at all when there is no file."""
    target = path or settings_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Settings()
    return parse(text, source=str(target))


def shell_keys(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    """The variable names set right now — the real shell's, if read first thing.

    Snapshot this *before* ``load_dotenv()`` and hand it to
    :func:`apply_settings` as ``protect``: that is what keeps the order
    *shell > settings.toml > .env* true even where a ``.env`` is loaded with
    ``override=True``, as the daemon loads ``~/.yuyutsava/.env``.
    """
    target = environ if environ is not None else os.environ
    return frozenset(key for key, value in target.items() if value.strip())


def apply_settings(
    environ: dict[str, str] | None = None,
    *,
    path: Path | None = None,
    protect: Collection[str] | None = None,
) -> list[str]:
    """Put ``settings.toml``'s values into the environment; the names set.

    *protect* names the variables to leave alone whatever they now say — the
    shell's own, from :func:`shell_keys`. Without it, anything already set is
    left alone, which is right when this is called before any ``.env`` is
    loaded and wrong after one.

    Called at start-up beside the ``load_dotenv()`` that was already there,
    which is why no reader in the codebase had to change. A file that does not
    load is not a reason to refuse to start: ``yuyutsava config check`` says
    what is wrong with it.
    """
    target = environ if environ is not None else os.environ
    try:
        settings = read_settings(path)
    except SettingsError:
        return []
    keep = frozenset(protect) if protect is not None else shell_keys(target)
    applied = []
    for key, value in settings.values.items():
        if key in keep:
            continue
        target[key] = value
        applied.append(key)
    return applied


def _document(values: Mapping[str, str]) -> tomlkit.TOMLDocument:
    """*values* as a whole document, in the catalog's groups."""
    document = tomlkit.document()
    for line in FILE_HEADER:
        document.add(tomlkit.comment(line))
    document.add(tomlkit.nl())
    document["version"] = FORMAT
    tables: dict[str, list[tuple[str, str]]] = {}
    for key, value in values.items():
        try:
            name = group_of(key).slug
        except KeyError:
            name = "other"
        tables.setdefault(name, []).append((key, value))
    for name in sorted(tables):
        table = tomlkit.table()
        for key, value in sorted(tables[name]):
            table[key] = value
        document[name] = table
    return document


class SettingsStore:
    """Reads and writes one ``settings.toml`` and the versions kept beside it."""

    def __init__(self, path: Path | None = None, versions: Path | None = None) -> None:
        self._path = path or settings_path()
        self._versions = versions or versions_dir()
        self._lock_path = self._path.with_name("settings.lock")

    @property
    def path(self) -> Path:
        """The ``settings.toml`` this store writes."""
        return self._path

    @property
    def versions_path(self) -> Path:
        """The directory holding kept versions."""
        return self._versions

    def read(self) -> Revision:
        """``settings.toml`` as it is now."""
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return Revision("", ABSENT)
        return Revision(text, _digest(text))

    def settings(self) -> Settings:
        """What the file says; nothing at all when there is no file."""
        return read_settings(self._path)

    def write(self, values: Mapping[str, str], *, expected: str) -> Revision:
        """Replace the file's settings with *values*, if it is still *expected*.

        Raises :class:`StaleWrite` when the file changed since that revision and
        :class:`SettingsError` when the result would not load; in both cases
        nothing is written.
        """
        with FileLock(self._lock_path):
            current = self.read()
            if current.digest != expected:
                raise StaleWrite(
                    f"{self._path} changed since it was read — "
                    "read it again and make the change again"
                )
            text = tomlkit.dumps(_document(values))
            parse(text, source=str(self._path))
            self._path.parent.mkdir(parents=True, exist_ok=True)
            replace_private_text(self._path, text)
        return Revision(text, _digest(text))

    def set(self, changes: Mapping[str, str], *, expected: str | None = None) -> Revision:
        """Add or change *changes*, leaving every other setting as it is.

        An empty value removes a setting, so one call can both set and clear.
        """
        current = self.read()
        values = dict(self.settings().values)
        for key, value in changes.items():
            if value == "":
                values.pop(key, None)
            else:
                values[key] = value
        return self.write(values, expected=current.digest if expected is None else expected)

    def unset(self, keys: Iterable[str], *, expected: str | None = None) -> Revision:
        """Remove *keys*, leaving every other setting as it is."""
        return self.set({key: "" for key in keys}, expected=expected)

    # --- versions ----------------------------------------------------------

    def version_path(self, name: str) -> Path:
        """The file the version *name* is kept in."""
        if not name or not all(c.isalnum() or c in "-_." for c in name):
            raise ValueError(f"{name!r} cannot name a version: use letters, digits, '-', '_', '.'")
        return self._versions / f"{name}.toml"

    def versions(self) -> list[str]:
        """Every kept version's name, in order."""
        if not self._versions.is_dir():
            return []
        return sorted(path.stem for path in self._versions.glob("*.toml"))

    def save_version(self, name: str) -> Path:
        """Keep what ``settings.toml`` says now under *name*, to come back to."""
        target = self.version_path(name)
        current = self.read()
        if current.digest == ABSENT:
            raise SettingsError(f"there is no {self._path} yet to keep a copy of")
        target.parent.mkdir(parents=True, exist_ok=True)
        replace_private_text(target, current.text)
        return target

    def use_version(self, name: str) -> Path:
        """Make the version *name* the live ``settings.toml``.

        What was live is kept first, as ``previous``, so this is always undoable.
        """
        source = self.version_path(name)
        try:
            text = source.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise SettingsError(
                f"there is no version called {name} (see `yuyutsava config version list`)"
            ) from None
        parse(text, source=str(source))
        with FileLock(self._lock_path):
            if self.read().digest != ABSENT:
                self.save_version("previous")
            self._path.parent.mkdir(parents=True, exist_ok=True)
            replace_private_text(self._path, text)
        return self._path

    def remove_version(self, name: str) -> bool:
        """Forget the version *name*; whether there was one to forget."""
        try:
            self.version_path(name).unlink()
        except FileNotFoundError:
            return False
        return True
