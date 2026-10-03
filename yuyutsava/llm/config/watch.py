"""The current models config, kept up to date with the files, for readers that must not wait.

Model selection happens on the event loop, on every turn, where file I/O is not
allowed (blockbuster) and would be slow anyway. So the reading is done
elsewhere: :meth:`ConfigSource.reload` reads ``models.toml`` and
``secrets.toml``, validates, resolves every secret reference and publishes the
result as an immutable :class:`ConfigSnapshot`; :meth:`ConfigSource.current`
hands it out without touching the disk. :class:`ConfigWatcher` calls
``reload`` on its own thread whenever either file changes, so an edit — by hand,
by the wizard, by the app — is in use from the next turn, with no restart.

An edit that does not load never replaces a working config: the last good
snapshot stays in use, and :attr:`ConfigState.problem` says what is wrong with
the file on disk, for the CLI, the app and ``yuyutsava models doctor`` to show.
Removing ``models.toml`` is different: it leaves nothing configured, which is
what sends the CLI and the app back to the setup wizard.

A reference whose value cannot be found does not make the file invalid — the
rest of the config still works. It is listed in
:attr:`ConfigSnapshot.unavailable`, and only the provider that needs it is out.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from yuyutsava.llm.config.loader import ConfigIssue, ModelsConfigError, load_text
from yuyutsava.llm.config.schema import ModelsConfig, secret_references
from yuyutsava.llm.config.secrets import (
    Secret,
    SecretResolver,
    SecretUnavailable,
    parse_secret_ref,
    parse_secrets,
)

logger = logging.getLogger(__name__)

#: How long the watcher waits for a burst of file events to settle before
#: reloading once. Editors write a file in several steps.
SETTLE_SECONDS = 0.2


@dataclass(frozen=True)
class ConfigSnapshot:
    """One valid config, with its secrets resolved. Never changes once published."""

    config: ModelsConfig
    #: Resolved secrets, keyed by the reference as written (``secret:groq``).
    secrets: Mapping[str, Secret]
    #: References whose value could not be found, keyed the same way, with why.
    unavailable: Mapping[str, ConfigIssue]
    #: Counts up by one with each new snapshot.
    generation: int
    #: Identifies the file contents this snapshot was made from.
    digest: str


@dataclass(frozen=True)
class ConfigState:
    """What a reader is told: the config in use, and anything wrong with the file on disk."""

    #: The config in use; ``None`` until a valid file has been read.
    snapshot: ConfigSnapshot | None = None
    #: Why the file on disk is not the one in use, when it is not.
    problem: ModelsConfigError | None = None

    @property
    def configured(self) -> bool:
        """Whether there is a config to use. ``False`` means "run the setup wizard"."""
        return self.snapshot is not None


Listener = Callable[[ConfigState], None]


class ConfigSource:
    """The models config in use; :meth:`current` is safe to call from anywhere."""

    def __init__(self, models_path: Path, secrets_path: Path) -> None:
        self._models = models_path
        self._secrets = secrets_path
        self._state = ConfigState()
        self._reload_lock = threading.Lock()
        self._listeners: list[Listener] = []

    @property
    def watched(self) -> tuple[Path, Path]:
        """The files whose changes this source follows."""
        return self._models, self._secrets

    def current(self) -> ConfigState:
        """The state as of the last reload. No I/O."""
        return self._state

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Call *listener* with each new state; returns the function that stops it."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def reload(self) -> ConfigState:
        """Read the files and publish what they say. Does I/O: never call it on the event loop."""
        with self._reload_lock:
            new = self._read()
            changed = new != self._state
            self._state = new
        if changed:
            self._announce(new)
        return new

    def _read(self) -> ConfigState:
        previous = self._state.snapshot
        try:
            text = self._models.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ConfigState()
        try:
            secrets_text = self._secrets.read_text(encoding="utf-8")
        except FileNotFoundError:
            secrets_text = ""
        digest = hashlib.sha256(f"{text}\0{secrets_text}".encode()).hexdigest()
        if previous is not None and previous.digest == digest:
            return ConfigState(previous)
        try:
            config = load_text(text, source=str(self._models))
            secrets = self._secrets_from(secrets_text)
        except ModelsConfigError as exc:
            logger.warning("models config not applied; the previous one stays in use:\n%s", exc)
            return ConfigState(previous, exc)
        generation = previous.generation + 1 if previous else 1
        return ConfigState(_snapshot(config, secrets, generation, digest))

    def _secrets_from(self, text: str) -> dict[str, str]:
        try:
            return parse_secrets(text)
        except tomllib.TOMLDecodeError as exc:
            issue = ConfigIssue(
                "",
                f"this is not valid TOML: {exc}",
                "fix the file, or store each secret again with `yuyutsava models secrets set`",
            )
            raise ModelsConfigError(str(self._secrets), [issue]) from exc

    def _announce(self, state: ConfigState) -> None:
        for listener in list(self._listeners):
            try:
                listener(state)
            except Exception:  # noqa: BLE001 — a listener's failure must not stop the others or the reload
                logger.exception("a models config listener failed")


def _snapshot(
    config: ModelsConfig, secrets: Mapping[str, str], generation: int, digest: str
) -> ConfigSnapshot:
    resolver = SecretResolver(secrets)
    found: dict[str, Secret] = {}
    missing: dict[str, ConfigIssue] = {}
    for path, reference in secret_references(config):
        try:
            found[reference] = resolver.resolve(parse_secret_ref(reference))
        except SecretUnavailable as exc:
            missing[reference] = ConfigIssue(path, exc.problem, exc.fix)
    return ConfigSnapshot(
        config=config,
        secrets=MappingProxyType(found),
        unavailable=MappingProxyType(missing),
        generation=generation,
        digest=digest,
    )


class _ReloadOnChange(FileSystemEventHandler):
    """Runs on watchdog's thread: waits for a burst to settle, then reloads once."""

    def __init__(self, source: ConfigSource) -> None:
        self._source = source
        self._names = {path.name for path in source.watched}
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def on_any_event(self, event: FileSystemEvent) -> None:
        touched = {
            Path(str(event.src_path)).name,
            Path(str(getattr(event, "dest_path", "") or "")).name,
        }
        if not touched & self._names:
            return
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(SETTLE_SECONDS, self._reload)
            self._timer.daemon = True
            self._timer.start()

    def _reload(self) -> None:
        try:
            self._source.reload()
        except Exception:  # noqa: BLE001 — the watcher thread must outlive one failed read
            logger.exception("reloading the models config failed")

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()


class ConfigWatcher:
    """Reloads a :class:`ConfigSource` whenever its files change on disk."""

    def __init__(self, source: ConfigSource) -> None:
        self._source = source
        self._handler = _ReloadOnChange(source)
        self._observer: Observer | None = None  # type: ignore[valid-type]

    def start(self) -> None:
        """Load the files now, then follow them."""
        directory = self._source.watched[0].parent
        directory.mkdir(parents=True, exist_ok=True)
        self._source.reload()
        observer = Observer()
        observer.schedule(self._handler, str(directory), recursive=False)
        observer.daemon = True
        observer.start()
        self._observer = observer

    def stop(self) -> None:
        """Stop following the files."""
        self._handler.cancel()
        observer, self._observer = self._observer, None
        if observer is not None:
            observer.stop()
            observer.join(timeout=2.0)


__all__ = ["ConfigSnapshot", "ConfigSource", "ConfigState", "ConfigWatcher", "SETTLE_SECONDS"]
