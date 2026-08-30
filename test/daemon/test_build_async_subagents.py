"""``build_async_subagents`` — the off path, and the invariant nobody named.

Phase 3 step 3.3, sixth slice. This was the **largest** block in
``build_daemon`` (152 lines) and produced five separate ``None``-able names that
were read hundreds of lines further down.

Only the disabled path runs here on purpose: enabling background subagents
starts a LangGraph dev server and races for a cross-process host lock, which is
not something a unit test should do. The disabled path is not filler — it is the
default, so it is the configuration almost every run uses, and "everything is
None and nothing was started" is a behaviour worth pinning.

The invariant this suite really exists for is ``available``. ``build_daemon``
gated three separate call sites on ``async_host_url is not None`` — *not* on
``async_host``, because a process that **attached** to a host another process
owns has a URL but no host object, and can still submit background runs. Getting
that backwards silently disables background delegation for the attached process.
It was carried only by a comment; now it is a named property with a test.

Run:  .venv/bin/python test/daemon/test_build_async_subagents.py
"""

from __future__ import annotations

import os
import unittest

from yuyutsava.daemon.bootstrap import AsyncSubagentSubsystem, build_async_subagents


class DisabledPath(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._prev = os.environ.get("YUYUTSAVA_ASYNC_SUBAGENTS")
        os.environ.pop("YUYUTSAVA_ASYNC_SUBAGENTS", None)

    async def asyncTearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("YUYUTSAVA_ASYNC_SUBAGENTS", None)
        else:
            os.environ["YUYUTSAVA_ASYNC_SUBAGENTS"] = self._prev

    async def _build(self) -> AsyncSubagentSubsystem:
        import asyncio

        from yuyutsava.async_subagents.launch_index import LaunchIndex

        return await build_async_subagents(
            bg_subagent_list=[],
            subagent_settings=None,  # never touched on the disabled path
            checkpointer=None,
            artifact_store=None,
            context_settings=None,
            summary_store=None,
            memory_store=None,
            channels=None,
            task_queue=asyncio.Queue(),
            launch_index=LaunchIndex(),
        )

    async def test_returns_an_all_none_bundle(self) -> None:
        subs = await self._build()
        self.assertFalse(subs.enabled)
        for field in ("host", "host_url", "mirror", "watcher", "attachment"):
            with self.subTest(field=field):
                self.assertIsNone(
                    getattr(subs, field),
                    f"{field} was populated with async subagents disabled — "
                    f"something was started that should not have been",
                )

    async def test_nothing_was_started(self) -> None:
        """No host lock, no watcher task: the disabled path must be inert.

        Asserted via the watcher, because starting one schedules a polling task
        that would outlive the call and keep hitting a URL that does not exist.
        """
        subs = await self._build()
        self.assertIsNone(subs.watcher)
        self.assertFalse(subs.available)

    async def test_env_flag_is_read_not_inferred(self) -> None:
        """``enabled`` reports configuration, not its own side effects.

        Inferring it from ``host_url is not None`` would make a *failed* host
        acquisition indistinguishable from the feature being switched off — and
        the failure is the case worth seeing in a log.
        """
        os.environ["YUYUTSAVA_ASYNC_SUBAGENTS"] = "0"
        self.assertFalse((await self._build()).enabled)


class AvailabilityInvariant(unittest.TestCase):
    """``available`` reads ``host_url``, never ``host``."""

    def test_attached_process_is_available_without_a_host_object(self) -> None:
        attached = AsyncSubagentSubsystem(
            enabled=True, host=None, host_url="http://127.0.0.1:2024",
            mirror=object(), watcher=object(), attachment=object(),
        )
        self.assertTrue(
            attached.available,
            "a process that attached to another owner's host has no host "
            "object but a perfectly usable URL. Gating on `host` here would "
            "silently disable background delegation for every attached process.",
        )

    def test_owner_process_is_available(self) -> None:
        owner = AsyncSubagentSubsystem(
            enabled=True, host=object(), host_url="http://127.0.0.1:2024",
            mirror=object(), watcher=object(), attachment=object(),
        )
        self.assertTrue(owner.available)

    def test_disabled_is_not_available(self) -> None:
        off = AsyncSubagentSubsystem(
            enabled=False, host=None, host_url=None,
            mirror=None, watcher=None, attachment=None,
        )
        self.assertFalse(off.available)

    def test_enabled_but_hostless_is_not_available(self) -> None:
        """Acquisition failed: configured on, but nothing to submit runs to."""
        broken = AsyncSubagentSubsystem(
            enabled=True, host=None, host_url=None,
            mirror=None, watcher=None, attachment=None,
        )
        self.assertFalse(
            broken.available,
            "`enabled` must not imply `available` — otherwise a failed host "
            "acquisition still advertises background subagents to triage, and "
            "every delegation it proposes fails later",
        )


class BuildDaemonUsesTheProperty(unittest.TestCase):
    def test_no_raw_host_url_comparisons_remain(self) -> None:
        """The three hand-written checks are replaced by the named property."""
        import inspect

        from yuyutsava.daemon import bootstrap

        src = inspect.getsource(bootstrap.build_daemon)
        self.assertNotIn(
            "async_host_url is not None", src,
            "build_daemon still hand-writes the availability check; that is the "
            "form that can drift between its three call sites",
        )
        self.assertIn("async_subs.available", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
