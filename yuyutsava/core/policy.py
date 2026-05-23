"""Tier-1.5 permission policy loaded from ``~/.yuyutsava/permissions.json``.

The MVP shows a Tier-2 prompt every time a subagent's ``tr_*`` tool hits a
PROMPT zone. This file lets the user pre-categorise tools so trusted
operations skip the prompt.

Lives in :mod:`yuyutsava.core` (not ``daemon/``) because the same policy
model is consumable by the CLI permission middleware as well as the
daemon. No daemon-runtime imports.

Supported policies (Phase 2, step 1)
------------------------------------
``auto_approve``
    Skip the prompt entirely. Used to flip PROMPT → ALLOW for a class of
    tools (e.g. ``tr_read_*`` always auto-approves out-of-workspace reads).

``propose`` (default)
    Current MVP behaviour — show the proposal/prompt to the user.

``daily_cap`` (for ``ws_*`` and other quota-bound tools)
    A per-day call counter persisted in the state.db. When the cap is hit
    the tool returns a refusal string instead of running. The counter
    resets at the next UTC midnight (cheap and predictable; no per-user
    timezone math).

Future policies — ``queue_for_user`` and ``refuse_when_no_ui`` — are
recognised but treated as ``propose`` until the §3.4 notification work
lands.

Schema
------
``~/.yuyutsava/permissions.json``::

    {
      "tool_categories": {
        "tr_read_*":         { "policy": "auto_approve" },
        "tr_write_*":        { "policy": "propose" },
        "ws_*":              { "policy": "auto_approve", "daily_cap": 50 }
      }
    }

Glob patterns use :mod:`fnmatch` semantics. The first matching entry wins;
entries are tried in insertion order so users can list specific rules
before broad ones.
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from yuyutsava.storage.paths import state_dir

logger = logging.getLogger("yuyutsava.core.policy")


# Policies we understand. Anything else is treated as "propose" and a
# warning is logged at load time.
_KNOWN_POLICIES: frozenset[str] = frozenset({
    "auto_approve", "propose", "queue_for_user", "refuse_when_no_ui",
})


@dataclass(frozen=True)
class PolicyEntry:
    """One ``tool_categories`` entry."""

    pattern: str
    policy: str  # one of _KNOWN_POLICIES (unknown values normalised to "propose")
    daily_cap: int | None = None  # None = unlimited


@dataclass(frozen=True)
class PermissionsPolicy:
    """Parsed ``permissions.json``. Empty = current MVP behaviour everywhere."""

    entries: list[PolicyEntry] = field(default_factory=list)

    @classmethod
    def empty(cls) -> PermissionsPolicy:
        return cls(entries=[])

    @classmethod
    def from_file(cls, path: Path | None = None) -> PermissionsPolicy:
        if path is None:
            path = state_dir() / "permissions.json"
        if not path.exists():
            logger.debug("no permissions.json at %s — defaulting to propose for all", path)
            return cls.empty()
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc

        cats = raw.get("tool_categories", {}) or {}
        entries: list[PolicyEntry] = []
        if not isinstance(cats, dict):
            logger.warning("permissions.json: tool_categories is not a dict; ignoring")
            return cls.empty()

        for pattern, body in cats.items():
            if not isinstance(body, dict):
                logger.warning("permissions.json: %r is not a dict; skipping", pattern)
                continue
            policy = str(body.get("policy", "propose")).lower()
            if policy not in _KNOWN_POLICIES:
                logger.warning(
                    "permissions.json: unknown policy %r for %r; treating as 'propose'",
                    policy, pattern,
                )
                policy = "propose"
            cap_raw = body.get("daily_cap")
            try:
                daily_cap = int(cap_raw) if cap_raw is not None else None
            except (TypeError, ValueError):
                logger.warning("permissions.json: bad daily_cap %r for %r", cap_raw, pattern)
                daily_cap = None
            entries.append(PolicyEntry(pattern=str(pattern), policy=policy, daily_cap=daily_cap))

        return cls(entries=entries)

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def match(self, tool_name: str) -> PolicyEntry | None:
        """Return the first matching entry for *tool_name*, or ``None``."""
        for entry in self.entries:
            if fnmatch.fnmatchcase(tool_name, entry.pattern):
                return entry
        return None

    def policy_for(self, tool_name: str) -> str:
        """Return the effective policy string (default ``propose``)."""
        entry = self.match(tool_name)
        return entry.policy if entry else "propose"

    def daily_cap_for(self, tool_name: str) -> int | None:
        """Return the configured daily cap for *tool_name*, or ``None``."""
        entry = self.match(tool_name)
        return entry.daily_cap if entry else None


def today_utc() -> str:
    """The UTC date key used by the daily counter (``YYYY-MM-DD``)."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Cap enforcer — used by the search-tool wrappers
# ---------------------------------------------------------------------------


class StorePolicyCapEnforcer:
    """:class:`_CapEnforcer` impl backed by the policy file + Store counters.

    Per call, look up the daily_cap for the tool. If unset, allow (no
    counting). If set, increment the per-day counter and refuse when the new
    count exceeds the cap. ``today_utc()`` defines the day boundary.
    """

    def __init__(self, policy: PermissionsPolicy, store: object) -> None:
        self._policy = policy
        self._store = store  # yuyutsava.storage.events.Store, kept untyped to avoid cycle

    def check_and_incr(self, tool_name: str) -> tuple[bool, str]:
        cap = self._policy.daily_cap_for(tool_name)
        if cap is None:
            return True, ""
        day = today_utc()
        try:
            new_count = self._store.incr_tool_call(tool_name, day)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — counter failure should not block work
            logger.warning("policy: counter incr failed for %s: %s — allowing", tool_name, exc)
            return True, ""
        if new_count > cap:
            return False, f"daily cap {cap} reached for {tool_name} on {day} (now {new_count})"
        return True, ""
