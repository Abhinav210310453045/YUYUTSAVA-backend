"""Whether the running install is the one the project declares and locks.

The user's daemon ran from a pipx install that resolved dependencies without
``uv.lock``: mcp 2.2.0 against a lock of 1.26.0 (every MCP server failed to
load, `R-BUG-25`), langchain-core 1.6.3 against 1.4.8, deepagents 0.6.12
against 0.6.3. The framework contracts and CI verify the locked set only, so
nothing said the daemon was running something else.

:func:`check_install` compares the installed versions of the dependencies the
code couples to (:data:`COUPLED`) with the declared ranges and, in a checkout,
with ``uv.lock``. The daemon logs the result at start-up and warns on drift.

In a checkout — including an editable install, whose recorded metadata is
whatever ``pyproject.toml`` said when it was installed — the declared ranges
are read from ``pyproject.toml`` itself; otherwise from the installed
distribution's metadata.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, requires, version
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement

#: Dependencies the code couples to closely enough that a new major must be
#: reviewed before it is installed: the agent framework (internals), the MCP SDK
#: (model field names) and the web-search clients (called directly). Matched as
#: substrings of the package name. ``scripts/verify_framework_contract.py``
#: requires each to carry an upper bound.
COUPLED = ("langchain", "langgraph", "deepagents", "mcp", "tavily-python", "exa-py")

_DIST = "yuyutsava"
#: The checkout this module sits in, when it is one.
_REPO = Path(__file__).resolve().parents[2]


def is_coupled(name: str) -> bool:
    return any(k in name for k in COUPLED)


@dataclass(frozen=True)
class InstallReport:
    """Installed versions of the coupled dependencies, and where they drift."""

    #: ``{name: installed version}`` for every coupled dependency installed.
    versions: dict[str, str] = field(default_factory=dict)
    #: ``"name 1.6.3 not in <1.5"`` — outside the declared range.
    out_of_range: tuple[str, ...] = ()
    #: ``"name 1.6.3 (lock 1.4.8)"`` — differs from ``uv.lock``.
    off_lock: tuple[str, ...] = ()

    @property
    def drifted(self) -> bool:
        return bool(self.out_of_range or self.off_lock)


def declared_requirements(repo: Path = _REPO) -> list[Requirement]:
    """Every dependency the project declares, extras included."""
    raw: list[str]
    pyproject = repo / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text()) if pyproject.is_file() else {}
    project = data.get("project", {})
    if project.get("name") == _DIST:
        raw = list(project.get("dependencies", []))
        for extra in project.get("optional-dependencies", {}).values():
            raw += extra
    else:
        raw = list(requires(_DIST) or [])
    out: list[Requirement] = []
    for line in raw:
        try:
            out.append(Requirement(line))
        except InvalidRequirement:
            continue
    return out


def locked_versions(repo: Path = _REPO) -> dict[str, str]:
    """``{name: version}`` from the checkout's ``uv.lock``; empty without one."""
    lock = repo / "uv.lock"
    if not lock.is_file():
        return {}
    data = tomllib.loads(lock.read_text())
    return {p["name"]: p["version"] for p in data.get("package", []) if "version" in p}


def check_install(repo: Path = _REPO) -> InstallReport:
    """Compare the installed coupled dependencies with the declared and locked ones."""
    locked = locked_versions(repo)
    versions: dict[str, str] = {}
    out_of_range: list[str] = []
    off_lock: list[str] = []
    for req in declared_requirements(repo):
        if not is_coupled(req.name) or req.name in versions:
            continue
        try:
            installed = version(req.name)
        except PackageNotFoundError:
            continue
        versions[req.name] = installed
        if req.specifier and not req.specifier.contains(installed, prereleases=True):
            out_of_range.append(f"{req.name} {installed} not in {req.specifier}")
        pinned = locked.get(req.name)
        if pinned is not None and pinned != installed:
            off_lock.append(f"{req.name} {installed} (lock {pinned})")
    return InstallReport(
        versions=dict(sorted(versions.items())),
        out_of_range=tuple(out_of_range),
        off_lock=tuple(off_lock),
    )
