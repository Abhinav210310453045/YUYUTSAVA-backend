"""``purge_session`` can be called with its dependencies passed in.

Phase 3 step 3.4 (ADR-003), addressing finding ``F-S08``.

``purge_session(session_id)`` had a one-argument signature and touched **four**
stores through process globals. Two costs followed:

* the signature hid its blast radius — a reader had no way to know;
* a test had to set and restore four globals, and forgetting leaked state into
  the next test in the same process.

``AppContext`` makes the dependencies passable. This is deliberately **additive**:
there are 91 ``get_default_*`` call sites across the codebase, so a big-bang
removal could not be verified incrementally. Omit ``ctx`` and the globals are
used exactly as before; pass one and the dependency is explicit and isolated.

What these tests establish is the property that matters: **the globals are no
longer load-bearing for this function.** ``test_resolution_prefers_explicit_over_global``
proves it by installing a global that would fail the assertion if it were
consulted.

Run:  .venv/bin/python test/storage/test_app_context.py
"""

from __future__ import annotations

import os
import tempfile
import unittest

from yuyutsava.storage.context import GLOBAL_CONTEXT, AppContext


class _Recorder:
    """Records the calls made to it, so a test can assert on the seam."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.deleted_threads: list[str] = []

    async def delete_for_thread(self, thread_id: str) -> int:
        self.deleted_threads.append(thread_id)
        return 1


class AppContextResolution(unittest.TestCase):
    def test_explicit_handles_are_returned_as_given(self) -> None:
        vis, fb = _Recorder("visual"), _Recorder("feedback")
        ctx = AppContext(visual_store=vis, feedback_store=fb)
        self.assertIs(ctx.visuals(), vis)
        self.assertIs(ctx.feedback(), fb)

    def test_resolution_prefers_explicit_over_global(self) -> None:
        """The load-bearing assertion: an installed global must NOT win.

        A global is installed that would fail this test if consulted, so passing
        the test means the explicit handle really is used — not that the global
        happened to be unset.
        """
        from yuyutsava.visuals.store import set_default_visual_store

        decoy = _Recorder("GLOBAL-decoy")
        set_default_visual_store(decoy)
        try:
            explicit = _Recorder("explicit")
            self.assertIs(
                AppContext(visual_store=explicit).visuals(), explicit,
                "AppContext consulted the global even though an explicit store "
                "was supplied — the seam does nothing",
            )
            self.assertIs(
                GLOBAL_CONTEXT.visuals(), decoy,
                "GLOBAL_CONTEXT should still resolve from the globals; that is "
                "what keeps the 91 unmigrated call sites working",
            )
        finally:
            import yuyutsava.visuals.store as vs

            vs._default_store = None

    def test_omitted_field_falls_back_to_the_global(self) -> None:
        """Partial contexts are the point — callers supply only what they use."""
        from yuyutsava.visuals.store import set_default_visual_store

        decoy = _Recorder("GLOBAL")
        set_default_visual_store(decoy)
        try:
            # feedback supplied, visuals not
            ctx = AppContext(feedback_store=_Recorder("explicit-fb"))
            self.assertIs(ctx.visuals(), decoy)
            self.assertEqual(ctx.feedback().name, "explicit-fb")
        finally:
            import yuyutsava.visuals.store as vs

            vs._default_store = None

    def test_context_is_frozen(self) -> None:
        """Immutable so a callee cannot mutate a caller's dependency set."""
        import dataclasses

        ctx = AppContext()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ctx.visual_store = _Recorder("x")  # type: ignore[misc]


class PurgeSessionAcceptsContext(unittest.TestCase):
    def test_signature_exposes_the_seam(self) -> None:
        import inspect

        from yuyutsava.storage.purge import purge_session

        params = inspect.signature(purge_session).parameters
        self.assertIn(
            "ctx", params,
            "purge_session no longer accepts an AppContext; its dependencies "
            "are hidden behind globals again (finding F-S08)",
        )
        self.assertIsNone(
            params["ctx"].default,
            "ctx must default to None so unmigrated call sites keep working",
        )

    def test_purge_resolves_stores_through_the_context(self) -> None:
        """The body must go through ``ctx``, not call ``get_default_*`` directly."""
        import inspect

        from yuyutsava.storage import purge

        src = inspect.getsource(purge.purge_session)
        for direct in ("get_default_visual_store(", "get_default_feedback_store(",
                       "get_default_session_store("):
            with self.subTest(call=direct):
                self.assertNotIn(
                    direct, src,
                    f"purge_session still calls {direct} directly, bypassing the "
                    f"context — passing ctx would then be silently ignored for "
                    f"that store.",
                )
        for resolved in ("ctx.sessions()", "ctx.visuals()", "ctx.feedback()"):
            with self.subTest(resolver=resolved):
                self.assertIn(resolved, src)


class PurgeRunsWithoutGlobals(unittest.IsolatedAsyncioTestCase):
    """End-to-end: a real purge, with globals that **explode if touched**.

    The other tests check the resolver in isolation. This one runs the whole of
    ``purge_session`` against a real SQLite session while the process globals are
    booby-trapped: if any step falls back to a global, the store raises and the
    test fails with the offending store named.

    That converts "the globals are no longer load-bearing here" from a claim
    into something the suite proves.
    """

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = {
            k: os.environ.get(k)
            for k in ("YUYUTSAVA_STATE_DIR", "YUYUTSAVA_STORAGE_BACKEND")
        }
        os.environ["YUYUTSAVA_STATE_DIR"] = self._tmp.name
        os.environ["YUYUTSAVA_STORAGE_BACKEND"] = "sqlite"

        from yuyutsava.storage.feedback_store import set_default_feedback_store
        from yuyutsava.visuals.store import set_default_visual_store

        set_default_visual_store(_Landmine("visual"))
        set_default_feedback_store(_Landmine("feedback"))

    async def asyncTearDown(self) -> None:
        import yuyutsava.storage.feedback_store as fs
        import yuyutsava.visuals.store as vs

        vs._default_store = None
        fs._default_store = None
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    async def test_full_purge_never_consults_a_global(self) -> None:
        from pathlib import Path

        from yuyutsava.storage.purge import purge_session
        from yuyutsava.storage.sessions import get_default_session_store

        store = get_default_session_store()
        session = await store.create(workspace=Path(self._tmp.name), origin="cli", task="t")

        vis, fb = _Recorder("visual"), _Recorder("feedback")
        report = await purge_session(
            session.id,
            ctx=AppContext(session_store=store, visual_store=vis, feedback_store=fb),
        )

        self.assertEqual(vis.deleted_threads, [session.thread_id])
        self.assertEqual(fb.deleted_threads, [session.thread_id])
        self.assertTrue(report.session_row_deleted)
        self.assertTrue(report.checkpoints_deleted)

    async def test_landmines_are_armed(self) -> None:
        """Negative control for the test above.

        If the decoys did not actually raise, ``test_full_purge_never_consults_
        a_global`` would pass whether or not the seam works.
        """
        from yuyutsava.visuals.store import get_default_visual_store

        with self.assertRaises(AssertionError):
            await get_default_visual_store().delete_for_thread("t")


class _Landmine:
    """A store that fails loudly, to prove a fallback path was not taken."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def delete_for_thread(self, thread_id: str) -> int:
        raise AssertionError(
            f"purge_session fell back to the global {self.name} store despite "
            f"being handed an explicit one — the AppContext seam is bypassed"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
