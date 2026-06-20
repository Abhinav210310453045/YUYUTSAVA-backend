"""Consent domains — the per-domain "build a subject key + match a grant" pieces.

The registry owns scope/expiry/precedence; a domain only knows how to (a) turn a
request descriptor into a normalized ``subject_key`` to store on a grant, and (b)
decide whether a stored grant covers a new request. New allowlist use cases
(events, proposals, tasks) add a domain here instead of forking the engine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from yuyutsava.consent.models import ConsentRequest, Grant


@runtime_checkable
class ConsentDomain(Protocol):
    name: str

    def subject_key(self, descriptor: dict) -> str: ...

    def matches(self, grant: Grant, request: ConsentRequest) -> bool: ...


def _abs(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path or ""))


def _within(path: str, directory: str) -> bool:
    """True when ``path`` is ``directory`` itself or lives under it."""
    path, directory = _abs(path), _abs(directory)
    if not directory:
        return False
    if path == directory:
        return True
    return path.startswith(directory.rstrip(os.sep) + os.sep)


@dataclass(frozen=True)
class ToolPermissionDomain:
    """Filesystem tool permissions, keyed by ``operation | zone | directory``.

    A grant approves an *operation* in a *zone* for everything under a
    *directory* (the common parent of the originally-approved paths). This is
    what collapses the "approve LIST for every file in this folder" storm into a
    single session/project grant, while keeping a different operation
    (e.g. DELETE) or a different folder gated.
    """

    name: str = "tool_permission"

    # Operations whose path argument is itself a directory (not a file).
    _DIR_OPS = frozenset({"list", "glob"})

    def directory_of(self, paths: list[str], operation: str | None = None) -> str:
        """Resolve the directory a grant should cover.

        For directory operations (LIST/GLOB) the path *is* the directory; for
        file operations we take its parent. We key off the operation rather than
        ``os.path.isdir`` so external / not-yet-existing paths resolve correctly.
        """
        is_dir_op = (operation or "").lower() in self._DIR_OPS
        dirs: list[str] = []
        for p in paths or []:
            ap = _abs(p)
            dirs.append(ap if is_dir_op else os.path.dirname(ap))
        if not dirs:
            return ""
        if len(dirs) == 1:
            return dirs[0]
        try:
            return os.path.commonpath(dirs)
        except ValueError:  # paths on different drives — no common root
            return ""

    def subject_key(self, descriptor: dict) -> str:
        op = str(descriptor.get("operation") or "").lower()
        zone = str(descriptor.get("zone") or "").lower()
        directory = descriptor.get("directory") or self.directory_of(
            list(descriptor.get("paths") or []), op
        )
        return f"{op}|{zone}|{directory}"

    def matches(self, grant: Grant, request: ConsentRequest) -> bool:
        try:
            g_op, g_zone, g_dir = grant.subject_key.split("|", 2)
        except ValueError:
            return False
        d = request.descriptor
        if str(d.get("operation") or "").lower() != g_op:
            return False
        if str(d.get("zone") or "").lower() != g_zone:
            return False
        if not g_dir:
            return False
        paths = list(d.get("paths") or [])
        if not paths:
            return False
        return all(_within(p, g_dir) for p in paths)
