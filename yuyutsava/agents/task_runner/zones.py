"""
Path canonicalization and filesystem zone classification.

Pure functions only — no side effects, no I/O.
Zone priority (highest → lowest): SYSTEM_CRITICAL → SANDBOX → WORKSPACE → EXTERNAL
"""

from __future__ import annotations

import os

from yuyutsava.models.operations import FilesystemZone
from yuyutsava.platform import host_profile

# ---------------------------------------------------------------------------
# System-critical path prefixes — always denied, checked first.
# Sourced from the HostProfile so the list is correct per-OS (POSIX /etc,…;
# Windows C:\Windows, C:\Program Files, …).
# ---------------------------------------------------------------------------


def _system_critical_prefixes() -> tuple[str, ...]:
    return host_profile().system_critical_prefixes


def canonicalize(path: str) -> str:
    """Resolve a path to its canonical absolute form (resolves .., symlinks, ~)."""
    return os.path.normpath(os.path.realpath(os.path.abspath(os.path.expanduser(path))))


def _norm(path: str) -> str:
    """Case/separator-normalized path — no-op on POSIX; lowercases + '\\' on Windows."""
    return os.path.normcase(os.path.normpath(path))


def _matches_prefix(path: str, prefix: str) -> bool:
    """True if *path* equals *prefix* or is a descendant — OS-separator aware."""
    p, pre = _norm(path), _norm(prefix)
    return p == pre or p.startswith(pre + os.sep)


def _is_within(path: str, directory: str) -> bool:
    """Return True if *path* is equal to or a descendant of *directory*."""
    return _matches_prefix(path, directory)


def classify_zone(
    path: str,
    workspace_root: Path,
    sandbox_root: Path,
) -> FilesystemZone:
    """
    Determine the filesystem zone for *path*.

    The path is canonicalized before classification so path-traversal attempts
    (e.g. ``/sandbox/../../etc/passwd``) resolve to their true location.

    Priority:
      1. SYSTEM_CRITICAL  — always checked first
      2. SANDBOX          — must be within sandbox_root
      3. WORKSPACE        — must be within workspace_root
      4. EXTERNAL         — everything else
    """
    canonical = canonicalize(path)
    ws = str(workspace_root.resolve())
    sb = str(sandbox_root.resolve())

    # 1. System-critical — highest priority, always deny.
    # Check BOTH the original path and the canonical path so that:
    # - Direct access (/etc/passwd) is caught by the original path check.
    # - Symlink attacks are caught by the canonical path check.
    # - On macOS /etc → /private/etc, so realpath changes the prefix; checking
    #   the original path handles that transparently.
    prefixes = _system_critical_prefixes()
    for _p in {path, canonical}:
        for prefix in prefixes:
            if _matches_prefix(_p, prefix):
                return FilesystemZone.SYSTEM_CRITICAL
    # Also canonicalize the prefixes themselves (handles macOS /etc→/private/etc)
    for prefix in prefixes:
        if _matches_prefix(canonical, canonicalize(prefix)):
            return FilesystemZone.SYSTEM_CRITICAL

    # 2. Sandbox — within sandbox_root (a subdirectory of workspace)
    if _is_within(canonical, sb):
        return FilesystemZone.SANDBOX

    # 3. Workspace — within workspace_root but outside sandbox
    if _is_within(canonical, ws):
        return FilesystemZone.WORKSPACE

    # 4. External — everything else
    return FilesystemZone.EXTERNAL
