"""Where a person's settings are kept, and moving them between the two places.

YUYUTSAVA reads both, and a person chooses which one their settings live in:

* ``~/.yuyutsava/settings.toml`` — validated, written whole or not at all, and
  keepable in named versions (:mod:`yuyutsava.config.settings_file`);
* a ``.env`` file — what YUYUTSAVA has always read, and what a person may
  already have (:mod:`yuyutsava.config.env_file`).

The choice is not a flag stored somewhere that can disagree with the files: it
*is* which file holds the settings. ``settings.toml`` wins where both set the
same variable, which :meth:`Source.overlaps` reports by name so nobody wonders
why an edit did nothing — the same courtesy ``models.toml`` pays a ``.env``
through :mod:`yuyutsava.llm.config.leftovers`.

:func:`move_settings` carries values from one to the other, so switching is one
command and never a retyping job.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yuyutsava.config.env_file import home_env_path, read_env_file, write_env_values
from yuyutsava.config.settings_file import SettingsStore, settings_path

#: What a source is called, in a sentence and on a command line.
SETTINGS = "settings"
ENV = "env"


@dataclass(frozen=True)
class Source:
    """One place settings are kept, named by its file."""

    kind: str
    path: Path

    @property
    def label(self) -> str:
        """How to name it to a person: always the path, never "the file"."""
        return str(self.path)

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def values(self) -> dict[str, str]:
        """Every setting it holds."""
        if self.kind == SETTINGS:
            return dict(SettingsStore(self.path).settings().values)
        return read_env_file(self.path)

    def write(self, changes: dict[str, str]) -> list[str]:
        """Set *changes* here; the names written. An empty value removes one."""
        if self.kind == SETTINGS:
            SettingsStore(self.path).set(changes)
            return [key for key, value in changes.items() if value != ""]
        return write_env_values(self.path, changes)

    def overlaps(self, other: Source) -> list[str]:
        """The variables both set, which is where a confusing edit comes from."""
        mine, theirs = self.values(), other.values()
        return sorted(key for key in mine if key in theirs)


def settings_source(path: Path | None = None) -> Source:
    """The ``settings.toml`` source."""
    return Source(SETTINGS, path or settings_path())


def env_source(path: Path | None = None) -> Source:
    """A ``.env`` source; the app-managed one unless another is named."""
    return Source(ENV, path or home_env_path())


def active_source(*, env_path: Path | None = None) -> Source:
    """Where settings are being kept now.

    ``settings.toml`` if there is one, because it wins at start-up; otherwise
    the ``.env``. With neither, ``settings.toml`` is where a new setting goes:
    it is the one that can be checked and kept in versions.
    """
    settings = settings_source()
    if settings.exists:
        return settings
    env = env_source(env_path)
    return env if env.exists else settings


def move_settings(
    to: str, *, env_path: Path | None = None, keep: bool = False
) -> tuple[Source, list[str]]:
    """Carry every setting into the *to* source; where they went and what moved.

    Nothing is removed from where they came from unless *keep* is false, and
    nothing is removed before the new home has them: a move that fails half way
    leaves both readable rather than neither.
    """
    if to not in (SETTINGS, ENV):
        raise ValueError(f"settings are kept in {SETTINGS} or {ENV}, not {to!r}")
    target = settings_source() if to == SETTINGS else env_source(env_path)
    other = env_source(env_path) if to == SETTINGS else settings_source()
    moving = other.values()
    if not moving:
        return target, []
    target.write(dict(moving))
    if not keep:
        other.write({key: "" for key in moving})
    return target, sorted(moving)
