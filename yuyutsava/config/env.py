"""Reading settings from environment variables.

Every read strips surrounding whitespace, and a variable that is unset or blank counts
as unset. ``role`` selects a role-specific override: ``env("MODEL", "orchestrator")``
reads ``ORCHESTRATOR_MODEL`` first and falls back to ``MODEL``.

A number that does not parse falls back to its default instead of raising, so a typo
in the environment cannot stop a process from starting. A flag spells out the values
it accepts, because the flags in this codebase do not all accept the same ones.
"""

from __future__ import annotations

import os
from collections.abc import Collection


def _raw(name: str, role: str | None) -> str:
    """The stripped value, role override first; ``""`` when unset or blank."""
    if role:
        value = os.environ.get(f"{role.upper()}_{name}", "").strip()
        if value:
            return value
    return os.environ.get(name, "").strip()


def env(name: str, role: str | None = None, default: str = "") -> str:
    """A string setting; *default* when unset or blank."""
    return _raw(name, role) or default


def env_int(name: str, role: str | None = None, default: int = 0) -> int:
    """An integer setting; *default* when unset, blank or not an integer."""
    raw = _raw(name, role)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def env_float(name: str, role: str | None = None, default: float = 0.0) -> float:
    """A float setting; *default* when unset, blank or not a number."""
    raw = _raw(name, role)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def env_flag(
    name: str,
    role: str | None = None,
    default: bool = False,
    *,
    true: Collection[str] | None = None,
    false: Collection[str] | None = None,
) -> bool:
    """A boolean setting; *default* when unset or blank.

    Pass exactly one vocabulary of lowercase values, compared case-insensitively:

    - ``true=`` — the flag is on only for one of these values;
    - ``false=`` — the flag is on unless it is one of these values.
    """
    if (true is None) == (false is None):
        raise ValueError("env_flag needs exactly one of true= or false=")
    raw = _raw(name, role).lower()
    if not raw:
        return default
    if true is not None:
        return raw in true
    return raw not in false
