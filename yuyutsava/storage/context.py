"""``AppContext`` — explicit store handles, instead of process globals.

Phase 3 step 3.4 (ADR-003), addressing finding ``F-S08``.

Five stores plus two policy objects are installed into module-level globals at
boot and fetched later through ``get_default_*()``. That is a service locator,
and it costs three things:

* **Hidden dependencies.** ``purge_session(session_id)`` has a one-argument
  signature and touches four stores. Nothing at the call site says so.
* **Manual test isolation.** Every test touching these paths must set and
  restore globals; forgetting leaks state between tests in the same process.
* **One instance per process.** Two agents cannot use different todo stores.
  Not needed today — but it is foreclosed structurally, not by choice.

The convenience is real, though: ``purge_session`` genuinely is nicer to call
with no wiring, and that is why the globals exist. So this is **additive** and
deliberately not a big-bang removal — there are 91 ``get_default_*`` call sites,
and rewriting them all at once would be a large change with no way to verify it
incrementally.

Instead: functions that want honesty accept an optional ``AppContext``. Pass one
and the dependency is explicit and test-isolated; omit it and the global
fallback keeps working exactly as before. Call sites migrate one at a time,
each independently verifiable.

``purge_session`` is the first, because it is the worst offender and the one the
review names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AppContext:
    """The store handles a caller needs, passed rather than looked up.

    Every field is optional: a caller supplies only what the function it is
    calling actually uses, and anything omitted falls back to the process
    global. That keeps partial migration possible — the alternative is
    constructing a fully-populated context at every call site on day one.
    """

    session_store: Any | None = None
    visual_store: Any | None = None
    feedback_store: Any | None = None
    todo_store: Any | None = None
    events_store: Any | None = None
    note_index: Any | None = None

    # -- resolution ---------------------------------------------------------
    # Each resolver returns the explicit handle when present, else the global.
    # Written out per store rather than via getattr so the fallback for each is
    # greppable, and so removing a global later is a visible, local change.

    def sessions(self) -> Any:
        if self.session_store is not None:
            return self.session_store
        from yuyutsava.storage.sessions import get_default_session_store

        return get_default_session_store()

    def visuals(self) -> Any:
        if self.visual_store is not None:
            return self.visual_store
        from yuyutsava.visuals.store import get_default_visual_store

        return get_default_visual_store()

    def feedback(self) -> Any:
        if self.feedback_store is not None:
            return self.feedback_store
        from yuyutsava.storage.feedback_store import get_default_feedback_store

        return get_default_feedback_store()

    def todos(self) -> Any:
        if self.todo_store is not None:
            return self.todo_store
        from yuyutsava.todoboard.store import get_default_todo_store

        return get_default_todo_store()

    # -- lifecycle-carrying resolution --------------------------------------
    # The events Store is not a global; it is *constructed*, and a constructed
    # store must be started and stopped. So its resolver returns the owner too,
    # rather than leaving the caller to guess whether it may call ``stop()`` on
    # a handle someone else is still using.

    def events(self, settings: Any, *, pg_pool: Any | None = None) -> tuple[Any, bool]:
        """Return ``(events_store, caller_owns_lifecycle)``.

        When a store is supplied, it is returned with ``False``: it belongs to
        whoever passed it (the daemon keeps one open for the whole process on
        ``app.state.store``), and stopping it would break every other user.
        When none is supplied, a fresh one is constructed and ``True`` says the
        caller must ``start()``/``stop()`` it.

        The daemon's ``DELETE /sessions/{id}`` was opening and closing a second
        events store per request while an identical one was already live.
        """
        if self.events_store is not None:
            return self.events_store, False
        from yuyutsava.storage.events import Store

        return Store.for_backend(settings, pg_pool=pg_pool), True


#: Shared "resolve everything from the globals" context. Passing this is
#: equivalent to the pre-3.4 behaviour, and makes the reliance on globals
#: *visible at the call site* rather than buried in the callee.
GLOBAL_CONTEXT = AppContext()


__all__ = ["AppContext", "GLOBAL_CONTEXT"]
