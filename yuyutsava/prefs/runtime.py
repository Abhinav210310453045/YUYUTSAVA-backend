"""Runtime toggles every surface must agree on (voice mode, subagent roster).

These are *hot* switches — flipped from the Electron titlebar, the Settings
panel, the voice overlay, or a CLI slash command — that take effect on the
running daemon without a restart. They live in the same ``user_prefs`` table as
the rest of the preferences (:class:`~yuyutsava.storage.prefs.PrefsStore`,
``state.db``) so:

  * the value survives a restart,
  * the CLI and the daemon read the *same* row (``yuyutsava prefs get
    runtime.voice`` shows exactly what the app shows), and
  * no new storage backend is introduced for two booleans.

Two keys, both stored as small JSON objects:

``runtime.voice``
    ``{"wake_enabled": bool, "tts_enabled": bool}`` — wake-word auto-detection
    and spoken replies. Both default ON. The manual mic button is deliberately
    NOT gated by either flag: with voice mode off the user can still tap to
    talk, they just aren't listened for automatically and aren't answered aloud.

``runtime.subagents``
    ``{"disabled": ["face-watcher", ...]}`` — a DENY-list of dedicated
    subagents, so an agent added later is enabled by default and needs no
    migration. ``general-purpose`` can never be disabled (see
    :data:`UNDISABLEABLE`): it is deepagents' delegation fallback, and removing
    it breaks ``task(...)`` itself rather than merely narrowing the roster.

Reader contract
---------------
Middleware and the ``/ws/converse`` turn loop need these values *synchronously*
(inside ``wrap_tool_call`` / mid-frame, where there is no place to await a DB
read). :class:`RuntimeSettings` therefore keeps an in-memory snapshot and hands
it out through the plain-``def`` accessors :meth:`voice` and :meth:`subagents`.
:meth:`load` primes it at boot, every write refreshes it, and :meth:`refresh`
re-reads the row when the snapshot is older than :data:`_TTL_SEC` — that last
part is what lets a ``/voice off`` typed into a CLI REPL reach a daemon that
never saw the write.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yuyutsava.storage.prefs import PrefsStore

logger = logging.getLogger("yuyutsava.prefs.runtime")

VOICE_KEY = "runtime.voice"
SUBAGENTS_KEY = "runtime.subagents"

# Subagents that are structural, not optional. ``general-purpose`` backs
# deepagents' built-in delegation default — disabling it would break `task(...)`
# for every caller rather than just narrowing the roster.
UNDISABLEABLE: frozenset[str] = frozenset({"general-purpose"})

# How stale the in-memory snapshot may get before a read re-hits the DB. Short
# enough that a CLI-side write lands quickly, long enough that a per-tool-call
# read is effectively free.
_TTL_SEC = 5.0


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


@dataclass(frozen=True)
class VoiceSettings:
    """Voice-mode switches. Both default ON (historical behaviour)."""

    wake_enabled: bool = True
    tts_enabled: bool = True

    @property
    def enabled(self) -> bool:
        """True when voice mode is on in any capacity (either switch)."""
        return self.wake_enabled or self.tts_enabled

    @classmethod
    def from_stored(cls, raw: Any) -> "VoiceSettings":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            wake_enabled=_as_bool(raw.get("wake_enabled"), True),
            tts_enabled=_as_bool(raw.get("tts_enabled"), True),
        )

    def to_dict(self) -> dict[str, bool]:
        return {"wake_enabled": self.wake_enabled, "tts_enabled": self.tts_enabled}


@dataclass(frozen=True)
class SubagentSettings:
    """Deny-list of dedicated subagents the master must not delegate to."""

    disabled: frozenset[str] = frozenset()

    @classmethod
    def from_stored(cls, raw: Any) -> "SubagentSettings":
        if not isinstance(raw, dict):
            return cls()
        names = raw.get("disabled")
        if not isinstance(names, (list, tuple, set, frozenset)):
            return cls()
        return cls(disabled=cls._clean(names))

    @staticmethod
    def _clean(names) -> frozenset[str]:
        return frozenset(
            n.strip() for n in names
            if isinstance(n, str) and n.strip() and n.strip() not in UNDISABLEABLE
        )

    def is_enabled(self, name: str) -> bool:
        return name not in self.disabled

    def to_dict(self) -> dict[str, list[str]]:
        return {"disabled": sorted(self.disabled)}


class RuntimeSettings:
    """Cached read/write access to the runtime toggles.

    Construct with the :class:`PrefsStore` the daemon (or the CLI) already
    built — the ``state.db`` connection is shared. Call :meth:`load` once at
    startup so the first synchronous read is warm.
    """

    def __init__(self, prefs: "PrefsStore") -> None:
        self._prefs = prefs
        self._voice = VoiceSettings()
        self._subagents = SubagentSettings()
        # 0.0 = never loaded, so the first refresh() always hits the store.
        self._loaded_at = 0.0

    # ------------------------------------------------------------------ #
    # Synchronous accessors (snapshot)                                     #
    # ------------------------------------------------------------------ #

    def voice(self) -> VoiceSettings:
        """Current voice switches from the in-memory snapshot."""
        return self._voice

    def subagents(self) -> SubagentSettings:
        """Current subagent deny-list from the in-memory snapshot."""
        return self._subagents

    def snapshot(self) -> dict[str, Any]:
        """Both groups as a wire-ready dict (what ``GET /settings/runtime`` returns)."""
        return {"voice": self._voice.to_dict(), "subagents": self._subagents.to_dict()}

    # ------------------------------------------------------------------ #
    # Async load / refresh                                                 #
    # ------------------------------------------------------------------ #

    async def load(self) -> "RuntimeSettings":
        """Prime the snapshot from the store. Never raises — defaults on failure."""
        try:
            voice_raw = await self._prefs.get(VOICE_KEY)
            subs_raw = await self._prefs.get(SUBAGENTS_KEY)
        except Exception:  # noqa: BLE001 — a settings read never blocks boot
            logger.warning("runtime settings: load failed; using defaults", exc_info=True)
            self._loaded_at = time.monotonic()
            return self
        self._voice = VoiceSettings.from_stored(voice_raw)
        self._subagents = SubagentSettings.from_stored(subs_raw)
        self._loaded_at = time.monotonic()
        return self

    async def refresh(self, *, force: bool = False) -> "RuntimeSettings":
        """Re-read the row when the snapshot is stale (or ``force``).

        This is what picks up a write made by *another* process — a CLI REPL's
        ``/voice off`` against a daemon that never saw the PATCH.
        """
        if not force and (time.monotonic() - self._loaded_at) < _TTL_SEC:
            return self
        return await self.load()

    # ------------------------------------------------------------------ #
    # Writes                                                               #
    # ------------------------------------------------------------------ #

    async def set_voice(
        self, *, wake_enabled: bool | None = None, tts_enabled: bool | None = None,
    ) -> VoiceSettings:
        """Patch the voice switches (omitted fields keep their value)."""
        await self.refresh()
        updated = self._voice
        if wake_enabled is not None:
            updated = replace(updated, wake_enabled=bool(wake_enabled))
        if tts_enabled is not None:
            updated = replace(updated, tts_enabled=bool(tts_enabled))
        await self._prefs.set(VOICE_KEY, updated.to_dict())
        self._voice = updated
        self._loaded_at = time.monotonic()
        logger.info(
            "runtime settings: voice wake=%s tts=%s",
            updated.wake_enabled, updated.tts_enabled,
        )
        return updated

    async def set_subagent_enabled(self, name: str, enabled: bool) -> SubagentSettings:
        """Enable/disable one dedicated subagent by name.

        Names in :data:`UNDISABLEABLE` are silently kept enabled — the caller
        (API/CLI) validates and reports; this layer just never stores them.
        """
        await self.refresh()
        name = (name or "").strip()
        if not name:
            return self._subagents
        disabled = set(self._subagents.disabled)
        if enabled or name in UNDISABLEABLE:
            disabled.discard(name)
        else:
            disabled.add(name)
        return await self.set_disabled_subagents(disabled)

    async def set_disabled_subagents(self, names) -> SubagentSettings:
        """Replace the whole deny-list."""
        updated = SubagentSettings(disabled=SubagentSettings._clean(names))
        await self._prefs.set(SUBAGENTS_KEY, updated.to_dict())
        self._subagents = updated
        self._loaded_at = time.monotonic()
        logger.info(
            "runtime settings: subagents disabled=%s",
            ", ".join(sorted(updated.disabled)) or "(none)",
        )
        return updated
