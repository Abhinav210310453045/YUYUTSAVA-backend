"""Writing ``models.toml`` and ``secrets.toml``: the one writer every surface goes through.

The setup wizards, the app and agent tools all change the files here, and people
edit ``models.toml`` by hand. So every write:

* holds one lock beside the files, so two programs never interleave;
* names the :class:`Revision` it was based on, and is refused with
  :class:`StaleWrite` if the file has changed since — a change made elsewhere is
  never silently overwritten;
* is validated before it is written, so ``models.toml`` on disk is never left
  invalid by a program (:class:`~yuyutsava.llm.config.loader.ModelsConfigError`,
  nothing written);
* keeps the comments and layout a person wrote (``tomlkit``);
* swaps the new file in whole, in one step (:mod:`yuyutsava.platform.files`), and
  ``secrets.toml`` is readable by its owner alone;
* with ``[security] require_user_presence``, has the OS confirm the owner is
  here before a key is stored or removed, or that check is turned off
  (:mod:`yuyutsava.platform.user_presence`) — and refuses to turn it on where
  nothing can ask.

Callers that keep a :class:`~yuyutsava.llm.config.watch.ConfigSource` reload it
after a write; the store does not know about readers.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from tomlkit import TOMLDocument

from yuyutsava.llm.config.edits import ConfigEditError
from yuyutsava.llm.config.loader import load_text
from yuyutsava.llm.config.schema import ModelsConfig
from yuyutsava.llm.config.secrets import NAME_PATTERN, SecretRef, SecretSource
from yuyutsava.platform import FileLock
from yuyutsava.platform.files import replace_private_text, replace_text
from yuyutsava.platform.user_presence import PresenceGate, presence_gate

#: The digest of a file that does not exist yet.
ABSENT = "absent"

_SECRET_NAME = re.compile(NAME_PATTERN)
_SECRETS_HEADER = (
    "Secrets that models.toml refers to as secret:<name>. Written by YUYUTSAVA;\n"
    "readable only by you. Change them with `yuyutsava models secrets set <name>`."
)


@dataclass(frozen=True)
class Revision:
    """``models.toml`` as read: its text and the digest a write must name."""

    text: str
    #: SHA-256 of :attr:`text`; :data:`ABSENT` when there is no file.
    digest: str


class StaleWrite(RuntimeError):
    """``models.toml`` changed between being read and being written."""


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ConfigStore:
    """Reads and writes one ``models.toml`` and its ``secrets.toml``."""

    def __init__(
        self,
        models_path: Path,
        secrets_path: Path,
        *,
        presence: Callable[[], PresenceGate] | None = None,
    ) -> None:
        self._models = models_path
        self._secrets = secrets_path
        self._lock_path = models_path.with_name("models.lock")
        #: This process's gate unless one is given; called only when the check is on.
        self._presence = presence or presence_gate

    @property
    def models_path(self) -> Path:
        """The ``models.toml`` this store writes."""
        return self._models

    @property
    def secrets_path(self) -> Path:
        """The ``secrets.toml`` beside it."""
        return self._secrets

    def read(self) -> Revision:
        """``models.toml`` as it is now."""
        try:
            text = self._models.read_text(encoding="utf-8")
        except FileNotFoundError:
            return Revision("", ABSENT)
        return Revision(text, _digest(text))

    def apply(self, edit: Callable[[TOMLDocument], None], *, expected: str) -> Revision:
        """Change ``models.toml`` with *edit*, if it is still the revision *expected* names.

        *edit* changes the parsed document in place; anything it leaves alone,
        comments included, is written back exactly as it was. Raises
        :class:`StaleWrite` when the file changed since that revision, and
        ``ModelsConfigError`` when the result would not load — in both cases
        nothing is written.
        """
        self._guard_the_presence_check(edit)
        with FileLock(self._lock_path):
            current = self.read()
            if current.digest != expected:
                raise StaleWrite(
                    f"{self._models} changed since it was read — reload it and make the change again"
                )
            document = tomlkit.parse(current.text)
            edit(document)
            text = tomlkit.dumps(document)
            load_text(text, source=str(self._models))
            replace_text(self._models, text)
        return Revision(text, _digest(text))

    def preview(self, edit: Callable[[TOMLDocument], None]) -> ModelsConfig:
        """``models.toml`` as *edit* would leave it, validated, without writing anything.

        For a caller that must check a change before making it — the setup wizard
        logs in to a provider before the file names it. Raises
        ``ModelsConfigError`` as :meth:`apply` would.
        """
        document = tomlkit.parse(self.read().text)
        edit(document)
        return load_text(tomlkit.dumps(document), source=str(self._models))

    def store_secret(self, name: str, value: str) -> SecretRef:
        """Keep *value* under *name* in ``secrets.toml``; the reference to write is returned."""
        if not _SECRET_NAME.match(name):
            raise ValueError(
                f"{name!r} cannot name a secret: use letters, digits, '_', '-' and '.'"
            )
        if not value.strip():
            raise ValueError(f"the value for secret {name!r} is empty")
        self._confirm_presence(f"change the key secret:{name}")
        with FileLock(self._lock_path):
            document = self._secrets_document()
            document[name] = value.strip()
            replace_private_text(self._secrets, tomlkit.dumps(document))
        return SecretRef(SecretSource.SECRET, name)

    def forget_secret(self, name: str) -> bool:
        """Remove *name* from ``secrets.toml``; whether it was there."""
        self._confirm_presence(f"remove the key secret:{name}")
        with FileLock(self._lock_path):
            document = self._secrets_document()
            if name not in document:
                return False
            del document[name]
            replace_private_text(self._secrets, tomlkit.dumps(document))
        return True

    def presence_required(self) -> bool:
        """Whether ``models.toml`` asks for the owner's presence before a key changes."""
        return _requires_presence(self.read().text)

    def presence_mechanism(self) -> str | None:
        """What would ask on this machine — "Touch ID or your password" — or ``None``."""
        return self._presence().mechanism

    def _confirm_presence(self, reason: str) -> None:
        """Raises ``PresenceNotConfirmed`` when the check is on and not passed."""
        if self.presence_required():
            self._presence().confirm(reason)

    def _guard_the_presence_check(self, edit: Callable[[TOMLDocument], None]) -> None:
        """Turning the check off needs it passed; turning it on needs something that can ask."""
        current = self.read().text
        document = tomlkit.parse(current)
        edit(document)
        before, after = _requires_presence(current), _requires_presence(tomlkit.dumps(document))
        if before and not after:
            self._presence().confirm("turn off its check before keys are changed")
        elif after and not before and self.presence_mechanism() is None:
            raise ConfigEditError(
                "this computer has no way to confirm it is you "
                "(no Touch ID or password dialog, Windows Hello or polkit agent)",
                "leave require_user_presence off here",
            )

    def _secrets_document(self) -> TOMLDocument:
        try:
            return tomlkit.parse(self._secrets.read_text(encoding="utf-8"))
        except FileNotFoundError:
            document = tomlkit.document()
            for line in _SECRETS_HEADER.splitlines():
                document.add(tomlkit.comment(line))
            return document


def _requires_presence(text: str) -> bool:
    """``[security] require_user_presence`` in *text*; off when the file is absent or unreadable.

    Read from the TOML itself, not the validated config, so a mistake elsewhere
    in the file does not switch the check off.
    """
    try:
        security = tomllib.loads(text).get("security", {})
    except tomllib.TOMLDecodeError:
        return False
    return isinstance(security, dict) and security.get("require_user_presence") is True


__all__ = ["ABSENT", "ConfigStore", "Revision", "StaleWrite"]
