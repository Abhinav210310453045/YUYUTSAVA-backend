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
  ``secrets.toml`` is readable by its owner alone.

Callers that keep a :class:`~yuyutsava.llm.config.watch.ConfigSource` reload it
after a write; the store does not know about readers.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from tomlkit import TOMLDocument

from yuyutsava.llm.config.loader import load_text
from yuyutsava.llm.config.schema import ModelsConfig
from yuyutsava.llm.config.secrets import NAME_PATTERN, SecretRef, SecretSource
from yuyutsava.platform import FileLock
from yuyutsava.platform.files import replace_private_text, replace_text

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

    def __init__(self, models_path: Path, secrets_path: Path) -> None:
        self._models = models_path
        self._secrets = secrets_path
        self._lock_path = models_path.with_name("models.lock")

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
        with FileLock(self._lock_path):
            document = self._secrets_document()
            document[name] = value.strip()
            replace_private_text(self._secrets, tomlkit.dumps(document))
        return SecretRef(SecretSource.SECRET, name)

    def forget_secret(self, name: str) -> bool:
        """Remove *name* from ``secrets.toml``; whether it was there."""
        with FileLock(self._lock_path):
            document = self._secrets_document()
            if name not in document:
                return False
            del document[name]
            replace_private_text(self._secrets, tomlkit.dumps(document))
        return True

    def _secrets_document(self) -> TOMLDocument:
        try:
            return tomlkit.parse(self._secrets.read_text(encoding="utf-8"))
        except FileNotFoundError:
            document = tomlkit.document()
            for line in _SECRETS_HEADER.splitlines():
                document.add(tomlkit.comment(line))
            return document


__all__ = ["ABSENT", "ConfigStore", "Revision", "StaleWrite"]
