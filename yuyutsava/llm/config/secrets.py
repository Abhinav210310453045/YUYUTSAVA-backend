"""Where a secret comes from, and how its value is found.

``models.toml`` never holds a key or a token (ADR-005 §2). A setting that needs
one holds a *reference* instead:

=================  ==========================================================
``secret:<name>``  the value stored under ``<name>`` in ``secrets.toml``
``env:<NAME>``     the environment variable ``NAME``
``file:<path>``    the whole content of the file at ``<path>`` (``~`` expands)
=================  ==========================================================

So the file can be shown, shared or committed without leaking anything, and a
value can live wherever its owner already keeps it.

A resolved value is a :class:`Secret`, whose ``repr`` never shows the value, so
it cannot leak through a log line or a traceback.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from yuyutsava.config.env import env

#: What a secret's or a provider's name may look like: it is written unquoted as
#: a TOML key and inside references, so letters, digits, ``_``, ``-`` and ``.``.
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
_NAME = re.compile(NAME_PATTERN)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: A reference that is longer than this and has no prefix is almost certainly a
#: key pasted in place of a reference.
_PASTED_KEY_LENGTH = 20


class SecretSource(str, Enum):
    """Where a referenced secret is read from."""

    SECRET = "secret"
    ENV = "env"
    FILE = "file"


@dataclass(frozen=True)
class SecretRef:
    """A parsed reference: where the secret is, not what it is."""

    source: SecretSource
    name: str

    def __str__(self) -> str:
        return f"{self.source.value}:{self.name}"


class SecretRefError(ValueError):
    """A setting that must hold a reference holds something else."""

    def __init__(self, problem: str, fix: str) -> None:
        super().__init__(problem)
        self.problem = problem
        self.fix = fix


class SecretUnavailable(LookupError):
    """A reference is well formed, but its value cannot be found."""

    def __init__(self, ref: SecretRef, problem: str, fix: str) -> None:
        super().__init__(problem)
        self.ref = ref
        self.problem = problem
        self.fix = fix


class Secret:
    """A secret's value. Its ``repr`` and ``str`` never show it."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """The value itself — for the one call that hands it to an SDK."""
        return self._value

    def __repr__(self) -> str:
        return "Secret(<hidden>)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


_REF_FIX = (
    'write a reference instead: key = "secret:<name>" (stored with `yuyutsava models '
    'secrets set <name>`), key = "env:<VARIABLE>", or key = "file:<path>"'
)


def parse_secret_ref(text: str) -> SecretRef:
    """Parse *text* as a reference, or explain what is wrong with it."""
    source, sep, name = text.partition(":")
    if not sep or source not in {s.value for s in SecretSource}:
        if len(text) >= _PASTED_KEY_LENGTH and " " not in text:
            problem = "this looks like a key pasted in directly; models.toml must never hold one"
        else:
            problem = f"{text!r} is not a reference to a secret"
        raise SecretRefError(problem, _REF_FIX)
    kind = SecretSource(source)
    if kind is SecretSource.SECRET and not _NAME.match(name):
        raise SecretRefError(
            f"{text!r} names no valid secret",
            'a secret name uses letters, digits, "_", "-" and ".", e.g. "secret:openrouter"',
        )
    if kind is SecretSource.ENV and not _ENV_NAME.match(name):
        raise SecretRefError(
            f"{text!r} names no valid environment variable",
            'an environment variable name uses letters, digits and "_", e.g. "env:GROQ_API_KEY"',
        )
    if kind is SecretSource.FILE and not name.strip():
        raise SecretRefError(
            f"{text!r} names no file", 'write the path, e.g. "file:~/keys/groq.txt"'
        )
    return SecretRef(kind, name)


def parse_secrets(text: str) -> dict[str, str]:
    """The values in the text of a ``secrets.toml``.

    Raises :class:`tomllib.TOMLDecodeError` for text that is not TOML, so the
    caller can report it rather than treat every secret as missing.
    """
    return {key: value for key, value in tomllib.loads(text).items() if isinstance(value, str)}


def read_secrets_file(path: Path) -> dict[str, str]:
    """The values in *path*; empty when the file does not exist yet."""
    try:
        return parse_secrets(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


class SecretResolver:
    """Finds the value behind a reference.

    ``secrets`` is the content of ``secrets.toml``, read once by the caller, so
    resolving many references reads that file once.
    """

    def __init__(self, secrets: Mapping[str, str]) -> None:
        self._secrets = secrets

    def resolve(self, ref: SecretRef) -> Secret:
        """The value behind *ref*, or :class:`SecretUnavailable` saying how to provide it."""
        if ref.source is SecretSource.SECRET:
            value = self._secrets.get(ref.name, "")
            if not value:
                raise SecretUnavailable(
                    ref,
                    f"no secret named {ref.name!r} is stored",
                    f"store it with `yuyutsava models secrets set {ref.name}`, or in the app's "
                    "Settings → Models",
                )
            return Secret(value)
        if ref.source is SecretSource.ENV:
            value = env(ref.name)
            if not value:
                raise SecretUnavailable(
                    ref,
                    f"the environment variable {ref.name} is not set",
                    f"set {ref.name} in the environment YUYUTSAVA starts from, or store the value "
                    f"with `yuyutsava models secrets set` and reference it as secret:<name>",
                )
            return Secret(value)
        return Secret(_read_secret_file(ref))


def _read_secret_file(ref: SecretRef) -> str:
    path = Path(ref.name).expanduser()
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise SecretUnavailable(
            ref,
            f"the file {path} does not exist",
            "create it, or point the reference at the right file",
        ) from None
    except OSError as exc:
        raise SecretUnavailable(
            ref, f"the file {path} cannot be read ({exc.strerror})", "check its permissions"
        ) from exc
    if not value:
        raise SecretUnavailable(ref, f"the file {path} is empty", "put the secret in it")
    return value


__all__ = [
    "NAME_PATTERN",
    "Secret",
    "SecretRef",
    "SecretRefError",
    "SecretResolver",
    "SecretSource",
    "SecretUnavailable",
    "parse_secret_ref",
    "parse_secrets",
    "read_secrets_file",
]
